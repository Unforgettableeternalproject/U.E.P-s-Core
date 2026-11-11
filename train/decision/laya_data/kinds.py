"""依決策 kind 擴充的題目與 state 定義（設計見 docs/架構重構/決策模型訓練設計草案.md）。

每個 kind 一份 ``KindSpec``：題目（questions）、state 組裝、expected 驗證。
題目 ID 規則：``turn.reply`` 沿用既有 adapter 的舊 ID（reply_mode／route／addressed）；
其他 kind 一律加前綴，避免 laya-train 對同名題目的定義衝突。
題目與 state 值一律英文（Laya 只用英文訓練）；推論端日後須與此處逐字一致。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"
DEFAULT_ASSISTANT_NAME = "U.E.P"

REPLY_MODES = ("respond", "passive", "pass", "defer")
ROUTES = ("direct_chat", "agent_loop", "task_request", "ignore")
ABSENT_INTENTS = ("presence_request", "message", "other")
TARGET_LOCATIONS = ("desktop", "discord", "website", "host", "none")

QUESTIONS_TURN_REPLY: dict[str, dict[str, Any]] = {
    "reply_mode": {
        "type": "choice",
        "instructions": "Given this conversation, which response mode should the assistant take right now?",
        "criteria": {
            "respond": "The assistant was addressed or asked something and should answer now",
            "passive": "The assistant was not addressed; it may listen in the background and only chip in if necessary",
            "pass": "Unrelated conversation, or nothing worth saying; stay silent",
            "defer": "Not enough information or risky; confirm with the user first or handle it later",
        },
    },
    "route": {
        "type": "choice",
        "instructions": "Which handling path should this message take?",
        "criteria": {
            "direct_chat": "Ordinary chat or a simple question; a conversational reply is enough",
            "agent_loop": "A complex task needing multi-step reasoning, planning or several tool calls",
            "task_request": "A single explicit command or tool request (toggle a device, check the calendar, send an email)",
            "ignore": "Not said to the assistant; ignore it",
        },
    },
    "addressed": {
        "type": "noul",
        "instructions": (
            "Is this message said to the assistant (speaking to it directly, or continuing a conversation with it)?"
        ),
    },
}

QUESTIONS_ABSENT_INTENT: dict[str, dict[str, Any]] = {
    "absent.intent": {
        "type": "choice",
        "instructions": "What does this message, sent while the assistant is not present, ask for?",
        "criteria": {
            "presence_request": "A request for the assistant to come, appear, or move to a place or platform",
            "message": "Content to pass on to the assistant or someone else; not a request to move",
            "other": "Anything else: chit-chat, questions, negations, past events, or unclear",
        },
    },
    "absent.target_location": {
        "type": "choice",
        "instructions": "Which place does the message ask the assistant to go to?",
        "criteria": {
            "desktop": "The desktop app",
            "discord": "Discord",
            "website": "The website",
            "host": "The host machine",
            "none": "No specific place named",
        },
    },
}


def _check_in(name: str, value: Any, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ValueError(f"{name} 不合法：{value!r}（允許 {allowed}）")


def validate_turn_reply_expected(expected: Mapping[str, Any]) -> None:
    for key, value in expected.items():
        if key == "reply_mode":
            _check_in(key, value, REPLY_MODES)
        elif key == "route":
            _check_in(key, value, ROUTES)
        elif key == "addressed":
            if not isinstance(value, bool):
                raise ValueError("addressed 必須是 bool")
        else:
            raise ValueError(f"turn.reply 不認得的題目：{key!r}")


def validate_absent_expected(expected: Mapping[str, Any]) -> None:
    for key, value in expected.items():
        if key == "absent.intent":
            _check_in(key, value, ABSENT_INTENTS)
        elif key == "absent.target_location":
            _check_in(key, value, TARGET_LOCATIONS)
        else:
            raise ValueError(f"presence.absent_intent 不認得的題目：{key!r}")
    target = expected.get("absent.target_location")
    if target is not None and target != "none" and expected.get("absent.intent") != "presence_request":
        raise ValueError("只有 intent=presence_request 才能標 target_location（非 none）")


def build_state_turn_reply(facts: Mapping[str, Any]) -> dict[str, str]:
    """turn.reply 的 state（鍵順序固定，屬於分佈的一部分）。

    facts：``message``（必填，已是英文）、``assistant_name``、``speaker``、``others``（在場他人數）、
    ``context``（list[{speaker,text}]，舊到新）。
    """
    message = str(facts.get("message", "")).strip()
    if not message:
        raise ValueError("message 不可為空")
    others = int(facts.get("others", 0) or 0)
    context = facts.get("context") or []
    lines = [f"{c['speaker']}: {str(c['text']).strip()}" for c in context]
    return {
        "assistant_name": str(facts.get("assistant_name") or DEFAULT_ASSISTANT_NAME),
        "speaker": str(facts.get("speaker") or "User"),
        "other_people_present": f"{others} other(s) present" if others > 0 else "none",
        "recent_context": "\n".join(lines) if lines else "(none)",
        "message": message,
    }


def build_state_absent(facts: Mapping[str, Any]) -> dict[str, str]:
    """presence.absent_intent 的 state。刻意不放 actor／owner 欄位（意圖不得夾帶授權特徵）。"""
    message = str(facts.get("message", "")).strip()
    if not message:
        raise ValueError("message 不可為空")
    return {
        "assistant_name": str(facts.get("assistant_name") or DEFAULT_ASSISTANT_NAME),
        "source_platform": str(facts.get("source_platform") or "unknown"),
        "message": message,
    }


@dataclass(frozen=True)
class KindSpec:
    kind: str
    questions: Mapping[str, Mapping[str, Any]]
    build_state: Callable[[Mapping[str, Any]], dict[str, str]]
    validate_expected: Callable[[Mapping[str, Any]], None]
    # 不交模型、永遠由規則處理的部分（說明用，供 README／文件對照）
    hard_rules: tuple[str, ...] = field(default=())


KIND_SPECS: dict[str, KindSpec] = {
    "turn.reply": KindSpec(
        kind="turn.reply",
        questions=QUESTIONS_TURN_REPLY,
        build_state=build_state_turn_reply,
        validate_expected=validate_turn_reply_expected,
        hard_rules=("global_stop", "presence_not_active", "audience_denied", "risky_approval", "interaction_to"),
    ),
    "presence.absent_intent": KindSpec(
        kind="presence.absent_intent",
        questions=QUESTIONS_ABSENT_INTENT,
        build_state=build_state_absent,
        validate_expected=validate_absent_expected,
        hard_rules=("authorization", "actor_authentication", "transfer_priority"),
    ),
}


def get_spec(kind: str) -> KindSpec:
    try:
        return KIND_SPECS[kind]
    except KeyError:
        raise ValueError(f"尚未實作的 kind：{kind!r}（已實作：{sorted(KIND_SPECS)}）") from None


def make_row(
    kind: str,
    facts: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    sample_id: str,
    source: str,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """組一列 laya-train JSONL。只帶 expected 有的題目的 questions（沒標的題不列入）。"""
    spec = get_spec(kind)
    spec.validate_expected(expected)
    if not expected:
        raise ValueError("expected 不可為空")
    return {
        "schema_version": SCHEMA_VERSION,
        "sample_id": sample_id,
        "kind": kind,
        "source": source,
        "lang": "en",
        "state": spec.build_state(facts),
        "questions": {qid: dict(spec.questions[qid]) for qid in expected},
        "expected": dict(expected),
        "meta": dict(meta or {}),
    }
