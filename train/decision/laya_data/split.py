"""train／val 切分，與「測試集不得和訓練集重疊」的檢查。

- 切分以 ``meta.group_id``（無則 ``sample_id``）為單位，``sha1`` 決定，可重現；同組不跨集。
- 測試集（zh-TW 原句經 MT、人工核過的英文）永遠不被本工具讀入訓練；``check`` 只做比對，
  任何重疊（正規化後精確相同或 Jaccard 近似）即回傳非零。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .synth import JACCARD_DUP, jaccard, normalize


def _group_key(row: Mapping[str, Any]) -> str:
    meta = row.get("meta") or {}
    return str(meta.get("group_id") or row["sample_id"])


def is_val(group: str, val_ratio: float, seed: int = 0) -> bool:
    digest = hashlib.sha1(f"{seed}:{group}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32 < val_ratio


def split_rows(
    rows: Iterable[Mapping[str, Any]], val_ratio: float = 0.1, seed: int = 0
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    train: list[Mapping[str, Any]] = []
    val: list[Mapping[str, Any]] = []
    for r in rows:
        (val if is_val(_group_key(r), val_ratio, seed) else train).append(r)
    return train, val


def message_of(row: Mapping[str, Any]) -> str:
    state = row.get("state")
    return str(state.get("message", "")) if isinstance(state, dict) else str(state or "")


def find_overlaps(
    train_texts: Iterable[str], test_texts: Sequence[str], threshold: float = JACCARD_DUP
) -> list[tuple[str, str, float]]:
    """回傳 (train 文字, test 文字, 相似度) 的重疊清單；1.0 表示正規化後相同。"""
    tests = [(t, normalize(t)) for t in test_texts]
    test_set = {n for _, n in tests}
    hits: list[tuple[str, str, float]] = []
    for tr in train_texts:
        n = normalize(tr)
        if not n:
            continue
        if n in test_set:
            hits.extend((tr, t, 1.0) for t, tn in tests if tn == n)
            continue
        for t, tn in tests:
            score = jaccard(n, tn)
            if score >= threshold:
                hits.append((tr, t, score))
    return hits


def read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="train/val 切分與測試集防洩漏檢查")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("split")
    s.add_argument("--data", nargs="+", required=True)
    s.add_argument("--train-out", required=True)
    s.add_argument("--val-out", required=True)
    s.add_argument("--val-ratio", type=float, default=0.1)
    s.add_argument("--seed", type=int, default=0)
    c = sub.add_parser("check", help="訓練集（含 val）與測試集英文句不得重疊")
    c.add_argument("--train", nargs="+", required=True)
    c.add_argument("--test", required=True, help="每列含 text（英文）的 JSONL")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.cmd == "split":
        rows = [r for path in args.data for r in read_rows(Path(path))]
        train, val = split_rows(rows, args.val_ratio, args.seed)
        for out, part in ((args.train_out, train), (args.val_out, val)):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in part), encoding="utf-8", newline="\n")
        print(json.dumps({"train": len(train), "val": len(val)}))
        return 0
    train_texts = [message_of(r) for path in args.train for r in read_rows(Path(path))]
    tests = [str(r.get("text", "")) for r in read_rows(Path(args.test)) if r.get("text")]
    hits = find_overlaps(train_texts, tests)
    print(json.dumps({"train": len(train_texts), "test": len(tests), "overlaps": len(hits)}))
    for tr, te, score in hits[:20]:
        print(f"  {score:.2f}\n    train: {tr}\n    test:  {te}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
