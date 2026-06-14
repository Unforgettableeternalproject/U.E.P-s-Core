"""
OpenAI Backend — GPT API 實作

支援：
- 精確 token 計數（tiktoken）
- Layer 2 摘要（GPT-4o-mini）
- Layer 3 完整壓縮（結構化摘要）
"""

from typing import List, Optional, Dict, Any

from session_control.backends.base import AbstractBackend
from session_control.schemas.message import Message
from session_control.schemas.session import SacredContent
from session_control.compression.rolling_summary import SUMMARY_PROMPT
from session_control.compression.full_compaction import COMPACTION_PROMPT


class OpenAIBackend(AbstractBackend):
    """OpenAI GPT API 後端"""

    CONTEXT_WINDOWS = {
        "gpt-4o": 128000,
        "gpt-4o-mini": 128000,
        "gpt-4-turbo": 128000,
        "gpt-4": 8192,
        "o3-mini": 200000,
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o",
        summary_model: str = "gpt-4o-mini",
    ):
        self.model = model
        self.summary_model = summary_model
        self._client = None
        self._api_key = api_key
        self._tokenizer = None

    def _get_client(self):
        """延遲初始化 OpenAI client"""
        if self._client is None:
            try:
                import openai

                self._client = openai.OpenAI(api_key=self._api_key)
            except ImportError:
                raise ImportError(
                    "openai package required. Install with: pip install openai"
                )
        return self._client

    def _get_tokenizer(self):
        """延遲初始化 tokenizer"""
        if self._tokenizer is None:
            try:
                import tiktoken

                self._tokenizer = tiktoken.encoding_for_model(self.model)
            except (ImportError, KeyError):
                try:
                    import tiktoken

                    self._tokenizer = tiktoken.get_encoding("cl100k_base")
                except ImportError:
                    self._tokenizer = None
        return self._tokenizer

    def get_context_window_size(self) -> int:
        return self.CONTEXT_WINDOWS.get(self.model, 128000)

    def count_tokens(self, text: str) -> int:
        tokenizer = self._get_tokenizer()
        if tokenizer:
            return len(tokenizer.encode(text))
        return max(1, len(text) // 4)

    async def summarize(
        self,
        messages: List[Message],
        instructions: Optional[str] = None,
    ) -> str:
        """Layer 2 摘要"""
        client = self._get_client()
        prompt = instructions or SUMMARY_PROMPT
        conversation_text = self._format_for_summary(messages)

        response = client.chat.completions.create(
            model=self.summary_model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": conversation_text},
            ],
        )

        return response.choices[0].message.content or ""

    async def compact(
        self,
        messages: List[Message],
        sacred: SacredContent,
        instructions: Optional[str] = None,
    ) -> str:
        """Layer 3 完整壓縮"""
        client = self._get_client()
        sacred_text = sacred.total_content
        prompt = (instructions or COMPACTION_PROMPT).replace(
            "{sacred_content}", sacred_text
        )
        conversation_text = self._format_for_summary(messages)

        response = client.chat.completions.create(
            model=self.summary_model,
            max_tokens=8192,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": conversation_text},
            ],
        )

        raw = response.choices[0].message.content or ""
        return self._extract_summary(raw)

    def _format_for_summary(self, messages: List[Message]) -> str:
        parts = []
        for msg in messages:
            label = {"user": "User", "assistant": "Assistant", "system": "System", "tool": "Tool"}.get(msg.role, msg.role)
            parts.append(f"[{label}]: {msg.content}")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_summary(raw: str) -> str:
        import re

        match = re.search(r"<summary>(.*?)</summary>", raw, re.DOTALL)
        if match:
            return match.group(1).strip()
        return raw.strip()
