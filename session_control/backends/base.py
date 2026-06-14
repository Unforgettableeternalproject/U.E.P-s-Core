"""
Abstract Backend — 多後端的統一介面

所有 LLM 後端必須實作此介面。
提供 token 計數、摘要、完整壓縮三大功能。
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from session_control.schemas.message import Message
from session_control.schemas.session import SacredContent


class AbstractBackend(ABC):
    """LLM 後端抽象基類"""

    @abstractmethod
    def get_context_window_size(self) -> int:
        """取得此後端的 context window 大小"""
        ...

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """精確計算 token 數量"""
        ...

    @abstractmethod
    async def summarize(
        self,
        messages: List[Message],
        instructions: Optional[str] = None,
    ) -> str:
        """將訊息列表摘要為文本（Layer 2 使用）"""
        ...

    @abstractmethod
    async def compact(
        self,
        messages: List[Message],
        sacred: SacredContent,
        instructions: Optional[str] = None,
    ) -> str:
        """將完整對話壓縮為結構化摘要（Layer 3 使用）"""
        ...

    def format_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """將統一 Message 格式轉為後端 API 格式

        子類可覆寫以適配特定 API 格式。
        預設輸出 OpenAI/Anthropic 相容的 dict 格式。
        """
        return [
            {"role": msg.role, "content": msg.content}
            for msg in messages
        ]
