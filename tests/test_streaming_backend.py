"""
串流後端測試 — AbstractBackend.stream_query 與 OpenAIBackend

全程用假 client，不打真 API。
"""

from __future__ import annotations

from typing import Any

import pytest

from echo_thought_core.backends.base import AbstractBackend
from echo_thought_core.backends.openai import (
    DEFAULT_CONTEXT_WINDOW,
    OpenAIBackend,
    _resolve_context_window,
)
from echo_thought_core.backends.registry import BackendRegistry
from echo_thought_core.schemas.message import Message, ToolCall


# ═══════════════════════════════════════════════════════════════════════════════
# 假 backend / 假 client
# ═══════════════════════════════════════════════════════════════════════════════


class LegacyBackend(AbstractBackend):
    """沒有覆寫 stream_query 的舊後端——驗證向後相容。"""

    def get_context_window_size(self) -> int:
        return 8192

    def count_tokens(self, text: str) -> int:
        return len(text)

    def query(self, prompt, mode="chat", system_instruction=None, **kwargs):
        return {"text": f"回應：{prompt}"}

    async def summarize(self, messages, instructions=None):
        return "摘要"

    async def compact(self, messages, sacred, instructions=None):
        return "壓縮"


class _Fn:
    def __init__(self, name: str | None, arguments: str | None) -> None:
        self.name = name
        self.arguments = arguments


class _ToolDelta:
    def __init__(self, index: int, id: str | None, name: str | None, arguments: str | None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content: str | None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, content: str | None, tool_calls: list | None = None) -> None:
        self.delta = _Delta(content, tool_calls)


class _Chunk:
    def __init__(self, content, empty: bool = False) -> None:
        if empty:
            self.choices = []
        elif isinstance(content, list):  # tool_call 分塊
            self.choices = [_Choice(None, content)]
        else:
            self.choices = [_Choice(content)]


class _FakeStream:
    def __init__(self, pieces: list[str], include_empty: bool = False) -> None:
        self._chunks = [_Chunk(p) for p in pieces]
        if include_empty:
            self._chunks.insert(0, _Chunk(None, empty=True))

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeAsyncCompletions:
    def __init__(self, pieces: list[str], include_empty: bool = False) -> None:
        self.pieces = pieces
        self.include_empty = include_empty
        self.last_params: dict[str, Any] = {}

    async def create(self, **params):
        self.last_params = params
        if params.get("stream"):
            return _FakeStream(self.pieces, self.include_empty)

        class _Msg:
            content = "".join(self.pieces)

        class _Ch:
            message = _Msg()

        class _Resp:
            choices = [_Ch()]
            usage = None

        return _Resp()


class FakeAsyncClient:
    def __init__(self, pieces: list[str], include_empty: bool = False) -> None:
        self.completions = FakeAsyncCompletions(pieces, include_empty)
        self.chat = type("Chat", (), {"completions": self.completions})()


# ═══════════════════════════════════════════════════════════════════════════════
# AbstractBackend.stream_query 的預設實作
# ═══════════════════════════════════════════════════════════════════════════════


async def test_舊後端不必改就能用_stream_query():
    """預設實作退回 query()，既有 gemini / ollama 不會壞。"""
    backend = LegacyBackend()
    messages = [Message(role="user", content="你好")]

    pieces = [p async for p in backend.stream_query(messages)]
    assert pieces == ["回應：你好"]


async def test_舊後端誠實回報自己不是真串流():
    """呼叫端可據此警告，而不是讓人以為串流生效卻量到爛數字。"""
    assert LegacyBackend().supports_streaming is False


async def test_預設實作取最後一則使用者訊息當_prompt():
    backend = LegacyBackend()
    messages = [
        Message(role="user", content="第一句"),
        Message(role="assistant", content="回應"),
        Message(role="user", content="第二句"),
    ]
    pieces = [p async for p in backend.stream_query(messages)]
    assert pieces == ["回應：第二句"]


# ═══════════════════════════════════════════════════════════════════════════════
# OpenAIBackend
# ═══════════════════════════════════════════════════════════════════════════════


def test_openai_宣告支援真串流():
    backend = OpenAIBackend(api_key="test", async_client=FakeAsyncClient([]))
    assert backend.supports_streaming is True


async def test_串流逐段吐出():
    client = FakeAsyncClient(["你好", "，", "今天", "不錯"])
    backend = OpenAIBackend(api_key="test", async_client=client)

    pieces = [
        p async for p in backend.stream_query([Message(role="user", content="嗨")])
    ]
    assert pieces == ["你好", "，", "今天", "不錯"]
    assert "".join(pieces) == "你好，今天不錯"


