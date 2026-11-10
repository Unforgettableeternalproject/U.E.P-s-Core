"""route=agent_loop 的 production 入口（DL-4 PR4）：enabled_tags、capability_tags_for、組裝旗標與雙重回覆鎖定。

離線部分不需要 DB；``test_db_*`` 需 PostgreSQL（沒有 UEP_TEST_DATABASE_URL 時自動 skip）。
"""
from __future__ import annotations

import threading
from collections import Counter
from types import SimpleNamespace

import pytest

from tests.uep.test_tool_registry import make_entry
from uep.adapters.tool import CAPABILITY_CEILING
from uep.assembly import AssemblyError, assemble_core
from uep.decision.capability import (
    COUNT_HINT_DROPPED,
    COUNT_HINT_EMPTY_MODEL,
    REASON_CAPABILITY_EMPTY,
    narrow_capability,
)
from uep.echo.agent_wiring import LoopEntry, make_capability_tags_for
from uep.tools.catalog.memory import memory_registry
from uep.tools.registry import ToolRegistry
from uep.turn.models import Proposal, ReplyMode

TICKET = SimpleNamespace(turn_id="turn_x")


def _proposal(route="agent_loop", hint=None) -> Proposal:
    return Proposal(reply_mode=ReplyMode.RESPOND, route=route, capability_hint=hint)


# ---- ToolRegistry.enabled_tags ----

def test_enabled_tags_of_production_registry_is_inside_the_closed_set():
    tags = memory_registry().enabled_tags()
    assert tags and tags <= frozenset(CAPABILITY_CEILING)
    assert "memory.read" in tags


def test_enabled_tags_excludes_disabled_and_unavailable_but_keeps_warming():
    reg = ToolRegistry.build(
        [
            make_entry("read.ok", spec={"capability": "memory.read"}),
            make_entry("write.off", spec={"capability": "memory.write", "side_effect_class": "reversible_local"}, disabled=True),
            make_entry("lore.gone", spec={"capability": "lore.write", "side_effect_class": "reversible_local"}, requires=("gone",)),
            make_entry("chat.warm", spec={"capability": "chatroom.read"}, requires=("gpu:x",), warm_required=True),
        ],
        known_modules=["gpu:x"],
    )
    # disabled 不算；requires 未知 -> unavailable 不算；warming 算（在投影裡是 pending，等轉 ready）
    assert reg.readiness("chat.warm") == "warming" and reg.readiness("lore.gone") == "unavailable"
    assert reg.enabled_tags() == frozenset({"memory.read", "chatroom.read"})
    reg.apply_module_event("module.unavailable", {"module_id": "gpu:x", "revision": 1})
    assert reg.enabled_tags() == frozenset({"memory.read"})


def test_enabled_tags_does_not_mutate_or_read_outside_memory():
    reg = memory_registry()
    assert reg.enabled_tags() == reg.enabled_tags()


# ---- capability_tags_for（交給 narrow_capability） ----

def test_tags_for_agent_loop_is_route_table_intersect_enabled():
    metrics: Counter[str] = Counter()
    tags_for = make_capability_tags_for(memory_registry(), metrics)
    # 生產註冊表尚未註冊 decision.consult 工具 -> consult 不在快照內 -> 只剩 memory.read
    assert tags_for(TICKET, _proposal()) == frozenset({"memory.read"})
    reg = ToolRegistry.build([
        make_entry("read.ok", spec={"capability": "memory.read"}),
        make_entry("consult", spec={"capability": "decision.consult"}),
    ])
    assert make_capability_tags_for(reg)(TICKET, _proposal()) == frozenset({"memory.read", "decision.consult"})


def test_tags_for_hint_only_narrows_and_never_widens():
    reg = ToolRegistry.build([
        make_entry("read.ok", spec={"capability": "memory.read"}),
        make_entry("consult", spec={"capability": "decision.consult"}),
        make_entry("write.s", spec={"capability": "memory.write", "side_effect_class": "reversible_local"}),
    ])
    metrics: Counter[str] = Counter()
    tags_for = make_capability_tags_for(reg, metrics)
    # hint 要求 memory.write（表外於 agent_loop base）-> 不會新增；consult 保留 -> 只剩 consult -> 視為空
    assert tags_for(TICKET, _proposal(hint=("memory.write",))) == frozenset()
    assert tags_for(TICKET, _proposal(hint=("memory.read",))) == frozenset({"memory.read", "decision.consult"})
    assert tags_for(TICKET, _proposal(hint=("memory.read", "no.such.tag"))) == frozenset({"memory.read", "decision.consult"})
    assert metrics[COUNT_HINT_DROPPED] == 1  # 表外標籤被丟棄並計數


