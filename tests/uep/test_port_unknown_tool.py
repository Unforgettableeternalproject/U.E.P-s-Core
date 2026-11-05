"""Port 攔截未知工具（切片 D1，§7.4 限制 1）：不多打一次模型，未知工具轉成 tool_intent 交給 Gateway 拒絕。"""
from __future__ import annotations

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from uep.models import ChatMessage, ModelRequest
from uep.models.pydantic_ai_port import PydanticAIModelPort
from uep.tools.gateway import GatewayRejected, ToolGateway

CAP = {"tools": [{
    "tool": "deep_recall", "version": 1, "description": "d", "variant": None,
    "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
}]}


async def _run(calls: dict[str, int], script):
    async def fn(messages, info):
        calls["n"] += 1
        for item in script:
            yield item

    port = PydanticAIModelPort("openai_chat", model=FunctionModel(stream_function=fn))
    return [e async for e in port.stream(ModelRequest("a1", (ChatMessage("user", "hi"),), CAP))]


async def test_only_unknown_tool_one_model_call_and_intent_emitted():
    calls = {"n": 0}
    events = await _run(calls, [{1: DeltaToolCall(name="ghost_tool", json_args='{"q": "x"}', tool_call_id="c0")}])
    assert calls["n"] == 1  # 修正前是 2（SDK 自行重試）
    intents = [e for e in events if e.kind == "tool_intent"]
    assert [(i.tool, i.call_id, i.args_complete) for i in intents] == [("ghost_tool", "c0", True)]
    assert events[-1].finish_reason == "tool_calls"  # 不再是 error
    with pytest.raises(GatewayRejected) as exc:  # 交給 Gateway 拒絕（不在 capability_set）
        ToolGateway().check_intent("ghost_tool", intents[0].args, CAP)
    assert exc.value.code == "capability_missing"


async def test_mixed_known_and_unknown_one_model_call_both_intents():
    calls = {"n": 0}
    events = await _run(calls, [
        {1: DeltaToolCall(name="ghost_tool", json_args='{"q": "x"}', tool_call_id="c0")},
        {2: DeltaToolCall(name="deep_recall", json_args='{"q": "y"}', tool_call_id="c1")},
    ])
    assert calls["n"] == 1
    assert [e.tool for e in events if e.kind == "tool_intent"] == ["ghost_tool", "deep_recall"]
    assert events[-1].finish_reason == "tool_calls"


async def test_known_tool_unchanged():
    calls = {"n": 0}
    events = await _run(calls, [{1: DeltaToolCall(name="deep_recall", json_args='{"q": "y"}', tool_call_id="c1")}])
    assert calls["n"] == 1 and events[-1].finish_reason == "tool_calls"
    assert [e.tool for e in events if e.kind == "tool_intent"] == ["deep_recall"]


async def test_text_only_still_stops():
    calls = {"n": 0}
    events = await _run(calls, ["你好"])
    assert calls["n"] == 1 and events[-1].finish_reason == "stop"
