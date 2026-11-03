"""S1-2 結構判定（離線）：想／說只看 SDK 的 part 歸屬，不看內容；無法確定歸屬時 fail-closed。

字串比對不能判通過／失敗：模型打草稿時 thought 內容可以和 text 完全相同，那是合法情境。
"""
from __future__ import annotations

import random

from pydantic_ai import (
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
)
from pydantic_ai.models.function import AgentInfo, DeltaThinkingPart, FunctionModel

from uep.models import ChatMessage, ModelRequest
from uep.models.pydantic_ai_port import PydanticAIModelPort, _PartRouter

CAP = {"tools": []}


def _router() -> tuple[PydanticAIModelPort, _PartRouter]:
    async def fn(messages, info):
        yield "x"

    port = PydanticAIModelPort("openai_chat", model=FunctionModel(stream_function=fn))
    return port, _PartRouter(port)


def _route(router: _PartRouter, events) -> list:
    out = []
    for ev in events:
        out += router.route(ev)
    return out


def test_interleaved_thought_and_text_follow_part_ownership():
    port, r = _router()
    events = [
        PartStartEvent(index=0, part=ThinkingPart("想A")),
        PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="想B")),
        PartStartEvent(index=1, part=TextPart("說A")),
        PartDeltaEvent(index=1, delta=TextPartDelta(content_delta="說B")),
        PartStartEvent(index=2, part=ThinkingPart("想C")),
        PartDeltaEvent(index=2, delta=ThinkingPartDelta(content_delta="想D")),
        PartDeltaEvent(index=1, delta=TextPartDelta(content_delta="說C")),  # 回到先前的 text part
    ]
    out = _route(r, events)
    assert [(e.kind, e.text) for e in out] == [
        ("thought_delta", "想A"), ("thought_delta", "想B"), ("text_delta", "說A"), ("text_delta", "說B"),
        ("thought_delta", "想C"), ("thought_delta", "想D"), ("text_delta", "說C"),
    ]
    assert port.dropped_deltas == []
    assert all((src, kind) in {("thinking", "thought_delta"), ("text", "text_delta")} for src, kind in port.audit)


def test_identical_thought_and_text_content_is_legal_drafting():
    port, r = _router()
    answer = "123 乘以 47 等於 5781。"
    out = _route(r, [
        PartStartEvent(index=0, part=ThinkingPart(answer)),
        PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=answer)),
        PartStartEvent(index=1, part=TextPart(answer)),
    ])
    assert [e.kind for e in out] == ["thought_delta", "thought_delta", "text_delta"]
    assert "".join(e.text for e in out if e.kind == "text_delta") == answer  # text 只有 text part 的內容
    assert port.dropped_deltas == []


def test_missing_part_start_is_dropped_never_text():
    port, r = _router()
    out = _route(r, [
        PartDeltaEvent(index=3, delta=TextPartDelta(content_delta="孤兒")),
        PartDeltaEvent(index=3, delta=ThinkingPartDelta(content_delta="孤兒想")),
    ])
    assert out == []
    assert len(port.dropped_deltas) == 2 and "缺 part_start" in port.dropped_deltas[0]


def test_index_jump_is_dropped():
    port, r = _router()
    out = _route(r, [
        PartStartEvent(index=0, part=ThinkingPart("想")),
        PartDeltaEvent(index=5, delta=TextPartDelta(content_delta="跳號")),
    ])
    assert [e.kind for e in out] == ["thought_delta"]
    assert len(port.dropped_deltas) == 1


def test_delta_type_mismatching_owner_is_dropped_in_both_directions():
    port, r = _router()
    out = _route(r, [
        PartStartEvent(index=0, part=ThinkingPart("想")),
        PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="想的 index 卻來 text delta")),
        PartStartEvent(index=1, part=TextPart("說")),
        PartDeltaEvent(index=1, delta=ThinkingPartDelta(content_delta="說的 index 卻來 thinking delta")),
        PartStartEvent(index=2, part=ToolCallPart("deep_recall", "{", "c1")),
        PartDeltaEvent(index=2, delta=TextPartDelta(content_delta="工具 index 卻來 text")),
    ])
    assert [e.kind for e in out] == ["thought_delta", "text_delta", "tool_call_delta"]
    assert len(port.dropped_deltas) == 3


