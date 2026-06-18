"""
Prompt — 提示詞管理介面

預留給未來 U.E.P Core Router → LLM 管道整合。
目前提供 Protocol 定義和最小預設實作。

Usage:
    from echo_thought_core.prompt import PromptBuilder, PromptContext, DefaultPromptBuilder
"""

from echo_thought_core.prompt.builder import DefaultPromptBuilder, PromptBuilder
from echo_thought_core.prompt.constants import (
    ALL_MODES,
    MODE_CHAT,
    MODE_DIRECT,
    MODE_WORK,
    PLACEHOLDER_INSTRUCTIONS,
    PLACEHOLDER_MEMORY,
    PLACEHOLDER_PERSONA,
    PLACEHOLDER_SACRED,
    PLACEHOLDER_SYSTEM_VALUES,
    PLACEHOLDER_WORKFLOW,
    SECTION_MARKER_INSTRUCTIONS,
    SECTION_MARKER_MEMORY,
    SECTION_MARKER_PERSONA,
    SECTION_MARKER_STATUS,
    SECTION_MARKER_WORKFLOW,
)
from echo_thought_core.prompt.context import PromptContext

__all__ = [
    # Protocol + 實作
    "PromptBuilder",
    "DefaultPromptBuilder",
    "PromptContext",
    # 模式常數
    "MODE_CHAT",
    "MODE_WORK",
    "MODE_DIRECT",
    "ALL_MODES",
    # 佔位符
    "PLACEHOLDER_PERSONA",
    "PLACEHOLDER_SYSTEM_VALUES",
    "PLACEHOLDER_MEMORY",
    "PLACEHOLDER_WORKFLOW",
    "PLACEHOLDER_SACRED",
    "PLACEHOLDER_INSTRUCTIONS",
    # 段落標記
    "SECTION_MARKER_PERSONA",
    "SECTION_MARKER_STATUS",
    "SECTION_MARKER_MEMORY",
    "SECTION_MARKER_WORKFLOW",
    "SECTION_MARKER_INSTRUCTIONS",
]
