"""彙總 outputs/v25_final_reviews/*.json 的盲測結果。

用法：
    python webapp/tally.py
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

# Windows 終端預設 cp950，中文輸出會亂碼
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW_DIR = os.path.join(PROJECT_ROOT, "outputs", "v25_final_reviews")

LABEL = {"A": "A 組（slice_03）", "B": "B 組（slice_04）",
         "base": "無 LoRA", "lora": "掛 LoRA", "tie": "差不多"}


def main() -> None:
    if not os.path.isdir(REVIEW_DIR):
        raise SystemExit("還沒有任何評測結果")

    files = sorted(f for f in os.listdir(REVIEW_DIR) if f.endswith(".json"))
    if not files:
        raise SystemExit("還沒有任何評測結果")

    # kind -> variant -> winner -> 票數
    tally: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    overall_tally: dict = defaultdict(lambda: defaultdict(int))
    notes: list = []
    comments: list = []

    for name in files:
        with open(os.path.join(REVIEW_DIR, name), encoding="utf-8") as fp:
            data = json.load(fp)
        nick = data.get("nickname", "anon")
        for r in data.get("results", []):
            key = r["variant"] + (f" [{r['group']}組]" if r.get("group") else "")
            tally[r["kind"]][key][r["winner"]] += 1
            overall_tally[r["kind"]][r["winner"]] += 1
            if r.get("note"):
                notes.append(f"  [{nick}] {key}：{r['note']}")
        if data.get("overall"):
            comments.append(f"  [{nick}] {data['overall']}")

    print(f"=== 盲測彙總（{len(files)} 位測試者）===\n")

    for kind, title in (("reference", "① 參考音對決：A 組 vs B 組"),
                        ("lora", "② LoRA 對決：無 LoRA vs 掛 LoRA")):
        if kind not in tally:
            continue
        print(title)
        print("-" * 52)
        for variant, votes in tally[kind].items():
            parts = " | ".join(
                f"{LABEL.get(k, k)} {v}" for k, v in sorted(votes.items(), key=lambda x: -x[1])
            )
            print(f"  {variant:<28} {parts}")
        total = overall_tally[kind]
        summary = " | ".join(
            f"{LABEL.get(k, k)} {v}" for k, v in sorted(total.items(), key=lambda x: -x[1])
        )
        print(f"  {'【總計】':<26} {summary}\n")

    if notes:
        print("單題備註")
        print("-" * 52)
        print("\n".join(notes), "\n")

    if comments:
        print("整體評論")
        print("-" * 52)
        print("\n".join(comments))


if __name__ == "__main__":
    main()