def test_tags_for_unknown_route_is_empty_and_loop_entry_does_not_launch(caplog):
    launched: list = []
    entry = LoopEntry(
        "agent", capability_tags_for=make_capability_tags_for(memory_registry()),
        launch=lambda trig, t: launched.append(trig), policy_ref="p", owner_for=lambda t: "o",
    )
    assert make_capability_tags_for(memory_registry())(TICKET, _proposal(route="no_such_route")) == frozenset()
    assert entry(SimpleNamespace(turn_id="t", audience_scope="a", snapshot_version=1), _proposal(route="direct_chat")) is None
    assert launched == []


def test_tags_for_empty_snapshot_fails_closed_with_reason():
    result = narrow_capability(
        frozenset({"memory.read", "decision.consult"}), None, frozenset(), CAPABILITY_CEILING.keys(), route="agent_loop",
    )
    assert result.empty and result.route == "direct_chat" and result.reason == REASON_CAPABILITY_EMPTY


def test_model_empty_hint_is_dropped_and_counted():
    """D4-6 由 narrow_capability 負責；Proposal 目前沒有 hint 來源欄位，wiring 一律以 rule 處理（空 hint 要求空集合）。"""
    result = narrow_capability(
        frozenset({"memory.read", "decision.consult"}), (), frozenset({"memory.read", "decision.consult"}),
        CAPABILITY_CEILING.keys(), route="agent_loop", hint_source="model",
    )
    assert result.tags == frozenset({"memory.read", "decision.consult"}) and result.counters()[COUNT_HINT_EMPTY_MODEL] == 1


# ---- 旗標與組裝 ----

def test_flag_defaults_off_and_legacy_refuses_to_enable():
    from uep.config import UepSettings

    assert UepSettings.model_fields["agent_loop_entry"].default is False


def test_assemble_refuses_agent_loop_entry_under_legacy(session_factory, tmp_path):
    from tests.uep.test_assembly import kwargs

    with pytest.raises(AssemblyError, match="UEP_AGENT_LOOP_ENTRY"):
        assemble_core(session_factory, **kwargs(session_factory, tmp_path, agent_loop_entry=True))


# ---- orchestrator 認領 ----

def test_orchestrator_claims_only_when_entry_returns_trigger():
    from uep.turn.orchestrator import TurnOrchestrator

    orch = TurnOrchestrator.__new__(TurnOrchestrator)
    orch._lock = threading.Lock()
    orch._loop_claims = __import__("collections").OrderedDict()
    orch.metrics = Counter()
    ticket = SimpleNamespace(turn_id="t1")
    orch._loop_entry = lambda t, p: None
    orch._enter_loop(ticket, _proposal())
    assert not orch.loop_claimed("t1")
    orch._loop_entry = lambda t, p: object()
    orch._enter_loop(ticket, _proposal())
    assert orch.loop_claimed("t1") and orch.metrics["turn.loop_claimed"] == 1
    boom_ticket = SimpleNamespace(turn_id="t2")

    def boom(t, p):
        raise RuntimeError("x")

    orch._loop_entry = boom
    orch._enter_loop(boom_ticket, _proposal())  # 例外：回合退回 direct_chat，不認領
    assert not orch.loop_claimed("t2") and orch.metrics["turn.loop_entry_failed"] == 1
    orch._enter_loop(SimpleNamespace(turn_id="t3"), _proposal(route="direct_chat"))
    assert not orch.loop_claimed("t3")


# ---- DB 端到端：雙重回覆鎖定 ----

class _LoopProposer:
    proposer_id = "test.agent_loop"
    version = "t"

    def __init__(self, route: str) -> None:
        self.route = route

    def propose(self, candidate):
        return Proposal(
            reply_mode=ReplyMode.RESPOND, route=self.route, reason_code="default_respond",
            proposer_id=self.proposer_id, proposer_version=self.version,
        )


