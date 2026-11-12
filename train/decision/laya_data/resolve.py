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
3. ``relay_vs_task``（presence.absent_intent 的訊息類卡片）：邊界見艾斯維爾裁決——
   - 「remind me／ping me／don't let me forget／make sure I remember」要她之後採取行動 → task（缺時間仍是 task）；
   - 轉告他人但帶「何時／何事件才送」的觸發（at five tell…、remind X when he gets home、when you see her）→ task；
   - 無觸發的轉告、單純 remember that… → message；
   - 混合句取第一個指示（先出現轉告動詞再出現提醒 → message，反之 task；連鎖指示之後另行處理）。
   與盲標一致才收，不一致進人工清單。
4. ``route_follows_action``（defer_* 卡）：盲標 reply_mode 仍是 defer 且 addressed 才收，route 採盲標。
6. ``passive_narrow``（艾斯維爾裁決，passive 收窄）：她要有具體理由開口才標 passive——(1) 與她答應過或記下的事有關，
   且內容有衝突、變更或待確認；(2) 沒點名、她答得出來的提問；(3) 沒點名、她確實做得到的請求（裝置控制不算）。
   其餘（第三人稱稱讚或回憶她做過的事、旁人之間的陳述、轉述消息、無可答問題的自言自語、人們已自行照記下的事行動）一律 pass。
   既有 passive 逐筆重審的結果列在 ``PASSIVE_NARROW``：改標 reply_mode=pass（addressed=False、route=ignore 不變），
   meta 記 ``label_rule=passive_narrow``。人工裁決（RULINGS）過的 passive 不動，另行回報。
5. ``ack_ruling``（ack_after_statement）：艾斯維爾裁決——她剛說完陳述、使用者回裸附和，這句是對她的回應，
   addressed=True，但她不必回話（reply_mode=pass、route=ignore）。盲標當時依舊規則標 addressed=False，
   以裁決為準：盲標 reply_mode=pass 且 route=ignore 即收（addressed 不比對）；否則進人工清單。
   沒有前文的冷啟動附和（ack_cold_start）維持 addressed=False。
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

from .blind import compare_labels, read_jsonl, write_jsonl
from .capabilities import apply_capabilities
from .compound import COMPOUND_QID, is_compound
from .leak_filter import filter_rows, fuzzy_pairs, test_entries
from .kinds import ROUTES, get_spec
from .split import split_rows
from .synth import CARD_SETS, CARDS

# 舊產物的 meta 沒有 label_rule，改由卡片定義查
CARD_RULES: dict[str, str] = {c.card_id: c.label_rule for cards in CARDS.values() for c in cards if c.label_rule}


# 人工裁決（逐筆）：規則與盲標不一致、經裁示採某答案的句子
RULINGS: dict[str, dict[str, Any]] = {
    # "until noon" 是轉告內容，不是何時投遞 -> message
    "synthetic-en-presence.absent_intent-00162": {"absent.intent": "message", "absent.target_location": "none"},
    # 到期日→提醒→信：提醒依賴查到的到期日，屬同一目標的相依步驟（compound 定義 a／c）-> no（盲標判 yes）
    "synthetic-en-turn.reply-00058": {"compound": False},
}


def _tr(reply_mode: str, addressed: bool, route: str) -> dict[str, Any]:
    return {"reply_mode": reply_mode, "addressed": addressed, "compound": False, "route": route}


# 艾斯維爾逐筆裁決待核清單（欄位：reply_mode／addressed／compound／route）
RULINGS.update({
    "synthetic-en-turn.reply-00364": _tr("respond", True, "task_request"),
    "synthetic-en-turn.reply-00016": _tr("respond", True, "direct_chat"),
    "synthetic-en-turn.reply-00017": _tr("respond", True, "direct_chat"),
    "synthetic-en-turn.reply-00018": _tr("respond", True, "direct_chat"),
    "synthetic-en-turn.reply-00019": _tr("respond", True, "direct_chat"),
    "synthetic-en-turn.reply-00099": _tr("passive", False, "ignore"),
    # 對其他成員說的話，她沒有開口的理由（艾斯維爾 11/12 改判；原為 passive）
    "synthetic-en-turn.reply-00107": _tr("pass", False, "ignore"),
    "synthetic-en-turn.reply-00228": _tr("passive", False, "ignore"),
    "synthetic-en-turn.reply-00233": _tr("respond", True, "task_request"),
    "synthetic-en-turn.reply-00375": _tr("respond", True, "direct_chat"),
    "synthetic-en-turn.reply-00411": _tr("respond", True, "direct_chat"),
    "synthetic-en-turn.reply-00428": _tr("pass", True, "ignore"),
    "synthetic-en-turn.reply-00571": _tr("respond", True, "direct_chat"),
    "synthetic-en-turn.reply-00612": _tr("respond", True, "direct_chat"),
    "synthetic-en-turn.reply-00714": _tr("defer", True, "task_request"),
    "synthetic-en-turn.reply-00739": _tr("respond", True, "direct_chat"),
    "synthetic-en-turn.reply-00805": _tr("respond", True, "agent_loop"),
})

