"""切片 D1 對既有路徑的回歸：``ModelPortRuntime`` 的 legacy 事件順序與 ``ModelPortBackend`` 的對話迴圈形狀不變。不需 DB。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from uep.adapters.runtime import RuntimeRequest
from uep.agent.runtime import ModelPortBackend, ModelPortRuntime, ToolCallPiece
from uep.models import ChatMessage
from uep.models.pydantic_ai_port import PydanticAIModelPort

CAP = {"tools": [{
    "tool": "deep_recall", "version": 1, "description": "d", "variant": None,
    "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
}]}


async def _fn(messages, info: AgentInfo):
    yield "我查一下"
    yield {1: DeltaToolCall(name="deep_recall", json_args='{"q": "貓"}', tool_call_id="c0")}
    yield {2: DeltaToolCall(name="deep_recall", json_args='{"q": "狗"}', tool_call_id="c1")}


def _port() -> PydanticAIModelPort:
    return PydanticAIModelPort("openai_chat", model=FunctionModel(stream_function=_fn))


def test_runtime_without_continue_keeps_event_order_and_legacy_intent_keys():
    runtime = ModelPortRuntime(_port(), lambda req: [ChatMessage("user", "hi")], continue_after_tools=False)
    request = RuntimeRequest(
        run_id="", attempt_id="att_1", snapshot_ref="s", capability_set=CAP, cancel_scope={},
        deadline=datetime.now(UTC) + timedelta(minutes=1), mode="direct_chat",
    )
    runtime.start(request)
    events = list(runtime.stream("att_1"))
    assert [e.kind for e in events] == ["text_delta", "tool_intent", "tool_intent"]  # 不產生 result／其他事件
    first = events[1].tool_intent
    # 舊鍵全部保留且值不變；新增的 batch_size 是純新增
    assert {k: first[k] for k in ("tool", "args", "args_complete", "call_id", "index", "side_effect_hint")} == {
        "tool": "deep_recall", "args": {"q": "貓"}, "args_complete": True, "call_id": "c0", "index": 1,
        "side_effect_hint": None,
    }
    assert first["batch_size"] == 2 and events[2].tool_intent["call_id"] == "c1"
    assert runtime.status("att_1").state == "completed"


async def test_backend_stream_query_still_yields_text_then_tool_pieces():
    backend = ModelPortBackend(_port())
    tools = [{"type": "function", "function": {
        "name": "deep_recall", "description": "d", "parameters": CAP["tools"][0]["input_schema"]}}]
    pieces = [p async for p in backend.stream_query([{"role": "user", "content": "hi"}], None, tools=tools)]
    assert pieces[0] == "我查一下"
    assert [p for p in pieces[1:] if isinstance(p, ToolCallPiece)] == [
        ToolCallPiece("c0", "deep_recall", '{"q": "貓"}'), ToolCallPiece("c1", "deep_recall", '{"q": "狗"}'),
    ]
