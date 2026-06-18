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


from echo_thought_core.schemas.config import PersonaContext
from echo_thought_core.schemas.session import SacredContent


class SacredManager:
    """神聖段落管理器"""

    def __init__(self, persona: PersonaContext | None = None):
        self._sacred = SacredContent()
        if persona:
            self._sacred.persona_context = persona.to_sacred_text()

    def get_sacred_content(self) -> SacredContent:
        """取得當前神聖段落"""
        return self._sacred

    def get_sacred_text(self) -> str:
        """取得神聖段落的完整文本"""
        return self._sacred.total_content
