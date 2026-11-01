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
from echo_thought_core.schemas.message import Message, ToolCall
from echo_thought_core.schemas.session import SacredContent

logger = logging.getLogger(__name__)

# ── Context Window 資料庫 ─────────────────────────────────────────────────────
#
# ⚠️ 只列出已確認的值。未收錄的模型會退到 DEFAULT_CONTEXT_WINDOW，
#    並在 log 留下警告——寧可保守估計，也不要編一個數字讓壓縮策略誤判
#    「還有很多空間」而把 context 撐爆。
#    要精確值請在 config 明確指定 context_window。

OPENAI_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5.6-luna": 1_050_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
}

COST_THRESHOLDS: dict[str, int] = {
    "gpt-5.6-luna": 272_000,
}
"""價格跳階的 input token 數。

GPT-5.6 系列：**超過 272K input tokens 的請求，整個請求**按 2x input /
1.5x output 計價（不是只有超出的部分）。

這對壓縮策略是關鍵：如果 :meth:`OpenAIBackend.get_context_window_size`
老實回傳 1,050,000，ContextManager 會覺得「空間還很多」而永遠不觸發壓縮，
然後每一次請求都吃 2x 價格。所以預設用這個門檻當**有效工作上限**。
"""

DEFAULT_CONTEXT_WINDOW = 128_000
"""未知模型的保守估計。新模型通常更大，估小只是浪費空間，估大會出事。"""

DEFAULT_REASONING_EFFORT = "high"
"""預設推理強度（艾斯維爾 2026-08-20 定為基準）。

⚠️ **這個參數直接影響首字延遲。** 推理強度越高，模型在吐出第一個 token
之前思考得越久——對串流語音管線來說，那段思考時間全部計入 TTFA。
要壓低 TTFA 就得往 ``low`` / ``none`` 調，這是品質與延遲的直接取捨。
gpt-5.6-luna 支援 none / low / medium / high / xhigh / max。
"""

DEFAULT_ENCODING = "o200k_base"
"""新模型（GPT-4o 之後）的 tokenizer。tiktoken 不認識模型名稱時的退路。"""


def _lookup(table: dict[str, int], model: str) -> int | None:
    """模糊匹配 — 依序嘗試精確、前綴（長的優先）。"""
    if model in table:
        return table[model]
    for key in sorted(table, key=len, reverse=True):
        if model.startswith(key):
            return table[key]
    return None


def _resolve_context_window(model: str) -> int:
    value = _lookup(OPENAI_CONTEXT_WINDOWS, model)
    if value is not None:
        return value
    logger.warning(
        "[OpenAI] 未知模型 %r 的 context window，退回保守值 %d。"
        "請在 config 指定 context_window 以取得精確值。",
        model,
        DEFAULT_CONTEXT_WINDOW,
    )
    return DEFAULT_CONTEXT_WINDOW


