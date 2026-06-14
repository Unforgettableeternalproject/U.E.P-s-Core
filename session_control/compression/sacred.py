"""
Sacred Content Manager — 神聖段落保護

管理在任何壓縮層級中都不會被移除的內容：
- 人設上下文（角色名稱、人格特徵、說話風格）
- 使用者明確指令原文
- 當前任務目標
- 自定義不可壓縮段落

參考 Anthropic Claude Code 的設計：
第 6 段「All User Messages Verbatim」是神聖段落。
我們增加第 10 段「Character & Persona」作為 U.E.P 特有的神聖段落。
"""

from typing import List, Optional
from session_control.schemas.session import SacredContent
from session_control.schemas.config import PersonaContext
from session_control.schemas.message import Message


class SacredManager:
    """神聖段落管理器"""

    def __init__(self, persona: Optional[PersonaContext] = None):
        self._sacred = SacredContent()
        if persona:
            self._sacred.persona_context = persona.to_sacred_text()

    def set_persona(self, persona: PersonaContext) -> None:
        """設定人設上下文"""
        self._sacred.persona_context = persona.to_sacred_text()

    def add_user_instruction(self, instruction: str) -> None:
        """新增使用者明確指令（如「不要碰這個檔案」）"""
        if instruction not in self._sacred.user_instructions:
            self._sacred.user_instructions.append(instruction)

    def set_current_task(self, task: str) -> None:
        """設定當前任務目標"""
        self._sacred.current_task = task

    def add_custom(self, key: str, content: str) -> None:
        """新增自定義神聖段落"""
        self._sacred.custom_sacred[key] = content

    def extract_user_instructions(self, messages: List[Message]) -> List[str]:
        """從對話歷史中提取使用者明確指令

        識別模式：
        - 「記住...」「不要...」「永遠...」「請注意...」
        - 包含強調語氣的指令性句子
        """
        instructions = []
        instruction_markers = [
            "記住", "不要", "永遠", "請注意", "一定要",
            "remember", "don't", "never", "always", "make sure",
        ]

        for msg in messages:
            if msg.role != "user":
                continue
            content_lower = msg.content.lower()
            for marker in instruction_markers:
                if marker.lower() in content_lower:
                    instructions.append(msg.content)
                    break

        return instructions

    def get_sacred_content(self) -> SacredContent:
        """取得當前神聖段落"""
        return self._sacred

    def get_sacred_text(self) -> str:
        """取得神聖段落的完整文本"""
        return self._sacred.total_content

    def estimate_tokens(self, counter) -> int:
        """估算神聖段落的 token 數"""
        return counter.count(self.get_sacred_text())