async def test_空的_choices_不會炸():
    """OpenAI 偶爾會送出沒有 choices 的 chunk（例如只帶 usage 的收尾）。"""
    client = FakeAsyncClient(["文字"], include_empty=True)
    backend = OpenAIBackend(api_key="test", async_client=client)

    pieces = [
        p async for p in backend.stream_query([Message(role="user", content="嗨")])
    ]
    assert pieces == ["文字"]


async def test_用_max_completion_tokens_而不是_max_tokens():
    """max_tokens 在新模型上會被 API 拒絕。"""
    client = FakeAsyncClient(["x"])
    backend = OpenAIBackend(api_key="test", async_client=client, max_tokens=512)

    _ = [p async for p in backend.stream_query([Message(role="user", content="嗨")])]

    params = client.completions.last_params
    assert params["max_completion_tokens"] == 512
    assert "max_tokens" not in params


async def test_temperature_預設不傳():
    """推理型模型不接受自訂 temperature，傳了會被拒絕。"""
    client = FakeAsyncClient(["x"])
    backend = OpenAIBackend(api_key="test", async_client=client)

    _ = [p async for p in backend.stream_query([Message(role="user", content="嗨")])]
    assert "temperature" not in client.completions.last_params


async def test_有設定時才傳_temperature():
    client = FakeAsyncClient(["x"])
    backend = OpenAIBackend(api_key="test", async_client=client, temperature=0.7)

    _ = [p async for p in backend.stream_query([Message(role="user", content="嗨")])]
    assert client.completions.last_params["temperature"] == 0.7


async def test_system_instruction_排在最前面():
    client = FakeAsyncClient(["x"])
    backend = OpenAIBackend(api_key="test", async_client=client)

    _ = [
        p
        async for p in backend.stream_query(
            [Message(role="user", content="嗨")], system_instruction="你是助理"
        )
    ]

    messages = client.completions.last_params["messages"]
    assert messages[0] == {"role": "system", "content": "你是助理"}
    assert messages[1]["content"] == "嗨"


async def test_character_與_system_instruction_會合併():
    client = FakeAsyncClient(["x"])
    backend = OpenAIBackend(api_key="test", async_client=client, character="人設")

    _ = [
        p
        async for p in backend.stream_query(
            [Message(role="user", content="嗨")], system_instruction="額外指示"
        )
    ]

    system = client.completions.last_params["messages"][0]["content"]
    assert "人設" in system
    assert "額外指示" in system


async def test_歷史訊息會被送出():
    client = FakeAsyncClient(["x"])
    backend = OpenAIBackend(api_key="test", async_client=client)

    history = [
        Message(role="user", content="第一輪"),
        Message(role="assistant", content="第一輪回應"),
        Message(role="user", content="第二輪"),
    ]
    _ = [p async for p in backend.stream_query(history)]

    contents = [m["content"] for m in client.completions.last_params["messages"]]
    assert contents == ["第一輪", "第一輪回應", "第二輪"]


async def test_tool_訊息不會被送進_chat():
    """OpenAI 的 tool 訊息需要 tool_call_id 配對，半套送出會被 API 拒絕。"""
    client = FakeAsyncClient(["x"])
    backend = OpenAIBackend(api_key="test", async_client=client)

    messages = [
        Message(role="user", content="問題"),
        Message(role="tool", content="工具輸出", tool_name="search"),
    ]
    _ = [p async for p in backend.stream_query(messages)]

    roles = [m["role"] for m in client.completions.last_params["messages"]]
    assert "tool" not in roles


# ── 工具呼叫 ──────────────────────────────────────────────────────────────


async def test_tool_calls_分塊累積後整包_yield():
    """id/name 在第一塊、arguments 分散在後續塊；文字片段照舊先吐。"""
    client = FakeAsyncClient([
        "好",
        [_ToolDelta(0, "call_1", "deep_recall", '{"qu')],
        [_ToolDelta(0, None, None, 'ery": "貓"}')],
    ])
    backend = OpenAIBackend(api_key="test", async_client=client)

    out = [p async for p in backend.stream_query(
        [Message(role="user", content="x")], tools=[{"type": "function"}]
    )]

    assert out[0] == "好"
    assert isinstance(out[1], ToolCall)
    assert out[1].id == "call_1"
    assert out[1].name == "deep_recall"
    assert out[1].arguments == '{"query": "貓"}'
    assert client.completions.last_params["tools"] == [{"type": "function"}]


