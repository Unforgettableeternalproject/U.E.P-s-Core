"""新增訓練資料的測試集防洩漏過濾：近似重複的列剔除，只回報數量與 sample_id，不輸出測試集原文。

比對對象（只讀）：``en_test.jsonl``（中文測試集 MT 英譯）、``en_reference.jsonl``（人工參考英譯）、``en_independent.jsonl``（獨立英文測試集）。
判定（任一成立即剔除）：
- 本句超過 ``SHORT_WORDS`` 詞：本句與任一測試句 Jaccard >= 門檻（預設 0.7，比合成去重的 0.85 更嚴）；
- 本句不超過 ``SHORT_WORDS`` 詞（裸附和等，標籤取決於前文）：本句＋前文合併後 Jaccard >= 0.85，即 state 層級相同才算。

用法：``python -m laya_data.leak_filter --in out/r4/synth.jsonl --out out/r4/synth_kept.jsonl --dropped out/r4/leak_dropped.json``
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .split import read_rows
from .synth import SHORT_WORDS, jaccard, normalize

TESTSETS = (
    ("en_test", "en_test.jsonl"),
    ("en_reference", "en_reference.jsonl"),
    ("en_independent", "en_independent.jsonl"),
)


def _ctx_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(x.get("text", x)) if isinstance(x, dict) else str(x) for x in value)
    return str(value or "")


def test_entries(testset_dir: Path, independent_path: Path | None = None) -> list[tuple[str, str, str]]:
    """回傳 (來源, 正規化本句, 正規化本句＋前文)。"""
    out: list[tuple[str, str, str]] = []
    for name, fname in TESTSETS:
        path = independent_path if (name == "en_independent" and independent_path) else testset_dir / fname
        for r in read_rows(path):
            if name == "en_reference":
                msg, ctx = r.get("text_en", ""), _ctx_text(r.get("context_en"))
            else:
                state = r.get("state") or {}
                msg, ctx = state.get("message") or r.get("text", ""), str(state.get("recent_context", ""))
            out.append((name, normalize(str(msg)), normalize(f"{msg} {ctx}")))
    return out


def row_texts(row: dict[str, Any]) -> tuple[str, str]:
    state = row["state"]
    msg = str(state.get("message", ""))
    return normalize(msg), normalize(f"{msg} {state.get('recent_context', '')}")


def check_row(row: dict[str, Any], tests: list[tuple[str, str, str]], threshold: float) -> tuple[str, float] | None:
    msg, full = row_texts(row)
    short = len(msg.split()) <= SHORT_WORDS
    best: tuple[str, float] | None = None
    for name, tmsg, tfull in tests:
        score = jaccard(full, tfull) if short else jaccard(msg, tmsg)
        limit = 0.85 if short else threshold
        if score >= limit and (best is None or score > best[1]):
            best = (name, score)
    return best


def filter_rows(rows: list[dict[str, Any]], tests: list[tuple[str, str, str]], threshold: float = 0.7) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept, dropped = [], []
    for r in rows:
        hit = check_row(r, tests, threshold)
        if hit:
            dropped.append({"sample_id": r["sample_id"], "testset": hit[0], "score": round(hit[1], 2), "card": r["meta"].get("card")})
        else:
            kept.append(r)
    return kept, dropped


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="新增資料的測試集防洩漏過濾")
    p.add_argument("--in", dest="inp", nargs="+", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--dropped", required=True)
    p.add_argument("--testset-dir", default="testset")
    p.add_argument("--independent", default=None, help="en_independent.jsonl 的路徑（預設取 testset-dir 內的）")
    p.add_argument("--threshold", type=float, default=0.7)
    args = p.parse_args(argv)
    rows = [r for path in args.inp for r in read_rows(Path(path))]
    tests = test_entries(Path(args.testset_dir), Path(args.independent) if args.independent else None)
    kept, dropped = filter_rows(rows, tests, args.threshold)
    Path(args.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8", newline="\n")
    by_set: dict[str, int] = {}
    for d in dropped:
        by_set[d["testset"]] = by_set.get(d["testset"], 0) + 1
    summary = {"input": len(rows), "kept": len(kept), "dropped": len(dropped), "dropped_by_testset": by_set, "threshold": args.threshold, "rows": dropped}
    Path(args.dropped).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
