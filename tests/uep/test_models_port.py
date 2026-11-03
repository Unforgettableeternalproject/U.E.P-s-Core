"""模型層離線測試（Spike 1，Agent 架構提案 §7.4）：不打真 API，用 PydanticAI 的 FunctionModel／TestModel。

涵蓋：part 事件對映（thought／text／tool_call -> tool_intent）、想／說零混入、SDK 不執行工具（S1-1）、
D60 啟動斷言（S1-5）、取消（S1-4）、ToolCallAssembler 對壞 JSON 的處理。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaThinkingPart,
    DeltaToolCall,
    FunctionModel,
)
from pydantic_ai.models.test import TestModel

from uep.models import (
    CapabilityMissingError,
    ChatMessage,
    ModelEvent,
    ModelRequest,
    ToolCallAssembler,
)
from uep.models.pydantic_ai_port import PydanticAIModelPort

CAPABILITY_SET = {"tools": [
    {"tool": "deep_recall", "version": 1, "description": "查記憶", "variant": None,
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"tool": "member_recall", "version": 1, "description": "查成員", "variant": None,
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]}


def _req(attempt: str = "att_1", cap=CAPABILITY_SET, **kw) -> ModelRequest:
    return ModelRequest(attempt_id=attempt, messages=[ChatMessage("user", "hi")], capability_set=cap, **kw)


async def _collect(port: PydanticAIModelPort, req: ModelRequest) -> list[ModelEvent]:
    return [e async for e in port.stream(req)]


def _port(stream_fn) -> PydanticAIModelPort:
    return PydanticAIModelPort("openai_chat", model=FunctionModel(stream_function=stream_fn))


async def test_thought_and_text_never_mix():
    thought = ["先想", "一下", "【THINK-MARK】"]
    text = ["你", "好", "世界"]

    async def fn(messages, info: AgentInfo):
        for t in thought:
            yield {0: DeltaThinkingPart(content=t)}
        for t in text:
            yield t

    events = await _collect(_port(fn), _req())
    got_thought = "".join(e.text for e in events if e.kind == "thought_delta")
    got_text = "".join(e.text for e in events if e.kind == "text_delta")
    assert got_thought == "".join(thought)
    assert got_text == "".join(text)
    assert "THINK-MARK" not in got_text and "你好" not in got_thought
    assert events[-1].kind == "finish" and events[-1].finish_reason == "stop"


async def test_tool_call_ends_run_and_sdk_never_executes():
    async def fn(messages, info: AgentInfo):
        assert {t.name for t in info.function_tools} == {"deep_recall", "member_recall"}  # 只收到渲染出的工具
        yield "我來查"
        yield {1: DeltaToolCall(name="deep_recall", json_args='{"que', tool_call_id="call_a")}
        yield {1: DeltaToolCall(json_args='ry": "貓"}')}
        yield {2: DeltaToolCall(name="member_recall", json_args='{"name": "x"}', tool_call_id="call_b")}

    port = _port(fn)
    events = await _collect(port, _req())
    intents = [e for e in events if e.kind == "tool_intent"]
    assert [(i.tool, dict(i.args), i.args_complete) for i in intents] == [
        ("deep_recall", {"query": "貓"}, True), ("member_recall", {"name": "x"}, True),
    ]
    assert [i.call_id for i in intents] == ["call_a", "call_b"]
    assert events[-1].finish_reason == "tool_calls"
    assert port.sdk_tool_executions[0] == 0
    # 增量期間沒有 tool_intent（只在 finish 前一次產生）
    assert events.index(intents[0]) > max(i for i, e in enumerate(events) if e.kind == "tool_call_delta")


async def test_testmodel_call_tools_is_deferred_not_executed():
    port = PydanticAIModelPort("openai_chat", model=TestModel(call_tools="all"))
    events = await _collect(port, _req())
    assert {e.tool for e in events if e.kind == "tool_intent"} == {"deep_recall", "member_recall"}
    assert port.sdk_tool_executions[0] == 0
    assert events[-1].finish_reason == "tool_calls"


async def test_history_with_tool_result_round_trip():
    seen = {}

    async def fn(messages, info: AgentInfo):
        seen["kinds"] = [type(m).__name__ for m in messages]
        yield "好的"

    msgs = [
        ChatMessage("user", "查貓"),
        ChatMessage("assistant", "", tool_calls=({"id": "c1", "name": "deep_recall", "args": {"query": "貓"}},)),
        ChatMessage("tool", json.dumps({"items": []}), tool_call_id="c1"),
    ]
    events = await _collect(_port(fn), ModelRequest("a", msgs, CAPABILITY_SET))
    assert "".join(e.text for e in events if e.kind == "text_delta") == "好的"
    assert seen["kinds"] == ["ModelRequest", "ModelResponse", "ModelRequest"]


async def test_d60_rejects_builtin_tools_before_any_request():
    called = []

    async def fn(messages, info):
        called.append(1)
        yield "x"

    with pytest.raises(CapabilityMissingError) as exc:
        await _collect(_port(fn), _req(builtin_tools=("web_search",)))
    assert exc.value.event.error["code"] == "CAPABILITY_MISSING"
    assert not called


async def test_d60_rejects_tool_set_not_equal_to_capability(monkeypatch):
    called = []

    async def fn(messages, info):
        called.append(1)
        yield "x"

    port = _port(fn)
    original = port._tool_defs

    def tampered(req):
        defs, expected = original(req)
        return defs[:1], expected  # 少給
    monkeypatch.setattr(port, "_tool_defs", tampered)
    with pytest.raises(CapabilityMissingError) as exc:
        await _collect(port, _req())
    assert "少給" in exc.value.event.error["detail"]
    assert not called


async def test_d60_rejects_malformed_capability_set():
    async def fn(messages, info):
        yield "x"

    with pytest.raises(ValueError):
        await _collect(_port(fn), _req(cap={"tools": [{"tool": "a"}]}))


async def test_cancel_stops_stream_within_500ms_and_no_late_events():
    async def fn(messages, info):
        for i in range(400):
            await asyncio.sleep(0.01)
            yield f"t{i} "

    port = _port(fn)
    got: list[ModelEvent] = []
    cancelled_at = 0.0
    stream = port.stream(_req("att_cancel"))
    async for e in stream:
        got.append(e)
        if len(got) == 5:
            cancelled_at = time.perf_counter()
            port.cancel("att_cancel")
            port.cancel("att_cancel")  # 冪等
            n_at_cancel = len(got)
    stopped_after = time.perf_counter() - cancelled_at
    assert stopped_after < 0.5
    assert got[-1].kind == "finish" and got[-1].finish_reason == "cancelled"
    assert [e.kind for e in got[n_at_cancel:]] == ["finish"]  # cancel 之後除了 finish 沒有任何內容事件
    assert not any(e.kind == "tool_intent" for e in got)


def test_assembler_invalid_json_is_never_guessed():
    asm = ToolCallAssembler()
    asm.feed(ModelEvent(kind="tool_call_delta", index=0, call_id="c", tool="deep_recall", args_fragment='{"query": "ca'))
    (intent,) = asm.intents("att")
    assert intent.args_complete is False and intent.args is None and intent.error_code == "invalid_tool_args"


def test_assembler_synthesizes_missing_call_id_deterministically():
    asm = ToolCallAssembler()
    asm.feed(ModelEvent(kind="tool_call_delta", index=1, tool="deep_recall", args_fragment="{}"))
    (intent,) = asm.intents("att_9")
    assert intent.call_id == "att_9:1" and intent.args_complete is True