# passive 收窄重審：違反新界線而改標 pass 的既有句（sample_id 流水號；皆為 turn.reply）。
# 判準：她插話時在場的人會覺得被打斷——例如只是複述／附和她記下的事、自言自語、旁人互相的陳述。
PASSIVE_NARROW: frozenset[str] = frozenset(
    f"synthetic-en-turn.reply-{n}" for n in (
        "00097", "00104", "00105", "00227", "00229", "00235", "00236", "00237", "00238",
        "00357", "00359", "00365", "00366", "00368", "00486", "00489", "00494", "00495", "00496",
        "00497", "00610", "00613", "00618", "00619", "00620", "00621", "00740", "00741", "00746",
        "00747", "00748", "00749", "00867", "00874", "00875", "00876", "00877",
    )
)


# 能力依據（艾斯維爾定案）：做得到＝電腦與各平台內的操作（查詢、訂位訂場、檔案與程式操作）加通訊（打電話、傳訊息、寄信）；
# 訓練資料沒出現過的能力一律視為做不到。下列沒點名的請求要的是翻譯，訓練資料沒有這類能力 -> pass。
PASSIVE_CAPABILITY: frozenset[str] = frozenset({"r4-passive-00019", "r4-passive-00058", "r4-passive-00098"})


def apply_passive_narrow(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """把 ``PASSIVE_NARROW`` 內且目前仍是 passive 的列改標 pass；回傳 (列, 實際改標的 sample_id)。"""
    out, changed = [], []
    for r in rows:
        if (r["sample_id"] in PASSIVE_NARROW or r["sample_id"] in PASSIVE_CAPABILITY) and r["expected"].get("reply_mode") == "passive":
            rule = "passive_capability" if r["sample_id"] in PASSIVE_CAPABILITY else "passive_narrow"
            r = {**r, "expected": _expected_with(r, reply_mode="pass", addressed=False, route="ignore"),
                 "meta": {**r["meta"], "label_rule": rule, "passive_narrow": "passive->pass"}}
            changed.append(r["sample_id"])
        out.append(r)
    return out, changed


# 剔除（不進 final／review）：句子與前文接不上等，原因必須寫明
DROPPED: dict[str, str] = {
    "synthetic-en-turn.reply-00212": "前文沒提到 washer，句子和前文接不上",
}


def sid_is_r4(row: Mapping[str, Any]) -> bool:
    return str(row["sample_id"]).startswith("r4-")


# defer 比例調降（艾斯維爾 11/12 裁決 8）：r4 新增的 defer 與 respond 請求比例失衡（ft-r2 把沒有 Yuki 稱呼的完整請求判成 defer），
# 補了成對的 respond 樣本之外，再各砍掉一部分資訊不足／高風險卡（依 sha1 決定，可重現）。
DEFER_TRIM: dict[str, int] = {"defer_missing_info": 10, "defer_risky": 10}


def trim_defer(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    import hashlib

    drop: set[str] = set()
    for card, n in DEFER_TRIM.items():
        cand = sorted((r["sample_id"] for r in rows if (r.get("meta") or {}).get("card") == card),
                      key=lambda x: hashlib.sha1(f"trim:{x}".encode()).hexdigest())
        drop.update(cand[:n])
    return [r for r in rows if r["sample_id"] not in drop], sorted(drop)


def drop_overlaps(rows: list[dict[str, Any]], testset_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """訓練資料與測試集（en_test／en_reference／en_independent）的近似重複，以及訓練資料內部的近似重複。

    - 對測試集：字詞 Jaccard >= 0.7（短句比本句＋前文 >= 0.85），加上正規化後的字元三連詞 Jaccard >= 0.8（抓錯字差異）；命中即刪。
    - 內部：三連詞 Jaccard >= 0.8 且標籤完全相同者，刪後出現的那筆；標籤不同的只列出、保留。
    測試集檔不在時略過對測試集的比對（回報 ``testset_checked=False``）。"""
    stats: dict[str, Any] = {"testset_checked": False, "test_word_level": 0, "test_fuzzy": 0, "internal_removed": 0, "internal_kept_diff_label": 0}
    drop: set[str] = set()
    try:
        tests = test_entries(testset_dir)
        stats["testset_checked"] = True
    except FileNotFoundError:
        tests = []
    if tests:
        _, hit = filter_rows(rows, tests, 0.7)
        word = {h["sample_id"] for h in hit}
        stats["test_word_level"] = len(word)
        drop |= word
        raw = []
        import json as _json

        for fname in ("en_test.jsonl", "en_test_reference.jsonl", "en_independent.jsonl"):
            f = testset_dir / fname
            if f.exists():
                raw += [_json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip() and _json.loads(x)["kind"] == "turn.reply"]
        tr = [r for r in rows if r["kind"] == "turn.reply"]
        fuzzy = {tr[i]["sample_id"] for i, _, _ in fuzzy_pairs(tr, raw, 0.8)} - word
        stats["test_fuzzy"] = len(fuzzy)
        drop |= fuzzy
    for kind in ("turn.reply", "presence.absent_intent"):
        sub = [r for r in rows if r["kind"] == kind and r["sample_id"] not in drop]
        for i, j, _ in fuzzy_pairs(sub, None, 0.8):
            if sub[j]["sample_id"] in drop or sub[i]["sample_id"] in drop:
                continue
            same = sub[i]["expected"] == sub[j]["expected"] and sub[i]["state"].get("available_capabilities") == sub[j]["state"].get("available_capabilities")
            if same:
                drop.add(sub[j]["sample_id"])
                stats["internal_removed"] += 1
            else:
                stats["internal_kept_diff_label"] += 1
    stats["removed_ids"] = sorted(drop)
    return [r for r in rows if r["sample_id"] not in drop], stats


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


_SELF_REMIND = re.compile(r"\b(remind me|ping me|don'?t let me forget|do not let me forget|make sure i remember)\b", re.IGNORECASE)
_RELAY_VERB = re.compile(r"\b(tell|let\b.{0,30}\bknow|pass|leave|inform|relay|ask|remind (?!me\b))", re.IGNORECASE)
_DELIVERY_TRIGGER = re.compile(
    r"\b(?:when|once|as soon as)\s+(?:you|she|he|they)\s+(?:see|meet|run into|talk to|speak to|get|gets|arrive|arrives|come|comes|are|is)\b"
    r"|^\W*(?:please\s+)?(?:at|by|around)\s+\w+(?:\s+o'?clock)?\s*,?\s*(?:tell|remind|let|ask)\b"
    r"|\b(?:tell|remind|let|ask)\b[^.?!]{0,40}\b(?:tomorrow morning|tonight at|later today|this evening)\b",
    re.IGNORECASE,
)


def relay_intent(message: str) -> str:
    """訊息類句子的 task／message 判定（規則 relay_vs_task）；僅對 absent_intent 的訊息類卡片使用。"""
    own = _SELF_REMIND.search(message)
    if own:
        verb = _RELAY_VERB.search(message)
        return "message" if verb and verb.start() < own.start() else "task"
    return "task" if _DELIVERY_TRIGGER.search(message) else "message"


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
    if row["sample_id"] in RULINGS:  # 人工裁決優先於盲標
        return {**row, "expected": _expected_with(row, **RULINGS[row["sample_id"]])}, "ruling"
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
    if rule == "relay_vs_task":
        label = relay_intent(row["state"]["message"])
        if blind.get("absent.intent") != label:
            return None, f"relay:rule_{label}_blind_{blind.get('absent.intent')}"
        return {**row, "expected": _expected_with(row, **{"absent.intent": label, "absent.target_location": "none"})}, f"relay_{label}"
    if rule == "ack_ruling":
        if blind.get("reply_mode") != "pass" or (blind.get("route") != "ignore" and not sid_is_r4(row)):
            return None, "ack_ruling:blind_not_pass_ignore"
        return {**row, "expected": _expected_with(row, reply_mode="pass", addressed=True, route="ignore")}, "ack_ruling"
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


CARD_EXPECTED: dict[str, dict[str, Any]] = {
    c.card_id: dict(c.expected) for card_set in CARD_SETS.values() for cards in card_set.values() for c in cards
}
_COMPOUND_QIDS = frozenset(COMPOUND_QID.values())


def relabel_from_cards(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str]]:
    """以目前的卡片定義重算 ``expected``（卡片標籤改版後不必重新合成；合成產物只存了舊標籤）。回傳 (列, 各卡重標筆數)。"""
    out, changed = [], Counter()
    for r in rows:
        want = CARD_EXPECTED.get((r.get("meta") or {}).get("card", ""))
        if want is not None:
            qid = COMPOUND_QID.get(r["kind"])
            if qid and qid not in want:  # 舊卡片沒有 compound 欄：既有句預設 no，例外見 compound.py
                want = {**want, qid: is_compound(r)}
        if want is not None and r["expected"] != want:
            if {k: v for k, v in r["expected"].items() if k not in _COMPOUND_QIDS} != {k: v for k, v in want.items() if k not in _COMPOUND_QIDS}:
                changed[r["meta"]["card"]] += 1
            r = {**r, "expected": want}
        # 題目說明一律取目前定義（schema 文字改版後，舊產物內嵌的 questions 跟著更新）
        out.append({**r, "questions": {q: dict(get_spec(r["kind"]).questions[q]) for q in r["expected"]}})
    return out, changed


def normalize_r4_labels(rows: list[dict[str, Any]], labels: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """r4 新增列的盲標正規化：reply_mode 為 pass／passive 的列，route 只是結果（一律 ignore），盲標員依題目定義
    把「對她說但不必回」的列標成 direct_chat 等，不代表分歧；reply_mode 與 addressed 一致時把 route 視為 ignore。
    只動 sample_id 以 ``r4-`` 開頭者，既有列的比對不變。"""
    out = dict(labels)
    for r in rows:
        sid = r["sample_id"]
        if not sid.startswith("r4-") or sid not in labels:
            continue
        exp, got = r["expected"], labels[sid]
        if exp.get("route") == "ignore" and exp.get("reply_mode") in ("pass", "passive")                 and got.get("reply_mode") == exp["reply_mode"] and got.get("addressed") == exp.get("addressed"):
            out[sid] = {**got, "route": "ignore"}
    return out


def run(mass_dir: Path, val_ratio: float = 0.1, seed: int = 0) -> dict[str, Any]:
    rows, relabeled = relabel_from_cards(read_jsonl(mass_dir / "synth_all.jsonl"))
    rows = [r for r in rows if r["sample_id"] not in DROPPED]
    data = {r["sample_id"]: r for r in rows}
    labels = {r["sample_id"]: r["answers"] for r in read_jsonl(mass_dir / "labels.jsonl")}
    labels = normalize_r4_labels(rows, labels)
    accepted, disputed, blind_stats = compare_labels(rows, labels)[:3]
    write_jsonl(mass_dir / "blind" / "accepted.jsonl", accepted)
    write_jsonl(mass_dir / "blind" / "disputed.jsonl", disputed)
    (mass_dir / "blind" / "stats.json").write_text(json.dumps(blind_stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    resolved: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    final: list[dict[str, Any]] = []
    for r in accepted:
        if rule_of(r) == "relay_vs_task" or r["sample_id"] in RULINGS:
            out, tag = resolve_row(r, labels.get(r["sample_id"]))
            if out is None:
                review.append(_review_entry(r, labels.get(r["sample_id"]), tag))
                counts[f"review:{tag}"] += 1
            else:
                final.append({**out, "meta": {**out["meta"], "blind_check": f"resolved:{tag}"}})
                counts[f"resolved:{tag}"] += 1
            continue
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
    final, narrowed = apply_passive_narrow(final)
    counts["passive_narrow"] = len(narrowed)
    final, trimmed = trim_defer(final)
    final, overlap_stats = drop_overlaps(final, Path("testset"))
    final, cap_stats = apply_capabilities(final)
    final.sort(key=lambda r: r["sample_id"])
    train, val = split_rows(final, val_ratio, seed)
    write_jsonl(mass_dir / "final.jsonl", final)
    write_jsonl(mass_dir / "train.jsonl", train)
    write_jsonl(mass_dir / "val.jsonl", val)
    write_jsonl(mass_dir / "blind" / "review_for_bernie.jsonl", review)
    (mass_dir / "blind" / "review_for_bernie.md").write_text(build_summary(review), encoding="utf-8", newline="\n")
    stats = {
        "accepted": len(accepted), "disputed": len(disputed), "resolved": len(resolved),
        "review": len(review), "final": len(final), "compound": {k: sum(1 for r in final if r["kind"] == k and r["expected"].get(q)) for k, q in COMPOUND_QID.items()}, "train": len(train), "val": len(val),
        "capabilities": cap_stats, "defer_trimmed": len(trimmed), "overlaps": overlap_stats, "rules": dict(sorted(counts.items())), "dropped": dict(sorted(DROPPED.items())), "relabeled_from_cards": dict(sorted(relabeled.items())),
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
