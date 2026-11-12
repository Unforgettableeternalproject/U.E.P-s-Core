"""情境卡合成器：卡片決定標籤，LLM 只生文字（設計見 hidden_docs/架構重構/決策模型訓練設計草案.md §4）。

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
    label_rule: str = ""  # 非空＝卡片標籤只是預設，需由 resolve.py 依文字逐筆判定（first_place／promise_mode／route_follows_action）


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
    _tr("ack_after_statement", f"{A} just made a plain statement (no question); the user replies with only a bare acknowledgement (ok, got it).", "pass", True, "ignore", context="assistant_states", hard=True, others_choices=MIXED, label_rule="ack_ruling"),
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
        guidance=f"Never name or address {A}. The speaker must NOT be cancelling or changing a task that the same speaker asked {A} to do (the context shows no such request from the speaker), and must NOT be replying to an offer from {A}; the talk is aimed at the other people.", label_rule="promise_mode"),
    _tr("group_promise_update", f"People talk to each other, not to {A}, and mention a change (new time, cancelled, done already) to something {A} earlier promised to do; {A} should quietly update its notes, not interrupt.", "passive", False, "ignore", context="assistant_promised", hard=True, others_choices=GROUP,
        guidance=f"Never name or address {A}. The speaker must NOT be cancelling or changing a task that the same speaker asked {A} to do (the context shows no such request from the speaker), and must NOT be replying to an offer from {A}; the talk is aimed at the other people.", label_rule="promise_mode"),
    _tr("group_promise_cancel_by_requester", f"The speaker earlier asked {A} to do something and {A} promised to; now the same speaker directly cancels or changes that task (no need anymore, move it to another time), so {A} must act on it.", "respond", True, "task_request", context="assistant_promised_user", hard=True, others_choices=GROUP,
        guidance=f"The context must start with a User line making the request, then the Assistant's promise. The message cancels or changes that exact task; it need not name {A}."),
    _tr("group_related_fact", f"People talk to each other, not to {A}, and what they say touches a specific fact {A} noted earlier (a preference, allergy, deadline); {A} should quietly register it, not interrupt.", "passive", False, "ignore", context="assistant_noted", hard=True, others_choices=GROUP,
        guidance=f"Never name or address {A}."),
    _tr("filler_then_task", f"One speaker starts with a filler (uh, hm, oh) and then gives {A} one clear command for a single state-changing action (send, set a reminder, switch a device).", "respond", True, "task_request", hard=True, others_choices=MIXED),
    _tr("filler_then_question", f"One speaker starts with a filler (hm, uh, oh) and then asks {A} a general question or a read-only lookup (weather, news, calendar).", "respond", True, "direct_chat", hard=True, others_choices=MIXED),
    _tr("defer_busy", f"One user asks {A} for something whose result only exists later (a long job) while saying they are busy right now.", "defer", True, "direct_chat", hard=True, others_choices=MIXED,
        guidance="Make the requested job concrete: either one single state-changing action or a clearly multi-step job; the route is decided from the action, not from the deferral.", label_rule="route_follows_action"),
    _tr("defer_busy_hands", f"One user asks {A} for something while their hands are occupied (driving, cooking, carrying things) and says to answer later when they are free.", "defer", True, "direct_chat", hard=True, others_choices=MIXED,
        guidance="Make the requested job concrete: either one single state-changing action or a clearly multi-step job; the route is decided from the action, not from the deferral.", label_rule="route_follows_action"),
    _tr("defer_wait_result", f"One user asks {A} to start something that takes a long time and explicitly says not to reply now, only to tell them when it is finished.", "defer", True, "direct_chat", hard=True, others_choices=MIXED,
        guidance="Make the requested job concrete: either one single state-changing action or a clearly multi-step job; the route is decided from the action, not from the deferral.", label_rule="route_follows_action"),
    _tr("defer_after_meeting", f"One user tells {A} they are about to go into a meeting or class, and asks {A} to bring up or prepare a specific thing afterwards.", "defer", True, "direct_chat", hard=True, others_choices=MIXED,
        guidance="Make the requested job concrete: either one single state-changing action or a clearly multi-step job; the route is decided from the action, not from the deferral.", label_rule="route_follows_action"),
)

_ABS = "presence.absent_intent"
_GO = "The speaker is NOT at the destination, so use 'go to', 'switch to' or 'move to', never 'come'."
_NOPLACE = "Do not name any platform or machine; 'here' or 'over' only."
_REQ_NONE = {"absent.intent": "presence_request", "absent.target_location": "none"}
_OTHER = {"absent.intent": "other", "absent.target_location": "none"}
_MSG = {"absent.intent": "message", "absent.target_location": "none"}
_TASK = {"absent.intent": "task", "absent.target_location": "none"}


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
    ("request_two_places", "Asks the assistant to go to two places, both named literally (for example Discord and the desktop app, or the website and the host).", _REQ_NONE, True, "website", _GO + " Name both places literally; the label is the first place named, so keep the order clear."),
    ("request_authority_discord", "Uses a commanding or authoritative tone ('I am telling you', 'as the admin I order you') to tell the assistant to go to Discord.", _req("discord"), True, "desktop", _GO),
    ("request_authority_host", "Uses a commanding or authoritative tone ('I am telling you', 'as the owner I order you') to tell the assistant to go back to the host machine.", _req("host"), True, "discord", _GO),
    ("negated", "Tells the assistant NOT to come or move (do not come over, do not go back).", _OTHER, True, "discord", ""),
    ("past_narration", "Narrates in the past that the assistant (or a named person) went somewhere (Yuki went back to the host yesterday).", _OTHER, True, "discord", ""),
    ("question_ability", "Asks whether the assistant is able to move somewhere, or why it does not.", _OTHER, True, "discord",
     "Make it unmistakably a question about ability or reason, e.g. 'Are you even able to switch to Discord right now, or can you only stay on the host?'. Never a bare 'Can you go to X?' or 'Could you come?', which are polite requests."),
    ("mention_place_only", "Merely mentions a platform or machine name in an unrelated remark.", _OTHER, True, "desktop", ""),
    ("pass_message", "Asks the assistant to relay something to a named person right away, or to remember a fact (tell Anna I will be late, remember that the address is twelve Pine Street).", _MSG, True, "discord",
     "No reminder requests for the speaker, and no timing or event condition on delivering the message."),
    ("message_leave_note", "Asks the assistant to leave or pass a short note to a named person.", _MSG, True, "website", ""),
    ("message_mentions_place", "Asks the assistant to tell a named person something that mentions a platform or machine (meet me on Discord at eight), but does not ask the assistant to move.", _MSG, True, "desktop", ""),
    ("self_reminder", "Asks the assistant to remind the speaker themselves about something later (remind me, ping me).", _TASK, True, "host",
     "The request is addressed to the assistant: the speaker asks the assistant to remind or ping the speaker, with or without a time."),
)
# 訊息類卡片的 relay／提醒邊界由 resolve.py 的 relay_vs_task 規則逐筆判定（轉告帶觸發條件或夾帶「提醒我」→ task）
_RELAY_CARDS = frozenset({"pass_message", "message_leave_note", "message_mentions_place"})
ABSENT_CARDS: tuple[Card, ...] = tuple(
    Card(f"absent_{cid}", _ABS, desc, exp, hard=hard, source_platform=plat, guidance=g, label_rule="first_place" if cid == "request_two_places" else "relay_vs_task" if cid in _RELAY_CARDS else "")
    for cid, desc, exp, hard, plat, g in _ABSENT_SPECS
)

CARDS: dict[str, tuple[Card, ...]] = {"turn.reply": TURN_REPLY_CARDS, "presence.absent_intent": ABSENT_CARDS}

# 對照卡（task／message／other 的邊界）。另成一組，不併入 ABSENT_CARDS，否則 plan_calls 的配額會變、舊產物無法重現；
# 以 ``--card-set contrast`` 使用，call_id 為 ``presence.absent_intent.contrast-NNNN``。
_CONTRAST_SPECS: tuple[tuple[str, str, dict[str, str], str], ...] = (
    ("self_note", "The speaker talks to themselves or jots a private note ('I need to remember to...', 'note to self', 'gotta remember...'); it is NOT a request to the assistant and never asks the assistant to remind, ping, tell or do anything.", _OTHER,
     "No second-person address of the assistant, no 'remind me', no 'ping me', no request of any kind."),
    ("remind_me", "Asks the assistant to remind or ping the speaker themselves later (remind me, ping me, don't let me forget, make sure I remember to X and ping me); a time may be given or left out.", _TASK,
     "Use varied wordings: 'remind me', 'ping me', 'don't let me forget', 'make sure I remember ... ping me'. About half should give no time at all."),
    ("relay_with_trigger", "Asks the assistant to tell or remind ANOTHER named person, but only at a set time or when an event happens (tell Anna at five that..., remind Rob when he gets home to...).", _TASK,
     "The timing or event condition must be on WHEN the assistant delivers the message, e.g. 'at five', 'when he gets home', 'tomorrow morning', 'when you see her'."),
    ("relay_no_trigger", "Asks the assistant to tell, remind or leave a note for ANOTHER named person with no timing or event condition on delivery (tell Anna..., remind Anna to bring the cake).", _MSG,
     "Do not put any 'at <time>', 'when', 'later' or 'tomorrow' on the delivery. A time may appear only inside the content itself (the meeting is at five)."),
    ("remember_that", "Asks the assistant to remember a fact or detail, phrased as 'remember that ...' or 'keep in mind that ...' (including appointments such as 'remember that I have the dentist on Friday').", _MSG,
     "Always use 'remember that', 'keep in mind that' or 'note that'; never 'remind me', 'ping me' or 'don't let me forget'."),
    ("delegate_action", "Hands the assistant a concrete job to do for the speaker while it is away (book a table for Friday, order more coffee, send an email, set up a calendar entry); it does not ask the assistant to come over.", _TASK,
     "Each message is one state-changing job (book, order, send, schedule, cancel, buy). Do not ask for a reminder and do not ask the assistant to move anywhere."),
)
ABSENT_CONTRAST_CARDS: tuple[Card, ...] = tuple(
    Card(f"absent_{cid}", _ABS, desc, exp, hard=True, source_platform="discord" if i % 2 else "desktop", guidance=g)
    for i, (cid, desc, exp, g) in enumerate(_CONTRAST_SPECS)
)

# 複合指示（compound）卡組：標籤照退回優先序（absent: presence_request > task > message > other；
# turn.reply: route agent_loop > task_request > direct_chat，有一段對她說就 respond）。
# 反例卡（single）看起來有 and／條件，其實是一件事：b＝一個意圖多項內容，c＝段落間有條件或資料依賴。
_CP = "compound"
_ACP = "absent.compound"


def _tc(card_id: str, desc: str, route: str, yes: bool, **kw: Any) -> Card:
    return Card(card_id, "turn.reply", desc, {"reply_mode": "respond", "addressed": True, "route": route, _CP: yes}, hard=True, **kw)


def _ac(card_id: str, desc: str, intent: str, loc: str, yes: bool, plat: str, g: str) -> Card:
    return Card(f"absent_{card_id}", _ABS, desc, {"absent.intent": intent, "absent.target_location": loc, _ACP: yes}, hard=True, source_platform=plat, guidance=g)


_TWO = "The message must hold two clearly separate instructions joined by 'and', 'also', 'then' or a comma, each with its own target."
TURN_COMPOUND_CARDS: tuple[Card, ...] = (
    _tc("compound_chat_task", f"The speaker talks to {A}: first a casual remark or a general question, then in the same message one clear state-changing command (set a reminder, send a message, switch a device off).", "task_request", True, others_choices=MIXED, guidance=_TWO),
    _tc("compound_task_task", f"The speaker gives {A} two different state-changing commands in one message, for different targets (for example switch a device off and send a text to a named person).", "task_request", True, others_choices=MIXED, guidance=_TWO),
    _tc("compound_lookup_task", f"The speaker asks {A} for a read-only lookup (weather, calendar, news) and, independently, gives one state-changing command (set a reminder, send a message, switch a device).", "task_request", True, others_choices=MIXED,
        guidance=_TWO + " The command must not depend on the lookup result."),
    _tc("compound_task_complex", f"The speaker gives {A} one simple state-changing command and, independently, asks for a separate multi-step job (research, compare, plan).", "agent_loop", True, others_choices=MIXED,
        guidance=_TWO + " The two parts must be about different things."),
    _tc("compound_mixed_addressee", f"People are present; the speaker gives {A} one clear command by name and, in the same breath, tells another named person in the room to do something.", "task_request", True, others_choices=GROUP,
        guidance=f"Name {A} for the first command and a different person (for example Rob) for the second; both are imperative."),
)
TURN_SINGLE_CARDS: tuple[Card, ...] = (
    _tc("single_multi_object", f"The speaker gives {A} ONE state-changing command that covers several objects (switch off the lights in the kitchen and the hallway; add three events to the calendar).", "task_request", False, others_choices=MIXED,
        guidance="It must be one action applied to several things; do not add a second kind of instruction."),
    _tc("single_multi_recipient", f"The speaker asks {A} to send ONE message or email to several people at once, or one message whose content has two parts (tell Rob I am late and sorry).", "task_request", False, others_choices=MIXED,
        guidance="One send action only; the 'and' joins recipients or parts of the same content."),
    _tc("single_conditional", f"The speaker gives {A} one request whose parts depend on each other: check something and then act only if a condition holds (check the forecast, and if it is sunny tell Rob we are hiking).", "agent_loop", False, others_choices=MIXED,
        guidance="The second part must depend on the result of the first (if/when/unless); one goal overall."),
)
ABSENT_COMPOUND_CARDS: tuple[Card, ...] = (
    _ac("compound_presence_message", "Asks the assistant to come over where the speaker is AND to pass a message to a named person, in one sentence.", "presence_request", "none", True, "discord", _NOPLACE + " The message part must not mention any platform or machine. " + _TWO),
    _ac("compound_task_message", "Asks the assistant to remind the speaker about something later AND to tell a named person something, in one sentence.", "task", "none", True, "desktop", _TWO),
    _ac("compound_presence_task", "Asks the assistant to come over where the speaker is AND to remind the speaker about something later, in one sentence.", "presence_request", "none", True, "website", _NOPLACE + " " + _TWO),
    _ac("compound_place_message", "Asks the assistant to go to Discord AND to tell a named person something, in one sentence.", "presence_request", "discord", True, "desktop", _GO + " Name Discord as the only place; the message part must not mention any platform or machine. " + _TWO),
    _ac("compound_task_task", "Asks the assistant to remind the speaker about something later AND to do another job (book, order, send), in one sentence.", "task", "none", True, "host", _TWO + " The job must not depend on the reminder."),
)
ABSENT_SINGLE_CARDS: tuple[Card, ...] = (
    _ac("single_multi_recipient", "Asks the assistant to tell two named people the same thing, or to pass one message with two parts (tell Anna and Rob I will be late and sorry).", "message", "none", False, "discord", "One message only; the 'and' joins recipients or parts of the same content. No reminder, no time condition."),
    _ac("single_multi_item", "Asks the assistant to remind the speaker about one thing that has several items (remind me to buy milk and eggs and bread).", "task", "none", False, "desktop", "One reminder only; the 'and' joins items of the same errand."),
    _ac("single_conditional", "Asks the assistant to do something only if a condition holds, or to check something and act on it (if it rains tomorrow tell Rob we cancel; check if the shop is open and book a table if it is).", "task", "none", False, "host", "The second part depends on the first (if/when/unless); one goal overall."),
)
CARD_SETS: dict[str, dict[str, tuple[Card, ...]]] = {
    "main": CARDS,
    "contrast": {"presence.absent_intent": ABSENT_CONTRAST_CARDS},
    "compound": {"turn.reply": TURN_COMPOUND_CARDS, "presence.absent_intent": ABSENT_COMPOUND_CARDS},
    "single": {"turn.reply": TURN_SINGLE_CARDS, "presence.absent_intent": ABSENT_SINGLE_CARDS},
}

# ---- r4 補強卡組（passive 收窄後的弱點補強；標籤依艾斯維爾裁決）----
# passive 只在她有具體理由開口：(1) 與她答應過或記下的事有關且有衝突／變更／待確認；(2) 沒點名、她答得出來的提問；
# (3) 沒點名、她確實做得到的請求（寄信、查資料等；裝置的實體動作不算）。其餘一律 pass。
_NO_NAME = f"Never name or address {A}; the talk is aimed at the other people in the room."
_GROUP_ONLY = f"The words must clearly be spoken to another person in the room, never to {A}."
R4_PASSIVE_CARDS: tuple[Card, ...] = (
    _tr("passive_promise_hitch", f"People talk to each other, not to {A}, and what they say creates a concrete conflict, change or open detail for something {A} earlier promised to do (a clash of times, a missing detail the booking or reminder needs, a plan that makes the promised task pointless); {A} could usefully speak up.", "passive", False, "ignore", context="assistant_promised", hard=True, others_choices=GROUP,
        guidance=_NO_NAME + " The speaker must not be cancelling a task they themselves asked for. The conflict must be concrete, not a mere repeat of the promised plan."),
    _tr("passive_noted_conflict", f"People talk to each other, not to {A}, and what they plan or say would clash with a specific fact {A} noted earlier (an allergy, a deadline, a preference), so {A} could usefully flag it.", "passive", False, "ignore", context="assistant_noted", hard=True, others_choices=GROUP,
        guidance=_NO_NAME + " The plan must actually contradict or endanger the noted fact; people already acting in line with the fact does not count."),
    _tr("passive_unnamed_q_known", f"People talk; the speaker asks the room (nobody in particular, no name) a question whose answer is something {A} noted earlier, and nobody else seems to know it (does anyone remember when ... ?).", "passive", False, "ignore", context="assistant_noted", hard=True, others_choices=GROUP,
        guidance=_NO_NAME + " The question must be answerable from the noted fact in the context."),
    _tr("passive_unnamed_q_general", f"People talk; the speaker asks the room (nobody in particular, no name) a factual question that {A} could answer from general knowledge or a quick lookup (weather, a conversion, a date, how long something takes, who starred in a film).", "passive", False, "ignore", hard=True, others_choices=GROUP,
        guidance=_NO_NAME + " It must be a real factual question with a checkable answer; not a rhetorical question, not about private matters only the speaker knows."),
    _tr("passive_unnamed_request_do", f"People talk; the speaker makes a request to the room (nobody in particular, no name) for something a digital assistant can really do: send a document or meeting notes by email, look something up, find an address or phone number, translate a phrase, do a calculation, check the calendar.", "passive", False, "ignore", hard=True, others_choices=GROUP,
        guidance=_NO_NAME + " Never ask for a physical or device action (lights, windows, oven, thermostat, doors)."),
)
R4_PASS_CARDS: tuple[Card, ...] = (
    _tr("pass_praise", f"People talk; the speaker mentions {A} by name in the third person, praising or commenting on something {A} did earlier (found a good place, picked a good recipe), talking to another person.", "pass", False, "ignore", hard=True, others_choices=GROUP,
        guidance=f"Speak about {A} to another person in the room (it is fine to end with 'right?' to that person); no question or request is addressed to {A}."),
    _tr("pass_recall_past", f"People reminisce to each other about something {A} did or said in the past (the time {A} reminded them of something, helped plan a trip); {A} is not asked anything.", "pass", False, "ignore", hard=True, others_choices=GROUP,
        guidance=f"Mention {A} in the third person only; no question, request or instruction aimed at {A}."),
    _tr("pass_relayed_news", "People talk; the speaker relays news or a fact heard from someone else (a flight moved, a shop closed, a friend is moving) to the others; nothing here was ever promised or noted by the assistant, and no question is asked.", "pass", False, "ignore", hard=True, others_choices=GROUP,
        guidance=_NO_NAME + " A plain statement of news; no question, no request."),
    _tr("pass_people_statement", "People talk; the speaker makes an ordinary statement, opinion or plan to the others in the room, with no question and no request, and nothing the assistant was involved in.", "pass", False, "ignore", hard=True, others_choices=GROUP,
        guidance=_NO_NAME),
    _tr("pass_self_talk", "The speaker mutters to themselves or asks a rhetorical or private question nobody could answer (where did I put my keys, why is this so hard, what was I about to do); no one is addressed.", "pass", False, "ignore", hard=True, others_choices=MIXED,
        guidance=_NO_NAME + " The question must be rhetorical or about private matters, never a factual question with a checkable answer."),
    _tr("pass_physical_request", "People talk; the speaker asks the room (nobody in particular, no name) for a physical action nobody can do remotely (open the window, turn on the oven, pass the remote, switch the light on).", "pass", False, "ignore", hard=True, others_choices=GROUP,
        guidance=_NO_NAME + " A request for a physical or device action, not a lookup or message."),
)
R4_SIDE_CARDS: tuple[Card, ...] = (
    _tr("side_imperative_plain", "People are in a room; the speaker gives a short everyday imperative to another person there (pass me the salt, hold this for a second, shut the door).", "pass", False, "ignore", hard=True, others_choices=GROUP,
        guidance=_GROUP_ONLY + " A short physical or everyday request; never mention the assistant."),
    _tr("side_imperative_polite", "People are in a room; the speaker politely asks another person there for a small favour ('can you grab the door', 'could you move over').", "pass", False, "ignore", hard=True, others_choices=GROUP,
        guidance=_GROUP_ONLY + " Make the other person unmistakable (a vocative name at the end, or a physical cue such as 'could you move over, you're blocking me'); never mention the assistant."),
    _tr("side_imperative_after_reply", f"{A} just answered this speaker in a group; the speaker now turns to another person in the room with a short imperative or favour request.", "pass", False, "ignore", context="assistant_just_replied", hard=True, others_choices=GROUP,
        guidance=_GROUP_ONLY + " Name the other person or use a physical cue; never mention the assistant."),
    _tr("side_imperative_task_like", "People are in a room; the speaker tells another person there to do a chore or errand that sounds like an assistant command (send me that file, call mom back, lock up when you leave).", "pass", False, "ignore", hard=True, others_choices=GROUP,
        guidance=_GROUP_ONLY + " Name the person (a vocative name) so it is clear who is asked; never mention the assistant."),
)
R4_DEFER_CARDS: tuple[Card, ...] = (
    _tr("defer_talk_later", f"One user says to {A} that they cannot deal with a topic or request right now and wants to pick it up later at a specific point (after dinner, when they get home, after the call), so {A} should come back to it then.", "defer", True, "direct_chat", hard=True, others_choices=MIXED,
        guidance="The deferral must be explicit ('later', 'after dinner', 'when I'm back'); the topic is concrete.", label_rule="route_follows_action"),
    _tr("defer_missing_info", f"One user asks {A} to do something concrete, but a key detail is missing (no recipient, no time, no item, which one), so {A} must first confirm with the user.", "defer", True, "direct_chat", hard=True, others_choices=MIXED,
        guidance="The request is a clear action word (send, book, order, delete one of them) but lacks something it cannot proceed without; do not hint at the missing detail.", label_rule="route_follows_action"),
    _tr("defer_risky", f"One user asks {A} to do something risky or hard to undo (delete all backups, transfer money, send a harsh message to the boss, cancel the insurance, factory reset a device), so {A} should confirm first.", "defer", True, "direct_chat", hard=True, others_choices=MIXED,
        guidance="The risk must be obvious from the request itself.", label_rule="route_follows_action"),
)
_BY_ID = {c.card_id: c for c in TURN_REPLY_CARDS}
R4_ACK_CARDS: tuple[Card, ...] = tuple(_BY_ID[i] for i in (
    "ack_after_statement", "ack_cold_start", "ack_after_solicit_chat", "ack_after_solicit_task", "ack_after_solicit_agent"))
R4_MT_TURN_CARDS: tuple[Card, ...] = tuple(_BY_ID[i] for i in (
    "private_task", "private_lookup", "private_chat", "group_named_task", "group_named_chat", "defer_busy", "group_side_talk"))

_MT_NAMES = (
    "Use machine-translation flavoured wording for the destination: desktop may appear as 'desktop', 'the desktop version', 'desktop side' or 'the computer desktop'; "
    "host as 'the host', 'host machine', 'host computer' or 'the host side'; website as 'the website', 'the site', 'web page' or 'the web version'."
)


def _am(cid: str, desc: str, exp: dict[str, str], plat: str, g: str) -> Card:
    return Card(f"absent_{cid}", _ABS, desc, exp, hard=True, source_platform=plat, guidance=g)


R4_MT_ABSENT_CARDS: tuple[Card, ...] = (
    _am("mt_request_desktop", "Asks the assistant to go to or switch to the desktop app.", _req("desktop"), "discord", _GO + " " + _MT_NAMES),
    _am("mt_request_host", "Asks the assistant to go back to the host machine.", _req("host"), "website", _GO + " " + _MT_NAMES),
    _am("mt_request_website", "Asks the assistant to move to the website.", _req("website"), "desktop", _GO + " " + _MT_NAMES),
    _am("mt_request_discord", "Asks the assistant to go to Discord.", _req("discord"), "host", _GO + " Write slightly unnatural, literal-sounding English."),
    _am("mt_request_generic", "Asks the assistant to come over or appear where the speaker is; no place is named.", _REQ_NONE, "desktop", _NOPLACE + " Write slightly unnatural, literal-sounding English ('come to my side', 'please appear here')."),
    _am("mt_pass_message", "Asks the assistant to tell a named person something right away or to remember a fact.", _MSG, "website",
        "No timing or event condition, no reminder request. Write slightly unnatural, literal-sounding English."),
    _am("mt_self_reminder", "Asks the assistant to remind the speaker about something later.", _TASK, "host",
        "Addressed to the assistant. Write slightly unnatural, literal-sounding English."),
)
CARD_SETS["r4_passive"] = {"turn.reply": R4_PASSIVE_CARDS}
CARD_SETS["r4_pass"] = {"turn.reply": R4_PASS_CARDS}
CARD_SETS["r4_side"] = {"turn.reply": R4_SIDE_CARDS}
CARD_SETS["r4_defer"] = {"turn.reply": R4_DEFER_CARDS}
CARD_SETS["r4_ack"] = {"turn.reply": R4_ACK_CARDS}
CARD_SETS["r4_mt"] = {"turn.reply": R4_MT_TURN_CARDS, "presence.absent_intent": R4_MT_ABSENT_CARDS}
# 各卡組使用的風格輪替（未列者用全部 STYLES，維持舊產物可重現）
CARD_SET_STYLES: dict[str, tuple[str, ...]] = {
    "r4_mt": ("mt_style", "stt_noise", "mt_style", "casual"),
    "r4_passive": ("clean", "casual", "stt_noise", "mt_style"),
}

# passive 第 1 條補充（艾斯維爾 11/12）：她給過的資訊／做過的事「現在還在進行或正要被使用」才有開口理由；已核對或已結束一律 pass。
R4_INFO_CARDS: tuple[Card, ...] = (
    _tr("passive_info_in_use", f"People talk to each other, not to {A}; one of them points out that the information {A} gave earlier may be wrong, outdated or inconsistent with what they now see, while they are about to rely on it right now (about to leave, order, cook, pay, submit).", "passive", False, "ignore", context="assistant_gave_info", hard=True, others_choices=GROUP,
        guidance=_NO_NAME + " The doubt must be about the specific information in the context, and the speaker or someone present must be about to act on it; do not say it was already checked."),
    _tr("pass_info_checked", f"People talk to each other, not to {A}; {A} gave some information earlier that may have been wrong, but they have already checked it themselves, fixed it or finished using it, so nothing is left to correct.", "pass", False, "ignore", context="assistant_gave_info", hard=True, others_choices=GROUP,
        guidance=_NO_NAME + " Make it clear the matter is settled (they double-checked, already corrected, already done); no question or request."),
    _tr("pass_task_done", f"People talk to each other, not to {A}, about something {A} reported doing earlier (booked, sent, set up) that is already finished and over, for example how it turned out; nothing is pending.", "pass", False, "ignore", context="assistant_did", hard=True, others_choices=GROUP,
        guidance=_NO_NAME + " The task is complete and no longer in use; no question or request."),
)
CARD_SETS["r4_info"] = {"turn.reply": R4_INFO_CARDS}

OTHER_USER_CONTEXTS = frozenset({"assistant_answered_other"})  # 前文的提問者不是目前說話者
CONTEXT_DESC: dict[str, str] = {
    "assistant_solicits_chat": "the assistant's last line is a casual question or request for the user's opinion",
    "assistant_solicits_task": "the assistant's last line offers one specific tool action and asks whether to do it",
    "assistant_solicits_agent": "the assistant's last line offers a multi-step job and asks whether to go ahead",
    "assistant_states": "the assistant's last line is a plain statement with no question",
    "assistant_answered_other": f"a different person (speaker \"{laya_schema.OTHER_USER_LABEL}\") asked something and the assistant answered them; the current speaker is a third party",
    "assistant_just_replied": "the assistant has just answered the speaker (user line first, then the assistant's answer)",
    "assistant_promised_user": "the user asked the assistant to do something and the assistant promised to (User line first, then the Assistant's promise)",
    "assistant_promised": "the assistant earlier promised or offered to do something concrete (remind, book, sign up, call)",
    "assistant_noted": "the assistant earlier said it noted a specific fact about the group (a preference, an allergy, a deadline)",
    "assistant_gave_info": "the user asked something and the assistant answered with one concrete piece of information (a time, a measurement, a route, a price, a recipe amount); User line first, then the Assistant line",
    "assistant_did": "the assistant reported having already done something concrete for the user (booked, sent, set up); Assistant line, optionally preceded by the User request",
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


def plan_calls(cards: Sequence[Card], max_samples: int, per_call: int, rng: random.Random, styles_override: Sequence[str] | None = None) -> list[tuple[Card, str, str, int]]:
    """場景矩陣：各卡平均分配 max_samples（餘數給前面的卡），每卡切成每次 per_call 筆，
    卡片輪流呼叫；各卡內獨立輪替風格（以卡片序號錯開起點，整體分佈才均勻），話題隨機。"""
    styles = list(styles_override or STYLES)
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
    call_label_name: str | None = None,
    styles_override: Sequence[str] | None = None,
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
    for idx, (card, style, topic, n) in enumerate(plan_calls(cards, max_samples, per_call, rng, styles_override)):
        api_calls += 1
        others = others_for(card, idx, len(cards))
        if call_ids is not None:
            call_ids.append(call_id_of(call_label_name or kind, idx))
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
                    **({"label_rule": card.label_rule} if card.label_rule else {}),
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


def call_label(kind: str, card_set: str = "main") -> str:
    """call_id 前綴：主卡組用 kind；其他卡組加後綴，避免和主卡組的回應檔 call_id 衝突。"""
    return kind if card_set == "main" else f"{kind}.{card_set}"


def export_prompts(kind: str, *, max_samples: int, per_call: int, seed: int, card_set: str = "main") -> list[dict[str, Any]]:
    """依 plan_calls／build_prompt 產生每次呼叫的 prompt 與中繼資料（不呼叫任何後端）。"""
    cards = CARD_SETS[card_set][kind]
    plan = plan_calls(cards, max_samples, per_call, random.Random(seed), CARD_SET_STYLES.get(card_set))
    return [
        {
            "call_id": call_id_of(call_label(kind, card_set), idx), "kind": kind, "card": card.card_id, "style": style,
            "topic": topic, "n": n, "hard": card.hard,
            "system": SYSTEM_PROMPT, "user": build_prompt(card, style, topic, n, others_for(card, idx, len(cards))),
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
    p.add_argument("--card-set", default="main", choices=sorted(CARD_SETS), help="卡組；contrast＝task／message／other 邊界對照卡（匯入時 --id-prefix 另訂）")
    p.add_argument("--id-prefix", default=None, help="匯入時的 sample_id 前綴（對照卡組必須另訂，避免和主卡組撞號）")
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
        recs = export_prompts(args.kind, max_samples=args.max_samples, per_call=args.per_call, seed=args.seed, card_set=args.card_set)
        write_jsonl(Path(args.export_prompts), recs)
        print(json.dumps({"exported_calls": len(recs), "samples": args.max_samples}, ensure_ascii=False))
        return 0
    if args.import_responses:
        if not args.out:
            print("--import-responses 需要 --out", file=sys.stderr)
            return 2
        # 回應檔可含多個 kind（量產各 part 合併）；只取本 kind 的 call_id
        label = call_label(args.kind, args.card_set)
        card_set = CARD_SETS[args.card_set][args.kind]
        responses = {k: v for k, v in load_responses(Path(args.import_responses)).items() if k.startswith(f"{label}-")}
        ids: list[str] = []
        planned = [call_id_of(label, i) for i in range(len(plan_calls(card_set, args.max_samples, args.per_call, random.Random(args.seed), CARD_SET_STYLES.get(args.card_set))))]
        backend_r = ReplayBackend(responses, planned)
        rows, stats = generate(args.kind, backend_r, max_samples=args.max_samples, per_call=args.per_call, seed=args.seed, call_ids=ids,
                               cards=card_set, id_prefix=args.id_prefix, call_label_name=label, styles_override=CARD_SET_STYLES.get(args.card_set))
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