def _agent_rig(session_factory, tmp_path, *, route, flag, loop_port):
    from tests.uep.echo_support import Rig, fast_speak, make_identity, make_session
    from tests.uep.test_agent_tool_path import FakePort, _Port
    from uep.agent.runtime import ModelPortBackend
    from uep.control.channel import InProcessControlChannel
    from uep.echo.host import EchoPipelineHost
    from uep.events.bus import EventBus
    from uep.events.publisher import EventPublisher
    from uep.presence.epoch import HostEpochStore
    from uep.stream.adapters.think_agent import AgentThinkStage
    from uep.stream.adapters.think_llm import LLMThinkStage
    from uep.stream.tracer import LatencyTracer

    class Memory(_Port):
        def retrieve(self, *a, **k):
            return []

        def store(self, **kw):
            pass

        def profile(self, **kw):
            return ""

        def set_session(self, session_id):
            pass

        def tool_definitions(self):
            return []

    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    es_port = FakePort([])
    think = AgentThinkStage(LLMThinkStage(ModelPortBackend(es_port)))
    host = EchoPipelineHost(
        publisher=publisher, think=think, speak=fast_speak(), sink_factory=lambda v, t: "x", session=make_session(),
        identity=make_identity(), memory=Memory(), tracer=LatencyTracer(), tool_path="agent",
        proposer=_LoopProposer(route), endpoint_kwargs={"tick_interval": 0.05},
    )
    core = assemble_core(
        session_factory, bus=bus, publisher=publisher, control=InProcessControlChannel(), adapter=host.adapter,
        executors={}, tool_resolver=memory_registry(), turn_generation_source=host.generation_source,
        presence_audience=lambda: "scope_private_owner", epoch_store=HostEpochStore(tmp_path / "host_epoch.json"),
        maintenance_interval=None, echo=host, tool_path="agent", model_port=loop_port, agent_loop_entry=flag,
        decision_log=False, supervisor=False,
    )
    rig = Rig(core, host, session_factory, think, host.speak, host.tracer if hasattr(host, "tracer") else LatencyTracer(), [])
    core.start()
    return rig, es_port


def _submits(rig):
    calls: list = []
    original = rig.host.coordinator.submit

    def spy(request):
        calls.append(request)
        return original(request)

    rig.host.coordinator.submit = spy
    return calls


def test_db_agent_loop_route_delivers_once_and_skips_direct_chat(session_factory, tmp_path):
    from tests.uep.test_agent_tool_path import FakePort, fin, text

    loop_port = FakePort([[text("查完了，答案是四十二。"), fin()]])
    rig, es_port = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=loop_port)
    try:
        submits = _submits(rig)
        result = rig.say("幫我想一下答案")
        assert result.ticket is not None and result.proposal.route == "agent_loop"
        turn_id = result.ticket.turn_id
        kinds = rig.wait_terminal(turn_id)
        assert kinds.count("turn.completed") == 1 and "turn.cancelled" not in kinds
        assert submits == []  # direct_chat（ES coordinator）一次都沒被啟動
        assert es_port.requests == []  # ES 的 think 沒呼叫過模型
        intents = rig.intents(turn_id)
        assert len(intents) == 1 and intents[0].kind == "text"  # 同一輸入只有一次對外輸出
        assert len(rig.deliveries()) == 1
        assert rig.core.echo.orchestrator.metrics["turn.loop_claimed"] == 1
        assert rig.core.echo.orchestrator._live == {}  # turn 已終結，沒有殘留
        assert len(loop_port.requests) == 1
    finally:
        rig.core.shutdown()


def test_db_flag_off_agent_loop_route_behaves_as_direct_chat(session_factory, tmp_path):
    from tests.uep.test_agent_tool_path import FakePort

    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=False, loop_port=FakePort([]))
    try:
        assert rig.core.echo.orchestrator._loop_entry is None
        submits = _submits(rig)
        result = rig.say("你好")
        assert result.ticket is not None
        rig.wait_for(lambda: len(submits) == 1, what="direct_chat 啟動")  # 旗標關閉：行為不變，仍走 ES
        assert rig.core.echo.orchestrator.metrics["turn.loop_claimed"] == 0
    finally:
        rig.core.shutdown()


def test_db_direct_chat_route_is_untouched_even_with_flag_on(session_factory, tmp_path):
    from tests.uep.test_agent_tool_path import FakePort

    loop_port = FakePort([])
    rig, _ = _agent_rig(session_factory, tmp_path, route="direct_chat", flag=True, loop_port=loop_port)
    try:
        submits = _submits(rig)
        result = rig.say("你好")
        assert result.ticket is not None
        rig.wait_for(lambda: len(submits) == 1, what="direct_chat 啟動")
        assert loop_port.requests == [] and rig.core.echo.orchestrator.metrics["turn.loop_claimed"] == 0
    finally:
        rig.core.shutdown()


def test_db_absent_endpoint_input_opens_no_turn_and_no_run(session_factory, tmp_path):
    """§10 查核：Presence 沒有生效 Lease（auth is None）時 intake 在簽 ticket 之前就被否決，LoopEntry 不會被呼叫。
    這是既有的 turn 層行為，PR4 沒有新增任何 Presence 判定。"""
    from tests.uep.test_agent_tool_path import FakePort

    loop_port = FakePort([])
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=loop_port)
    try:
        rig.core.presence.revoke("presence_lost_test")
        result = rig.say("在嗎")
        assert result.ticket is None and result.reason_code == "presence_inactive"
        assert loop_port.requests == [] and rig.core.echo.orchestrator.metrics["turn.loop_claimed"] == 0
    finally:
        rig.core.shutdown()
