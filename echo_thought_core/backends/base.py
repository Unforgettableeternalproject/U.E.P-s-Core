"""
Abstract Backend — 多後端的統一介面

所有 LLM 後端必須實作此介面。
提供 token 計數、查詢、串流查詢、摘要、完整壓縮五大功能。
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
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

    async def stream_query(
        self,
        messages: list[Message],
        system_instruction: str | None = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        """串流查詢 LLM API — 逐段吐出文字片段。

        為什麼是「片段」而不是「完整回應」：
            串流語音管線（Echo Stream）要在句子邊界就把文字送去 TTS 合成，
            讓 LLM 生成下一句的時間被 TTS 合成上一句的時間吃掉。等整段
            生成完才回傳，這個重疊就沒了，各層耗時直接相加。

        **這不是 @abstractmethod。** 預設實作退回 :meth:`query`，
        把完整回應一次吐出——這樣既有後端（gemini / ollama）不必改就能用，
        只是拿不到串流的延遲好處。真正在意首字延遲的後端應該覆寫它。

        Args:
            messages: 對話訊息列表（含歷史）
            system_instruction: 系統提示詞
            **kwargs: 後端特定參數

        Yields:
            文字片段。串接起來等於完整回應。
        """
        prompt = ""
        for msg in reversed(messages):
            if msg.role == "user":
                prompt = msg.content
                break

        result = self.query(
            prompt,
            mode=kwargs.pop("mode", "chat"),
            system_instruction=system_instruction,
            **kwargs,
        )
        text = result.get("text", "") if isinstance(result, dict) else str(result)
        if text:
            yield text

    @property
    def supports_streaming(self) -> bool:
        """是否為真串流。

        預設 False——:meth:`stream_query` 的退回實作雖然能跑，
        但拿不到延遲好處。呼叫端可據此警告使用者，
        而不是讓人以為串流生效了卻量到爛數字。
        """
        return False

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
