"""Output Gate 前置「端點須具 interaction 能力」（平台端點契約 IC-3、E3；需 PostgreSQL）。

預設 shadow：未宣告只計數與記 log、照常放行；``require_interaction=True`` 才拒絕並回 REJECT_NOT_INTERACTION。
"""
from __future__ import annotations

import logging

import pytest

from tests.uep.presence_support import activate, count_events, intent_for, make_rig
from uep.contracts.platform import (
    InteractionCapability,
    MonitorCapability,
    PlatformCapabilities,
)
from uep.contracts.presence import GateDecision

pytestmark = pytest.mark.usefixtures("session_factory")


def _interaction_caps(endpoint: str) -> PlatformCapabilities:
    return PlatformCapabilities("desktop", endpoint, 1, interaction=InteractionCapability(frozenset({"text"})))


def _monitor_only_caps(endpoint: str) -> PlatformCapabilities:
    return PlatformCapabilities("website", endpoint, 1, monitor=MonitorCapability(frozenset({"status_view"})))


def test_shadow_counts_undeclared_endpoint_but_admits(session_factory, tmp_path, caplog):
    rig = make_rig(session_factory, tmp_path)  # 無 resolver：視為未宣告
    auth = activate(rig)
    with caplog.at_level(logging.DEBUG, logger="uep.presence.gate"):
        assert rig.gate.admit_intent(intent_for(auth)) is GateDecision.ADMIT
        assert rig.gate.admit_intent(intent_for(auth)) is GateDecision.ADMIT
    assert rig.gate.not_interaction_count == 2
    assert rig.gate.not_interaction_by_endpoint() == {auth.endpoint_id: 2}
    assert any("reason=not_interaction" in r.getMessage() for r in caplog.records)
    assert count_events(session_factory, "output.rejected") == 0


def test_shadow_counts_monitor_only_and_unknown_endpoint(session_factory, tmp_path):
    caps = {"ep_desktop": _monitor_only_caps("ep_desktop")}
    rig = make_rig(session_factory, tmp_path, capability_resolver=caps.get)
    auth = activate(rig)
    assert rig.gate.admit_intent(intent_for(auth)) is GateDecision.ADMIT
    assert rig.gate.not_interaction_count == 1  # 僅 monitor
    caps.clear()
    assert rig.gate.admit_intent(intent_for(auth)) is GateDecision.ADMIT
    assert rig.gate.not_interaction_count == 2  # 查無宣告


def test_resolver_failure_is_treated_as_undeclared(session_factory, tmp_path):
    def boom(_endpoint):
        raise RuntimeError("registry down")

    rig = make_rig(session_factory, tmp_path, capability_resolver=boom)
    auth = activate(rig)
    assert rig.gate.admit_intent(intent_for(auth)) is GateDecision.ADMIT
    assert rig.gate.not_interaction_count == 1


def test_declared_interaction_is_not_counted(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path, capability_resolver=lambda ep: _interaction_caps(ep))
    auth = activate(rig)
    assert rig.gate.admit_intent(intent_for(auth)) is GateDecision.ADMIT
    assert rig.gate.not_interaction_count == 0 and rig.gate.not_interaction_by_endpoint() == {}


def test_enforce_rejects_undeclared_with_dedicated_code(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path, require_interaction=True)
    auth = activate(rig)
    intent = intent_for(auth)
    assert rig.gate.admit_intent(intent) is GateDecision.REJECT_NOT_INTERACTION
    assert count_events(session_factory, "output.rejected") == 1
    assert rig.gate.not_interaction_count == 0  # 強制拒絕不走 shadow 計數


def test_enforce_rejects_monitor_only_but_admits_interaction(session_factory, tmp_path):
    caps = {"ep_desktop": _monitor_only_caps("ep_desktop")}
    rig = make_rig(session_factory, tmp_path, capability_resolver=caps.get, require_interaction=True)
    auth = activate(rig)
    assert rig.gate.admit_intent(intent_for(auth)) is GateDecision.REJECT_NOT_INTERACTION
    caps["ep_desktop"] = _interaction_caps("ep_desktop")
    assert rig.gate.admit_intent(intent_for(auth)) is GateDecision.ADMIT


def test_not_interaction_is_a_rejection_code():
    assert not GateDecision.REJECT_NOT_INTERACTION.admitted
