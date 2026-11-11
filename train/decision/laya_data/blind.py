"""盲標流程：生成產物 -> 盲標任務 -> 與卡片標籤比對（雙代理一致才收，不一致送人工）。

- ``export_tasks``：每列只放 sample_id、kind、推論端看得到的 state、該 kind 的英文題目定義；
  不放卡片名、標籤、hard 標記、meta。
- ``compare_labels``：盲標答案與卡片標籤逐題比對，輸出 accepted／disputed／stats。
  reply_mode 的 ``acceptable_modes`` 落在範圍內算一致；此時 addressed／route 因前提不同不再比對
  （記為 ``skipped_acceptable``）。
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .kinds import get_spec

OPTIONAL_QIDS = frozenset({"compound", "absent.compound"})


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def export_tasks(rows: Iterable[Mapping[str, Any]], only: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """盲標任務：不含任何標籤或卡片資訊。``only`` 指定時只出這些題（補標新題目用）。"""
    tasks = []
    for r in rows:
        spec = get_spec(r["kind"])
        qids = [q for q in r["expected"] if only is None or q in set(only)]  # 該 kind 該列有標的題目；題目定義取自 KindSpec
        tasks.append({
            "sample_id": r["sample_id"],
            "kind": r["kind"],
            "state": dict(r["state"]),
            "questions": {q: dict(spec.questions[q]) for q in qids},
        })
    return tasks


def load_labels(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for no, rec in enumerate(read_jsonl(path), 1):
        sid, ans = rec.get("sample_id"), rec.get("answers")
        if not isinstance(sid, str) or not isinstance(ans, dict):
            raise TypeError(f"第 {no} 列缺少 sample_id／answers")
        if sid in out:
            raise ValueError(f"sample_id 重複：{sid}")
        out[sid] = ans
    return out


def _valid(row: Mapping[str, Any], qid: str, value: Any) -> bool:
    criteria = row["questions"][qid].get("criteria") or {}
    return value in criteria


def compare_row(row: Mapping[str, Any], answers: Mapping[str, Any] | None) -> dict[str, Any]:
    """回傳 {mismatch: [qid], missing: [qid], invalid: [qid], skipped: [qid]}。"""
    expected = row["expected"]
    acceptable = (row.get("meta") or {}).get("acceptable_modes") or []
    res: dict[str, list[str]] = {"mismatch": [], "missing": [], "invalid": [], "skipped": []}
    answers = answers or {}
    mode_accepted_other = False
    if "reply_mode" in expected:
        given = answers.get("reply_mode")
        if given is not None and given != expected["reply_mode"] and given in acceptable:
            mode_accepted_other = True
    for qid, want in expected.items():
        if qid not in answers:
            if qid in OPTIONAL_QIDS:  # compound 只對候選句盲標，其餘句沒有答案不算缺漏
                res["skipped"].append(qid)
                continue
            res["missing"].append(qid)
            continue
        got = answers[qid]
        ok = isinstance(got, bool) if isinstance(want, bool) else _valid(row, qid, got)
        if not ok:
            res["invalid"].append(qid)
            continue
        if qid in ("addressed", "route") and mode_accepted_other:
            res["skipped"].append(qid)
            continue
        if qid == "reply_mode" and got in acceptable:
            continue
        if got != want:
            res["mismatch"].append(qid)
    return res


def compare_labels(
    rows: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    disputed: list[dict[str, Any]] = []
    per_q: dict[str, Counter[str]] = {}
    per_card: dict[str, Counter[str]] = {}
    known = {r["sample_id"] for r in rows}
    for r in rows:
        card = (r.get("meta") or {}).get("card", "?")
        res = compare_row(r, labels.get(r["sample_id"]))
        bad = res["mismatch"] + res["missing"] + res["invalid"]
        for qid in r["expected"]:
            if qid in res["skipped"]:
                continue
            c = per_q.setdefault(qid, Counter())
            c["n"] += 1
            c["agree"] += qid not in bad
        pc = per_card.setdefault(card, Counter())
        pc["n"] += 1
        if not bad:
            pc["agree"] += 1
            kept = {**r, "meta": {**(r.get("meta") or {}), "blind_check": "agree"}}
            accepted.append(kept)
            continue
        disputed.append({
            "sample_id": r["sample_id"], "kind": r["kind"], "card": card,
            "hard": bool((r.get("meta") or {}).get("hard")),
            "message": r["state"].get("message"), "state": r["state"],
            "card_answers": dict(r["expected"]),
            "blind_answers": {q: labels[r["sample_id"]].get(q) for q in r["expected"]} if r["sample_id"] in labels else None,
            "mismatch": res["mismatch"], "missing": res["missing"], "invalid": res["invalid"],
            "acceptable_modes": (r.get("meta") or {}).get("acceptable_modes", []),
        })
    stats = {
        "total": len(rows), "accepted": len(accepted), "disputed": len(disputed),
        "unlabeled": sum(1 for r in rows if r["sample_id"] not in labels),
        "unmatched_label_ids": sorted(set(labels) - known),
        "per_question": {q: {"n": c["n"], "agree": c["agree"], "rate": round(c["agree"] / c["n"], 4)} for q, c in sorted(per_q.items())},
        "per_card": {k: {"n": c["n"], "agree": c["agree"], "rate": round(c["agree"] / c["n"], 4)} for k, c in sorted(per_card.items())},
    }
    return accepted, disputed, stats


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run_import(data: Path, labels_path: Path, out_dir: Path) -> dict[str, Any]:
    rows = read_jsonl(data)
    accepted, disputed, stats = compare_labels(rows, load_labels(labels_path))
    out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(out_dir / "accepted.jsonl", accepted)
    write_jsonl(out_dir / "disputed.jsonl", disputed)
    (out_dir / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return stats
