"""Laya 決策模型的題目與 state 定義（純資料，無 torch／laya 依賴）。

推論端（``uep.echo.laya_wiring``）與訓練工具（``train/decision``）共用同一份定義：題目說明、選項說明與
state 欄位的字面值全為英文（決策中樞輸入走 Boundary Translator，見決策中樞架構提案 §5.4），
訓練與推論不得各寫一份，以免字面值漂移。

state 欄位只有助理名稱、泛稱的說話者、在場人數、近期脈絡與本句文字；不含 ``speaker_ref``／事件 ID
或任何 Risky／秘密欄位。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SPEAKER_LABEL = "user"  # 泛稱的說話者，不送 speaker_ref
NO_OTHER_PEOPLE = "none"
NO_CONTEXT = "(none)"
CONTEXT_MAX_CHARS = 160  # recent_context 各段（使用者上一句、她上一句回覆）的長度上限
USER_LABEL = "User"  # 上一輪使用者與本句說話者相同
OTHER_USER_LABEL = "Another user"  # 上一輪使用者與本句說話者不同（群組）
ASSISTANT_LABEL = "Assistant"
WARMUP_MESSAGE = "Hello, are you there?"  # warmup 假推論用的固定短句

# 能力類別（粗粒度，給模型判斷「她做不做得到」）。來源：工具層 CAPABILITY_CEILING 的標籤（memory／lore／chatroom／desktop.file_*／
# web／image 等）歸入 query／messaging／files，其餘類別（booking／calling／media_control／timer_alarm／host_control／device_control）
# 對應訓練資料中出現的能力與艾斯維爾裁決的範圍。新增功能只要在工具層登記並映射到類別，模型即知道她做得到，不必重訓標註規則。
CAPABILITY_CATEGORIES: tuple[str, ...] = (
    "query", "booking", "messaging", "calling", "files", "media_control", "timer_alarm", "host_control", "device_control",
)
# 現行預設清單：系統內與通訊能力；不含 device_control（燈、門鎖、恆溫器、窗戶等外部實體裝置）。工具層清單接上之前由此提供。
DEFAULT_CAPABILITIES: tuple[str, ...] = tuple(c for c in CAPABILITY_CATEGORIES if c != "device_control")
NO_CAPABILITIES = "none"


def format_capabilities(capabilities: "Iterable[str] | None" = None) -> str:
    """能力清單 -> state 字面值（依類別固定順序，逗號分隔；空集合為 ``none``）。None 取現行預設清單。"""
    caps = set(DEFAULT_CAPABILITIES if capabilities is None else capabilities)
    unknown = caps - set(CAPABILITY_CATEGORIES)
    if unknown:
        raise ValueError(f"不認得的能力類別：{sorted(unknown)}")
    ordered = [c for c in CAPABILITY_CATEGORIES if c in caps]
    return ", ".join(ordered) if ordered else NO_CAPABILITIES

# 問題集（laya 的 choice／noul 題型）；選項說明是給零樣本模型看的
QUESTIONS: dict[str, dict[str, Any]] = {
    "reply_mode": {
        "type": "choice",
        "instructions": "Based on this conversation, which reply mode should the assistant take right now? A request that needs a capability missing from available_capabilities is something it cannot do.",
        "criteria": {
            "respond": "The assistant was called or asked something and should answer right away (if the request needs a capability it lacks, it still answers and says it cannot)",
            "passive": "The assistant was not called, but has a concrete reason to speak up: the talk touches something it promised, noted or said that is still in progress or about to be used, or someone asks the room a question it can answer, or asks the room for something it can do; otherwise pass",
            "pass": "The conversation has nothing to do with the assistant or gives it no good reason to speak (talk between other people, praise or recollection of what it did, relayed news, muttering, an unaddressed request for something it cannot do); stay silent and do not reply",
            "defer": "A key detail is missing, the request is risky, or the user asks to come back to it later; confirm with the user first or postpone",
        },
    },
    "route": {
        "type": "choice",
        "instructions": "Which handling route should this message be sent to?",
        "criteria": {
            "direct_chat": "Casual chat, a simple question, a read-only lookup that changes nothing (weather, news, checking the calendar, recalling a memory), or telling the user it cannot do something; a conversational reply is enough",
            "agent_loop": "A complex task that needs multi-step reasoning, planning or several tool calls",
            "task_request": "A single, clear command for one action that changes state (change a setting, set a reminder or timer, send an email, etc.)",
            "ignore": "Not addressed to the assistant; ignore it",
        },
    },
    "addressed": {
        "type": "noul",
        "instructions": "Is this message addressed to the assistant (speaking to it directly or continuing a conversation with it)?",
    },
    "compound": {
        "type": "noul",
        "instructions": "Does this message contain two or more independent intents that go to different handling routes or targets (for example a chat remark plus a command, or a reminder plus a message to someone else)? Answer no for the steps of one multi-step goal, for one intent with several items or recipients, and for steps that depend on each other's results.",
    },
}


def others_present(participant_count: int | None) -> str:
    """在場人數 -> 字面值；人數未知視為單人（與 D80 一致）。"""
    if participant_count is not None and participant_count > 1:
        others = participant_count - 1
        return f"{others} other {'person' if others == 1 else 'people'} present"
    return NO_OTHER_PEOPLE


def format_recent_context(user_en: str | None, reply_en: str | None, *, same_speaker: bool = True) -> str:
    """把上一輪的使用者句與她的回覆（皆已是英文）組成 recent_context；都沒有回 ``(none)``。

    ``same_speaker``：上一輪使用者與本句說話者相同標 ``User``，不同標 ``Another user``；
    無法判斷時視為相同（預設）。訓練與推論共用。
    """
    parts = []
    if user_en and user_en.strip():
        label = USER_LABEL if same_speaker else OTHER_USER_LABEL
        parts.append(f"{label}: {user_en.strip()[:CONTEXT_MAX_CHARS]}")
    if reply_en and reply_en.strip():
        parts.append(f"{ASSISTANT_LABEL}: {reply_en.strip()[:CONTEXT_MAX_CHARS]}")
    return "\n".join(parts) if parts else NO_CONTEXT


def build_state_dict(
    *, assistant_name: str, message: str, participant_count: int | None = None, recent_context: str = NO_CONTEXT,
    capabilities: Iterable[str] | None = None,
) -> dict[str, str]:
    """組 laya 的 state dict（訓練與推論共用）。"""
    return {
        "assistant_name": assistant_name,
        "speaker": SPEAKER_LABEL,
        "other_people_present": others_present(participant_count),
        "recent_context": recent_context or NO_CONTEXT,
        "available_capabilities": format_capabilities(capabilities),
        "message": message,
    }
