"""DL-1b：Host 注入決策中樞，預設 primary=rules.default、shadow=respond.minimal，UEP_DECISION_* 組態（不需資料庫）。"""
from __future__ import annotations

import dataclasses
import threading
import time

import pytest

from uep.config import UepSettings
from uep.decision import DecisionHub
from uep.echo.host import EchoPipelineHost
from uep.turn.decision_config import DecisionConfig, decision_config_from_settings
from uep.turn.models import (
    Proposal,
    ReplyMode,
    SessionContext,
    TurnCandidate,
    TurnOutcomeFacts,
)
from uep.turn.orchestrator import TurnOrchestrator
from uep.turn.proposer import RespondProposer
from uep.turn.rule_proposer import RuleProposer

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")


def _host(**kw) -> EchoPipelineHost:
    return EchoPipelineHost(
        publisher=None, think=object(), speak=object(), sink_factory=lambda *a: "x", session=CTX, **kw  # type: ignore[arg-type]
    )


def _shadow_ids(host: EchoPipelineHost) -> list[str]:
    return [sp.proposer_id for sp, _ in host.orchestrator._shadows]


def _cand(cid="cand1", text="hi", speaker="discord:1") -> TurnCandidate:
    return TurnCandidate(
        candidate_id=cid, utterance_refs=("e1",), speakers=(speaker,), ctx=CTX, interaction_state="IDLE",
        interaction_version=1, presence_active=True, text=text, participant_count=1, addressed_hint=False,
    )


class _Named:
    def __init__(self, pid):
        self.proposer_id = pid

    def propose(self, candidate):
        return Proposal(reply_mode=ReplyMode.PASS, addressed=False, confidence=0.0, reason_code="x",
                        proposer_id=self.proposer_id, proposer_version="1")


def test_host_builds_own_hub_and_injects_it():
    host = _host()
    try:
        assert isinstance(host.decision_hub, DecisionHub)
        assert host.orchestrator.hub is host.decision_hub
    finally:
        host.orchestrator.close()


def test_host_uses_external_hub():
    hub = DecisionHub()
    host = _host(decision_hub=hub)
    try:
        assert host.decision_hub is hub and host.orchestrator.hub is hub
    finally:
        host.orchestrator.close()


def test_default_primary_is_rules_default_and_shadow_is_respond_minimal():
    host = _host()
    try:
        assert isinstance(host.orchestrator.proposer, RuleProposer)
        assert _shadow_ids(host) == [RespondProposer.proposer_id]
    finally:
        host.orchestrator.close()


def test_explicit_shadow_proposers_win_over_config():
    mine = _Named("mine.shadow")
    host = _host(shadow_proposers=[mine])
    try:
        assert _shadow_ids(host) == ["mine.shadow"]
    finally:
        host.orchestrator.close()
    host = _host(shadow_proposers=())
    try:
        assert _shadow_ids(host) == []
    finally:
        host.orchestrator.close()


def test_empty_config_disables_shadow():
    host = _host(decision=DecisionConfig(shadow_proposer_ids=()))
    try:
        assert _shadow_ids(host) == []
    finally:
        host.orchestrator.close()


def test_same_id_as_primary_not_duplicated():
    host = _host(proposer=RespondProposer())
    try:
        assert _shadow_ids(host) == []
    finally:
        host.orchestrator.close()
    host = _host(proposer=RespondProposer(), decision=DecisionConfig(shadow_proposer_ids=("rules.default", "rules.default")))
    try:
        assert _shadow_ids(host) == ["rules.default"]
    finally:
        host.orchestrator.close()


def _settings(**kw) -> UepSettings:
    return UepSettings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_settings_unset_keeps_defaults():
    cfg = decision_config_from_settings(_settings())
    assert cfg.proposer_id == "rules.default" and cfg.shadow_proposer_ids == ("respond.minimal",)


def test_env_selects_primary_and_shadows(monkeypatch):
    monkeypatch.setenv("UEP_DECISION_PROPOSER", "respond.minimal")
    monkeypatch.setenv("UEP_DECISION_SHADOW_PROPOSERS", "rules.default, respond.minimal, rules.default")
    cfg = decision_config_from_settings(_settings())
    assert cfg.proposer_id == "respond.minimal"
    assert cfg.shadow_proposer_ids == ("rules.default",)  # 去重並剔除與 primary 相同者
    host = _host(decision=cfg)
    try:
        assert isinstance(host.orchestrator.proposer, RespondProposer)
        assert _shadow_ids(host) == ["rules.default"]
    finally:
        host.orchestrator.close()


def test_env_empty_shadow_disables_shadow(monkeypatch):
    monkeypatch.setenv("UEP_DECISION_SHADOW_PROPOSERS", "")
    assert decision_config_from_settings(_settings()).shadow_proposer_ids == ()


