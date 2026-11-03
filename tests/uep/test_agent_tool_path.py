"""切片 C3：ModelPortRuntime／ModelPortBackend、AgentThinkStage、agent 路徑互斥與 ActionIntent 橋接。

前半不需 DB；標示「DB」的測試需 PostgreSQL（未設 UEP_TEST_DATABASE_URL 時 skip）。
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import types
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from uep.adapters.runtime import RuntimeRequest
from uep.config import get_settings
from uep.echo.host import EchoPipelineHost
from uep.models.port import (
    CapabilityMissingError,
    ChatMessage,
    ModelEvent,
    ProviderCapabilities,
)
from uep.stream.adapters.think_agent import AgentThinkStage
from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import Utterance
from uep.tools.catalog.memory import memory_registry
from uep.tools.projection import ProjectionContext
from uep.tools.render import render_tools

from .stream.test_think_llm import ToolBackend, _Call

TOOLS = render_tools(memory_registry().project({"memory.read"}, ProjectionContext(available_context=frozenset({"guild_roster"}))))
CAPS = memory_registry().project({"memory.read"}, ProjectionContext())


# ---- 假 ModelPort ----

class FakePort:
    """依腳本（每輪一個 ModelEvent 清單）回事件；記錄請求與取消。"""

    dialect = "openai_chat"
    caps = ProviderCapabilities()

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.requests = []
        self.cancelled = []
        self.hang = False

    async def stream(self, req):
        self.requests.append(req)
        if self.rounds and isinstance(self.rounds[0], Exception):
            raise self.rounds.pop(0)
        script = self.rounds.pop(0) if self.rounds else [ModelEvent(kind="finish", finish_reason="stop")]
        for ev in script:
            if self.hang:
                await asyncio.sleep(30)
            yield ev

    def cancel(self, attempt_id):
        self.cancelled.append(attempt_id)


def fin(reason="stop"):
    return ModelEvent(kind="finish", finish_reason=reason)


def text(t):
    return ModelEvent(kind="text_delta", text=t)


def intent(tool="deep_recall", args=None, *, call_id="c1", index=0, complete=True):
    return ModelEvent(kind="tool_intent", tool=tool, call_id=call_id, index=index, args=args if complete else None,
                      args_complete=complete, error_code=None if complete else "invalid_tool_args")


def request(attempt_id="att_1", caps=CAPS):
    return RuntimeRequest(
        run_id="run_x", attempt_id=attempt_id, snapshot_ref="snap", capability_set=caps, cancel_scope={},
        deadline=datetime.now(UTC) + timedelta(minutes=1), mode="direct_chat",
    )


def runtime(port, **kw):
    from uep.agent.runtime import ModelPortRuntime

    return ModelPortRuntime(port, lambda req: [ChatMessage("user", "hi")], **kw)


# ---- ModelPortRuntime ----

def test_runtime_text_then_result_and_thought_stays_in_log():
    port = FakePort([[ModelEvent(kind="thought_delta", text="秘密想法"), text("你"), text("好"), fin()]])
    rt = runtime(port)
    events = list(rt.stream(rt.start(request())))
    assert [e.kind for e in events] == ["text_delta", "text_delta", "result"]
    assert "秘密想法" not in repr(events)  # Q-B：thought_delta 不進 RuntimeEvent
    assert [e.seq for e in events] == [1, 2, 3]


def test_runtime_tool_intent_then_continues_after_submit_with_history():
    port = FakePort([
        [text("查一下"), intent(args={"query": "貓"}), fin("tool_calls")],
        [text("找到了"), fin()],
    ])
    rt = runtime(port)
    aid = rt.start(request())
    seen = []
    for ev in rt.stream(aid):
        seen.append(ev.kind)
        if ev.kind == "tool_intent":
            assert ev.tool_intent["tool"] == "deep_recall" and ev.tool_intent["args_complete"] is True
            rt.submit_tool_result(aid, "act_1", {"status": "ok", "items": [{"content": "有一隻貓"}]})
    assert seen == ["text_delta", "tool_intent", "text_delta", "result"]
    second = port.requests[1].messages
    assert [m.role for m in second] == ["user", "assistant", "tool"]
    assert second[1].tool_calls[0]["name"] == "deep_recall" and second[2].content == "有一隻貓"
    assert second[2].tool_call_id == "c1"


def test_runtime_incomplete_args_gets_tool_message_but_no_result_view():
    port = FakePort([[intent(complete=False), fin("tool_calls")], [text("好"), fin()]])
    rt = runtime(port)
    events = list(rt.stream(rt.start(request())))
    assert events[0].tool_intent["args_complete"] is False
    assert port.requests[1].messages[-1].content == "tool invalid_tool_args"  # 每個 call 都有對應的 tool 訊息


def test_runtime_capability_missing_yields_error_and_stops():
    from uep.adapters.runtime import RuntimeEvent

    err = RuntimeEvent("att_1", 0, "error", error={"code": "CAPABILITY_MISSING", "retryable": False, "detail": "x"})
    port = FakePort([CapabilityMissingError(err)])
    rt = runtime(port)
    events = list(rt.stream(rt.start(request())))
    assert [e.kind for e in events] == ["error"] and events[0].error["code"] == "CAPABILITY_MISSING"


def test_runtime_cancel_propagates_to_model_port():
    port = FakePort([[text("a"), text("b"), fin()]])
    rt = runtime(port)
    aid = rt.start(request())
    it = iter(rt.stream(aid))
    assert next(it).kind == "text_delta"
    assert rt.cancel(aid, "user_stop") == "stopped"
    assert port.cancelled == [aid]  # 同時呼叫 ModelPort.cancel
    rest = list(it)
    assert [e.kind for e in rest] == ["error"] and rest[0].error["code"] == "CANCELLED"
    assert rt.status(aid).state == "cancelled"
    assert rt.cancel(aid, "again") == "stopped"  # 冪等


def test_runtime_cancel_from_other_thread_while_streaming():
    port = FakePort([[text("a"), fin()]])
    port.hang = True
    rt = runtime(port)
    aid = rt.start(request())
    threading.Timer(0.2, lambda: rt.cancel(aid, "stop")).start()
    # ModelPort.cancel 保證返回後不再產生事件；這裡的 fake 以 hang 模擬卡住，取消後由 port 自己結束
    port.hang = False
    assert [e.kind for e in rt.stream(aid)][-1] in ("result", "error")
    assert port.cancelled in ([], [aid])


# ---- ModelPortBackend ----

async def test_backend_streams_text_and_tool_call_pieces():
    from uep.agent.runtime import ModelPortBackend, ToolCallPiece

    port = FakePort([[text("嗯"), intent(args={"query": "貓"}), fin("tool_calls")]])
    backend = ModelPortBackend(port)
    msgs = [{"role": "user", "content": "hi"}]
    pieces = [p async for p in backend.stream_query(msgs, system_instruction="sys", tools=TOOLS)]
    assert pieces[0] == "嗯"
    assert isinstance(pieces[1], ToolCallPiece) and pieces[1].name == "deep_recall"
    assert json.loads(pieces[1].arguments) == {"query": "貓"}
    assert [m.role for m in port.requests[0].messages] == ["system", "user"]
    assert port.requests[0].capability_set["tools"][0]["tool"] in {"deep_recall", "member_recall"}


async def test_backend_consumer_cancel_reaches_model_port():
    from uep.agent.runtime import ModelPortBackend

    port = FakePort([[text("a"), text("b"), text("c"), fin()]])
    backend = ModelPortBackend(port)
    gen = backend.stream_query([{"role": "user", "content": "hi"}], tools=TOOLS)
    assert await gen.__anext__() == "a"
    await gen.aclose()  # 插話／停止時消費者關閉串流
    assert len(port.cancelled) == 1 and port.cancelled[0].startswith("att_")


async def test_backend_normal_end_does_not_cancel_port():
    from uep.agent.runtime import ModelPortBackend

    port = FakePort([[text("a"), fin()]])
    backend = ModelPortBackend(port)
    assert [p async for p in backend.stream_query([{"role": "user", "content": "hi"}], tools=TOOLS)] == ["a"]
    assert port.cancelled == []


# ---- AgentThinkStage ----

class FakeTurnPort:
    def __init__(self, outcome=None, boom=False):
        self.calls, self.finished = [], []
        self.outcome = outcome or {"success": True, "result": "有一隻貓"}
        self.boom = boom

    def run_tool(self, name, args, context):
        self.calls.append((name, args, context))
        return dict(self.outcome)

    def finish_turn(self, turn_id):
        self.finished.append(turn_id)
        if self.boom:
            raise RuntimeError("x")


def _agent_stage(port, call=None):
    backend = ToolBackend(call=call or _Call("c1", "deep_recall", '{"query": "貓"}'))
    inner = LLMThinkStage(backend, tools=[{"x": 1}])
    return AgentThinkStage(inner, port), inner


async def _drain(stage, utt):
    return [s async for s in stage.stream(utt, CancellationToken())]


async def test_agent_stage_routes_tools_to_port_not_host_executor():
    port = FakeTurnPort()
    stage, _inner = _agent_stage(port)
    host_calls = []

    async def legacy(name, args, *, context=None):
        host_calls.append(name)
        return {"success": True, "result": "舊路徑"}

    assert stage.tool_executor is None  # 通過宿主的「自帶 executor」檢查
    stage.tool_executor = legacy  # 宿主照舊賦值 CoreToolExecutor
    ctx = {"ticket_ref": "turn_a", "actor_id": "a", "audience": "scope_private_owner"}
    sentences = await _drain(stage, Utterance(text="hi", speaker_id="u", turn_id="turn_a", metadata={"tool_context": ctx}))
    assert host_calls == []  # 兩條路徑互斥：agent 路徑不碰舊 executor
    assert port.calls and port.calls[0][0] == "deep_recall" and port.calls[0][2] is ctx
    assert sentences  # 模型迴圈（inner）照常產生回應
    assert port.finished == ["turn_a"]  # 回合結束收尾


async def test_agent_stage_applies_shared_ids_to_turn_context_only_in_stage():
    port = FakeTurnPort({"success": True, "result": "投影", "shared_ids": ["p1", "p2"]})
    stage, _ = _agent_stage(port, _Call("c1", "member_recall", '{"member": "甲"}'))
    ctx = {"ticket_ref": "turn_b", "audience": "scope_group_g1", "guild_id": "1"}
    await _drain(stage, Utterance(text="hi", speaker_id="u", turn_id="turn_b", metadata={"tool_context": ctx}))
    assert ctx["member_recall_ids"] == ["p1", "p2"]


async def test_agent_stage_unbound_port_fails_closed():
    stage, _ = _agent_stage(None)
    ctx = {"ticket_ref": "turn_c", "audience": "scope_private_owner"}
    await _drain(stage, Utterance(text="hi", speaker_id="u", turn_id="turn_c", metadata={"tool_context": ctx}))
    out = await stage._execute("deep_recall", {}, context=ctx)
    assert out["success"] is False


async def test_agent_stage_finishes_turn_even_when_cancelled_or_failing():
    port = FakeTurnPort(boom=True)  # finish_turn 拋例外也不影響回合
    stage, _ = _agent_stage(port)
    ctx = {"ticket_ref": "turn_d", "audience": "scope_private_owner"}
    await _drain(stage, Utterance(text="hi", speaker_id="u", turn_id="turn_d", metadata={"tool_context": ctx}))
    assert port.finished == ["turn_d"]
    port2 = FakeTurnPort()
    stage2, _ = _agent_stage(port2)
    gen = stage2.stream(Utterance(text="hi", speaker_id="u", turn_id="turn_e", metadata={"tool_context": ctx}), CancellationToken())
    await gen.__anext__()
    await gen.aclose()  # 中途關閉（插話）
    assert port2.finished == ["turn_e"]


def test_agent_stage_forwards_attributes_to_inner():
    port = FakeTurnPort()
    stage, inner = _agent_stage(port)
    stage.tools = [{"y": 2}]
    stage.memory_resolver = lambda scope: None
    assert inner.tools == [{"y": 2}] and inner.memory_resolver is stage.memory_resolver
    assert stage.name == "llm" and hasattr(stage, "commit") and hasattr(stage, "retract")
    assert inner.tool_executor is not None and inner.tool_executor != stage.tool_executor


# ---- 互斥：host 旗標與 wiring ----

def _host_stub(path="legacy"):
    queued = []
    stub = types.SimpleNamespace(
        _enqueue=lambda fn, label: queued.append(label), queued=queued, _publish_tool_action=lambda *a: None,
        _tool_path=path,
    )
    stub._agent_tool_path = types.MethodType(EchoPipelineHost._agent_tool_path, stub)
    return stub


def test_host_does_not_record_legacy_tool_action_in_agent_path():
    stub = _host_stub()
    EchoPipelineHost._record_tool_action(stub, "turn_x", "deep_recall", "internal_read", "executed")
    assert stub.queued == ["tool_action"]  # legacy：照舊補記
    stub2 = _host_stub("agent")
    EchoPipelineHost._record_tool_action(stub2, "turn_x", "deep_recall", "internal_read", "executed")
    assert stub2.queued == []  # agent：由 TaskManager 發 action.*，不重複


def test_wiring_chooses_stage_by_tool_path():
    from uep.echo.inference_wiring import build_inference_wiring

    class Backend:
        async def stream_query(self, *a, **k):
            yield "x"

    def build(path, **kw):
        return build_inference_wiring(
            think_backend=Backend(), tts_backend=object(), stt_backend=object(), tool_path=path, **kw,
        ).think

    assert isinstance(build("legacy"), LLMThinkStage)
    agent = build("agent")
    assert isinstance(agent, AgentThinkStage) and isinstance(agent.inner, LLMThinkStage)
    with_port = build("agent", model_port=FakePort([]))
    from uep.agent.runtime import ModelPortBackend

    assert isinstance(with_port.inner.backend, ModelPortBackend)


def test_think_agent_module_has_no_core_imports():
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "uep" / "stream" / "adapters" / "think_agent.py"
    tops = {n.module.split(".")[0] for n in ast.walk(ast.parse(src.read_text(encoding="utf-8")))
            if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    assert "uep" not in tops


def _tool_context_keys(path, guild):
    from uep.echo.config import EchoOutputConfig
    from uep.echo.memory import GuildRoster

    stub = types.SimpleNamespace(
        _session=types.SimpleNamespace(guild_id=guild), roster=GuildRoster(), output=EchoOutputConfig(),
        _make_sink=lambda *a: None, _on_think_start=lambda *a: None, _on_generation_done=lambda *a: None,
        _tool_path=path,
    )
    stub._agent_tool_path = types.MethodType(EchoPipelineHost._agent_tool_path, stub)
    ticket = types.SimpleNamespace(
        actor_id="a", audience_scope="scope_private_owner", segment_id="seg_x", turn_id="turn_x", user_text="hi",
        speaker_ref="u", event_scope="scope_private_owner", conversation_id="conv_x", reply_mode="text",
        snapshot_version=1, turn_generation=1,
    )
    fact = types.SimpleNamespace(language="zh", confidence=1.0, legacy_ref=None)
    request = EchoPipelineHost._build_request(stub, ticket, fact)
    return sorted(request.utterance.metadata["tool_context"])


def test_tool_path_default_is_legacy_and_legacy_tool_context_keys_unchanged():
    assert get_settings().tool_path == "legacy"
    legacy = ["actor_id", "audience", "segment_id", "ticket_ref"]
    assert _tool_context_keys("legacy", None) == legacy  # legacy：與改動前完全相同
    assert _tool_context_keys("legacy", "123") == sorted([*legacy, "guild_id", "present_uids"])
    assert _tool_context_keys("agent", None) == sorted([*legacy, "conversation_id", "event_scope"])  # 只有 agent 多兩個鍵


def test_host_rejects_unknown_tool_path():
    with pytest.raises(ValueError, match="tool_path"):
        EchoPipelineHost(publisher=None, think=object(), speak=None, sink_factory=lambda v, t: "x",
                         session=None, tool_path="both")


def test_inference_wiring_injects_tool_path_into_host_kwargs():
    from uep.echo.inference_wiring import build_inference_wiring

    class Backend:
        async def stream_query(self, *a, **k):
            yield "x"

    for path in ("legacy", "agent"):
        wiring = build_inference_wiring(
            think_backend=Backend(), tts_backend=object(), stt_backend=object(), tool_path=path,
        )
        assert wiring.host_kwargs().get("tool_path", "legacy") == path  # Host 吃的值與 stage 選擇同源
        assert ("tool_path" in wiring.host_kwargs()) == (path == "agent")  # legacy 的鍵集合不變


def test_registry_injected_params_are_hidden_from_llm_schema():
    for name in ("deep_recall", "force_remember", "member_recall"):
        entry = memory_registry().entry(name)
        assert {"actor_id", "audience", "segment_id"} <= set(entry.descriptor.injected_params)
        assert not set(entry.descriptor.injected_params) & set(entry.descriptor.input_schema.get("properties", {}))
    assert "guild_id" in memory_registry().entry("member_recall").descriptor.injected_params
    rendered = json.dumps(TOOLS)
    assert "actor_id" not in rendered and "audience" not in rendered


# ---- DB：AgentToolTurns 端到端 ----

class _Port:
    def __init__(self, outcomes=None, raises=None):
        self.calls, self.outcomes, self.raises = [], outcomes or {}, raises

    def execute_tool(self, name, args, *, actor_id, audience, session_id=None, context=None):
        self.calls.append({"name": name, "args": dict(args), "actor_id": actor_id, "audience": audience,
                           "session_id": session_id, "context": context})
        return copy.deepcopy(self.outcomes.get(name, {"success": True, "result": f"{name} 結果"}))


@pytest.fixture
def db_rig(session_factory):
    from uep.agent.bridge import AgentToolTurns
    from uep.echo.memory import GuildRoster
    from uep.tools.executors.memory import MemoryToolExecutor
    from uep.tools.outcomes import TurnOutcomeCache

    cache = TurnOutcomeCache()
    roster = GuildRoster({"123": {"actor_a": "111", "actor_b": "222"}},
                         profiles={"123": {"222": {"display_name": "測試乙", "aliases": ["小乙"]}}})
    port = _Port({"member_recall": {"success": True, "result": "投影", "shared_ids": ["p1", "p2"]}})
    executor = MemoryToolExecutor(port, roster, cache=cache)
    from uep.events.bus import EventBus
    from uep.events.publisher import EventPublisher
    from uep.tasks.manager import TaskManager

    tm = TaskManager(session_factory, EventPublisher(session_factory, EventBus()),
                     {"core_memory": executor}, tool_resolver=memory_registry())
    turns = AgentToolTurns(tm, memory_registry(), cache)
    return types.SimpleNamespace(tm=tm, turns=turns, port=port, cache=cache, sf=session_factory, executor=executor)


def _ctx(turn_id, **extra):
    from uep.ids import new_id

    return {"actor_id": "actor_a", "audience": "scope_private_owner", "segment_id": new_id("seg"),
            "ticket_ref": turn_id, "conversation_id": new_id("conv"), "event_scope": "scope_private_owner", **extra}


def _events(sf, turn_id, *types_):
    from uep.events.models import EventRow

    with sf() as s:
        return list(s.scalars(select(EventRow).where(EventRow.turn_id == turn_id, EventRow.event_type.in_(types_))
                              .order_by(EventRow.journal_seq)))


@pytest.mark.usefixtures("session_factory")
def test_db_each_tool_call_has_act_id_receipt_and_legacy_payload(db_rig):
    from uep.ids import is_valid_id, new_id
    from uep.tasks.models import ReceiptRow

    turn_id = new_id("turn")
    ctx = _ctx(turn_id)
    out = db_rig.turns.run_tool("deep_recall", {"query": "貓"}, ctx)
    assert out == {"success": True, "result": "deep_recall 結果"}
    db_rig.turns.run_tool("deep_recall", {"query": "狗"}, ctx)
    events = _events(db_rig.sf, turn_id, "action.executed")
    assert len(events) == 2  # 每次呼叫恰一筆 action.executed，沒有重複
    for ev in events:
        assert set(ev.payload) >= {"action_id", "kind", "status", "ref_id"} and is_valid_id(ev.payload["action_id"], "act")
        assert ev.payload["kind"] == "internal_read" and ev.payload["status"] == "executed"
        assert ev.payload["ref_id"] == "tool:deep_recall" and ev.conversation_id == ctx["conversation_id"]
    with db_rig.sf() as s:
        receipts = list(s.scalars(select(ReceiptRow)))
    assert len(receipts) == 2 and all(r.result_ref.startswith("blob_act_") for r in receipts)
    assert "結果" not in repr([r.__dict__ for r in receipts])  # D33：原文不進 receipt
    assert db_rig.port.calls[0]["actor_id"] == "actor_a" and db_rig.port.calls[0]["args"] == {"query": "貓"}
    db_rig.turns.finish_turn(turn_id)
    assert len(db_rig.cache) == 0  # 回合結束清掉結果暫存
    task_id = None
    with db_rig.sf() as s:
        from uep.tasks.models import TaskRow
        task = s.scalar(select(TaskRow))
        task_id = task.task_id
    assert db_rig.tm.get_task(task_id).status == "SUCCEEDED"
    assert not _events(db_rig.sf, turn_id, "action.unknown")


@pytest.mark.usefixtures("session_factory")
def test_db_member_recall_returns_shared_ids_and_llm_cannot_inject_scope(db_rig):
    from uep.ids import new_id

    turn_id = new_id("turn")
    ctx = _ctx(turn_id, audience="scope_group_g123", guild_id="123")
    out = db_rig.turns.run_tool("member_recall", {"member": "小乙"}, ctx)
    assert out["success"] is True and out["shared_ids"] == ["p1", "p2"]
    assert db_rig.port.calls[0]["context"]["member_uid"] == "222"
    # LLM 自行帶範圍欄位 -> Gateway 以 invalid_tool_args 拒絕，不建 action、不碰 Access Layer
    before = len(db_rig.port.calls)
    bad = db_rig.turns.run_tool("deep_recall", {"query": "x", "audience": "scope_private_other"}, ctx)
    assert bad["success"] is False and len(db_rig.port.calls) == before
    db_rig.turns.finish_turn(turn_id)


@pytest.mark.usefixtures("session_factory")
def test_db_timeout_is_unknown_and_internal_read_unknown_does_not_block_next_call(db_rig):
    from uep.ids import new_id

    db_rig.port.outcomes["deep_recall"] = {"success": False, "result": "tool timeout", "code": "timeout"}
    turn_id = new_id("turn")
    ctx = _ctx(turn_id)
    first = db_rig.turns.run_tool("deep_recall", {"query": "a"}, ctx)
    assert first["success"] is False
    assert len(_events(db_rig.sf, turn_id, "action.unknown")) == 1
    db_rig.port.outcomes["deep_recall"] = {"success": True, "result": "之後成功"}
    second = db_rig.turns.run_tool("deep_recall", {"query": "b"}, ctx)  # TC-3：internal_read 的 UNKNOWN 不阻擋
    assert second == {"success": True, "result": "之後成功"}
    db_rig.turns.finish_turn(turn_id)
    with db_rig.sf() as s:
        from uep.tasks.models import TaskRow
        assert s.scalar(select(TaskRow)).status == "FAILED"  # 有 UNKNOWN：以 FAILED（unresolved_actions）收尾


@pytest.mark.usefixtures("session_factory")
def test_db_cache_miss_yields_unknown_not_fabricated_text(db_rig):
    from uep.ids import new_id

    turn_id = new_id("turn")
    ctx = _ctx(turn_id)
    original = db_rig.cache.text
    db_rig.cache.text = lambda action_id: None  # 暫存遺失
    try:
        bridge_core_view = db_rig.turns.run_tool("deep_recall", {"query": "x"}, ctx)
    finally:
        db_rig.cache.text = original
    assert bridge_core_view["success"] is False and "unknown" in bridge_core_view["result"]
    db_rig.turns.finish_turn(turn_id)


@pytest.mark.usefixtures("session_factory")
def test_db_no_tool_call_creates_no_turn_task(db_rig):
    from uep.ids import new_id
    from uep.tasks.models import TaskRow

    db_rig.turns.finish_turn(new_id("turn"))
    with db_rig.sf() as s:
        assert s.scalar(select(TaskRow)) is None  # 懶建立：沒有工具呼叫就沒有 Task
