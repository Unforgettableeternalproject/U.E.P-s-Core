"""決策日誌重放評估（DL-3）：讀 decision_log（或 JSONL 匯出）算 primary／shadow 一致率與延遲分佈。

用法::

    python -m uep.decision.replay stats --jsonl log.jsonl [--kind turn.reply] [--since 2026-11-01] [--until ...] [--json]
    python -m uep.decision.replay stats --db [--db-url URL]            # 讀 decision_log（預設 UEP_DATABASE_URL）
    python -m uep.decision.replay rerun --candidates cands.jsonl --proposers respond.minimal,rules.default

輸出人可讀表格；加 ``--json`` 只輸出 JSON，``--json-out PATH`` 另存 JSON。葉節點：只用標準函式庫
（DB 與 proposer 重跑經函式內延遲 import ``uep.decision.db`` ／ ``uep.decision.replay_turn``）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def percentile(values: Sequence[float], p: float) -> float | None:
    """nearest-rank 百分位（p 為 0~100）；無資料回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def parse_time(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def filter_rows(
    rows: Iterable[dict[str, Any]], *, kind: str | None = None,
    since: str | datetime | None = None, until: str | datetime | None = None,
) -> list[dict[str, Any]]:
    """依 kind 與 [since, until) 篩選；沒有 created_at 的列在指定時間範圍時被排除。"""
    lo, hi = parse_time(since), parse_time(until)
    out = []
    for row in rows:
        if kind and row.get("kind") != kind:
            continue
        if lo is not None or hi is not None:
            created = parse_time(row.get("created_at"))
            if created is None or (lo is not None and created < lo) or (hi is not None and created >= hi):
                continue
        out.append(row)
    return out


def _latency(values: list[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95)}


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reasons: dict[str, int] = Counter(r.get("reason_code") or "(none)" for r in rows)
    return {
        "count": len(rows),
        "status": dict(Counter(r.get("status") or "(none)" for r in rows)),
        "elapsed_ms": _latency([r["elapsed_ms"] for r in rows if r.get("elapsed_ms") is not None]),
        "by_outcome": dict(Counter(r.get("outcome") or "(none)" for r in rows)),
        "by_reason": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def analyze(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """計算統計。shadow 以 ``(kind, proposer_id)`` 分組，與 ``primary_decision_id`` 指到的 primary 配對成混淆矩陣。"""
    rows = list(rows)
    primaries = [r for r in rows if r.get("role") == "primary"]
    by_id = {r["decision_id"]: r for r in primaries}
    kinds = sorted({r.get("kind") or "(none)" for r in rows})
    result: dict[str, Any] = {"total_rows": len(rows), "kinds": {}}
    for kind in kinds:
        k_primary = [r for r in primaries if (r.get("kind") or "(none)") == kind]
        k_shadow = [r for r in rows if r.get("role") == "shadow" and (r.get("kind") or "(none)") == kind]
        shadows: dict[str, Any] = {}
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in k_shadow:
            groups[r.get("proposer_id") or "(unknown)"].append(r)
        for sid, group in sorted(groups.items()):
            summary = _summarize(group)
            decided = [r for r in group if r.get("outcome") is not None]  # 排除例外／驗證失敗（無 outcome）
            agree = sum(1 for r in decided if r.get("agree"))
            confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            orphans = 0
            for r in decided:
                primary = by_id.get(r.get("primary_decision_id") or "")
                if primary is None:
                    orphans += 1
                    continue
                confusion[primary.get("outcome") or "(none)"][r["outcome"]] += 1
            summary.update(
                decided=len(decided), agree=agree,
                agree_rate=(agree / len(decided)) if decided else None,
                orphans=orphans,
                confusion={p: dict(c) for p, c in sorted(confusion.items())},
                reason_agree={
                    reason: {"count": n, "agree": sum(1 for r in decided if (r.get("reason_code") or "(none)") == reason and r.get("agree"))}
                    for reason, n in Counter(r.get("reason_code") or "(none)" for r in decided).items()
                },
            )
            shadows[sid] = summary
        result["kinds"][kind] = {"primary": _summarize(k_primary), "shadows": shadows}
    return result


def format_report(report: dict[str, Any]) -> str:
    def lat(d: dict[str, Any]) -> str:
        fmt = lambda v: "-" if v is None else f"{v:g}"  # noqa: E731
        return f"n={d['n']} p50={fmt(d['p50'])}ms p95={fmt(d['p95'])}ms"

    def table(title: str, mapping: dict[str, Any], limit: int | None = None) -> list[str]:
        out = [f"  {title}:"]
        items = list(mapping.items())[:limit]
        out += [f"    {k:<28} {v}" for k, v in items] or ["    (無)"]
        return out

    lines = [f"決策日誌重放：共 {report['total_rows']} 列"]
    for kind, block in report["kinds"].items():
        p = block["primary"]
        lines += ["", f"== {kind} ==", f"primary：{p['count']} 筆，延遲 {lat(p['elapsed_ms'])}"]
        lines += table("status", p["status"]) + table("outcome", p["by_outcome"]) + table("reason_code", p["by_reason"])
        for sid, s in block["shadows"].items():
            rate = "-" if s["agree_rate"] is None else f"{s['agree_rate']:.1%}"
            lines += ["", f"-- shadow {sid}：{s['count']} 筆，一致率 {rate}（{s['agree']}/{s['decided']}，無配對 {s['orphans']}），延遲 {lat(s['elapsed_ms'])}"]
            lines += table("status", s["status"])
            lines += ["  混淆矩陣（列 = primary，欄 = shadow）:"]
            modes = sorted({m for row in s["confusion"].values() for m in row} | set(s["confusion"]))
            if modes:
                lines.append("    " + f"{'':<10}" + "".join(f"{m:>9}" for m in modes))
                for pm in sorted(s["confusion"]):
                    lines.append("    " + f"{pm:<10}" + "".join(f"{s['confusion'][pm].get(m, 0):>9}" for m in modes))
            else:
                lines.append("    (無配對資料)")
            lines += ["  reason_code（shadow）:"]
            lines += [f"    {r:<28} count={v['count']} agree={v['agree']}" for r, v in sorted(s["reason_agree"].items())] or ["    (無)"]
    return "\n".join(lines)


def _load(args: argparse.Namespace) -> list[dict[str, Any]]:
    since, until = parse_time(args.since), parse_time(args.until)
    if args.jsonl:
        rows = read_jsonl(args.jsonl)
    else:
        from uep.decision.db import load_rows_from_url

        rows = load_rows_from_url(args.db_url, kind=args.kind, since=since, until=until)
    return filter_rows(rows, kind=args.kind, since=since, until=until)


def _emit(report: dict[str, Any], args: argparse.Namespace) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    print(text if args.json else format_report(report) + "\n\n" + text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m uep.decision.replay", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    stats = sub.add_parser("stats", help="統計 decision_log／JSONL")
    source = stats.add_mutually_exclusive_group(required=True)
    source.add_argument("--jsonl", help="decision_log 的 JSONL 匯出")
    source.add_argument("--db", action="store_true", help="讀 decision_log 表")
    stats.add_argument("--db-url", help="覆寫 UEP_DATABASE_URL")
    stats.add_argument("--kind")
    stats.add_argument("--since", help="ISO 時間（含）")
    stats.add_argument("--until", help="ISO 時間（不含）")
    stats.add_argument("--json", action="store_true", help="只輸出 JSON")
    stats.add_argument("--json-out", help="另存 JSON 檔")
    rerun = sub.add_parser("rerun", help="把 TurnCandidate 序列餵給多個 proposer 離線重跑比對")
    rerun.add_argument("--candidates", required=True, help="TurnCandidate JSONL")
    rerun.add_argument("--proposers", required=True, help="proposer_id 逗號分隔；第一個為 baseline（primary）")
    rerun.add_argument("--json", action="store_true")
    rerun.add_argument("--json-out")
    rerun.add_argument("--rows-out", help="把重跑結果存成 decision_log 形狀的 JSONL")
    args = parser.parse_args(argv)
    if args.cmd == "stats":
        _emit(analyze(_load(args)), args)
        return 0
    from uep.decision.replay_turn import rerun_candidates, write_rows

    rows = rerun_candidates(read_jsonl(args.candidates), [p.strip() for p in args.proposers.split(",") if p.strip()])
    if args.rows_out:
        write_rows(rows, args.rows_out)
    _emit(analyze(rows), args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
