"""
Gemini Backend — Google Gemini / Vertex AI 實作

從 U.E.P Core 的 gemini_client.py 和 cache_manager.py 遷出改造。
支援：
- Vertex AI 和 API Key 兩種認證
- Context caching（顯性 + 隱性）
- Layer 2/3 摘要
"""

import os
from typing import List, Optional, Dict, Any

from session_control.backends.base import AbstractBackend
from session_control.schemas.message import Message
from session_control.schemas.session import SacredContent
from session_control.compression.rolling_summary import SUMMARY_PROMPT
from session_control.compression.full_compaction import COMPACTION_PROMPT


class GeminiBackend(AbstractBackend):
    """Google Gemini / Vertex AI 後端"""

    CONTEXT_WINDOWS = {
        "gemini-2.5-flash-lite": 1048576,
        "gemini-2.5-flash": 1048576,
        "gemini-2.5-pro": 1048576,
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        project_id: Optional[str] = None,
        location: str = "us-central1",
        model: str = "gemini-2.5-flash-lite",
    ):
        self.model = model
        self._client = None
        self._api_key = api_key
        self._project_id = project_id or os.getenv("GCP_PROJECT_ID")
        self._location = location

    def _get_client(self):
        """延遲初始化 Gemini client"""
        if self._client is None:
            try:
                from google import genai

                if self._project_id:
                    self._client = genai.Client(
                        vertexai=True,
                        project=self._project_id,
                        location=self._location,
                    )
                else:
                    self._client = genai.Client(api_key=self._api_key)
            except ImportError:
                raise ImportError(
                    "google-genai package required. Install with: pip install google-genai"
                )
        return self._client

    def get_context_window_size(self) -> int:
        return self.CONTEXT_WINDOWS.get(self.model, 1048576)

    def count_tokens(self, text: str) -> int:
        """Gemini 的 token 計數 — 使用 API 或 fallback"""
        # Gemini 沒有本地 tokenizer，使用字元估算
        cjk_count = sum(1 for c in text if "一" <= c <= "鿿")
        ascii_count = len(text) - cjk_count
        return (ascii_count // 4) + (cjk_count * 2 // 3) + 1

    async def summarize(
        self,
        messages: List[Message],
        instructions: Optional[str] = None,
    ) -> str:
        """Layer 2 摘要"""
        client = self._get_client()
        prompt = instructions or SUMMARY_PROMPT
        conversation_text = self._format_for_summary(messages)

        response = client.models.generate_content(
            model=self.model,
            contents=f"{prompt}\n\n---\n\n{conversation_text}",
        )

        return response.text or ""

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

        response = client.models.generate_content(
            model=self.model,
            contents=f"{prompt}\n\n---\nCONVERSATION TO COMPRESS:\n---\n\n{conversation_text}",
        )

        raw = response.text or ""
        return self._extract_summary(raw)

    def format_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """轉為 Gemini API 格式"""
        result = []
        for msg in messages:
            role = "user" if msg.role in ("user", "system") else "model"
            result.append({"role": role, "parts": [{"text": msg.content}]})
        return result

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
