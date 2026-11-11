"""train/nlp 舊 NLP 資料 -> laya-train JSONL（選用種子，只產 ``route`` 題）。

標籤來源只用 ``bio_labels``（不用 ``segments``：部分 segments 元素是字串而非 dict）。
舊 label -> route 對映見 ``LABEL_TO_ROUTE``；對映不明或衝突者丟棄並計數，不修改原檔。
用法：``python -m laya_data.convert_nlp --out out/route_seed.jsonl``（於 train/decision 下執行）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .kinds import make_row

DEFAULT_INPUTS = (
    "../nlp/nlp_training_data.jsonl",
    "../nlp/nlp_training_data_additional.jsonl",
)

# 舊 label -> route。None 表示該 label 不提供 route 資訊（視為不存在，不影響其他 label）。
LABEL_TO_ROUTE: dict[str, str | None] = {
    "CHAT": "direct_chat",
    "DIRECT_WORK": "task_request",  # 立即、單一的工作請求
    "BACKGROUND_WORK": "agent_loop",  # 背景／較長的工作 -> 多步任務
    "CALL": None,  # 單純叫喚助理，不決定處理路徑（反映 addressed，不是 route）
}
# 明確丟棄的 label（出現即整列丟棄）
DROP_LABELS = frozenset({"UNKNOWN"})

_BIO_TAG = re.compile(r"^(B|I)-([A-Z_]+)$")


def read_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any] | None]]:
    """逐列讀取；壞 JSON 回傳 (行號, None)。"""
    with path.open(encoding="utf-8") as f:
        for no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                yield no, None
                continue
            yield no, row if isinstance(row, dict) else None


def row_labels(row: dict[str, Any]) -> tuple[set[str] | None, str | None]:
    """從 bio_labels 取出出現的 label 集合；資料壞掉回 (None, 原因)。"""
    tokens, bio = row.get("tokens"), row.get("bio_labels")
    if not isinstance(tokens, list) or not isinstance(bio, list):
        return None, "missing_tokens_or_bio"
    if len(tokens) != len(bio):
        return None, "bio_length_mismatch"
    labels: set[str] = set()
    for tag in bio:
        if tag == "O":
            continue
        m = _BIO_TAG.match(str(tag))
        if m is None:
            return None, "bad_bio_tag"
        labels.add(m.group(2))
    return labels, None


def route_for(labels: set[str]) -> tuple[str | None, str | None]:
    """label 集合 -> (route, 丟棄原因)。"""
    if not labels:
        return None, "no_entity"
    if labels & DROP_LABELS:
        return None, "unknown_label"
    unmapped = labels - set(LABEL_TO_ROUTE)
    if unmapped:
        return None, "unmapped_label"
    routes = {LABEL_TO_ROUTE[x] for x in labels if LABEL_TO_ROUTE[x] is not None}
    if not routes:
        return None, "call_only_no_route"
    if len(routes) > 1:
        return None, "mixed_routes"
    return next(iter(routes)), None


def convert(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """轉換多個來源檔；回傳 (laya-train 列, 統計)。同文字不同 route 的衝突整組丟棄。"""
    stats: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    kept: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()
    for path in paths:
        for no, row in read_jsonl(path):
            stats["read"] += 1
            if row is None:
                dropped["bad_json"] += 1
                continue
            text = str(row.get("text", "")).strip()
            if not text:
                dropped["empty_text"] += 1
                continue
            labels, err = row_labels(row)
            if labels is None:
                dropped[err or "bad_row"] += 1
                continue
            route, reason = route_for(labels)
            if route is None:
                dropped[reason or "no_route"] += 1
                continue
            key = " ".join(text.lower().split())
            if key in conflicts:
                dropped["duplicate_conflict"] += 1
                continue
            if key in kept:
                if kept[key]["expected"]["route"] != route:
                    del kept[key]
                    conflicts.add(key)
                    dropped["duplicate_conflict"] += 2  # 已收的那筆與這筆
                else:
                    dropped["duplicate_same"] += 1
                continue
            sample_id = f"nlpseed-en-{len(kept):05d}"
            kept[key] = make_row(
                "turn.reply",
                {"message": text},
                {"route": route},
                sample_id=sample_id,
                source="train_nlp_seed",
                meta={"origin_id": str(row.get("id", "")), "origin_file": path.name, "origin_line": no},
            )
    rows = list(kept.values())
    # 重編號：衝突移除後保持連號
    for i, r in enumerate(rows):
        r["sample_id"] = f"nlpseed-en-{i:05d}"
    by_route = Counter(r["expected"]["route"] for r in rows)
    summary = {
        "read": stats["read"],
        "kept": len(rows),
        "kept_by_route": dict(sorted(by_route.items())),
        "dropped_total": sum(dropped.values()),
        "dropped_by_reason": dict(sorted(dropped.items())),
        "label_to_route": {k: v for k, v in LABEL_TO_ROUTE.items()},
        "note": "ignore 在種子中不存在，須由合成補",
    }
    return rows, summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="train/nlp -> laya-train JSONL（route 種子）")
    p.add_argument("--input", nargs="*", default=None, help="來源 JSONL（預設為 train/nlp 兩個檔）")
    p.add_argument("--out", required=True, help="輸出 JSONL")
    args = p.parse_args(argv)
    base = Path(__file__).resolve().parents[1]
    inputs = [Path(x) for x in args.input] if args.input else [(base / d).resolve() for d in DEFAULT_INPUTS]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    rows, summary = convert(inputs)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
