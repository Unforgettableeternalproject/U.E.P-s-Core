"""
PromptBuilder — 提示詞組裝介面

定義 PromptBuilder Protocol 及最小預設實作 DefaultPromptBuilder。
未來整合 U.E.P Core 時，實作 UEPPromptBuilder 替換預設實作，
即可接入 Router → StateManager → LLM 的完整管道。

Usage:
    from echo_thought_core.prompt import DefaultPromptBuilder, PromptContext

    builder = DefaultPromptBuilder()
    ctx = PromptContext(persona_text="You are U.E.P.", memory_context="...")
    system_prompt = builder.build_system_prompt("chat", ctx)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

try:
    from typing import Protocol, runtime_checkable
except ImportError:
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]

if TYPE_CHECKING:
    from echo_thought_core.prompt.context import PromptContext

logger = logging.getLogger(__name__)


@runtime_checkable
class PromptBuilder(Protocol):
    """提示詞組裝協定。

    整合方式：
    1. 實作此 Protocol
    2. 注入 ContextManager(prompt_builder=your_builder)
    3. ContextManager 在需要 system prompt 時呼叫 build_system_prompt()

    未來 U.E.P Core 的 Router/StateManager 會透過此介面
    告訴 LLM 當前系統狀態、應該用什麼模式回應。
    """

    def build_system_prompt(self, mode: str, context: PromptContext) -> str:
        """根據模式和上下文組裝完整的 system prompt。

        Args:
            mode:    當前模式（"chat" / "work" / "direct" 等）
            context: 提示詞上下文（人設、狀態值、記憶、工作流等）

        Returns:
            組裝完成的 system prompt 字串
        """
        ...

    def build_status_injection(self, status: dict) -> str:
        """將系統狀態值轉換為可注入 prompt 的描述文字。

        對應 U.E.P Core 的 _build_status_context_with_guide()。
        將 mood/pride/helpfulness/boredom 等數值轉為英文描述。

        Args:
            status: 系統狀態 dict（key: 狀態名, value: 數值或字串）

        Returns:
            狀態描述文字（空字串表示不注入）
        """
        ...

    def get_available_modes(self) -> list[str]:
        """回傳此 builder 支援的模式列表。

        Returns:
            模式名稱列表（如 ["chat", "work", "direct"]）
        """
        ...


class DefaultPromptBuilder:
    """最小預設 PromptBuilder — 直接串接上下文欄位。

    不做模式分派、不做狀態值轉換。
    僅將 persona_text + memory_context + custom_instructions 串接。
    適用於簡單的單模式場景（CLI 測試、DC bot 純 chat）。
    """

    def build_system_prompt(self, mode: str, context: PromptContext) -> str:
        """串接上下文欄位為 system prompt。"""
        parts: list[str] = []

        if context.persona_text:
            parts.append(context.persona_text)

        if context.memory_context:
            parts.append(context.memory_context)

        if context.custom_instructions:
            parts.append(context.custom_instructions)

        return "\n\n".join(parts)

    def build_status_injection(self, status: dict) -> str:
        """預設不注入狀態。"""
        return ""

    def get_available_modes(self) -> list[str]:
        """僅支援 default 模式。"""
        return ["default"]
