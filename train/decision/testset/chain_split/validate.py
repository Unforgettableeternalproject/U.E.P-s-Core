"""拆分器評測集驗證器：格式、列舉值、段數與 expect_single 一致性，並印出分布。

用法：python validate.py smoke.jsonl [--stats]
結束碼：有錯誤為 1，否則 0。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from split_schema import load_jsonl, validate_rows


def print_stats(rows: list[dict]) -> None:
    print(f"總列數 {len(rows)}")
    print("lang:", dict(Counter(r["lang"] for r in rows)))
    print("kind x lang:", dict(Counter(f'{r["kind"]}/{r["lang"]}' for r in rows)))
    print("independent 段數:", dict(sorted(Counter(len(r["segments"]) for r in rows if r["kind"] == "independent").items())))
    segs = [s for r in rows for s in r["segments"]]
    print("segment route:", dict(Counter(s["route"] for s in segs)))
    print("independent 段落 route:", dict(Counter(s["route"] for r in rows if r["kind"] == "independent" for s in r["segments"])))
    print("vocative 段落:", sum(1 for s in segs if s["vocative"]), "；addressed=false 段落:", sum(1 for s in segs if not s["addressed"]))
    pron = Counter(s["route"] for s in segs for _ in s["pronouns"])
    print("pronouns 依段落 route:", dict(pron))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="拆分器評測集驗證器")
    p.add_argument("path", type=Path)
    p.add_argument("--stats", action="store_true")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    rows = load_jsonl(args.path)
    errors, warns = validate_rows(rows)
    for rid, msgs in errors.items():
        for m in msgs:
            print(f"錯誤 {rid}: {m}")
    for rid, msgs in warns.items():
        for m in msgs:
            print(f"警告 {rid}: {m}")
    print(f"{args.path.name}: {len(rows)} 列，錯誤 {sum(map(len, errors.values()))}，警告 {sum(map(len, warns.values()))}")
    if args.stats and not errors:
        print_stats(rows)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
