"""不出聲的回合（passive）不進 loop 入口、不啟動 Run、不跑工具（離線，不需資料庫）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.uep.test_action_unavailable import AUTH, CTX, REMINDER, _Proposer, _Publisher
from uep.echo.agent_wiring import LoopEntry
from uep.turn.decision_config import DecisionConfig
from uep.turn.generations import TurnGenerations
from uep.turn.models import Proposal, ReplyMode, UtteranceFact
from uep.turn.orchestrator import TurnOrchestrator


def _rig(mode: ReplyMode, route: str):
    launched: list = []
    entry = LoopEntry(
        "agent", capability_tags_for=lambda t, p: frozenset({"calendar.write"}),
        launch=lambda trig, t: launched.append(trig), policy_ref="policy_default", owner_for=lambda t: "actor_owner",
    )
    proposal = Proposal(reply_mode=mode, route=route, addressed=True, reason_code="laya_model",
                        proposer_id="test.proposer", proposer_version="1")
    orch = TurnOrchestrator(
        _Publisher(), TurnGenerations(), auth_provider=lambda: AUTH,  # type: ignore[arg-type]
        proposer=_Proposer(proposal), decision=DecisionConfig(),
    )
    orch.bind_loop_entry(entry)
    return orch, launched


def _say(orch: TurnOrchestrator):
    return orch.on_utterance(CTX, UtteranceFact(speaker_ref="discord:1", text=REMINDER, seq=1))


@pytest.mark.parametrize("route", ["task_request", "agent_loop"])
def test_passive_never_enters_loop_nor_marks_degraded(route):
    orch, launched = _rig(ReplyMode.PASSIVE, route)
    result = _say(orch)
    assert result.ticket is not None and result.ticket.reply_mode == "passive"
    assert launched == []  # 沒有 Run、沒有工具
    assert result.ticket.degraded is None
    assert orch.metrics["turn.loop_claimed"] == 0 and orch.metrics["turn.silent_route_not_run"] == 1
    assert orch.metrics["turn.action_unavailable"] == 0


def test_passive_task_request_without_entry_is_not_marked_degraded():
    orch, _ = _rig(ReplyMode.PASSIVE, "task_request")
    orch.bind_loop_entry(None)
    assert _say(orch).ticket.degraded is None


def test_respond_task_request_still_enters_loop():
    orch, launched = _rig(ReplyMode.RESPOND, "task_request")
    result = _say(orch)
    assert result.ticket is not None and len(launched) == 1
    assert orch.metrics["turn.loop_claimed"] == 1
