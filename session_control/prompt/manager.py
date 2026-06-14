"""
Prompt Manager — 提示詞管理

從 U.E.P Core 的 prompt_manager.py 遷出並精簡。
只保留與 Session Control 相關的功能：
- 系統提示詞模板管理
- 人設佔位符替換
- 對話歷史格式化
"""

from typing import Dict, Any, Optional, List
from session_control.schemas.message import Message
from session_control.schemas.config import PersonaContext


class PromptManager:
    """提示詞管理器"""

    def __init__(
        self,
        persona: Optional[PersonaContext] = None,
        system_instructions: Optional[Dict[str, str]] = None,
    ):
        self.persona = persona
        self.system_instructions = system_instructions or {}

    def build_system_prompt(self, mode: str = "chat") -> str:
        """構建系統提示詞

        Args:
            mode: "chat" | "work" | "direct"
        """
        parts = []

        # 基礎人格
        base = self.system_instructions.get("base_personality")
        if base:
            parts.append(self._replace_placeholders(base))

        # 模式特定指令
        mode_instruction = self.system_instructions.get(f"{mode}_mode")
        if mode_instruction:
            parts.append(self._replace_placeholders(mode_instruction))

        # 人設上下文
        if self.persona:
            parts.append(self.persona.to_sacred_text())

        return "\n\n".join(parts)

    def format_history_for_prompt(
        self,
        messages: List[Message],
        max_recent: int = 5,
    ) -> str:
        """格式化對話歷史為文本"""
        recent = messages[-max_recent:] if len(messages) > max_recent else messages

        parts = []
        for msg in recent:
            label = {
                "user": "User",
                "assistant": "Assistant",
                "system": "System",
            }.get(msg.role, msg.role)
            parts.append(f"{label}: {msg.content}")

        return "\n".join(parts) if parts else ""

    def _replace_placeholders(self, text: str) -> str:
        """替換提示詞中的佔位符"""
        if self.persona and "{persona}" in text:
            text = text.replace("{persona}", self.persona.to_sacred_text())

        if "{persona_name}" in text and self.persona:
            text = text.replace("{persona_name}", self.persona.name)

        return text

    def update_instruction(self, key: str, value: str) -> None:
        """更新系統指令"""
        self.system_instructions[key] = value
