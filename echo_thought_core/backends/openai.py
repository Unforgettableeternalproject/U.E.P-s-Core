"""
OpenAI Backend — OpenAI API 後端（支援 Project-based 存取與串流）

實作 AbstractBackend 介面供 ContextManager 壓縮管線使用，
並覆寫 stream_query() 提供**真串流**——這是串流語音管線
（Echo Stream）能在句子邊界就開始合成語音的前提。

兩種 API 路徑：
  chat       — Chat Completions（相容性最廣，預設）
  responses  — Responses API（支援 previous_response_id 做 server 端
               對話狀態，能省掉重送歷史，對首字延遲有直接幫助）

認證（Project-based）：
  api_key      sk-proj-... 形式的 Project 金鑰
  project      Project ID，金鑰橫跨多個 project 時需要指定
  organization Organization ID（選填）

Usage:
    from echo_thought_core.backends.openai import OpenAIBackend

    backend = OpenAIBackend(model="gpt-5.6-luna", api_key="sk-proj-...")

    async for piece in backend.stream_query(messages):
        print(piece, end="")
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from echo_thought_core.backends.base import AbstractBackend
from echo_thought_core.compression.full_compaction import COMPACTION_PROMPT
from echo_thought_core.compression.rolling_summary import SUMMARY_PROMPT
from echo_thought_core.schemas.message import Message
from echo_thought_core.schemas.session import SacredContent

logger = logging.getLogger(__name__)

# ── Context Window 資料庫 ─────────────────────────────────────────────────────
#
# ⚠️ 只列出已確認的值。未收錄的模型會退到 DEFAULT_CONTEXT_WINDOW，
#    並在 log 留下警告——寧可保守估計，也不要編一個數字讓壓縮策略誤判
#    「還有很多空間」而把 context 撐爆。
#    要精確值請在 config 明確指定 context_window。

OPENAI_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
}

DEFAULT_CONTEXT_WINDOW = 128_000
"""未知模型的保守估計。新模型通常更大，估小只是浪費空間，估大會出事。"""

DEFAULT_ENCODING = "o200k_base"
"""新模型（GPT-4o 之後）的 tokenizer。tiktoken 不認識模型名稱時的退路。"""


def _resolve_context_window(model: str) -> int:
    """模糊匹配 context window — 依序嘗試精確、前綴。"""
    if model in OPENAI_CONTEXT_WINDOWS:
        return OPENAI_CONTEXT_WINDOWS[model]
    for key in sorted(OPENAI_CONTEXT_WINDOWS, key=len, reverse=True):
        if model.startswith(key):
            return OPENAI_CONTEXT_WINDOWS[key]
    logger.warning(
        "[OpenAI] 未知模型 %r 的 context window，退回保守值 %d。"
        "請在 config 指定 context_window 以取得精確值。",
        model,
        DEFAULT_CONTEXT_WINDOW,
    )
    return DEFAULT_CONTEXT_WINDOW


# ═══════════════════════════════════════════════════════════════════════════════
# OpenAIBackend
# ═══════════════════════════════════════════════════════════════════════════════


class OpenAIBackend(AbstractBackend):
    """OpenAI API 後端，支援真串流。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        model: str = "gpt-5.6-luna",
        api_key: str | None = None,
        project: str | None = None,
        organization: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 2048,
        character: str = "",
        timeout: int = 120,
        api: str = "chat",
        client: Any = None,
        async_client: Any = None,
    ):
        cfg = config or {}

        self.model_name: str = cfg.get("model", model)
        self.api: str = cfg.get("api", api)
        """``chat`` 或 ``responses``。"""

        self.character: str = cfg.get("character", character)
        self.timeout: int = cfg.get("timeout", timeout)
        self.max_tokens: int = cfg.get("max_output_tokens", max_tokens)

        self.temperature: float | None = cfg.get("temperature", temperature)
        """None 表示不傳這個參數。

        推理型模型（o 系列等）不接受自訂 temperature，傳了會直接被 API 拒絕。
        預設 None 是為了讓「不知道模型支不支援」時也能正常運作。"""

        self._api_key: str | None = (
            cfg.get("api_key") or api_key or os.environ.get("OPENAI_API_KEY")
        )
        self._project: str | None = (
            cfg.get("project") or project or os.environ.get("OPENAI_PROJECT")
        )
        self._organization: str | None = (
            cfg.get("organization") or organization or os.environ.get("OPENAI_ORG")
        )
        self._base_url: str | None = (
            cfg.get("base_url") or base_url or os.environ.get("OPENAI_BASE_URL")
        )

        # 允許注入 client（測試、或呼叫端已經有一個設定好的 client）
        self._client = client
        self._async_client = async_client

        self._context_window: int = cfg.get("context_window") or _resolve_context_window(
            self.model_name
        )
        self._encoder = None
        self._last_usage: dict[str, int] = {}
        self._last_response_id: str | None = None
        """Responses API 的對話狀態。用 previous_response_id 串接可以省掉
        重送歷史——但那會讓對話狀態存在 OpenAI 端，與 ContextManager 的
        壓縮機制搶管理權，所以預設不啟用（見 :meth:`stream_query`）。"""

        logger.debug(
            "[OpenAI] Initialized: model=%s, api=%s, project=%s",
            self.model_name,
            self.api,
            self._project or "(default)",
        )

    # ── Client ────────────────────────────────────────────────────────────────

    def _credentials(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._project:
            kwargs["project"] = self._project
        if self._organization:
            kwargs["organization"] = self._organization
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if self.timeout:
            kwargs["timeout"] = self.timeout
        return kwargs

    def _require_key(self) -> None:
        if not self._api_key:
            raise RuntimeError(
                "缺少 OpenAI API 金鑰。請設定 OPENAI_API_KEY 環境變數，"
                "或在 config 傳入 api_key。"
            )

    @property
    def client(self):
        """同步 client（給 query / count_tokens 等用）。"""
        if self._client is None:
            self._require_key()
            from openai import OpenAI

            self._client = OpenAI(**self._credentials())
        return self._client

    @property
    def async_client(self):
        """非同步 client（給 stream_query / summarize / compact 用）。"""
        if self._async_client is None:
            self._require_key()
            from openai import AsyncOpenAI

            self._async_client = AsyncOpenAI(**self._credentials())
        return self._async_client

    # ── AbstractBackend 介面 ──────────────────────────────────────────────────

    def get_context_window_size(self) -> int:
        return self._context_window

    def count_tokens(self, text: str) -> int:
        """用 tiktoken 精確計算。

        模型名稱未被 tiktoken 收錄時退回 :data:`DEFAULT_ENCODING`——
        新模型剛推出時一定會走這條路，不能讓它直接爆掉。
        """
        if self._encoder is None:
            import tiktoken

            try:
                self._encoder = tiktoken.encoding_for_model(self.model_name)
            except KeyError:
                logger.debug(
                    "[OpenAI] tiktoken 不認識 %r，改用 %s",
                    self.model_name,
                    DEFAULT_ENCODING,
                )
                self._encoder = tiktoken.get_encoding(DEFAULT_ENCODING)
        return len(self._encoder.encode(text))

    @property
    def supports_streaming(self) -> bool:
        return True

    def query(
        self,
        prompt: str,
        mode: str = "chat",
        system_instruction: str | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """同步查詢，回傳完整回應。

        壓縮管線（summarize / compact）走這條；串流對話走
        :meth:`stream_query`。
        """
        messages = self._build_chat_messages(
            [Message(role="user", content=prompt)], system_instruction
        )
        params = self._completion_params(messages, stream=False, **kwargs)

        response = self.client.chat.completions.create(**params)
        text = response.choices[0].message.content or ""

        usage = getattr(response, "usage", None)
        if usage is not None:
            self._last_usage = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            }

        return {"text": text, "usage": dict(self._last_usage), "model": self.model_name}

    async def stream_query(
        self,
        messages: list[Message],
        system_instruction: str | None = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        """真串流——逐段吐出文字片段。

        兩種 API 路徑的差別：

        * ``chat``（預設）— Chat Completions。每次都送完整歷史，
          context 完全由 ContextManager 掌控。
        * ``responses`` — Responses API。可以用 ``previous_response_id``
          讓 OpenAI 端保留對話狀態，省掉重送歷史（對首字延遲有幫助），
          代價是**壓縮機制失去對 context 的控制權**。
          要啟用得明確傳 ``use_previous_response=True``。
        """
        if self.api == "responses":
            async for piece in self._stream_responses(
                messages, system_instruction, **kwargs
            ):
                yield piece
        else:
            async for piece in self._stream_chat(messages, system_instruction, **kwargs):
                yield piece

    async def _stream_chat(
        self,
        messages: list[Message],
        system_instruction: str | None,
        **kwargs,
    ) -> AsyncIterator[str]:
        params = self._completion_params(
            self._build_chat_messages(messages, system_instruction),
            stream=True,
            **kwargs,
        )
        stream = await self.async_client.chat.completions.create(**params)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield piece

    async def _stream_responses(
        self,
        messages: list[Message],
        system_instruction: str | None,
        use_previous_response: bool = False,
        **kwargs,
    ) -> AsyncIterator[str]:
        params: dict[str, Any] = {
            "model": self.model_name,
            "stream": True,
            "max_output_tokens": kwargs.pop("max_tokens", None) or self.max_tokens,
        }
        instructions = self._system_text(system_instruction)
        if instructions:
            params["instructions"] = instructions
        if self.temperature is not None:
            params["temperature"] = self.temperature

        if use_previous_response and self._last_response_id:
            params["previous_response_id"] = self._last_response_id
            latest = next(
                (m.content for m in reversed(messages) if m.role == "user"), ""
            )
            params["input"] = latest
        else:
            params["input"] = [
                {"role": m.role, "content": m.content}
                for m in messages
                if m.role in ("user", "assistant", "system")
            ]

        params.update(kwargs)

        stream = await self.async_client.responses.create(**params)
        async for event in stream:
            event_type = getattr(event, "type", "")
            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    yield delta
            elif event_type == "response.completed":
                response = getattr(event, "response", None)
                if response is not None:
                    self._last_response_id = getattr(response, "id", None)

    async def summarize(
        self,
        messages: list[Message],
        instructions: str | None = None,
    ) -> str:
        """將訊息列表摘要為文本（Layer 2 使用）。"""
        prompt = instructions or SUMMARY_PROMPT
        conversation = "\n".join(f"{m.role}: {m.content}" for m in messages)
        return await self._complete(f"{prompt}\n\n{conversation}")

    async def compact(
        self,
        messages: list[Message],
        sacred: SacredContent,
        instructions: str | None = None,
    ) -> str:
        """將完整對話壓縮為結構化摘要（Layer 3 使用）。"""
        prompt = instructions or COMPACTION_PROMPT
        conversation = "\n".join(f"{m.role}: {m.content}" for m in messages)
        sacred_text = ""
        if sacred is not None:
            sacred_text = f"\n\n[必須保留的內容]\n{sacred}"
        return await self._complete(f"{prompt}{sacred_text}\n\n{conversation}")

    async def _complete(self, prompt: str) -> str:
        """非串流的一次性生成。壓縮/摘要用——它們本來就要完整結果。"""
        params = self._completion_params(
            [{"role": "user", "content": prompt}], stream=False
        )
        response = await self.async_client.chat.completions.create(**params)
        return response.choices[0].message.content or ""

    # ── 內部 ──────────────────────────────────────────────────────────────────

    def _system_text(self, system_instruction: str | None) -> str:
        parts = [p for p in (self.character, system_instruction) if p]
        return "\n\n".join(parts)

    def _build_chat_messages(
        self, messages: list[Message], system_instruction: str | None
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        system_text = self._system_text(system_instruction)
        if system_text:
            result.append({"role": "system", "content": system_text})
        result.extend(
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant", "system")
        )
        return result

    def _completion_params(
        self, messages: list[dict[str, str]], stream: bool, **kwargs
    ) -> dict[str, Any]:
        """組 Chat Completions 的參數。

        用 ``max_completion_tokens`` 而不是 ``max_tokens``——後者在新模型上
        已被拒絕。舊模型仍接受新參數名，所以統一用新的。
        """
        params: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "stream": stream,
            "max_completion_tokens": kwargs.pop("max_tokens", None) or self.max_tokens,
        }
        if self.temperature is not None:
            params["temperature"] = self.temperature
        kwargs.pop("mode", None)
        params.update(kwargs)
        return params

    # ── 對話介面（與 OllamaBackend 對稱）────────────────────────────────────

    def chat(self, prompt: str, system_instruction: str | None = None) -> str:
        return self.query(prompt, system_instruction=system_instruction)["text"]

    async def achat(self, prompt: str, system_instruction: str | None = None) -> str:
        return await asyncio.to_thread(self.chat, prompt, system_instruction)

    def get_last_usage(self) -> dict[str, int]:
        return dict(self._last_usage)
