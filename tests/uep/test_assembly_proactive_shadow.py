"""DL-6b：proactive.initiate 接上 assembly（shadow-only）。無 DB：session_factory 為替身，DbSink 換成記憶體 sink。

shadow-only 的斷言重點：評估前後不碰 session_factory（不可能建立 Run／output_intents）、tasks／delivery 零呼叫、
結果只進 logger 與決策日誌；capability_lookup 回 None 時 ready 的候選得 ``capability_empty``。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.uep.test_assembly_decision_log import _host, _kw, cores, sinks  # noqa: F401 - fixtures
from uep.assembly import assemble_core
from uep.contracts.presence import PresenceStatus
from uep.decision.proactive import (
    KIND_PROACTIVE_INITIATE,
    LT_CAPABILITY_EMPTY,
    LT_NOT_READY,
    LT_WOULD_START,
    ProactiveCandidate,
    ProactiveEngine,
    ProactiveInjections,
)
from uep.echo.proactive_wiring import PLACEHOLDER_SNAPSHOT_REF, ProactiveShadow
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.presence.gate import ALLOW_ALL

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def _cand(**over) -> ProactiveCandidate:
    base = {
        "candidate_id": "pcand_dl6b", "trigger_event_ids": ("evt_1",), "rule_version": "rv1",
        "goal_ref": "assignment_pickup", "evidence_refs": (), "memory_salience": 0.6, "urgency": "normal",
        "confidence": 0.8, "privacy_scope": "scope_private", "expires_at": NOW + timedelta(hours=1),
        "dedupe_key": "assign:t1:o", "source_kind": "assignment", "owner_actor_id": "actor_owner",
        "created_at": NOW - timedelta(minutes=5),
    }
    return ProactiveCandidate(**{**base, **over})


class _Presence:
    def __init__(self, status=PresenceStatus.ACTIVE, location="desktop", transfer_id=None, generation=3, expires_at=None):
        self.view = SimpleNamespace(status=status, location=location, generation=generation, transfer_id=transfer_id,
                                    expires_at=expires_at)

    def peek(self):
        return self.view


class _State:
    def __init__(self, interaction="IDLE", behavior="CHAT"):
        self.values = {"Interaction": interaction, "Behavior": behavior}

    def current(self, axis, subject=""):
        return SimpleNamespace(state=self.values[axis.value])


class _Stop:
    def __init__(self, engaged=False):
        self.engaged = engaged

    def is_engaged(self):
        return self.engaged


@pytest.fixture
def built(tmp_path, cores, sinks):  # noqa: F811
    session_factory = MagicMock()

    def _build(**over):
        bus = EventBus()
        publisher = EventPublisher(MagicMock(), bus)
        host = _host(publisher)
        core = assemble_core(session_factory, **_kw(tmp_path, publisher, bus, host, **over))
        cores.append(core)
        return core, host, session_factory

    return _build


def _fake_facts(core, **kw):
    """把 shadow 的事實來源換成可控替身（production 為 PresenceManager／StateCoordinator／GlobalStop）。"""
    shadow = core.proactive_shadow
    shadow._presence = kw.get("presence", _Presence())
    shadow._state = kw.get("state", _State())
    shadow._stop = kw.get("stop", _Stop())
    shadow._clock = lambda: NOW
    shadow._audience = kw.get("audience", lambda: "scope_private")
    return shadow


# ---- 組裝 ----

def test_assembly_registers_proactive_and_log_attached(built, sinks):  # noqa: F811
    core, host, _ = built()
    hub = host.decision_hub
    assert isinstance(core.proactive_shadow, ProactiveShadow)
    assert isinstance(hub.engine(KIND_PROACTIVE_INITIATE), ProactiveEngine)
    assert core.decision_log is not None and len(hub._observers) > 0


def test_disabled_log_registers_nothing(built, sinks):  # noqa: F811
    core, host, _ = built(decision_log=False)
    assert core.proactive_shadow is None and host.decision_hub.engine(KIND_PROACTIVE_INITIATE) is None


def test_no_echo_has_no_shadow(tmp_path, cores, sinks):  # noqa: F811
    bus = EventBus()
    publisher = EventPublisher(MagicMock(), bus)
    core = assemble_core(MagicMock(), **_kw(tmp_path, publisher, bus, None, decision_log=True))
    cores.append(core)
    assert core.proactive_shadow is None


# ---- shadow-only ----

def test_ready_with_empty_capability_lookup_is_capability_empty_and_touches_nothing(built, sinks, caplog):  # noqa: F811
    core, host, session_factory = built()
    shadow = _fake_facts(core)
    core.tasks, core.delivery = MagicMock(), MagicMock()  # 若有人誤用就會留下呼叫紀錄
    before = session_factory.call_count
    with caplog.at_level(logging.INFO, logger="uep.echo.proactive_wiring"):
        ev = shadow.evaluate(_cand())
    assert ev.result.outcome == "ready" and ev.result.plan.loop.start is True
    assert ev.lt_reason == LT_CAPABILITY_EMPTY and ev.would_start is False
    assert "lt=capability_empty" in caplog.text and "would_start=False" in caplog.text
    # 0 個 Run、0 個 output_intents：沒有任何 DB session 被開啟，tasks／delivery 零呼叫
    assert session_factory.call_count == before
    assert core.tasks.mock_calls == [] and core.delivery.mock_calls == []
    # 決策日誌有紀錄
    assert core.decision_log.flush(5.0)
    rows = [r for r in sinks[0].rows if r.get("kind") == KIND_PROACTIVE_INITIATE]
    assert len(rows) == 1 and rows[0]["candidate_id"] == "pcand_dl6b" and rows[0]["reason_code"] == "ready_assignment"


def test_would_start_only_recorded_when_lookup_injected(built, sinks, caplog):  # noqa: F811
    core, _, session_factory = built()
    shadow = _fake_facts(core)
    shadow._inj = ProactiveInjections(capability_lookup=lambda goal: frozenset({"agent.read"}), snapshot_ref="snap_x")
    before = session_factory.call_count
    ev = shadow.evaluate(_cand())
    assert ev.would_start is True and ev.lt_reason == LT_WOULD_START
    assert session_factory.call_count == before  # 就算 would_start 也不啟動任何東西


def test_default_injections_are_conservative(built, sinks):  # noqa: F811
    core, _, _ = built()
    inj = core.proactive_shadow._inj
    assert inj.capability_lookup("assignment_pickup") is None and inj.snapshot_ref == PLACEHOLDER_SNAPSHOT_REF


def test_not_ready_reports_not_ready(built, sinks):  # noqa: F811
    core, _, _ = built()
    shadow = _fake_facts(core, stop=_Stop(engaged=True))
    ev = shadow.evaluate(_cand())
    assert ev.result.reason_code == "global_stop" and ev.lt_reason == LT_NOT_READY and not ev.would_start


# ---- ProactiveContext 組裝與保守預設 ----

def test_context_assembled_from_presence_state_stop(built, sinks):  # noqa: F811
    core, _, _ = built()
    shadow = _fake_facts(core, presence=_Presence(location="discord", generation=7, transfer_id="tr_1"),
                         state=_State(interaction="SPEAKING", behavior="SLEEP"), audience=lambda: "scope_group_g1")
    ctx = shadow.build_context(_cand())
    assert (ctx.presence_status, ctx.current_platform.value, ctx.presence_generation) == (PresenceStatus.ACTIVE, "discord", 7)
    assert ctx.transfer_pending is True and ctx.audience_scope == "scope_group_g1"
    assert ctx.muted is True and ctx.activity_interruptible is False and ctx.in_committed_interaction is True
    assert ctx.global_stop is False and ctx.grant is None and ctx.private_locations == () and ctx.ledger == ()


def test_expired_active_lease_is_treated_as_none(built, sinks):  # noqa: F811
    """peek 不處理過期：ACTIVE 但 Lease 已過期須視同 NONE，不把過期位置當成 current_platform。"""
    core, _, _ = built()
    shadow = _fake_facts(core, presence=_Presence(expires_at=datetime(2000, 1, 1, tzinfo=UTC)))
    ctx = shadow.build_context(_cand())
    assert ctx.presence_status is PresenceStatus.NONE and ctx.current_platform is None


def test_missing_data_uses_conservative_defaults(built, sinks):  # noqa: F811
    core, _, _ = built(presence_audience=ALLOW_ALL)
    shadow = core.proactive_shadow
    assert shadow._audience is None  # ALLOW_ALL 不是 callable
    # MagicMock session 下 Presence／State／Stop 讀取皆失敗或無法解析 -> fail-closed
    shadow._presence, shadow._state, shadow._stop = MagicMock(**{"peek.side_effect": RuntimeError}), MagicMock(
        **{"current.side_effect": RuntimeError}), MagicMock(**{"is_engaged.side_effect": RuntimeError})
    ctx = shadow.build_context(_cand())
    assert ctx.audience_scope == "scope_public" and ctx.presence_status is PresenceStatus.RECOVERING
    assert ctx.global_stop is True and ctx.muted is True and ctx.activity_interruptible is False


def test_non_assignment_candidate_has_no_authorization_without_grant_service(built, sinks):  # noqa: F811
    core, _, _ = built()
    shadow = _fake_facts(core)
    ev = shadow.evaluate(_cand(source_kind="schedule", goal_ref="remind_due", authorization_ref="grant_1"))
    assert ev.result.reason_code == "no_authorization"


@pytest.mark.parametrize(("kw", "reason"), [
    ({"presence": _Presence(status=PresenceStatus.NONE, location=None)}, "presence_none_queued"),
    ({"presence": _Presence(transfer_id="tr_1")}, "presence_transferring"),
    ({"state": _State(interaction="THINKING")}, "activity_not_interruptible"),
    ({"state": _State(behavior="SLEEP")}, "muted"),
    ({"state": _State(behavior="REST")}, "muted"),
    ({"audience": lambda: "scope_public"}, "audience_too_wide_wait"),
])
def test_context_driven_reasons_reach_decision(built, sinks, kw, reason):  # noqa: F811
    core, _, _ = built()
    ev = _fake_facts(core, **kw).evaluate(_cand())
    assert ev.result.reason_code == reason


def test_repeat_evaluation_uses_distinct_request_ids_and_log_quiet(built, sinks):  # noqa: F811
    core, _, _ = built()
    shadow = _fake_facts(core)
    ids = {shadow.evaluate(_cand()).result.request_id for _ in range(3)}
    assert ids == {"pcand_dl6b:eval:1", "pcand_dl6b:eval:2", "pcand_dl6b:eval:3"}
    assert core.decision_log.flush(5.0)
    assert len([r for r in sinks[0].rows if r.get("kind") == KIND_PROACTIVE_INITIATE]) == 1  # 同 reason 連續只記一次


def test_injected_locations_never_include_console(built, sinks):  # noqa: F811
    """shadow 組裝的 private_locations／grant 不得帶 console（測試平台只能被排程指定）。"""
    core, _, _ = built()
    for loc in ("desktop", "discord", "website"):
        shadow = _fake_facts(core, presence=_Presence(location=loc))
        ctx = shadow.build_context(_cand())
        assert "console" not in {v.value for v in ctx.private_locations}
        assert ctx.grant is None or "console" not in {v.value for v in ctx.grant.locations}
