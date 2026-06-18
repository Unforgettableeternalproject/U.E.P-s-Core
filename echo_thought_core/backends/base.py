"""
Abstract Backend — 多後端的統一介面

所有 LLM 後端必須實作此介面。
提供 token 計數、查詢、摘要、完整壓縮四大功能。
"""

from abc import ABC, abstractmethod
from typing import Any

from echo_thought_core.schemas.message import Message
from echo_thought_core.schemas.session import SacredContent


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
    def query(
        self,
        prompt: str,
        mode: str = "chat",
        system_instruction: str | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """查詢 LLM API

        Args:
            prompt: 用戶輸入文字
            mode: 回應模式（chat / work / direct 等）
            system_instruction: 自定義系統提示詞
            **kwargs: 後端特定參數（cached_content, tools, tool_choice 等）

        Returns:
            dict，至少包含 "text" 鍵
        """
        ...

    @abstractmethod
    async def summarize(
        self,
        messages: list[Message],
        instructions: str | None = None,
    ) -> str:
        """將訊息列表摘要為文本（Layer 2 使用）"""
        ...

    @abstractmethod
    async def compact(
        self,
        messages: list[Message],
        sacred: SacredContent,
        instructions: str | None = None,
    ) -> str:
        """將完整對話壓縮為結構化摘要（Layer 3 使用）"""
        ...

    def format_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """將統一 Message 格式轉為後端 API 格式

        子類可覆寫以適配特定 API 格式。
        預設輸出通用的 dict 格式。
        """
        return [
            {"role": msg.role, "content": msg.content}
            for msg in messages
        ]