@pytest.mark.parametrize("name", ["UEP_DECISION_PROPOSER", "UEP_DECISION_SHADOW_PROPOSERS"])
def test_env_unknown_id_fails_fast(monkeypatch, name):
    monkeypatch.setenv(name, "nope.nothing")
    with pytest.raises(ValueError, match="nope.nothing"):
        decision_config_from_settings(_settings())


def test_host_without_decision_reads_env(monkeypatch):
    monkeypatch.setenv("UEP_DECISION_PROPOSER", "respond.minimal")
    from uep.config import get_settings

    get_settings.cache_clear()
    try:
        host = _host()
        try:
            assert isinstance(host.orchestrator.proposer, RespondProposer)
            assert _shadow_ids(host) == []
        finally:
            host.orchestrator.close()
    finally:
        get_settings.cache_clear()


def test_explicit_proposer_wins_over_env(monkeypatch):
    monkeypatch.setenv("UEP_DECISION_PROPOSER", "respond.minimal")
    from uep.config import get_settings

    get_settings.cache_clear()
    try:
        mine = RuleProposer()
        host = _host(proposer=mine)
        try:
            assert host.orchestrator.proposer is mine
        finally:
            host.orchestrator.close()
    finally:
        get_settings.cache_clear()


def _orch(proposer, shadows=()):
    return TurnOrchestrator(
        None, None, auth_provider=lambda: None, proposer=proposer, shadow_proposers=shadows,  # type: ignore[arg-type]
        decision=DecisionConfig(),
    )


def test_slow_shadow_does_not_delay_primary():
    class Slow:
        proposer_id = "slow.shadow"

        def propose(self, candidate):
            time.sleep(0.5)
            return _Named("slow.shadow").propose(candidate)

    orch = _orch(_Named("fast.primary"), [Slow()])
    try:
        started = time.monotonic()
        result = orch._propose(_cand())
        elapsed = time.monotonic() - started
        assert result.reason_code == "x" and elapsed < 0.2
        assert len(orch.shadow_log) == 0  # shadow 尚未跑完，primary 已回傳
        assert orch.wait_shadow(3.0)
        assert len(orch.shadow_log) == 1 and orch.shadow_log[0].shadow_id == "slow.shadow"
    finally:
        orch.close()


def test_shadow_receives_feedback_and_cannot_affect_primary():
    seen: list[tuple[str, str]] = []
    done = threading.Event()
    started = threading.Event()

    class Spy(_Named):
        def on_turn_started(self, turn_id, candidate_id, speakers):
            seen.append(("started", candidate_id))
            started.set()
            raise RuntimeError("shadow hook boom")

        def on_turn_outcome(self, facts):
            seen.append(("outcome", facts.candidate_id))
            done.set()
            raise RuntimeError("shadow hook boom")

    primary_seen: list[str] = []

    class Primary(_Named):
        def on_turn_started(self, turn_id, candidate_id, speakers):
            primary_seen.append("started")

        def on_turn_outcome(self, facts):
            primary_seen.append("outcome")

    orch = _orch(Primary("p"), [Spy("s")])
    try:
        facts = TurnOutcomeFacts(turn_id="t", candidate_id="cand1", speakers=("discord:1",), reply_mode="respond",
                                 reason_code="called", status="completed")
        orch._feedback("on_turn_started", "t", "cand1", ("discord:1",))
        assert started.wait(2.0)
        time.sleep(0.1)  # shadow 的 guard 是單執行緒，忙碌時回饋會被略過（計數），這裡等上一個結束
        orch._feedback("on_turn_outcome", facts)
        assert done.wait(2.0)
        assert primary_seen == ["started", "outcome"]  # shadow 例外不影響 primary
        assert ("started", "cand1") in seen and ("outcome", "cand1") in seen  # shadow 收得到回饋
    finally:
        orch.close()


def test_rule_shadow_state_isolated_from_primary_rule_proposer():
    cfg = DecisionConfig()
    sequence = [("c1", "U.E.P 你好", "discord:1"), ("c2", "今天天氣不錯", "discord:2"), ("c3", "UEP 在嗎", "discord:1")]

    def drive(shadow):
        primary = RuleProposer(cfg, clock=lambda: 100.0)
        orch = _orch(primary, [shadow] if shadow is not None else [])
        try:
            modes = []
            for cid, text, spk in sequence:
                modes.append(orch._propose(_cand(cid, text, spk)).reply_mode)
                orch.wait_shadow(3.0)
                orch._feedback("on_turn_started", f"t-{cid}", cid, (spk,))
                orch._feedback("on_turn_outcome", TurnOutcomeFacts(
                    turn_id=f"t-{cid}", candidate_id=cid, speakers=(spk,), reply_mode="respond",
                    reason_code="called", status="completed"))
                time.sleep(0.05)
            return modes, primary.coupling_value, primary.pending_topics, primary.channel_mode
        finally:
            orch.close()

    control = drive(None)
    with_shadow = drive(RuleProposer(dataclasses.replace(cfg), clock=lambda: 100.0))
    assert with_shadow == control
