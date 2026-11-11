"""Laya 決策模型的題目與 state 定義（純資料，無 torch／laya 依賴）。

推論端（``uep.echo.laya_wiring``）與訓練工具（``train/decision``）共用同一份定義：題目說明、選項說明與
state 欄位的字面值全為英文（決策中樞輸入走 Boundary Translator，見決策中樞架構提案 §5.4），
訓練與推論不得各寫一份，以免字面值漂移。

state 欄位只有助理名稱、泛稱的說話者、在場人數、近期脈絡與本句文字；不含 ``speaker_ref``／事件 ID
或任何 Risky／秘密欄位。
"""
from __future__ import annotations

from typing import Any

SPEAKER_LABEL = "user"  # 泛稱的說話者，不送 speaker_ref
NO_OTHER_PEOPLE = "none"
NO_CONTEXT = "(none)"
CONTEXT_MAX_CHARS = 160  # recent_context 各段（使用者上一句、她上一句回覆）的長度上限
WARMUP_MESSAGE = "Hello, are you there?"  # warmup 假推論用的固定短句

# 問題集（laya 的 choice／noul 題型）；選項說明是給零樣本模型看的
QUESTIONS: dict[str, dict[str, Any]] = {
    "reply_mode": {
        "type": "choice",
        "instructions": "Based on this conversation, which reply mode should the assistant take right now?",
        "criteria": {
            "respond": "The assistant was called or asked something and should answer right away",
            "passive": "The assistant was not called, but may listen in the background and chime in briefly only if necessary",
            "pass": "The conversation has nothing to do with the assistant; stay silent and do not reply",
            "defer": "Information is insufficient or the request is risky; confirm with the user first or postpone",
        },
    },
    "route": {
        "type": "choice",
        "instructions": "Which handling route should this message be sent to?",
        "criteria": {
            "direct_chat": "Casual chat or a simple question; a conversational reply is enough",
            "agent_loop": "A complex task that needs multi-step reasoning, planning or several tool calls",
            "task_request": "A single, clear command or tool request (switch a device, check the calendar, send an email, etc.)",
            "ignore": "Not addressed to the assistant; ignore it",
        },
    },
    "addressed": {
        "type": "noul",
        "instructions": "Is this message addressed to the assistant (speaking to it directly or continuing a conversation with it)?",
    },
}


def others_present(participant_count: int | None) -> str:
    """在場人數 -> 字面值；人數未知視為單人（與 D80 一致）。"""
    if participant_count is not None and participant_count > 1:
        others = participant_count - 1
        return f"{others} other {'person' if others == 1 else 'people'} present"
    return NO_OTHER_PEOPLE


def format_recent_context(user_en: str | None, reply_en: str | None) -> str:
    """把上一輪的使用者句與她的回覆（皆已是英文）組成 recent_context；都沒有回 ``(none)``。"""
    parts = []
    if user_en and user_en.strip():
        parts.append(f"User: {user_en.strip()[:CONTEXT_MAX_CHARS]}")
    if reply_en and reply_en.strip():
        parts.append(f"Assistant: {reply_en.strip()[:CONTEXT_MAX_CHARS]}")
    return "\n".join(parts) if parts else NO_CONTEXT


def build_state_dict(
    *, assistant_name: str, message: str, participant_count: int | None = None, recent_context: str = NO_CONTEXT,
) -> dict[str, str]:
    """組 laya 的 state dict（訓練與推論共用）。"""
    return {
        "assistant_name": assistant_name,
        "speaker": SPEAKER_LABEL,
        "other_people_present": others_present(participant_count),
        "recent_context": recent_context or NO_CONTEXT,
        "message": message,
    }
