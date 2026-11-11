"""情境卡合成器：卡片決定標籤，LLM 只生文字（設計見 docs/架構重構/決策模型訓練設計草案.md §4）。

- 預設不呼叫任何外部服務；使用 OpenAI 後端必須明示 ``--confirm-api``。
- 金鑰只從環境變數讀（``UEP_LLM_API_KEY`` 或 ``OPENAI_API_KEY``），不寫檔、不印出。
- 試產上限 ``TRIAL_CAP`` 筆；超過須 ``--budget-confirmed``。
- 後端以 ``Backend`` 協定抽象，測試用 ``FakeBackend``。
- 檔案式後端（不連網）：``--export-prompts`` 匯出每次呼叫的 prompt，由外部（人或其他模型）填回應，
  再用 ``--import-responses`` 走同一條解析／去重／寫出流程。匯入須使用和匯出相同的
  ``--kind``／``--max-samples``／``--per-call``／``--seed``，``call_id`` 才會對得上。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import urllib.request
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .kinds import DEFAULT_ASSISTANT_NAME, make_row

TRIAL_CAP = 200
DEFAULT_MODEL = "gpt-5.6-luna"
MODEL_ENV = "LAYA_SYNTH_MODEL"
PER_CALL = 10
MAX_MESSAGE_CHARS = 280
JACCARD_DUP = 0.85

STYLES: dict[str, str] = {
    "clean": "plain, grammatical English",
    "casual": "casual spoken English: contractions, fillers, fragments, lowercase is fine",
    "stt_noise": "speech-to-text errors: homophone mistakes, missing punctuation, dropped or repeated words",
    "mt_style": "stiff machine-translated English from Traditional Chinese: literal word order, odd articles/prepositions",
}
TOPICS = (
    "weather", "music", "cooking", "work schedule", "games", "travel", "health and sleep", "shopping",
    "movies", "pets", "study", "smart-home devices", "email and calendar", "coding", "news", "fitness",
)


@dataclass(frozen=True)
class Card:
    """一張情境卡。``expected`` 是該卡的確定性標籤；``context`` 說明前文需求。"""

    card_id: str
    kind: str
    description: str  # 給 LLM 的情境描述（英文）
    expected: Mapping[str, Any]
    others: int = 0  # 在場他人數（0 = 私聊）
    context: str | None = None  # None = 不需前文；否則為前文的描述
    hard: bool = False
    acceptable_modes: tuple[str, ...] = ()
    source_platform: str | None = None  # absent kind 用


def _tr(card_id: str, desc: str, reply_mode: str, addressed: bool, route: str, **kw: Any) -> Card:
    return Card(
        card_id, "turn.reply", desc,
        {"reply_mode": reply_mode, "addressed": addressed, "route": route}, **kw,
    )


A = DEFAULT_ASSISTANT_NAME
TURN_REPLY_CARDS: tuple[Card, ...] = (
    _tr("private_chat", "One user alone with the assistant makes a casual remark or shares something.", "respond", True, "direct_chat"),
    _tr("private_question", "One user alone asks the assistant a general question.", "respond", True, "direct_chat"),
    _tr("private_task", f"One user alone gives {A} one clear command that needs a single tool call (set a timer, open a file, check the calendar).", "respond", True, "task_request"),
    _tr("private_complex", f"One user alone asks {A} for something requiring several steps, research or planning.", "respond", True, "agent_loop"),
    _tr("private_filler", "One user alone says only a filler or interjection (hm, uh, haha, oh).", "pass", False, "ignore"),
    _tr("ack_after_solicit", f"{A} just asked the user a question or asked for their opinion; the user replies with only a bare acknowledgement (ok, yeah, sure).", "respond", True, "direct_chat", context="assistant_solicits", hard=True),
    _tr("ack_after_statement", f"{A} just made a plain statement (no question); the user replies with only a bare acknowledgement (ok, got it).", "pass", False, "ignore", context="assistant_states", hard=True),
    _tr("ack_cold_start", "The user says only a bare acknowledgement (ok, yeah) with no earlier conversation visible.", "pass", False, "ignore", hard=True),
    _tr("group_named_chat", f"Several people are talking; one speaker addresses {A} by name (start or middle of the sentence) with a casual remark or question.", "respond", True, "direct_chat", others=2),
    _tr("group_named_task", f"Several people are talking; one speaker addresses {A} by name with one clear tool-type command.", "respond", True, "task_request", others=2),
    _tr("group_named_complex", f"Several people are talking; one speaker addresses {A} by name with a multi-step request.", "respond", True, "agent_loop", others=2),
    _tr("group_side_talk", f"Several people are talking to each other; the speaker talks to another person and never addresses {A}.", "pass", False, "ignore", others=2),
    _tr("group_task_like_side", f"Several people are present; the speaker gives a command-like request but it is clearly aimed at another person, not {A}.", "pass", False, "ignore", others=2, hard=True),
    _tr("group_followup", f"{A} just answered this speaker in a group; the speaker continues the same thread with a follow-up that makes sense only for {A}.", "respond", True, "direct_chat", others=2, context="assistant_just_replied", hard=True),
    _tr("group_turn_away", f"{A} just answered this speaker in a group; the speaker now turns to another person in the room and talks to them.", "pass", False, "ignore", others=2, context="assistant_just_replied", hard=True),
    _tr("group_tentative_name", f"Several people talk; the speaker says a word that sounds like {A}'s name but is probably an ordinary word or another name, so it is unclear whether {A} is addressed.", "pass", False, "ignore", others=2, hard=True, acceptable_modes=("pass", "respond")),
    _tr("group_commitment_related", f"Several people talk to each other, not to {A}, but the topic touches something {A} earlier promised to do; worth quietly noting, not interrupting.", "passive", False, "ignore", others=2, context="assistant_promised", hard=True),
    _tr("defer_busy", f"One user asks {A} for something whose result only exists later (a long job) while saying they are busy right now.", "defer", True, "direct_chat", hard=True),
)

ABSENT_CARDS: tuple[Card, ...] = tuple(
    Card(f"absent_{cid}", "presence.absent_intent", desc, exp, hard=hard, source_platform=plat)
    for cid, desc, exp, hard, plat in (
        ("request_generic", "Asks the assistant to come over or appear, no place named (come here, show up).", {"absent.intent": "presence_request", "absent.target_location": "none"}, False, "discord"),
        ("request_discord", "Asks the assistant to come to or switch to Discord.", {"absent.intent": "presence_request", "absent.target_location": "discord"}, False, "desktop"),
        ("request_desktop", "Asks the assistant to come to or switch to the desktop app.", {"absent.intent": "presence_request", "absent.target_location": "desktop"}, False, "discord"),
        ("request_host", "Asks the assistant to go back to the host machine.", {"absent.intent": "presence_request", "absent.target_location": "host"}, False, "website"),
        ("request_website", "Asks the assistant to move to the website.", {"absent.intent": "presence_request", "absent.target_location": "website"}, False, "discord"),
        ("negated", "Tells the assistant NOT to come or move (do not come over, do not go back).", {"absent.intent": "other", "absent.target_location": "none"}, True, "discord"),
        ("past_narration", "Narrates in the past that someone or the assistant went somewhere (she went back to the host yesterday).", {"absent.intent": "other", "absent.target_location": "none"}, True, "discord"),
        ("question_ability", "Asks a question about whether the assistant can or why it does not go somewhere.", {"absent.intent": "other", "absent.target_location": "none"}, True, "discord"),
        ("mention_place_only", "Merely mentions a platform or machine name in an unrelated remark.", {"absent.intent": "other", "absent.target_location": "none"}, True, "desktop"),
        ("pass_message", "Asks the assistant to relay or remember something (tell Bernie I will be late, remind me about the meeting).", {"absent.intent": "message", "absent.target_location": "none"}, True, "discord"),
    )
)

CARDS: dict[str, tuple[Card, ...]] = {"turn.reply": TURN_REPLY_CARDS, "presence.absent_intent": ABSENT_CARDS}


class Backend(Protocol):
    def complete(self, system: str, user: str) -> str:  # pragma: no cover - 協定
        ...


class FakeBackend:
    """測試用：依序回傳預設字串；用完回最後一個。記錄呼叫次數。"""

    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r


class OpenAIBackend:
    """OpenAI 相容 chat/completions（urllib，不依賴額外套件）。金鑰只讀環境變數，絕不記錄。"""

    def __init__(self, model: str | None = None, base_url: str | None = None, timeout: float = 120.0) -> None:
        key = os.environ.get("UEP_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("找不到 API 金鑰環境變數（UEP_LLM_API_KEY 或 OPENAI_API_KEY）")
        self._key = key
        self.model = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        self._url = (base_url or os.environ.get("UEP_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        self._timeout = timeout
        self.calls = 0
        self.usage = Counter()

    def complete(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.calls += 1
        usage = data.get("usage") or {}
        self.usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
        self.usage["completion_tokens"] += int(usage.get("completion_tokens", 0))
        return str(data["choices"][0]["message"]["content"])


SYSTEM_PROMPT = (
    "You write short, realistic utterances for training a decision model of a voice assistant. "
    "Reply with ONLY a JSON array, no prose, no code fences."
)


class ReplayBackend:
    """檔案式後端：依呼叫順序回放已存檔的回應（以 call_id 對應）。缺少的 call_id 視為後端錯誤。"""

    def __init__(self, responses: Mapping[str, str], call_ids: Sequence[str]) -> None:
        self._responses = dict(responses)
        self._call_ids = list(call_ids)
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        call_id = self._call_ids[self.calls]
        self.calls += 1
        try:
            return self._responses[call_id]
        except KeyError:
            raise LookupError(f"缺少 {call_id} 的回應") from None


def call_id_of(kind: str, idx: int) -> str:
    return f"{kind}-{idx:04d}"


def build_prompt(card: Card, style: str, topic: str, n: int) -> str:
    """產生給 LLM 的使用者提示。標籤不給 LLM；它只負責依描述生文字。"""
    lines = [
        f"The assistant is always called \"{DEFAULT_ASSISTANT_NAME}\" (the translation layer replaces every alias with this name); whenever the speaker names the assistant, use exactly \"{DEFAULT_ASSISTANT_NAME}\".",
        f"Scenario: {card.description}",
        f"Write {n} DIFFERENT examples. Topic hint: {topic}. Style: {STYLES[style]}.",
        (
            "Each example must be an object with key \"message\" (the final utterance to classify, at most "
            f"{MAX_MESSAGE_CHARS} characters)."
        ),
    ]
    if card.kind == "turn.reply":
        if card.others:
            lines.append("The speaker is one of several people in the room; do not write the other people's lines in \"message\".")
        if card.context:
            lines.append(
                "Also include key \"context\": a list of 1-3 earlier lines, oldest first, each {\"speaker\": ..., \"text\": ...}. "
                f"Use speaker \"Assistant\" for the assistant and \"User\" for the speaker. "
                f"The context must realise: {card.context.replace('_', ' ')}."
            )
    lines.append("Do not include any label or explanation.")
    return "\n".join(lines)


def _extract_json_array(text: str) -> list[Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("回應中找不到 JSON 陣列")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise TypeError("回應不是陣列")
    return data


def parse_items(card: Card, text: str) -> tuple[list[dict[str, Any]], Counter]:
    """解析並驗證 LLM 回應；回傳 (合格項目, 拒絕原因計數)。"""
    rejected: Counter[str] = Counter()
    try:
        raw = _extract_json_array(text)
    except (ValueError, TypeError):
        rejected["unparseable"] += 1
        return [], rejected
    items: list[dict[str, Any]] = []
    for it in raw:
        if not isinstance(it, dict) or not isinstance(it.get("message"), str):
            rejected["bad_item"] += 1
            continue
        msg = " ".join(it["message"].split())
        if not msg:
            rejected["empty_message"] += 1
            continue
        if len(msg) > MAX_MESSAGE_CHARS:
            rejected["too_long"] += 1
            continue
        if re.search(r"[一-鿿]", msg):
            rejected["contains_cjk"] += 1  # Laya 只用英文
            continue
        entry: dict[str, Any] = {"message": msg}
        if card.context:
            ctx = it.get("context")
            if (
                not isinstance(ctx, list) or not ctx
                or not all(isinstance(c, dict) and isinstance(c.get("speaker"), str) and isinstance(c.get("text"), str) and c["text"].strip() for c in ctx)
            ):
                rejected["bad_context"] += 1
                continue
            entry["context"] = [{"speaker": c["speaker"], "text": " ".join(c["text"].split())} for c in ctx]
        items.append(entry)
    return items, rejected


_NORM = re.compile(r"[^a-z0-9 ]+")


def normalize(text: str) -> str:
    return " ".join(_NORM.sub(" ", text.lower()).split())


def jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass
class Deduper:
    """精確（正規化後）＋近似（Jaccard）去重。"""

    threshold: float = JACCARD_DUP
    _seen: list[str] = field(default_factory=list)
    _set: set[str] = field(default_factory=set)

    def add(self, text: str) -> bool:
        key = normalize(text)
        if not key or key in self._set:
            return False
        if any(jaccard(key, s) >= self.threshold for s in self._seen):
            return False
        self._set.add(key)
        self._seen.append(key)
        return True


def plan_calls(cards: Sequence[Card], max_samples: int, per_call: int, rng: random.Random) -> list[tuple[Card, str, str, int]]:
    """場景矩陣：卡片輪流 × 風格輪流 × 隨機話題，直到湊滿 max_samples。"""
    styles = list(STYLES)
    calls: list[tuple[Card, str, str, int]] = []
    remaining, i = max_samples, 0
    while remaining > 0:
        card = cards[i % len(cards)]
        style = styles[(i // len(cards)) % len(styles)]
        n = min(per_call, remaining)
        calls.append((card, style, rng.choice(TOPICS), n))
        remaining -= n
        i += 1
    return calls


def generate(
    kind: str,
    backend: Backend,
    *,
    max_samples: int,
    per_call: int = PER_CALL,
    seed: int = 0,
    id_prefix: str = "synthetic-en",
    cards: Sequence[Card] | None = None,
    call_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """產生合成列與統計。每次呼叫產出的樣本屬同一 group（供切分用）。"""
    cards = tuple(cards or CARDS[kind])
    rng = random.Random(seed)
    dedupe = Deduper()
    rows: list[dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    by_card: Counter[str] = Counter()
    by_style: Counter[str] = Counter()
    api_calls = 0
    for idx, (card, style, topic, n) in enumerate(plan_calls(cards, max_samples, per_call, rng)):
        api_calls += 1
        if call_ids is not None:
            call_ids.append(call_id_of(kind, idx))
        try:
            reply = backend.complete(SYSTEM_PROMPT, build_prompt(card, style, topic, n))
        except Exception as exc:  # noqa: BLE001 - 單次呼叫失敗只計數，不中斷
            rejected[f"backend_error:{type(exc).__name__}"] += 1
            continue
        items, rej = parse_items(card, reply)
        rejected.update(rej)
        for it in items:
            if not dedupe.add(it["message"]):
                rejected["duplicate"] += 1
                continue
            facts: dict[str, Any] = {"message": it["message"], "others": card.others}
            if "context" in it:
                facts["context"] = it["context"]
            if card.source_platform:
                facts["source_platform"] = card.source_platform
            rows.append(make_row(
                kind, facts, dict(card.expected),
                sample_id=f"{id_prefix}-{len(rows):05d}", source="synthetic",
                meta={
                    "card": card.card_id, "style": style, "topic": topic, "hard": card.hard,
                    "group_id": f"{card.card_id}-{idx:04d}",
                    **({"acceptable_modes": list(card.acceptable_modes)} if card.acceptable_modes else {}),
                },
            ))
            by_card[card.card_id] += 1
            by_style[style] += 1
    stats = {
        "api_calls": api_calls, "samples": len(rows), "rejected": dict(sorted(rejected.items())),
        "by_card": dict(sorted(by_card.items())), "by_style": dict(sorted(by_style.items())),
        "hard": sum(1 for r in rows if r["meta"].get("hard")),
    }
    return rows, stats


def export_prompts(kind: str, *, max_samples: int, per_call: int, seed: int) -> list[dict[str, Any]]:
    """依 plan_calls／build_prompt 產生每次呼叫的 prompt 與中繼資料（不呼叫任何後端）。"""
    plan = plan_calls(CARDS[kind], max_samples, per_call, random.Random(seed))
    return [
        {
            "call_id": call_id_of(kind, idx), "kind": kind, "card": card.card_id, "style": style,
            "topic": topic, "n": n, "hard": card.hard,
            "system": SYSTEM_PROMPT, "user": build_prompt(card, style, topic, n),
        }
        for idx, (card, style, topic, n) in enumerate(plan)
    ]


def load_responses(path: Path) -> dict[str, str]:
    """讀「每列 {call_id, response}」的 JSONL；call_id 重複視為錯誤。"""
    out: dict[str, str] = {}
    with path.open(encoding="utf-8") as f:
        for no, line in enumerate(f, 1):
            if not line.strip():
                continue
            rec = json.loads(line)
            cid, resp = rec.get("call_id"), rec.get("response")
            if not isinstance(cid, str) or not isinstance(resp, str):
                raise TypeError(f"第 {no} 列缺少字串型 call_id／response")
            if cid in out:
                raise ValueError(f"call_id 重複：{cid}")
            out[cid] = resp
    return out


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="情境卡合成器（預設不連網）")
    p.add_argument("--kind", default="turn.reply", choices=sorted(CARDS))
    p.add_argument("--max-samples", type=int, default=TRIAL_CAP)
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--per-call", type=int, default=PER_CALL, help="每次呼叫要幾筆（影響卡片×風格輪替密度）")
    p.add_argument("--export-prompts", default=None, metavar="PATH", help="只把每次呼叫的 prompt 寫成 JSONL，不呼叫後端")
    p.add_argument("--import-responses", default=None, metavar="PATH", help="讀 {call_id, response} JSONL，走解析／去重／寫出流程")
    p.add_argument("--model", default=None, help=f"覆寫模型（亦可用環境變數 {MODEL_ENV}；預設 {DEFAULT_MODEL}）")
    p.add_argument("--confirm-api", action="store_true", help="明示同意呼叫外部 API（否則只列出計畫）")
    p.add_argument("--budget-confirmed", action="store_true", help="超過試產上限時需要")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.max_samples > TRIAL_CAP and not args.budget_confirmed:
        print(f"超過試產上限 {TRIAL_CAP}；量產須經確認並加 --budget-confirmed", file=sys.stderr)
        return 2
    if args.export_prompts and args.import_responses:
        print("--export-prompts 與 --import-responses 不可同時使用", file=sys.stderr)
        return 2
    if args.export_prompts:
        recs = export_prompts(args.kind, max_samples=args.max_samples, per_call=args.per_call, seed=args.seed)
        write_jsonl(Path(args.export_prompts), recs)
        print(json.dumps({"exported_calls": len(recs), "samples": args.max_samples}, ensure_ascii=False))
        return 0
    if args.import_responses:
        responses = load_responses(Path(args.import_responses))
        ids: list[str] = []
        planned = [call_id_of(args.kind, i) for i in range(len(plan_calls(CARDS[args.kind], args.max_samples, args.per_call, random.Random(args.seed))))]
        backend_r = ReplayBackend(responses, planned)
        rows, stats = generate(args.kind, backend_r, max_samples=args.max_samples, per_call=args.per_call, seed=args.seed, call_ids=ids)
        stats["unmatched_response_ids"] = sorted(set(responses) - set(planned))
        write_jsonl(Path(args.out), rows)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    if not args.confirm_api:
        plan = plan_calls(CARDS[args.kind], args.max_samples, args.per_call, random.Random(args.seed))
        print(json.dumps({"dry_run": True, "api_calls_would_be": len(plan), "samples": args.max_samples}, ensure_ascii=False))
        return 0
    backend = OpenAIBackend(model=args.model)
    rows, stats = generate(args.kind, backend, max_samples=args.max_samples, per_call=args.per_call, seed=args.seed)
    write_jsonl(Path(args.out), rows)
    stats.update(model=backend.model, usage=dict(backend.usage))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
