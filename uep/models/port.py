"""模型層埠 ``ModelPort``（Agent 架構提案 §7.2）：Core 自有介面，Agent 層只依賴這裡，看不到任何 SDK 型別。

事件形狀沿用提案：``thought_delta | text_delta | tool_call_delta | tool_call_done | tool_intent | usage | finish``。
``tool_intent`` 由 ``ToolCallAssembler`` 在呼叫完整後產生；增量期間只緩衝，不建 action（TC-2、SA-3）。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from uep.adapters.runtime import RuntimeEvent

Dialect = Literal["openai_chat", "openai_responses", "ollama"]
ModelEventKind = Literal[
    "thought_delta", "text_delta", "tool_call_delta", "tool_call_done", "tool_intent", "usage", "finish",
]
FinishReason = Literal["stop", "tool_calls", "length", "error", "cancelled"]


@dataclass(frozen=True)
class ProviderCapabilities:
    """provider 能力表；不支援的能力由 adapter 明示降級並記錄。"""

    text_stream: bool = True
    tool_calling: bool = True
    parallel_tool_calls: bool = True
    tools_with_reasoning: bool = True  # gpt-5.6-luna 的 chat 路徑為 False（帶 tools 時 effort 降為 none）
    emits_thinking: bool = False  # 串流是否會產生 thinking（thought_delta）
    cancel_semantics: str = "task_cancel"


@dataclass(frozen=True)
class ChatMessage:
    """Core 持有的 history 項目（SDK 不保存跨 run 的 history，§7.2-7）。

    ``role``：system／user／assistant／tool。assistant 的 ``tool_calls`` 為 ``({id, name, args}, ...)``；
    tool 訊息帶 ``tool_call_id``。
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    tool_call_id: str | None = None


def messages_to_json(messages: Sequence[ChatMessage]) -> str:
    """Core history -> 正規化 JSON（鍵排序、不跳脫中文、拒絕 NaN／Infinity）。park 時持久化與 ``history_hash`` 共用同一份表示。

    注意：欄位或格式一變，既存 checkpoint 的 ``history_hash`` 就會失效（resume 會 fail-closed 為 context_lost）；
    變更前須先處理已 park 的 Run。"""
    return json.dumps(
        [
            {
                "role": m.role, "content": m.content, "tool_call_id": m.tool_call_id,
                "tool_calls": [dict(c) for c in m.tool_calls],
            }
            for m in messages
        ],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )


def messages_from_json(text: str) -> list[ChatMessage]:
    """``messages_to_json`` 的反向；格式變更會讓既存 checkpoint 的 hash 失效（同 ``messages_to_json``）。"""
    return [
        ChatMessage(
            d["role"], d["content"], tool_calls=tuple(d["tool_calls"]), tool_call_id=d["tool_call_id"],
        )
        for d in json.loads(text)
    ]


def history_hash(messages: Sequence[ChatMessage]) -> str:
    """checkpoint 的 ``history_hash``（§4.10、S2-1）：對 Core 持有的 ``ChatMessage`` 序列做 SHA-256，
    不碰 SDK 型別（SDK 的 ModelResponse 帶 timestamp，不可作為雜湊對象）。"""
    return hashlib.sha256(messages_to_json(messages).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ModelRequest:
    attempt_id: str
    messages: Sequence[ChatMessage]
    capability_set: Mapping[str, Any]
    max_output_tokens: int = 256
    reasoning_effort: str | None = None
    builtin_tools: Sequence[Any] = ()  # 任何內建（hosted）工具一律拒絕啟動（D60）


@dataclass(frozen=True)
class ModelEvent:
    kind: ModelEventKind
    text: str = ""  # thought_delta／text_delta
    index: int = 0  # 同一模型訊息內的呼叫順序
    call_id: str = ""
    tool: str = ""
    args_fragment: str = ""  # tool_call_delta
    args: Mapping[str, Any] | None = None  # tool_intent
    args_complete: bool = False
    usage: Mapping[str, int] | None = None
    finish_reason: FinishReason | None = None
    error_code: str | None = None  # tool_intent 的 invalid_tool_args 等


class CapabilityMissingError(Exception):
    """D60 啟動斷言未過：拒絕啟動 attempt，不得送出請求。``event`` 是 ``CAPABILITY_MISSING`` 的 RuntimeEvent。"""

    def __init__(self, event: RuntimeEvent) -> None:
        super().__init__(str((event.error or {}).get("detail", "CAPABILITY_MISSING")))
        self.event = event


class ModelPort(Protocol):
    dialect: Dialect
    caps: ProviderCapabilities

    def stream(self, req: ModelRequest) -> AsyncIterator[ModelEvent]:
        """依序產生 ModelEvent；D60 斷言未過拋 ``CapabilityMissingError``（尚未送出任何請求）。"""
        ...

    def cancel(self, attempt_id: str) -> None:
        """冪等。呼叫返回後不再產生新事件。"""
        ...


@dataclass
class ToolCallAssembler:
    """provider 無關：累積 ``tool_call_delta`` -> ``finish`` 前解析 JSON -> 產生 ``tool_intent``。

    JSON 不合法（含截斷）⇒ ``args_complete=False``、``error_code='invalid_tool_args'``，絕不猜測補值。"""

    _slots: dict[int, dict[str, Any]] = field(default_factory=dict)

    def feed(self, event: ModelEvent) -> None:
        if event.kind != "tool_call_delta":
            return
        slot = self._slots.setdefault(event.index, {"call_id": "", "tool": "", "args": ""})
        slot["call_id"] = event.call_id or slot["call_id"]
        slot["tool"] = event.tool or slot["tool"]
        slot["args"] += event.args_fragment

    def intents(self, attempt_id: str) -> list[ModelEvent]:
        out: list[ModelEvent] = []
        for index in sorted(self._slots):
            slot = self._slots[index]
            if not slot["tool"]:
                continue
            call_id = slot["call_id"] or f"{attempt_id}:{index}"  # 缺 id（如 Ollama）以 (attempt, index) 確定性合成
            raw = slot["args"] or "{}"
            try:
                args = json.loads(raw)
                ok = isinstance(args, dict)
            except ValueError:
                args, ok = None, False
            out.append(ModelEvent(
                kind="tool_intent", index=index, call_id=call_id, tool=slot["tool"],
                args=args if ok else None, args_complete=ok, error_code=None if ok else "invalid_tool_args",
            ))
        self._slots.clear()
        return out
