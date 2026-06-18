"""
PromptContext — 提示詞組裝的上下文資料結構

集中定義 PromptBuilder 所需的所有上下文欄位。
所有欄位皆有預設值，允許呼叫方部分填入。

未來整合 U.E.P Core 時，由 Router/StateManager 填充
status_values 和 workflow_context。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PromptContext:
    """提示詞組裝上下文。

    Attributes:
        persona_text:       人設文本（角色名稱、人格特徵、說話風格）
        status_values:      系統狀態值（mood/pride/helpfulness/boredom 等）
                            由外部 StatusManager 填充，PromptBuilder 轉為描述注入
        memory_context:     記憶上下文（由 MemoryBridge.prefetch 回傳）
        workflow_context:   工作流上下文（WORK 模式下由 SysModule 填充）
                            None 表示非工作流模式
        sacred_content:     神聖段落文本（壓縮時保護用）
        custom_instructions: 使用者自定義指令或額外注入
        mode:               當前模式（"chat" / "work" / "direct" 等）
    """

    persona_text: str = ""
    status_values: dict[str, object] = field(default_factory=dict)
    memory_context: str = ""
    workflow_context: str | None = None
    sacred_content: str = ""
    custom_instructions: str = ""
    mode: str = "chat"
