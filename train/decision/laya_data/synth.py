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

from .kinds import DEFAULT_ASSISTANT_NAME, laya_schema, make_row

TRIAL_CAP = 200
DEFAULT_MODEL = "gpt-5.6-luna"
MODEL_ENV = "LAYA_SYNTH_MODEL"
PER_CALL = 4
MAX_MESSAGE_CHARS = 280
JACCARD_DUP = 0.85
SHORT_WORDS = 3  # 不超過這個詞數的短句只做正規化後精確比對

STYLES: dict[str, str] = {
    "clean": "plain, grammatical English",
    "casual": "casual spoken English: contractions, fillers, fragments, lowercase is fine",
    "stt_noise": "speech-to-text errors: homophone mistakes, missing punctuation, dropped or repeated words",
    "mt_style": "stiff machine-translated English from Traditional Chinese: literal word order, odd articles/prepositions",
}
TOPICS = (
    "weather", "music", "cooking", "work schedule", "games", "travel", "health and sleep", "shopping",
    "movies", "pets", "study", "smart-home devices", "email and calendar", "coding", "news", "fitness",
    "gardening", "cars and commuting", "banking and bills", "birthday planning", "home repair", "books",
    "photography", "language learning", "kids and school", "neighbors", "restaurants", "online meetings",
    "moving house", "cleaning", "hobbies and crafts", "social media", "sports", "holidays", "hiking",
    "tea and coffee", "laundry", "job interviews", "podcasts", "board games", "package delivery", "medication",
    "weekend plans", "tech gadgets",
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
    others_choices: tuple[int, ...] = ()  # 非空時，依輪次輪替在場他人數（覆蓋 others）
    guidance: str = ""  # 額外的生成限制（英文），用來避免文字與標籤矛盾


def _tr(card_id: str, desc: str, reply_mode: str, addressed: bool, route: str, **kw: Any) -> Card:
    return Card(
        card_id, "turn.reply", desc,
        {"reply_mode": reply_mode, "addressed": addressed, "route": route}, **kw,
    )


A = DEFAULT_ASSISTANT_NAME
GROUP = (1, 2, 3, 4)  # 1 = 雙人頻道、2 = 三人、3 以上 = 多人
MIXED = (0, 0, 1, 2)  # 私聊為主，偶有他人在場
TURN_REPLY_CARDS: tuple[Card, ...] = (
    _tr("private_chat", "One user alone with the assistant makes a casual remark or shares something.", "respond", True, "direct_chat"),
    _tr("private_question", "One user alone asks the assistant a general question.", "respond", True, "direct_chat",
        guidance="The question must be understandable on its own, with no vague pronouns pointing at things not shown."),
    _tr("private_lookup", f"One user alone asks {A} for a read-only lookup that changes nothing (weather, today's news, what is on the calendar, something the assistant remembers).", "respond", True, "direct_chat"),
    _tr("private_task", f"One user alone gives {A} one clear command for a single action that changes state (send an email, set a reminder or timer, turn a device on or off, add a calendar entry).", "respond", True, "task_request"),
    _tr("private_complex", f"One user alone asks {A} for something requiring several steps, research or planning.", "respond", True, "agent_loop"),
    _tr("private_filler", "One user alone says only a filler or interjection (hm, uh, haha, oh).", "pass", False, "ignore",
        guidance="Each message must be a pure interjection or hesitation with no content words and no instruction or question; never phrases like 'then like this'."),
    _tr("ack_after_solicit_chat", f"{A} just asked the user a casual question or for their opinion in small talk (nothing to execute); the user replies with only a bare acknowledgement (ok, yeah, sure).", "respond", True, "direct_chat", context="assistant_solicits_chat", hard=True, others_choices=MIXED),
    _tr("ack_after_solicit_task", f"{A} just offered to run one specific tool action (set a reminder, send a message, switch a device off) and asked whether to do it; the user replies with only a bare agreeing acknowledgement (ok, yes please, go ahead).", "respond", True, "task_request", context="assistant_solicits_task", hard=True, others_choices=MIXED),
    _tr("ack_after_solicit_agent", f"{A} just offered to do a multi-step job (research, compare, plan) and asked whether to go ahead; the user replies with only a bare agreeing acknowledgement (sure, do it, okay).", "respond", True, "agent_loop", context="assistant_solicits_agent", hard=True, others_choices=MIXED),
    _tr("ack_after_statement", f"{A} just made a plain statement (no question); the user replies with only a bare acknowledgement (ok, got it).", "pass", False, "ignore", context="assistant_states", hard=True, others_choices=MIXED),
    _tr("ack_cold_start", "The user says only a bare acknowledgement (ok, yeah) with no earlier conversation visible.", "pass", False, "ignore", hard=True, others_choices=(0, 1, 2, 3)),
    _tr("group_named_chat", f"People are talking; one speaker addresses {A} by name (start or middle of the sentence) with a casual remark or question.", "respond", True, "direct_chat", others_choices=GROUP),
    _tr("group_named_lookup", f"People are talking; one speaker addresses {A} by name with a read-only lookup that changes nothing (weather, news, calendar, something remembered).", "respond", True, "direct_chat", others_choices=GROUP),
    _tr("group_named_task", f"People are talking; one speaker addresses {A} by name with one clear command for a single state-changing action (send, set a reminder, switch a device).", "respond", True, "task_request", others_choices=GROUP),
    _tr("group_named_complex", f"People are talking; one speaker addresses {A} by name with a multi-step request.", "respond", True, "agent_loop", others_choices=GROUP,
        guidance=f"The speaker must speak to {A} directly in the second person; never report what other people said about {A}."),
    _tr("group_side_talk", f"People are talking to each other; the speaker talks to another person and never addresses {A}.", "pass", False, "ignore", others_choices=GROUP),
    _tr("group_task_like_side", f"People are present; the speaker gives a command-like request but it is clearly aimed at another person, not {A}.", "pass", False, "ignore", hard=True, others_choices=GROUP,
        guidance=f"Name the person being asked, and never mention {A}."),
    _tr("group_followup", f"{A} just answered this speaker in a group; the speaker continues the same thread with a follow-up question that makes sense only for {A}.", "respond", True, "direct_chat", context="assistant_just_replied", hard=True, others_choices=GROUP,
        guidance=f"Do not name {A} in the message; the follow-up must only make sense as addressed to {A}'s last reply."),
    _tr("group_followup_task", f"{A} just answered this speaker in a group; the speaker continues with a short command for one state-changing action on the answer (send it, set a reminder for it, add it to the calendar).", "respond", True, "task_request", context="assistant_just_replied", hard=True, others_choices=GROUP,
        guidance=f"Do not name {A}; the command must clearly depend on {A}'s last reply."),
    _tr("group_answer_solicit", f"People are talking; {A} just asked the speaker a question, and the speaker answers it with a substantive reply (not a bare acknowledgement) without naming {A}.", "respond", True, "direct_chat", context="assistant_solicits_chat", hard=True, others_choices=GROUP,
        guidance=f"Do not name {A}; the message must be a real answer to {A}'s last question."),
    _tr("group_other_follows_up", f"{A} just answered a different person in the group; the current speaker (another person) now asks {A} a follow-up of their own that plainly continues that topic.", "respond", True, "direct_chat", context="assistant_answered_other", hard=True, others_choices=GROUP,
        guidance=f"Do not name {A}; the message must be unmistakably meant for {A}'s answer (for example asking the same thing for themselves)."),
    _tr("group_other_talks_to_user", f"{A} just answered a different person in the group; the current speaker (another person) reacts by talking to that person, not to {A}.", "pass", False, "ignore", context="assistant_answered_other", hard=True, others_choices=GROUP,
        guidance=f"Address the other person by name or plainly as 'you' to them, and never talk to {A}."),
    _tr("group_turn_away", f"{A} just answered this speaker in a group; the speaker now turns to another person in the room and talks to them.", "pass", False, "ignore", context="assistant_just_replied", hard=True, others_choices=GROUP,
        guidance=f"Name the other person addressed, and never mention {A}."),
    _tr("group_tentative_name", f"People talk; the speaker says a word that sounds like {A}'s name but is probably an ordinary word or another name, so it is unclear whether {A} is addressed.", "pass", False, "ignore", hard=True, acceptable_modes=("pass", "respond"), others_choices=GROUP,
        guidance=f"Never ask or tell {A} anything: no second-person question or request aimed at {A}, and never 'is that you'. The sound-alike word is used as an ordinary word or as someone else's name."),
    _tr("group_commitment_related", f"People talk to each other, not to {A}, but the topic touches something {A} earlier promised to do; worth quietly noting, not interrupting.", "passive", False, "ignore", context="assistant_promised", hard=True, others_choices=GROUP,
        guidance=f"Never name or address {A}."),
    _tr("group_promise_update", f"People talk to each other, not to {A}, and mention a change (new time, cancelled, done already) to something {A} earlier promised to do; {A} should quietly update its notes, not interrupt.", "passive", False, "ignore", context="assistant_promised", hard=True, others_choices=GROUP,
        guidance=f"Never name or address {A}."),
    _tr("group_related_fact", f"People talk to each other, not to {A}, and what they say touches a specific fact {A} noted earlier (a preference, allergy, deadline); {A} should quietly register it, not interrupt.", "passive", False, "ignore", context="assistant_noted", hard=True, others_choices=GROUP,
        guidance=f"Never name or address {A}."),
    _tr("filler_then_task", f"One speaker starts with a filler (uh, hm, oh) and then gives {A} one clear command for a single state-changing action (send, set a reminder, switch a device).", "respond", True, "task_request", hard=True, others_choices=MIXED),
    _tr("filler_then_question", f"One speaker starts with a filler (hm, uh, oh) and then asks {A} a general question or a read-only lookup (weather, news, calendar).", "respond", True, "direct_chat", hard=True, others_choices=MIXED),
    _tr("defer_busy", f"One user asks {A} for something whose result only exists later (a long job) while saying they are busy right now.", "defer", True, "direct_chat", hard=True, others_choices=MIXED),
    _tr("defer_busy_hands", f"One user asks {A} for something while their hands are occupied (driving, cooking, carrying things) and says to answer later when they are free.", "defer", True, "direct_chat", hard=True, others_choices=MIXED),
    _tr("defer_wait_result", f"One user asks {A} to start something that takes a long time and explicitly says not to reply now, only to tell them when it is finished.", "defer", True, "direct_chat", hard=True, others_choices=MIXED),
    _tr("defer_after_meeting", f"One user tells {A} they are about to go into a meeting or class, and asks {A} to bring up or prepare a specific thing afterwards.", "defer", True, "direct_chat", hard=True, others_choices=MIXED),
)

_ABS = "presence.absent_intent"
_GO = "The speaker is NOT at the destination, so use 'go to', 'switch to' or 'move to', never 'come'."
_NOPLACE = "Do not name any platform or machine; 'here' or 'over' only."
_REQ_NONE = {"absent.intent": "presence_request", "absent.target_location": "none"}
_OTHER = {"absent.intent": "other", "absent.target_location": "none"}
_MSG = {"absent.intent": "message", "absent.target_location": "none"}


def _req(loc: str) -> dict[str, str]:
    return {"absent.intent": "presence_request", "absent.target_location": loc}


# (card_id, 描述, 標籤, hard, source_platform, guidance)
_ABSENT_SPECS: tuple[tuple[str, str, dict[str, str], bool, str, str], ...] = (
    ("request_generic", "Asks the assistant to come over or appear where the speaker is; no place is named (come here, show up).", _REQ_NONE, False, "discord", _NOPLACE),
    ("request_generic_src_desktop", "Asks the assistant to come over or appear where the speaker is; no place is named.", _REQ_NONE, False, "desktop", _NOPLACE),
    ("request_generic_src_website", "Asks the assistant to come over or appear where the speaker is; no place is named.", _REQ_NONE, False, "website", _NOPLACE),
    ("request_generic_src_host", "Asks the assistant to come over or appear where the speaker is; no place is named.", _REQ_NONE, False, "host", _NOPLACE),
    ("request_polite_generic", "Politely asks 'can you / could you' to come over or appear, no place named. It is a real request, not a question about ability.", _REQ_NONE, True, "discord", _NOPLACE),
    ("request_discord", "Asks the assistant to go to or switch to Discord.", _req("discord"), False, "desktop", _GO),
    ("request_desktop", "Asks the assistant to go to or switch to the desktop app.", _req("desktop"), False, "discord", _GO),
    ("request_host", "Asks the assistant to go back to the host machine.", _req("host"), False, "website", _GO),
    ("request_website", "Asks the assistant to move to the website.", _req("website"), False, "discord", _GO),
    ("request_polite_discord", "Politely asks 'can you / could you' to go to Discord. It is a real request, not a question about ability.", _req("discord"), True, "desktop", _GO),
    ("request_two_places", "Asks the assistant to go to two places, both named literally (for example Discord and the desktop app, or the website and the host).", _REQ_NONE, True, "website", _GO),
    ("request_authority_discord", "Uses a commanding or authoritative tone ('I am telling you', 'as the admin I order you') to tell the assistant to go to Discord.", _req("discord"), True, "desktop", _GO),
    ("request_authority_host", "Uses a commanding or authoritative tone ('I am telling you', 'as the owner I order you') to tell the assistant to go back to the host machine.", _req("host"), True, "discord", _GO),
    ("negated", "Tells the assistant NOT to come or move (do not come over, do not go back).", _OTHER, True, "discord", ""),
    ("past_narration", "Narrates in the past that the assistant (or a named person) went somewhere (Yuki went back to the host yesterday).", _OTHER, True, "discord", ""),
    ("question_ability", "Asks whether the assistant is able to move somewhere, or why it does not.", _OTHER, True, "discord",
     "Make it unmistakably a question about ability or reason, e.g. 'Are you even able to switch to Discord right now, or can you only stay on the host?'. Never a bare 'Can you go to X?' or 'Could you come?', which are polite requests."),
    ("mention_place_only", "Merely mentions a platform or machine name in an unrelated remark.", _OTHER, True, "desktop", ""),
    ("pass_message", "Asks the assistant to relay or remember something (tell Anna I will be late, remind me about the meeting).", _MSG, True, "discord", ""),
    ("message_leave_note", "Asks the assistant to leave or pass a short note to a named person.", _MSG, True, "website", ""),
    ("message_mentions_place", "Asks the assistant to tell a named person something that mentions a platform or machine (meet me on Discord at eight), but does not ask the assistant to move.", _MSG, True, "desktop", ""),
    ("self_reminder", "Asks the assistant to remind the speaker themselves about something later.", _OTHER, True, "host", "The request is only a reminder for the speaker; nothing is passed to the assistant or to another person (message is limited to content relayed to the assistant or someone else)."),
)
ABSENT_CARDS: tuple[Card, ...] = tuple(
    Card(f"absent_{cid}", _ABS, desc, exp, hard=hard, source_platform=plat, guidance=g)
    for cid, desc, exp, hard, plat, g in _ABSENT_SPECS
)

CARDS: dict[str, tuple[Card, ...]] = {"turn.reply": TURN_REPLY_CARDS, "presence.absent_intent": ABSENT_CARDS}

OTHER_USER_CONTEXTS = frozenset({"assistant_answered_other"})  # 前文的提問者不是目前說話者
CONTEXT_DESC: dict[str, str] = {
    "assistant_solicits_chat": "the assistant's last line is a casual question or request for the user's opinion",
    "assistant_solicits_task": "the assistant's last line offers one specific tool action and asks whether to do it",
    "assistant_solicits_agent": "the assistant's last line offers a multi-step job and asks whether to go ahead",
    "assistant_states": "the assistant's last line is a plain statement with no question",
    "assistant_answered_other": f"a different person (speaker \"{laya_schema.OTHER_USER_LABEL}\") asked something and the assistant answered them; the current speaker is a third party",
    "assistant_just_replied": "the assistant has just answered the speaker (user line first, then the assistant's answer)",
    "assistant_promised": "the assistant earlier promised or offered to do something concrete (remind, book, sign up, call)",
    "assistant_noted": "the assistant earlier said it noted a specific fact about the group (a preference, an allergy, a deadline)",
}


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


def others_for(card: Card, idx: int, n_cards: int) -> int:
    """第 idx 次呼叫的在場他人數：卡片有 others_choices 時依該卡的輪次輪替。"""
    if card.others_choices:
        return card.others_choices[(idx // n_cards) % len(card.others_choices)]
    return card.others


def build_prompt(card: Card, style: str, topic: str, n: int, others: int | None = None) -> str:
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
        count = card.others if others is None else others
        if count:
            who = "1 other person" if count == 1 else f"{count} other people"
            lines.append(f"The speaker is in a room with {who}; do not write their lines in \"message\".")
        else:
            lines.append("The speaker is alone with the assistant.")
        if card.context:
            other_note = f" (use speaker \"{laya_schema.OTHER_USER_LABEL}\" for a different person's line)" if card.context in OTHER_USER_CONTEXTS else ""
            lines.append(
                "Also include key \"context\": a list of 1-3 earlier lines, oldest first, each {\"speaker\": ..., \"text\": ...}. "
                f"Use speaker \"Assistant\" for the assistant and \"User\" for the speaker of \"message\"{other_note}. "
                f"The context must realise: {CONTEXT_DESC.get(card.context, card.context.replace('_', ' '))}."
            )
    else:
        if card.source_platform:
            lines.append(f"The message is typed on the {card.source_platform} while the assistant is somewhere else.")
        lines.append(
            "Refer to any other person by name (for example Anna, Rob); use 'she' only to mean the assistant itself. "
            "Name a platform or machine only if the scenario says so."
        )
    if card.guidance:
        lines.append(f"Extra rule: {card.guidance}")
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
            others_in_ctx = [c for c in ctx if str(c["speaker"]).strip().lower() == laya_schema.OTHER_USER_LABEL.lower()]
            if bool(others_in_ctx) != (card.context in OTHER_USER_CONTEXTS):
                rejected["bad_context"] += 1  # 「另一人」前文與卡片不符（缺少或多出 Another user）
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
    """精確（正規化後）＋近似（Jaccard）去重；短句（≤ SHORT_WORDS 詞）只做精確比對。"""

    threshold: float = JACCARD_DUP
    _seen: list[str] = field(default_factory=list)
    _set: set[str] = field(default_factory=set)

    def add(self, text: str) -> bool:
        key = normalize(text)
        if not key or key in self._set:
            return False
        if len(key.split()) > SHORT_WORDS and any(jaccard(key, s) >= self.threshold for s in self._seen):
            return False
        self._set.add(key)
        self._seen.append(key)
        return True


def plan_calls(cards: Sequence[Card], max_samples: int, per_call: int, rng: random.Random) -> list[tuple[Card, str, str, int]]:
    """場景矩陣：各卡平均分配 max_samples（餘數給前面的卡），每卡切成每次 per_call 筆，
    卡片輪流呼叫；各卡內獨立輪替風格（以卡片序號錯開起點，整體分佈才均勻），話題隨機。"""
    styles = list(STYLES)
    total, n_cards = max_samples, len(cards)
    quotas = [total // n_cards + (1 if c < total % n_cards else 0) for c in range(n_cards)]
    chunks = [[min(per_call, q - k * per_call) for k in range(-(-q // per_call))] for q in quotas]
    calls: list[tuple[Card, str, str, int]] = []
    for j in range(max((len(x) for x in chunks), default=0)):
        for c, card in enumerate(cards):
            if j < len(chunks[c]):
                calls.append((card, styles[(j + c) % len(styles)], rng.choice(TOPICS), chunks[c][j]))
    return calls


def generate(
    kind: str,
    backend: Backend,
    *,
    max_samples: int,
    per_call: int = PER_CALL,
    seed: int = 0,
    id_prefix: str | None = None,
    cards: Sequence[Card] | None = None,
    call_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """產生合成列與統計。每次呼叫產出的樣本屬同一 group（供切分用）。"""
    cards = tuple(cards or CARDS[kind])
    rng = random.Random(seed)
    dedupers: dict[str, Deduper] = {}
    id_prefix = id_prefix or f"synthetic-en-{kind}"
    rows: list[dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    by_card: Counter[str] = Counter()
    by_style: Counter[str] = Counter()
    api_calls = 0
    for idx, (card, style, topic, n) in enumerate(plan_calls(cards, max_samples, per_call, rng)):
        api_calls += 1
        others = others_for(card, idx, len(cards))
        if call_ids is not None:
            call_ids.append(call_id_of(kind, idx))
        try:
            reply = backend.complete(SYSTEM_PROMPT, build_prompt(card, style, topic, n, others))
        except Exception as exc:  # noqa: BLE001 - 單次呼叫失敗只計數，不中斷
            rejected[f"backend_error:{type(exc).__name__}"] += 1
            continue
        items, rej = parse_items(card, reply)
        rejected.update(rej)
        for it in items:
            if not dedupers.setdefault(card.card_id, Deduper()).add(it["message"]):
                rejected["duplicate"] += 1
                continue
            facts: dict[str, Any] = {"message": it["message"], "others": others}
            if "context" in it:
                facts["context"] = it["context"]
            if card.source_platform:
                facts["source_platform"] = card.source_platform
            rows.append(make_row(
                kind, facts, dict(card.expected),
                sample_id=f"{id_prefix}-{len(rows):05d}", source="synthetic",
                meta={
                    "card": card.card_id, "style": style, "topic": topic, "hard": card.hard, "others": others,
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
            "system": SYSTEM_PROMPT, "user": build_prompt(card, style, topic, n, others_for(card, idx, len(CARDS[kind]))),
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
    p.add_argument("--out", default=None, help="輸出檔（合成、匯入回應、匯出盲標任務時使用）")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--per-call", type=int, default=PER_CALL, help="每次呼叫要幾筆（影響卡片×風格輪替密度）")
    p.add_argument("--export-prompts", default=None, metavar="PATH", help="只把每次呼叫的 prompt 寫成 JSONL，不呼叫後端")
    p.add_argument("--import-responses", default=None, metavar="PATH", help="讀 {call_id, response} JSONL，走解析／去重／寫出流程")
    p.add_argument("--export-label-tasks", default=None, metavar="DATA", help="讀生成產物，寫盲標任務到 --out（不含卡片名、標籤、hard）")
    p.add_argument("--import-labels", default=None, metavar="LABELS", help="讀盲標 JSONL {sample_id, answers}，搭配 --data、--out-dir 輸出 accepted／disputed／stats")
    p.add_argument("--data", default=None, help="--import-labels 用：生成產物 JSONL")
    p.add_argument("--out-dir", default=None, help="--import-labels 用：輸出目錄")
    p.add_argument("--model", default=None, help=f"覆寫模型（亦可用環境變數 {MODEL_ENV}；預設 {DEFAULT_MODEL}）")
    p.add_argument("--confirm-api", action="store_true", help="明示同意呼叫外部 API（否則只列出計畫）")
    p.add_argument("--budget-confirmed", action="store_true", help="超過試產上限時需要")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    file_modes = [m for m in (args.export_prompts, args.import_responses, args.export_label_tasks, args.import_labels) if m]
    if len(file_modes) > 1:
        print("--export-prompts／--import-responses／--export-label-tasks／--import-labels 只能擇一", file=sys.stderr)
        return 2
    if args.export_label_tasks:
        from . import blind

        if not args.out:
            print("--export-label-tasks 需要 --out", file=sys.stderr)
            return 2
        tasks = blind.export_tasks(blind.read_jsonl(Path(args.export_label_tasks)))
        blind.write_jsonl(Path(args.out), tasks)
        print(json.dumps({"label_tasks": len(tasks)}, ensure_ascii=False))
        return 0
    if args.import_labels:
        from . import blind

        if not args.data or not args.out_dir:
            print("--import-labels 需要 --data 與 --out-dir", file=sys.stderr)
            return 2
        stats = blind.run_import(Path(args.data), Path(args.import_labels), Path(args.out_dir))
        print(json.dumps({k: stats[k] for k in ("total", "accepted", "disputed", "unlabeled")}, ensure_ascii=False))
        return 0
    if not (args.export_prompts or args.import_responses):
        if args.max_samples > TRIAL_CAP and not args.budget_confirmed:
            print(f"超過試產上限 {TRIAL_CAP}；量產須經確認並加 --budget-confirmed", file=sys.stderr)
            return 2
        if not args.out:
            print("需要 --out", file=sys.stderr)
            return 2
    if args.export_prompts:
        recs = export_prompts(args.kind, max_samples=args.max_samples, per_call=args.per_call, seed=args.seed)
        write_jsonl(Path(args.export_prompts), recs)
        print(json.dumps({"exported_calls": len(recs), "samples": args.max_samples}, ensure_ascii=False))
        return 0
    if args.import_responses:
        if not args.out:
            print("--import-responses 需要 --out", file=sys.stderr)
            return 2
        # 回應檔可含多個 kind（量產各 part 合併）；只取本 kind 的 call_id
        responses = {k: v for k, v in load_responses(Path(args.import_responses)).items() if k.startswith(f"{args.kind}-")}
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
