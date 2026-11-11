"""盲標 disputed 的規則化裁決，並產出最終訓練集（決策層裁決，見 README「量產收斂」）。

規則（逐筆套用，只動 ``meta.label_rule`` 標記的卡，其餘不一致一律進人工清單）：

1. ``first_place``（absent_request_two_places）：target_location 取文字中第一個字面明說的地點；
   與盲標一致才收。
2. ``promise_mode``（group_promise_update／group_commitment_related）：
   - respond：(a) 前文有 ``User:`` 行（說話者就是請她記這件事的人）且這句直接取消或改掉待辦；
     (b) 她上一句是提議／徵詢，這句在回覆；(c) 承諾不明確／時間衝突等需要看語意，一律進人工清單。
   - passive：前文沒有 ``User:`` 行、她上一句不是提議（對別人說、順帶提到）。
   - 判不準（有 User 行但沒有取消語氣、提議但沒有回覆語氣）進人工清單。
   respond 時 addressed=True、route 採盲標；passive 時 addressed=False、route=ignore。
   與盲標一致才收；已 accepted 的這兩張卡若規則判為 respond，視為規則衝突，改進人工清單。
3. ``route_follows_action``（defer_* 卡）：盲標 reply_mode 仍是 defer 且 addressed 才收，route 採盲標。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .blind import read_jsonl, write_jsonl
from .kinds import ROUTES, get_spec
from .split import split_rows
from .synth import CARDS

# 舊產物的 meta 沒有 label_rule，改由卡片定義查
CARD_RULES: dict[str, str] = {c.card_id: c.label_rule for cards in CARDS.values() for c in cards if c.label_rule}


def rule_of(row: Mapping[str, Any]) -> str:
    meta = row.get("meta") or {}
    return str(meta.get("label_rule") or CARD_RULES.get(meta.get("card", ""), ""))


_PLACES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("discord", re.compile(r"discord", re.IGNORECASE)),
    ("desktop", re.compile(r"desk\s?top", re.IGNORECASE)),
    ("website", re.compile(r"web\s?site", re.IGNORECASE)),
    ("host", re.compile(r"\bhost\b", re.IGNORECASE)),
)
_CANCEL = re.compile(
    r"\b(no need|don'?t need|do not need|not needed|cancel|(?<!n't )(?<!nt )(?<!not )forget|never ?mind|instead|no longer|already (?:bought|buy|ordered|order|did|done|booked|book|have|got|moved|sorted)|called off|postponed|moved to)\b", re.IGNORECASE,
)
_OFFER = re.compile(r"\b(shall i|should i|want me to|would you like me|do you want me|i can|i could)\b", re.IGNORECASE)
_PROMISE = re.compile(r"\b(i'?ll|i will)\b", re.IGNORECASE)
_REPLY = re.compile(r"^\W*(yes|yeah|yep|no|nope|sure|ok|okay|please|go ahead|not now|that works|sounds good)\b", re.IGNORECASE)


def first_place(message: str) -> str | None:
    """文字中最早出現的地點（desktop／discord／website／host）；沒有回 None。"""
    hits = [(m.start(), name) for name, pat in _PLACES if (m := pat.search(message))]
    return min(hits)[1] if hits else None


def parse_context(recent: str) -> tuple[bool, str]:
    """回傳 (前文是否有同一人的 ``User:`` 行, 她最後一句)。"""
    has_user, last_assistant = False, ""
    for line in recent.split("\n"):
        if line.startswith("User:"):
            has_user = True
        elif line.startswith("Assistant:"):
            last_assistant = line[len("Assistant:"):].strip()
    return has_user, last_assistant


def judge_promise(state: Mapping[str, Any]) -> tuple[str, str] | None:
    """回傳 (reply_mode, 規則代號)；判不準回 None（進人工清單）。"""
    msg = str(state.get("message", ""))
    has_user, asst = parse_context(str(state.get("recent_context", "")))
    is_offer = bool(_OFFER.search(asst)) and not _PROMISE.search(asst)
    cancel = bool(_CANCEL.search(msg))
    if has_user and cancel:
        return "respond", "a"
    if is_offer:
        return ("respond", "b") if (cancel or _REPLY.search(msg)) else None
    if has_user:
        return None
    return "passive", "other"


def _expected_with(row: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    exp = {**row["expected"], **changes}
    get_spec(row["kind"]).validate_expected(exp)
    return exp


def resolve_row(row: Mapping[str, Any], blind: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    """回傳 (裁決後的列或 None, 規則代號／不收原因)。"""
    rule = rule_of(row)
    if not blind:
        return None, "no_blind_answer"
    if rule == "first_place":
        place = first_place(row["state"]["message"])
        if place is None:
            return None, "first_place:no_literal_place"
        if blind.get("absent.intent") != "presence_request" or blind.get("absent.target_location") != place:
            return None, "first_place:blind_differs"
        exp = _expected_with(row, **{"absent.target_location": place})
        return {**row, "expected": exp}, "first_place"
    if rule == "route_follows_action":
        if blind.get("reply_mode") != "defer" or blind.get("addressed") is not True or blind.get("route") not in ROUTES:
            return None, "defer:blind_not_defer"
        return {**row, "expected": _expected_with(row, route=blind["route"])}, "route_follows_action"
    if rule == "promise_mode":
        verdict = judge_promise(row["state"])
        if verdict is None:
            return None, "promise:ambiguous"
        mode, tag = verdict
        if blind.get("reply_mode") != mode:
            return None, f"promise:rule_{mode}_blind_{blind.get('reply_mode')}"
        if mode == "respond":
            if blind.get("addressed") is not True or blind.get("route") not in ROUTES:
                return None, "promise:blind_route_invalid"
            return {**row, "expected": _expected_with(row, reply_mode="respond", addressed=True, route=blind["route"])}, f"promise_respond_{tag}"
        return {**row, "expected": _expected_with(row, reply_mode="passive", addressed=False, route="ignore")}, "promise_passive"
    return None, "other_card"


def _review_entry(row: Mapping[str, Any], blind: Mapping[str, Any] | None, reason: str) -> dict[str, Any]:
    diff = [q for q, v in row["expected"].items() if (blind or {}).get(q) != v]
    if reason == "rule_conflict_accepted":
        note = f"{reason}：卡片與盲標都是 {row['expected'].get('reply_mode')}，但規則 (a)／(b) 判為 respond"
    else:
        note = (
            f"{reason}：卡片 {', '.join(f'{q}={row['expected'][q]}' for q in diff)}；盲標 "
            f"{', '.join(f'{q}={(blind or {}).get(q)}' for q in diff) or '無答案'}"
        )
    return {
        "sample_id": row["sample_id"], "kind": row["kind"], "card": row["meta"].get("card"),
        "message": row["state"].get("message"), "context": row["state"].get("recent_context", "(none)"),
        "card_answers": dict(row["expected"]), "blind_answers": dict(blind) if blind else None, "note": note,
    }


def build_summary(review: list[dict[str, Any]], limit: int = 40) -> str:
    by_card = Counter(r["card"] for r in review)
    lines = [f"# 人工核清單摘要（共 {len(review)} 筆）", "", "| 卡片 | 筆數 |", "|---|---|"]
    lines += [f"| {c} | {n} |" for c, n in by_card.most_common()]
    lines += ["", "## 範例（每卡 1 筆）"]
    shown: set[str] = set()
    for r in review:
        if r["card"] in shown or len(lines) >= limit - 2:
            continue
        shown.add(r["card"])
        lines.append(f"- {r['card']}: 「{str(r['message'])[:70]}」 {r['note']}"[:300])
    return "\n".join(lines[:limit]) + "\n"


def distribution(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    dist: dict[str, Counter[str]] = {}
    for r in rows:
        dist.setdefault("kind", Counter())[r["kind"]] += 1
        for q, v in r["expected"].items():
            dist.setdefault(q, Counter())[str(v)] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(dist.items())}


def run(mass_dir: Path, val_ratio: float = 0.1, seed: int = 0) -> dict[str, Any]:
    data = {r["sample_id"]: r for r in read_jsonl(mass_dir / "synth_all.jsonl")}
    labels = {r["sample_id"]: r["answers"] for r in read_jsonl(mass_dir / "labels.jsonl")}
    accepted = read_jsonl(mass_dir / "blind" / "accepted.jsonl")
    disputed = read_jsonl(mass_dir / "blind" / "disputed.jsonl")
    resolved: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    final: list[dict[str, Any]] = []
    for r in accepted:
        if rule_of(r) == "promise_mode":
            verdict = judge_promise(r["state"])
            if verdict and verdict[0] == "respond" and r["expected"].get("reply_mode") != "respond":
                review.append(_review_entry(r, labels.get(r["sample_id"]), "rule_conflict_accepted"))
                counts["review:rule_conflict_accepted"] += 1
                continue
        final.append(r)
    for d in disputed:
        row = data[d["sample_id"]]
        out, tag = resolve_row(row, labels.get(d["sample_id"]))
        if out is None:
            review.append(_review_entry(row, labels.get(d["sample_id"]), tag))
            counts[f"review:{tag}"] += 1
            continue
        out = {**out, "meta": {**out["meta"], "blind_check": f"resolved:{tag}"}}
        resolved.append(out)
        counts[f"resolved:{tag}"] += 1
    final += resolved
    final.sort(key=lambda r: r["sample_id"])
    train, val = split_rows(final, val_ratio, seed)
    write_jsonl(mass_dir / "final.jsonl", final)
    write_jsonl(mass_dir / "train.jsonl", train)
    write_jsonl(mass_dir / "val.jsonl", val)
    write_jsonl(mass_dir / "blind" / "review_for_bernie.jsonl", review)
    (mass_dir / "blind" / "review_for_bernie.md").write_text(build_summary(review), encoding="utf-8", newline="\n")
    stats = {
        "accepted": len(accepted), "disputed": len(disputed), "resolved": len(resolved),
        "review": len(review), "final": len(final), "train": len(train), "val": len(val),
        "rules": dict(sorted(counts.items())),
        "final_distribution": distribution(final), "val_distribution": distribution(val),
    }
    (mass_dir / "final_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="盲標 disputed 規則化裁決與最終訓練集")
    p.add_argument("--mass-dir", required=True, help="含 synth_all.jsonl、labels.jsonl、blind/ 的目錄")
    p.add_argument("--val-ratio", type=float, default=0.1)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(run(Path(args.mass_dir), args.val_ratio, args.seed), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