async def test_沒傳_tools_輸出只有字串():
    client = FakeAsyncClient(["a", "b"])
    backend = OpenAIBackend(api_key="test", async_client=client)
    out = [p async for p in backend.stream_query([Message(role="user", content="x")])]
    assert all(isinstance(p, str) for p in out)


async def test_工具結果與發出呼叫的_assistant_訊息會送回_api():
    client = FakeAsyncClient(["續寫"])
    backend = OpenAIBackend(api_key="test", async_client=client)
    call = ToolCall(id="call_1", name="deep_recall", arguments='{"query": "貓"}')
    messages = [
        Message(role="user", content="問題"),
        Message(role="assistant", content="", metadata={"tool_calls": [call]}),
        Message(role="tool", content="找到三筆", tool_call_id="call_1", tool_name="deep_recall"),
    ]
    _ = [p async for p in backend.stream_query(messages)]

    sent = client.completions.last_params["messages"]
    assert sent[1]["role"] == "assistant"
    assert sent[1]["content"] is None
    assert sent[1]["tool_calls"][0]["id"] == "call_1"
    assert sent[1]["tool_calls"][0]["function"]["name"] == "deep_recall"
    assert sent[2] == {"role": "tool", "tool_call_id": "call_1", "content": "找到三筆"}


# ── 認證 ──────────────────────────────────────────────────────────────────


def test_project_參數會傳進_client():
    """Project-based 存取。"""
    backend = OpenAIBackend(api_key="sk-proj-x", project="proj_123", organization="org_1")
    creds = backend._credentials()
    assert creds["api_key"] == "sk-proj-x"
    assert creds["project"] == "proj_123"
    assert creds["organization"] == "org_1"


