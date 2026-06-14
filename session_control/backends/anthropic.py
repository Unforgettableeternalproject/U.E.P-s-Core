"""
Anthropic Backend — Claude API 實作

支援：
- 精確 token 計數（tiktoken cl100k_base）
- Layer 2 摘要（用較便宜的 Haiku 模型）
- Layer 3 完整壓縮（九+一段式結構化摘要）
- Server-side compaction（Beta API，可選）
"""

import time
from typing import List, Optional, Dict, Any

from session_control.backends.base import AbstractBackend
from session_control.schemas.message import Message
from session_control.schemas.session import SacredContent
from session_control.compression.rolling_summary import SUMMARY_PROMPT
from session_control.compression.full_compaction import COMPACTION_PROMPT


class AnthropicBackend(AbstractBackend):
    """Anthropic Claude API 後端"""

    # Claude 模型的 context window 大小
    CONTEXT_WINDOWS = {
        "claude-opus-4-6": 200000,
        "claude-opus-4-7": 200000,
        "claude-sonnet-4-6": 200000,
        "claude-haiku-4-5": 200000,
        "claude-sonnet-4-5": 200000,
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-6",
        compaction_model: str = "claude-haiku-4-5",
        server_side_compaction: bool = False,
    ):
        self.model = model
        self.compaction_model = compaction_model
        self.server_side_compaction = server_side_compaction
        self._client = None
        self._api_key = api_key
        self._tokenizer = None

    def _get_client(self):
        """延遲初始化 Anthropic client"""
        if self._client is None:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=self._api_key)
            except ImportError:
                raise ImportError(
                    "anthropic package required. Install with: pip install anthropic"
                )
        return self._client

    def _get_tokenizer(self):
        """延遲初始化 tokenizer"""
        if self._tokenizer is None:
            try:
                import tiktoken

                self._tokenizer = tiktoken.get_encoding("cl100k_base")
            except ImportError:
                self._tokenizer = None
        return self._tokenizer

    def get_context_window_size(self) -> int:
        """取得 context window 大小"""
        return self.CONTEXT_WINDOWS.get(self.model, 200000)

    def count_tokens(self, text: str) -> int:
        """精確計算 token 數量"""
        tokenizer = self._get_tokenizer()
        if tokenizer:
            return len(tokenizer.encode(text))
        # Fallback
        return max(1, len(text) // 4)

    async def summarize(
        self,
        messages: List[Message],
        instructions: Optional[str] = None,
    ) -> str:
        """Layer 2 摘要 — 用較便宜的模型"""
        client = self._get_client()
        prompt = instructions or SUMMARY_PROMPT

        # 將訊息格式化為可讀文本
        conversation_text = self._format_for_summary(messages)

        response = client.messages.create(
            model=self.compaction_model,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": f"{prompt}\n\n---\n\n{conversation_text}",
                }
            ],
        )

        return response.content[0].text  # type: ignore[union-attr]

    async def compact(
        self,
        messages: List[Message],
        sacred: SacredContent,
        instructions: Optional[str] = None,
    ) -> str:
        """Layer 3 完整壓縮 — 九+一段式結構化摘要"""
        client = self._get_client()

        # 構建壓縮 prompt
        sacred_text = sacred.total_content
        prompt = (instructions or COMPACTION_PROMPT).replace(
            "{sacred_content}", sacred_text
        )

        # 將訊息格式化
        conversation_text = self._format_for_summary(messages)

        response = client.messages.create(
            model=self.compaction_model,
            max_tokens=8192,
            messages=[
                {
                    "role": "user",
                    "content": f"{prompt}\n\n---\nCONVERSATION TO COMPRESS:\n---\n\n{conversation_text}",
                }
            ],
        )

        raw_output = response.content[0].text  # type: ignore[union-attr]

        # 提取 <summary> 區塊（丟棄 <analysis>）
        return self._extract_summary(raw_output)

    def format_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """轉為 Anthropic API 格式"""
        result = []
        for msg in messages:
            if msg.role == "system":
                # Anthropic 把 system 放在 top-level，不在 messages 裡
                continue
            result.append({"role": msg.role, "content": msg.content})
        return result

    def _format_for_summary(self, messages: List[Message]) -> str:
        """將訊息格式化為可讀文本供摘要用"""
        parts = []
        for msg in messages:
            role_label = {
                "user": "User",
                "assistant": "Assistant",
                "system": "System",
                "tool": "Tool",
            }.get(msg.role, msg.role)

            content = msg.content
            if msg.is_compressed:
                content = f"[Previously compressed summary]\n{content}"

            parts.append(f"[{role_label}]: {content}")

        return "\n\n".join(parts)

    @staticmethod
    def _extract_summary(raw: str) -> str:
        """從壓縮輸出中提取 <summary> 區塊"""
        import re

        match = re.search(r"<summary>(.*?)</summary>", raw, re.DOTALL)
        if match:
            return match.group(1).strip()
        # 如果沒有 XML 標籤，返回完整輸出
        return raw.strip()