def _resolve_cost_threshold(model: str) -> int | None:
    """取得價格跳階門檻。沒有跳階的模型回 None。"""
    return _lookup(COST_THRESHOLDS, model)


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
        reasoning_effort: str | None = None,
        use_cost_threshold: bool = True,
        client: Any = None,
        async_client: Any = None,
    ):
        cfg = config or {}

        self.model_name: str = cfg.get("model", model)
        self.api: str = cfg.get("api", api)
        """``chat`` 或 ``responses``。"""

        self.reasoning_effort: str | None = cfg.get(
            "reasoning_effort", reasoning_effort or DEFAULT_REASONING_EFFORT
        )
        """推理強度。⚠️ **直接影響首字延遲**——強度越高，吐出第一個 token
        前思考得越久，那段時間全部計入串流管線的 TTFA。
        設為 None 表示不傳這個參數（給不支援的模型用）。"""

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
        self._use_cost_threshold: bool = cfg.get("use_cost_threshold", use_cost_threshold)
        self._cost_threshold: int | None = cfg.get(
            "cost_threshold", _resolve_cost_threshold(self.model_name)
        )
        self._encoder = None
        self._last_usage: dict[str, int] = {}
        self._last_stream_usage: dict[str, int] = {}
        """最近一次 :meth:`stream_query` 的用量（input_tokens / cached_tokens / output_tokens），
        與非串流的 ``_last_usage`` 分開，避免壓縮管線的 query 蓋掉對話那一輪的數字。"""
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
        """回傳**有效工作上限**，不一定是模型的理論上限。

        GPT-5.6 系列的 context window 是 1,050,000，但超過 272K input tokens
        的請求會**整個請求**按 2x input / 1.5x output 計價。老實回傳 1.05M
        會讓 ContextManager 覺得空間還很多而永遠不觸發壓縮，
        然後每次請求都吃 2x 價格。

        所以預設取 ``min(context_window, cost_threshold)``，讓壓縮在跳階前
        就開始工作。不在乎成本、要用滿長 context 的話，
        設 ``use_cost_threshold=False``。
        """
        if self._use_cost_threshold and self._cost_threshold:
            return min(self._context_window, self._cost_threshold)
        return self._context_window

    def get_max_context_window(self) -> int:
        """模型的理論上限，不考慮價格跳階。"""
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
    ) -> AsyncIterator[str | ToolCall]:
        """真串流——逐段吐出文字片段。

        兩種 API 路徑的差別：

        * ``chat``（預設）— Chat Completions。每次都送完整歷史，
          context 完全由 ContextManager 掌控。
        * ``responses`` — Responses API。可以用 ``previous_response_id``
          讓 OpenAI 端保留對話狀態，省掉重送歷史（對首字延遲有幫助），
          代價是**壓縮機制失去對 context 的控制權**。
          要啟用得明確傳 ``use_previous_response=True``。

        工具呼叫（兩條路徑都支援）：傳 ``tools=[...]``（chat 格式的 function
        schema，responses 路徑會自動攤平）後，模型若決定呼叫工具，會在文字片段之後 yield
        :class:`~echo_thought_core.schemas.message.ToolCall`。呼叫端執行完工具，
        把「發出呼叫的 assistant message（metadata.tool_calls）」與
        「role=tool 的結果訊息（tool_call_id）」接回 messages 再呼叫一次即可。
        沒傳 ``tools`` 時輸出只有 str，既有消費端不受影響。

        ⚠️ gpt-5.6-luna 的 chat 路徑不接受 tools + reasoning_effort，會被降成
        ``none``；要工具又要推理，用 ``api="responses"``。
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
    ) -> AsyncIterator[str | ToolCall]:
        params = self._completion_params(
            self._build_chat_messages(messages, system_instruction),
            stream=True,
            **kwargs,
        )
        if params.get("tools") and params.get("reasoning_effort") not in (None, "none"):
            # gpt-5.6-luna 在 /v1/chat/completions 不接受「function tools + reasoning_effort」
            # （400：要嘛走 /v1/responses，要嘛 effort=none）。這裡選後者保住工具；
            # 要保留推理請用 api="responses"。
            logger.warning(
                "chat completions 帶 tools 時 reasoning_effort=%r 不被接受，本次改為 none",
                params["reasoning_effort"],
            )
            params["reasoning_effort"] = "none"
        if not self._base_url:   # 官方 API 才要求最後一塊帶 usage；相容端點不一定吃這個參數
            params.setdefault("stream_options", {"include_usage": True})
        self._last_stream_usage = {}
        stream = await self.async_client.chat.completions.create(**params)
        # tool_calls 是分塊送來的：第一塊帶 id/name，後續塊只帶 arguments 片段，
        # 用 index 對回同一次呼叫。串流結束才整包 yield——半截 JSON 沒人能用。
        pending_calls: dict[int, dict[str, str]] = {}
        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                self._record_stream_usage(usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield piece
            for tc in getattr(delta, "tool_calls", None) or []:
                slot = pending_calls.setdefault(
                    tc.index, {"id": "", "name": "", "arguments": ""}
                )
                if tc.id:
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if fn.name:
                        slot["name"] = fn.name
                    if fn.arguments:
                        slot["arguments"] += fn.arguments
        for index in sorted(pending_calls):
            slot = pending_calls[index]
            if slot["name"]:
                yield ToolCall(**slot)

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
        if self.reasoning_effort:
            params["reasoning"] = {"effort": self.reasoning_effort}

        if use_previous_response and self._last_response_id:
            params["previous_response_id"] = self._last_response_id
            latest = next(
                (m.content for m in reversed(messages) if m.role == "user"), ""
            )
            params["input"] = latest
        else:
            params["input"] = self._build_responses_input(messages)

        tools = kwargs.pop("tools", None)
        if tools:
            # Responses API 的 tool schema 是平的（name/description/parameters 直接在
            # 頂層），chat 那種 {"type":"function","function":{...}} 要攤開
            params["tools"] = [
                {"type": "function", **t["function"]} if "function" in t else t
                for t in tools
            ]
        params.update(kwargs)

        self._last_stream_usage = {}
        stream = await self.async_client.responses.create(**params)
        async for event in stream:
            event_type = getattr(event, "type", "")
            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    yield delta
            elif event_type == "response.output_item.done":
                # function_call item 在 done 時 arguments 已經完整，不必自己拼 delta
                item = getattr(event, "item", None)
                if getattr(item, "type", "") == "function_call":
                    yield ToolCall(
                        id=item.call_id, name=item.name, arguments=item.arguments or ""
                    )
            elif event_type == "response.completed":
                response = getattr(event, "response", None)
                if response is not None:
                    self._last_response_id = getattr(response, "id", None)
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        self._record_stream_usage(usage)

    def _record_stream_usage(self, usage: Any) -> None:
        """把串流末端的 usage 正規化成 input / cached / output tokens。

        Responses API 是 ``input_tokens`` / ``output_tokens`` / ``input_tokens_details.cached_tokens``；
        Chat Completions 是 ``prompt_tokens`` / ``completion_tokens`` / ``prompt_tokens_details.cached_tokens``。
        """
        def pick(obj: Any, *names: str) -> int:
            for name in names:
                value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
                if isinstance(value, (int, float)):
                    return int(value)
            return 0

        def detail(obj: Any, *names: str) -> Any:
            for name in names:
                value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
                if value is not None:
                    return value
            return None

        details = detail(usage, "input_tokens_details", "prompt_tokens_details")
        self._last_stream_usage = {
            "input_tokens": pick(usage, "input_tokens", "prompt_tokens"),
            "cached_tokens": pick(details, "cached_tokens") if details is not None else 0,
            "output_tokens": pick(usage, "output_tokens", "completion_tokens"),
        }

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

    def _build_responses_input(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Responses API 的 input items。

        工具往返的表示法與 chat 不同：發出呼叫 = ``function_call`` item
        （不是 assistant message 的欄位）、結果 = ``function_call_output`` item。
        """
        items: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                if m.tool_call_id:
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": m.tool_call_id,
                            "output": m.content,
                        }
                    )
                continue
            if m.role not in ("user", "assistant", "system"):
                continue
            calls = m.metadata.get("tool_calls") if m.role == "assistant" else None
            if m.content:
                items.append({"role": m.role, "content": m.content})
            for c in calls or []:
                c = c.to_openai() if isinstance(c, ToolCall) else c
                items.append(
                    {
                        "type": "function_call",
                        "call_id": c["id"],
                        "name": c["function"]["name"],
                        "arguments": c["function"]["arguments"] or "{}",
                    }
                )
        return items

    def _build_chat_messages(
        self, messages: list[Message], system_instruction: str | None
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        system_text = self._system_text(system_instruction)
        if system_text:
            result.append({"role": "system", "content": system_text})
        for m in messages:
            if m.role == "tool":
                # 工具結果：必須帶回 provider 給的 call id，否則 API 會拒絕整段對話
                if not m.tool_call_id:
                    continue
                result.append(
                    {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
                )
            elif m.role in ("user", "assistant", "system"):
                item: dict[str, Any] = {"role": m.role, "content": m.content}
                calls = m.metadata.get("tool_calls") if m.role == "assistant" else None
                if calls:
                    # 發出工具呼叫的那則 assistant message（content 可以是空）
                    item["tool_calls"] = [
                        c.to_openai() if isinstance(c, ToolCall) else c for c in calls
                    ]
                    if not item["content"]:
                        item["content"] = None
                result.append(item)
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
        if self.reasoning_effort:
            params["reasoning_effort"] = self.reasoning_effort
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

    def get_last_stream_usage(self) -> dict[str, int]:
        """最近一次串流的 ``{input_tokens, cached_tokens, output_tokens}``；沒有（或還沒結束）回空 dict。"""
        return dict(self._last_stream_usage)
