"""測試集與 train／val 的重疊報告（逐測試列）。

``split.py check`` 只比本句文字；裸附和（``Okay.``、``ok``）的標籤取決於前文，本句相同不等於樣本相同。
這裡對每列測試句分三層回報：
- ``state_exact``：整個 state（本句＋前文＋人數／平台）正規化後相同，才算真重疊；
- ``message_exact``：只有本句相同（前文或人數不同），列出供人工判斷；
- ``near``：本句 Jaccard >= 門檻但不相同。

用法：``python -m laya_data.overlap --train out/mass/train.jsonl out/mass/val.jsonl --test testset/en_test.jsonl``
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .split import message_of, read_rows
from .synth import JACCARD_DUP, jaccard, normalize


def state_key(row: dict[str, Any]) -> str:
    state = row["state"]
    return "|".join(normalize(str(state.get(k, ""))) for k in ("message", "recent_context", "other_people_present", "source_platform"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="測試集與訓練集重疊報告")
    p.add_argument("--train", nargs="+", required=True)
    p.add_argument("--test", required=True)
    p.add_argument("--threshold", type=float, default=JACCARD_DUP)
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    train = [r for path in args.train for r in read_rows(Path(path))]
    test = read_rows(Path(args.test))
    t_state = {}
    t_msg: dict[str, list[dict[str, Any]]] = {}
    for r in train:
        t_state.setdefault((r["kind"], state_key(r)), []).append(r)
        t_msg.setdefault(normalize(message_of(r)), []).append(r)
    report = []
    for row in test:
        n = normalize(message_of(row))
        entry: dict[str, Any] = {"id": row["id"], "text": message_of(row)}
        same_state = t_state.get((row["kind"], state_key(row)), [])
        same_msg = t_msg.get(n, [])
        entry["state_exact"] = len(same_state)
        entry["message_exact"] = len(same_msg)
        if same_state:
            entry["state_exact_labels_agree"] = sum(
                1 for r in same_state if all(r["expected"].get(k) == v for k, v in row["expected"].items())
            )
        if not same_msg and n:
            near = [(jaccard(n, normalize(message_of(r))), message_of(r)) for r in train if r["kind"] == row["kind"]]
            near = sorted((x for x in near if x[0] >= args.threshold), reverse=True)[:3]
            if near:
                entry["near"] = [{"score": round(s, 2), "train": t} for s, t in near]
        if entry["state_exact"] or entry["message_exact"] or entry.get("near"):
            report.append(entry)
    summary = {
        "test": len(test), "train_rows": len(train),
        "state_exact_rows": sum(1 for e in report if e["state_exact"]),
        "message_only_rows": sum(1 for e in report if e["message_exact"] and not e["state_exact"]),
        "near_rows": sum(1 for e in report if e.get("near")),
        "rows": report,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 1 if summary["state_exact_rows"] or summary["near_rows"] else 0


if __name__ == "__main__":
    sys.exit(main())