def test_沒有金鑰時給出可行動的錯誤(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    backend = OpenAIBackend()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        _ = backend.async_client


def test_從環境變數讀金鑰(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setenv("OPENAI_PROJECT", "proj_env")
    backend = OpenAIBackend()
    creds = backend._credentials()
    assert creds["api_key"] == "sk-env"
    assert creds["project"] == "proj_env"


# ── context window / token ────────────────────────────────────────────────


def test_已知模型的_context_window():
    assert _resolve_context_window("gpt-4o") == 128_000


def test_前綴比對():
    assert _resolve_context_window("gpt-4o-2024-11-20") == 128_000


def test_未知模型退回保守值():
    """寧可估小浪費空間，也不要估大讓壓縮策略把 context 撐爆。"""
    assert _resolve_context_window("gpt-9-未來模型") == DEFAULT_CONTEXT_WINDOW


def test_config_可覆寫_context_window():
    backend = OpenAIBackend(
        config={
            "model": "gpt-5.6-luna",
            "context_window": 400_000,
            "use_cost_threshold": False,
        }
    )
    assert backend.get_context_window_size() == 400_000


# ── 價格門檻 ──────────────────────────────────────────────────────────────


def test_luna_的_context_window():
    """官方值：1,050,000（max input 922,000 / max output 128,000）。"""
    backend = OpenAIBackend(model="gpt-5.6-luna", use_cost_threshold=False)
    assert backend.get_context_window_size() == 1_050_000


def test_有效上限預設卡在價格門檻():
    """>272K input 會讓**整個請求** 2x input / 1.5x output。

    老實回傳 1.05M 會讓壓縮永遠不觸發，然後每次請求都吃 2x 價格。
    """
    backend = OpenAIBackend(model="gpt-5.6-luna")
    assert backend.get_context_window_size() == 272_000
    assert backend.get_max_context_window() == 1_050_000


def test_可以關掉價格門檻用滿長_context():
    backend = OpenAIBackend(model="gpt-5.6-luna", use_cost_threshold=False)
    assert backend.get_context_window_size() == 1_050_000


def test_沒有價格跳階的模型不受影響():
    backend = OpenAIBackend(model="gpt-4o")
    assert backend.get_context_window_size() == 128_000


# ── reasoning effort ──────────────────────────────────────────────────────


async def test_預設帶_high_reasoning_effort():
    """艾斯維爾定的基準。⚠️ 這直接影響首字延遲。"""
    client = FakeAsyncClient(["x"])
    backend = OpenAIBackend(api_key="test", async_client=client)

    _ = [p async for p in backend.stream_query([Message(role="user", content="嗨")])]
    assert client.completions.last_params["reasoning_effort"] == "high"


async def test_可以調低_effort_換首字延遲():
    client = FakeAsyncClient(["x"])
    backend = OpenAIBackend(api_key="test", async_client=client, reasoning_effort="low")

    _ = [p async for p in backend.stream_query([Message(role="user", content="嗨")])]
    assert client.completions.last_params["reasoning_effort"] == "low"


async def test_effort_設_None_就不傳():
    """給不支援這個參數的模型用。"""
    client = FakeAsyncClient(["x"])
    backend = OpenAIBackend(api_key="test", async_client=client, reasoning_effort=None)
    backend.reasoning_effort = None

    _ = [p async for p in backend.stream_query([Message(role="user", content="嗨")])]
    assert "reasoning_effort" not in client.completions.last_params


def test_tiktoken_不認識模型時不會爆():
    """新模型剛推出時一定會走這條路。"""
    backend = OpenAIBackend(model="gpt-5.6-luna", api_key="test")
    assert backend.count_tokens("你好世界") > 0


# ── registry ──────────────────────────────────────────────────────────────


def test_registry_有註冊_openai():
    BackendRegistry.reset_default()
    registry = BackendRegistry.default()
    assert "openai" in registry.list_providers()


def test_從_registry_建立():
    BackendRegistry.reset_default()
    registry = BackendRegistry.default()
    backend = registry.create("openai", {"model": "gpt-4o", "api_key": "test"})
    assert isinstance(backend, OpenAIBackend)
    assert backend.model_name == "gpt-4o"


# ── gpt-5.6-luna 的限制 + Responses 路徑 ─────────────────────────────────


async def test_chat_帶tools時reasoning_effort降成none():
    """luna 在 /v1/chat/completions 拒絕 tools + reasoning_effort（400）。"""
    client = FakeAsyncClient(["x"])
    backend = OpenAIBackend(api_key="test", async_client=client, reasoning_effort="high")
    _ = [p async for p in backend.stream_query(
        [Message(role="user", content="x")], tools=[{"type": "function"}]
    )]
    assert client.completions.last_params["reasoning_effort"] == "none"

    _ = [p async for p in backend.stream_query([Message(role="user", content="x")])]
    assert client.completions.last_params["reasoning_effort"] == "high", "沒 tools 不動它"


class _Item:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


class _Event:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


class _FakeEventStream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        self._it = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeResponses:
    def __init__(self, events):
        self.events = events
        self.last_params = {}

    async def create(self, **params):
        self.last_params = params
        return _FakeEventStream(self.events)


def _responses_backend(events):
    client = type("C", (), {})()
    client.responses = FakeResponses(events)
    return OpenAIBackend(api_key="test", async_client=client, api="responses"), client


async def test_responses_路徑_function_call_item_變成ToolCall():
    backend, client = _responses_backend([
        _Event("response.output_text.delta", delta="好"),
        _Event("response.output_item.done",
               item=_Item("function_call", call_id="call_7", name="deep_recall",
                          arguments='{"query": "貓"}', id="fc_1")),
        _Event("response.completed", response=_Item("response", id="resp_1")),
    ])
    out = [p async for p in backend.stream_query(
        [Message(role="user", content="x")],
        tools=[{"type": "function", "function": {"name": "deep_recall", "parameters": {}}}],
    )]
    assert out[0] == "好"
    assert isinstance(out[1], ToolCall) and out[1].id == "call_7"
    assert out[1].arguments == '{"query": "貓"}'
    # chat 格式的 schema 被攤平成 Responses 格式
    assert client.responses.last_params["tools"] == [
        {"type": "function", "name": "deep_recall", "parameters": {}}
    ]


async def test_responses_路徑_工具往返的input_items():
    backend, client = _responses_backend([])
    call = ToolCall(id="call_7", name="deep_recall", arguments='{"query": "貓"}')
    messages = [
        Message(role="user", content="問題"),
        Message(role="assistant", content="讓我想一下。", metadata={"tool_calls": [call]}),
        Message(role="tool", content="找到三筆", tool_call_id="call_7", tool_name="deep_recall"),
    ]
    _ = [p async for p in backend.stream_query(messages)]
    items = client.responses.last_params["input"]
    assert items == [
        {"role": "user", "content": "問題"},
        {"role": "assistant", "content": "讓我想一下。"},
        {"type": "function_call", "call_id": "call_7", "name": "deep_recall",
         "arguments": '{"query": "貓"}'},
        {"type": "function_call_output", "call_id": "call_7", "output": "找到三筆"},
    ]
