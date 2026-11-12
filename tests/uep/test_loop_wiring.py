"""route=agent_loop -> LoopTrigger 的接線（切片 D1）：只在 UEP_TOOL_PATH=agent 下可達；legacy 行為不變。不需 DB。"""
from __future__ import annotations

import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest

from uep.assembly import AssemblyError, assemble_core
from uep.control.channel import InProcessControlChannel
from uep.echo.agent_wiring import (
    LOOP_GOAL_USER_TURN,
    LoopEntry,
    create_loop_run,
    trigger_from_turn,
)
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.fakes.output import FakeOutputAdapter
from uep.fakes.tool import FakeToolExecutor
from uep.fakes.tool_spec import FakeToolSpecResolver
from uep.presence.gate import ALLOW_ALL
from uep.tasks.states import InterruptPolicy
from uep.turn.decision_config import DecisionConfig
from uep.turn.models import Proposal, ReplyMode
from uep.turn.orchestrator import TurnOrchestrator

TICKET = SimpleNamespace(turn_id="turn_abc", audience_scope="aud", snapshot_version=9)


def _proposal(route: str) -> Proposal:
    return Proposal(reply_mode=ReplyMode.RESPOND, route=route)


def _entry(launched: list, tags=frozenset({"memory.read"})) -> LoopEntry:
    return LoopEntry(
        "agent", capability_tags_for=lambda t, p: tags, launch=lambda trig, t: launched.append((trig, t)),
        policy_ref="policy_default", owner_for=lambda t: "actor_owner",
    )


def test_trigger_from_turn_maps_ticket_fields():
    trig = trigger_from_turn(
        TICKET, _proposal("agent_loop"), capability_tags=frozenset({"memory.read"}), policy_ref="p", owner_actor_id="o",
    )
    assert (trig.kind, trig.origin_ref, trig.goal_code, trig.scope_ref) == ("user_turn", "turn_abc", LOOP_GOAL_USER_TURN, "aud")
    assert trig.capability_tags == frozenset({"memory.read"}) and trig.owner_actor_id == "o" and trig.plan_ref is None


def test_loop_entry_cannot_be_built_under_legacy():
    with pytest.raises(ValueError, match="agent"):
        LoopEntry("legacy", capability_tags_for=lambda t, p: frozenset({"x"}), launch=lambda *a: None, policy_ref="p", owner_for=lambda t: "o")


def test_loop_entry_launches_only_for_agent_loop_route():
    launched: list = []
    entry = _entry(launched)
    assert entry(TICKET, _proposal("direct_chat")) is None and launched == []
    trig = entry(TICKET, _proposal("agent_loop"))
    assert trig is not None and launched == [(trig, TICKET)]


def test_loop_entry_with_empty_tags_does_not_launch(caplog):
    launched: list = []
    with caplog.at_level(logging.ERROR):
        assert _entry(launched, tags=frozenset())(TICKET, _proposal("agent_loop")) is None
    assert launched == [] and "capability_tags 為空" in caplog.text


def _orch() -> TurnOrchestrator:
    return TurnOrchestrator(None, None, auth_provider=lambda: None, decision=DecisionConfig())  # type: ignore[arg-type]


def test_orchestrator_without_entry_ignores_route():
    orch = _orch()
    try:
        assert orch._loop_entry is None
        orch._enter_loop(TICKET, _proposal("agent_loop"))  # 不拋、不做事：legacy 下 agent_loop 等同 direct_chat
    finally:
        orch.close()


def test_orchestrator_with_entry_calls_it_for_agent_loop_only_and_isolates_failures():
    orch = _orch()
    calls: list = []
    try:
        orch.bind_loop_entry(lambda t, p: calls.append(t.turn_id))
        orch._enter_loop(TICKET, _proposal("direct_chat"))
        assert calls == []
        orch._enter_loop(TICKET, _proposal("agent_loop"))
        assert calls == ["turn_abc"]

        def boom(t, p):
            raise RuntimeError("入口壞了")

        orch.bind_loop_entry(boom)
        orch._enter_loop(TICKET, _proposal("agent_loop"))  # 不得讓 turn 簽發失敗
        assert orch.metrics["turn.loop_entry_failed"] == 1
    finally:
        orch.close()


def test_issue_calls_enter_loop_after_turn_started():
    import inspect

    source = inspect.getsource(TurnOrchestrator._issue)
    assert source.index('"turn.started"') < source.index("self._enter_loop(ticket, proposal)") < source.index("return IntakeResult")


def test_assemble_core_refuses_loop_entry_under_legacy():
    bus = EventBus()
    with pytest.raises(AssemblyError, match="loop_entry"):
        assemble_core(
            object(), bus=bus, publisher=EventPublisher(object(), bus), control=InProcessControlChannel(),  # type: ignore[arg-type]
            adapter=FakeOutputAdapter(), executors={"fake": FakeToolExecutor()},
            tool_resolver=FakeToolSpecResolver(**{"files.write": {}}), turn_generation_source=ALLOW_ALL,
            presence_audience=ALLOW_ALL, allow_gate_bypass=True, maintenance_interval=None, tool_path="legacy",
            loop_entry=lambda t, p: None,
        )


def test_legacy_import_graph_does_not_load_agent_loop():
    code = "import sys, uep.assembly; sys.exit(1 if 'uep.agent.loop' in sys.modules else 0)"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_create_loop_run_builds_task_and_run_from_trigger():
    seen: dict = {}

    class Tasks:
        def create_task(self, **kw):
            seen["task"] = kw
            return SimpleNamespace(task_id="task_1")

        def start_run(self, task_id, **kw):
            seen["run"] = (task_id, kw)
            return SimpleNamespace(run_id="run_1", worker_epoch=1)

    trig = trigger_from_turn(TICKET, _proposal("agent_loop"), capability_tags=frozenset({"memory.read"}), policy_ref="pol", owner_actor_id="o")
    task, run = create_loop_run(Tasks(), trig, persona_id="uep", persona_version=2, policy_version=3)  # type: ignore[arg-type]
    assert (task.task_id, run.run_id) == ("task_1", "run_1")
    kw = seen["task"]
    assert kw["goal"] == LOOP_GOAL_USER_TURN and kw["owner_actor_id"] == "o" and kw["scope_ref"] == "aud"
    assert kw["policy"].policy_ref == "pol" and kw["policy"].interrupt_policy is InterruptPolicy.INTERRUPTIBLE
    assert kw["origin_ref"] == {"kind": "user_turn", "ref": "turn_abc", "route": "agent_loop"}
    assert seen["run"] == ("task_1", {"persona_version": 2, "policy_version": 3})