def test_repeated_part_start_replaces_ownership():
    port, r = _router()  # SDK 規定：同 index 的新 PartStartEvent 完全取代舊的
    out = _route(r, [
        PartStartEvent(index=0, part=ThinkingPart("想")),
        PartStartEvent(index=0, part=TextPart("改成說")),
        PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="續")),
        PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="不該再收")),
    ])
    assert [(e.kind, e.text) for e in out] == [("thought_delta", "想"), ("text_delta", "改成說"), ("text_delta", "續")]
    assert len(port.dropped_deltas) == 1


def test_tool_call_deltas_stay_tool_call():
    port, r = _router()
    out = _route(r, [
        PartStartEvent(index=1, part=ToolCallPart("deep_recall", '{"q', "c1")),
        PartDeltaEvent(index=1, delta=ToolCallPartDelta(args_delta='": 1}')),
    ])
    assert [e.kind for e in out] == ["tool_call_delta", "tool_call_delta"]
    assert "".join(e.args_fragment for e in out) == '{"q": 1}'


def test_random_streams_thinking_deltas_never_become_text():
    """隨機交錯：text_delta 的內容必須恰為所有 text part 內容的串接，thought 同理；異常 delta 一律丟棄。"""
    rng = random.Random(20261103)
    for _ in range(200):
        port, r = _router()
        owners: dict[int, str] = {}
        want = {"text": [], "thinking": []}
        events = []
        for step in range(rng.randint(1, 30)):
            idx = rng.randint(0, 4)
            roll = rng.random()
            if idx not in owners or roll < 0.15:  # 起一個新 part
                kind = rng.choice(["text", "thinking"])
                owners[idx] = kind
                tok = f"[{kind[0]}{step}]"
                events.append(PartStartEvent(index=idx, part=TextPart(tok) if kind == "text" else ThinkingPart(tok)))
                want[kind].append(tok)
            elif roll < 0.85:  # 合法 delta
                tok = f"<{owners[idx][0]}{step}>"
                delta = TextPartDelta(content_delta=tok) if owners[idx] == "text" else ThinkingPartDelta(content_delta=tok)
                events.append(PartDeltaEvent(index=idx, delta=delta))
                want[owners[idx]].append(tok)
            else:  # 型別不符的 delta：必須被丟棄，內容不得出現在任何輸出
                bad = ThinkingPartDelta(content_delta="!BAD!") if owners[idx] == "text" else TextPartDelta(content_delta="!BAD!")
                events.append(PartDeltaEvent(index=idx, delta=bad))
        out = _route(r, events)
        assert "".join(e.text for e in out if e.kind == "text_delta") == "".join(want["text"])
        assert "".join(e.text for e in out if e.kind == "thought_delta") == "".join(want["thinking"])
        assert not any("!BAD!" in e.text for e in out)


async def test_end_to_end_function_model_interleaved_and_identical_content():
    answer = "答案是 5781"

    async def fn(messages, info: AgentInfo):
        yield {0: DeltaThinkingPart(content=answer)}  # 打草稿：和最終 text 一字不差
        yield {0: DeltaThinkingPart(content="。再確認")}
        yield answer

    port = PydanticAIModelPort("openai_chat", model=FunctionModel(stream_function=fn))
    events = [e async for e in port.stream(ModelRequest("a", [ChatMessage("user", "x")], CAP))]
    assert "".join(e.text for e in events if e.kind == "thought_delta") == answer + "。再確認"
    assert "".join(e.text for e in events if e.kind == "text_delta") == answer
    assert port.dropped_deltas == []
    assert {pair for pair in port.audit} <= {("thinking", "thought_delta"), ("text", "text_delta")}
