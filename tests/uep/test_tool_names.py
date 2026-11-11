"""工具名稱的 provider 邊界轉譯（`.` <-> `__`）：round-trip、碰撞與歧義 fail-closed、表外名稱不被解碼、
history 回送也編碼，以及 ``decision.consult`` 經 Port 能被呼叫（離線，用 SDK 的 FunctionModel）。"""
from __future__ import annotations

import pytest
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from uep.models import ChatMessage, ModelRequest
from uep.models.pydantic_ai_port import PydanticAIModelPort
from uep.models.tool_names import ToolNameError, decode_name, encode_name, wire_map
from uep.tools.catalog.decision import (
    CONSULT_TAG,
    CONSULT_TOOL,
    INJECTED_CONSULT,
    consult_entry,
)
from uep.tools.gateway import ToolGateway
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry

REGISTRY = ToolRegistry.build([consult_entry()])
CAP = REGISTRY.project({CONSULT_TAG}, ProjectionContext())


def test_encode_decode_round_trip_and_provider_safe_charset():
    for name in ("decision.consult", "lore.recall", "desktop.file_read", "deep_recall"):
        wire = encode_name(name)
        assert "." not in wire and wire_map([name])[wire] == name and decode_name(wire, wire_map([name])) == name


def test_collision_and_ambiguity_fail_closed():
    with pytest.raises(ToolNameError, match="碰撞|__"):
        wire_map(["a.b", "a__b"])
    with pytest.raises(ToolNameError, match="__"):
        encode_name("x__y")  # 名稱本身含分隔符：解碼會歧義
    with pytest.raises(ToolNameError):
        encode_name("bad name")


def test_decode_only_resolves_names_in_the_mapping():
    mapping = wire_map(["decision.consult"])
    assert decode_name("decision__consult", mapping) == "decision.consult"
    assert decode_name("ghost__tool", mapping) == "ghost__tool"  # 表外原樣，交給 Gateway 拒絕


async def _run(script, cap=CAP, messages=None, seen=None):
    async def fn(msgs, info):
        if seen is not None:
            seen["tools"] = [t.name for t in info.function_tools]
            seen["history"] = [p.tool_name for m in msgs for p in m.parts if isinstance(p, ToolCallPart)]
        for item in script:
            yield item

    port = PydanticAIModelPort("openai_chat", model=FunctionModel(stream_function=fn))
    return [e async for e in port.stream(ModelRequest("a1", tuple(messages or (ChatMessage("user", "hi"),)), cap))]


async def test_dotted_tool_is_sent_encoded_and_comes_back_decoded_for_the_gateway():
    seen: dict = {}
    events = await _run(
        [{1: DeltaToolCall(name="decision__consult", json_args='{"sub_kind": "speak"}', tool_call_id="c1")}], seen=seen,
    )
    assert seen["tools"] == ["decision__consult"]  # provider 只看到安全名稱
    (intent,) = [e for e in events if e.kind == "tool_intent"]
    assert intent.tool == CONSULT_TOOL and events[-1].finish_reason == "tool_calls"
    injected = {name: "x" for name in INJECTED_CONSULT}  # decision.consult 能通過 Gateway 的 capability 與 schema 檢查
    assert ToolGateway(REGISTRY).check_intent(intent.tool, intent.args, CAP, injected=injected)["sub_kind"] == "speak"


async def test_unknown_encoded_name_is_not_decoded_and_is_rejected_by_the_gateway():
    events = await _run([{1: DeltaToolCall(name="ghost__tool", json_args="{}", tool_call_id="c1")}])
    (intent,) = [e for e in events if e.kind == "tool_intent"]
    assert intent.tool == "ghost__tool"
    from uep.tools.gateway import GatewayRejected

    with pytest.raises(GatewayRejected) as exc:
        ToolGateway(REGISTRY).check_intent(intent.tool, intent.args, CAP)
    assert exc.value.code == "capability_missing"


async def test_history_tool_calls_are_encoded_when_sent_back():
    seen: dict = {}
    history = (
        ChatMessage("user", "hi"),
        ChatMessage("assistant", "", tool_calls=({"id": "c1", "name": CONSULT_TOOL, "args": {"sub_kind": "speak"}},)),
        ChatMessage("tool", "{}", tool_call_id="c1"),
    )
    await _run(["好"], messages=history, seen=seen)
    assert seen["history"] == ["decision__consult"]


async def test_colliding_capability_set_is_refused_before_any_request():
    def cap(*names):
        return {"tools": [{"tool": n, "description": "d", "input_schema": {"type": "object", "properties": {}}} for n in names]}

    with pytest.raises(ToolNameError):
        await _run(["x"], cap=cap("a.b", "a__b"))


def test_assembly_check_uses_the_same_mapping_over_registry_entries():
    wire_map(REGISTRY.entries())  # 目前目錄可轉譯
