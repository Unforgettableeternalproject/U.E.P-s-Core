"""CC-7 State Coordinator（VC-7A-1／2／3、S-1~S-6；需 PostgreSQL）。"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from tests.uep.presence_support import FakeClock
from tests.uep.state_support import (
    PROPOSERS,
    activate_endpoint,
    journal_events,
    make_stack,
)
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.publisher import EventPublisher
from uep.ids import new_id
from uep.presence.models import PresenceCurrentRow
from uep.state.coordinator import (
    Axis,
    Decision,
    RuntimeState,
    StateCoordinator,
    StateTransitionRequest,
)
from uep.state.models import StateProjectionRow, StateRequestRow
from uep.stop.service import make_flag_blocker

pytestmark = pytest.mark.usefixtures("session_factory")


def make_state(session_factory, *, ready: bool = True, **kw) -> StateCoordinator:
    kw.setdefault("authorized_proposers", PROPOSERS)
    state = StateCoordinator(session_factory, EventPublisher(session_factory, EventBus()), clock=FakeClock(), **kw)
    if ready:
        state.set_runtime(RuntimeState.READY)
    return state


def req(axis="Interaction", frm="IDLE", to="LISTENING", version=0, proposer="nlp", **kw) -> StateTransitionRequest:
    return StateTransitionRequest(
        request_id=kw.pop("request_id", new_id("req")), axis=axis, proposer=proposer, expected_version=version,
        from_state=frm, to_state=to, **kw,
    )


# ---- VC-7A-1：過期版本被拒 ----

def test_second_proposer_with_same_expected_version_is_rejected(session_factory):
    state = make_state(session_factory)
    first = state.request_transition(req(to="LISTENING", proposer="nlp"))
    second = state.request_transition(req(to="THINKING", proposer="proactive"))
    assert first.decision is Decision.ACCEPTED and first.new_version == 1
    assert second.decision is Decision.REJECTED_VERSION
    view = state.current(Axis.INTERACTION)
    assert (view.state, view.version) == ("LISTENING", 1)
    changed = [e for e in journal_events(session_factory, "state.changed") if e.payload["axis"] == "Interaction"]
    assert len(changed) == 1
    assert changed[0].payload["from_state"] == "IDLE" and changed[0].payload["to_state"] == "LISTENING"
    assert changed[0].payload["cause_event_id"]
    # 兩個請求都有紀錄（含被拒者）
    with session_factory() as s:
        decisions = sorted(s.scalars(select(StateRequestRow.decision)))
    assert decisions == ["ACCEPTED", "REJECTED_VERSION"]
    assert len(journal_events(session_factory, "state.transition_requested")) == 2


def test_concurrent_requests_with_same_version_only_one_wins(session_factory):
    state = make_state(session_factory)
    barrier = threading.Barrier(8)
    results: list[Decision] = []

    def worker(i: int) -> None:
        barrier.wait()
        to = "LISTENING" if i % 2 else "THINKING"
        results.append(state.request_transition(req(to=to, proposer=f"p{i}")).decision)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(Decision.ACCEPTED) == 1 and results.count(Decision.REJECTED_VERSION) == 7
    assert state.current(Axis.INTERACTION).version == 1


def test_request_id_is_idempotent_and_does_not_rewrite(session_factory):
    state = make_state(session_factory)
    request = req(to="LISTENING")
    first = state.request_transition(request)
    again = state.request_transition(request)
    assert again.duplicate and again.decision is first.decision and again.new_version == first.new_version
    assert state.current(Axis.INTERACTION).version == 1
    assert len(journal_events(session_factory, "state.transition_requested")) == 1


# ---- VC-7A-2：Presence 軸不接受請求（S-2）----

def test_presence_axis_request_is_rejected_and_presence_untouched(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    state = stack.state
    state.set_runtime(RuntimeState.READY)
    before = stack.manager.current()
    decision = state.request_transition(req(axis="Presence", frm="NONE", to="ACTIVE", proposer="workflow"))
    assert decision.decision is Decision.REJECTED_ILLEGAL and decision.reason_code == "axis_not_requestable"
    after = stack.manager.current()
    assert (after.status, after.row_version, after.generation) == (before.status, before.row_version, before.generation)
    assert state.current(Axis.PRESENCE).version == 0  # 投影也沒被動
    rejected = journal_events(session_factory, "state.transition_requested")
    assert rejected and rejected[-1].payload["status"] == "REJECTED_ILLEGAL"
    with session_factory() as s:
        assert s.get(PresenceCurrentRow, "uep").status == "NONE"


def test_unknown_axis_and_unknown_state_are_rejected(session_factory):
    state = make_state(session_factory)
    assert state.request_transition(req(axis="Nonsense")).decision is Decision.REJECTED_ILLEGAL
    bad = state.request_transition(req(to="FLYING"))
    assert bad.decision is Decision.REJECTED_ILLEGAL and bad.reason_code == "unknown_state"


def test_illegal_transition_and_from_state_mismatch(session_factory):
    state = make_state(session_factory)
    assert state.request_transition(req(to="SPEAKING")).reason_code == "transition_not_allowed"
    mismatch = state.request_transition(req(frm="THINKING", to="SPEAKING"))
    assert mismatch.decision is Decision.REJECTED_ILLEGAL and mismatch.reason_code == "from_state_mismatch"
    assert state.current(Axis.INTERACTION).version == 0


def test_expired_request_is_rejected(session_factory):
    state = make_state(session_factory)
    expired = req(expires_at=FakeClock().now - timedelta(seconds=1))
    assert state.request_transition(expired).decision is Decision.REJECTED_EXPIRED
    assert state.current(Axis.INTERACTION).version == 0


def test_only_allowed_proposers_may_request(session_factory):
    state = make_state(session_factory, authorized_proposers={Axis.INTERACTION: frozenset({"nlp"})})
    assert state.request_transition(req(proposer="rogue")).decision is Decision.REJECTED_UNAUTHORIZED
    assert state.request_transition(req(proposer="nlp")).decision is Decision.ACCEPTED


def test_deferred_while_runtime_starting_and_rejected_while_stopping(session_factory):
    state = make_state(session_factory, ready=False)
    state.set_runtime(RuntimeState.STARTING)
    assert state.request_transition(req()).decision is Decision.DEFERRED
    state.set_runtime(RuntimeState.READY)
    assert state.request_transition(req()).decision is Decision.ACCEPTED
    state.set_runtime(RuntimeState.STOPPING)
    stopped = state.request_transition(req(frm="LISTENING", to="THINKING", version=1))
    assert stopped.decision is Decision.REJECTED_ILLEGAL and stopped.reason_code == "runtime_stopping"
    with pytest.raises(ValueError):
        state.set_runtime(RuntimeState.READY)  # STOPPING 為終點，只有新程序的 STARTING 能重來


def test_behavior_axis_table(session_factory):
    state = make_state(session_factory)
    assert state.request_transition(req("Behavior", "CHAT", "SLEEP")).decision is Decision.ACCEPTED
    direct = state.request_transition(req("Behavior", "SLEEP", "MISCHIEF", version=1))
    assert direct.decision is Decision.REJECTED_ILLEGAL  # 睡眠需先喚醒（S-5：不自動擴權）
    assert state.request_transition(req("Behavior", "SLEEP", "CHAT", version=1)).decision is Decision.ACCEPTED


def test_global_stop_flag_blocks_active_behaviors_only(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    stack.state.set_runtime(RuntimeState.READY)
    stack.stop.engage(operator="tester")
    work = stack.state.request_transition(req("Behavior", "CHAT", "WORK"))
    assert work.decision is Decision.REJECTED_ILLEGAL and work.reason_code == "global_stop_engaged"
    assert stack.state.request_transition(req("Behavior", "CHAT", "SLEEP")).decision is Decision.ACCEPTED
    assert stack.state.task_entry_blocked() == "global_stop_engaged"
    stack.stop.disengage(operator="tester")
    assert stack.state.task_entry_blocked() is None


# ---- VC-7A-3：服務錯誤不污染全域狀態（S-4）----

def test_health_event_updates_health_axis_only(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    state = stack.state
    state.set_runtime(RuntimeState.READY)
    state.request_transition(req(to="THINKING"))
    event = stack.publisher.publish(
        EventDraft(event_type="core.component_health_changed", payload={"ref_id": "tts", "status": "unavailable"}),
        TrustedContext(source="core.health", actor_id="uep:health", scope_ref="scope_private_owner"),
    ).event
    assert state.project_event(event) is True
    assert state.health() == {"tts": "unavailable"}
    assert state.current(Axis.INTERACTION).state == "THINKING"  # 沒被寫成 ERROR
    assert state.current(Axis.RUNTIME).state == "READY"
    assert state.project_event(event) is False  # 重播為 no-op


# ---- S-6：投影可由事件重建、重播不重複 ----

def test_presence_projection_follows_events_and_replay_is_noop(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    state = stack.state
    activate_endpoint(stack.manager)
    stack.manager.revoke("t")
    events = [
        e for t in ("presence.granted", "presence.activated", "presence.revoked")
        for e in journal_events(session_factory, t)
    ]
    events.sort(key=lambda e: e.aggregate_version or 0)
    changed = [state.project_event(e) for e in events]
    assert changed == [True, True, True]
    view = state.current(Axis.PRESENCE)
    assert view.state == "NONE" and view.version == 3
    assert [state.project_event(e) for e in events] == [False, False, False]
    assert state.current(Axis.PRESENCE).version == 3


def test_late_old_event_does_not_regress_projection(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    activate_endpoint(stack.manager)
    activated = journal_events(session_factory, "presence.activated")[0]
    stack.manager.revoke("t")
    revoked = journal_events(session_factory, "presence.revoked")[0]
    assert stack.state.project_event(revoked) is True
    assert stack.state.project_event(activated) is False  # 晚到的舊事件
    assert stack.state.current(Axis.PRESENCE).state == "NONE"


def test_foreign_and_stateless_events_are_ignored(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    activate_endpoint(stack.manager)
    granted = journal_events(session_factory, "presence.granted")[0]
    other = granted.model_copy(update={"persona_id": "someone_else"})
    assert stack.state.project_event(other) is False
    assert stack.state.current(Axis.PRESENCE).version == 0


# ---- DB 保護 ----

def test_state_requests_are_append_only_and_projection_never_regresses(session_factory):
    state = make_state(session_factory)
    state.request_transition(req())
    with session_factory() as s, pytest.raises(DBAPIError), s.begin():
        s.execute(text("UPDATE state_requests SET decision = 'ACCEPTED'"))
    with session_factory() as s, pytest.raises(DBAPIError), s.begin():
        s.execute(text("DELETE FROM state_requests"))
    with session_factory() as s, pytest.raises(DBAPIError), s.begin():
        s.execute(text("UPDATE state_projection SET version = 0 WHERE axis = 'Interaction'"))
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(StateProjectionRow)) >= 2


def test_flag_blocker_reads_latest_committed_value(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    blocker = make_flag_blocker("uep")
    with session_factory() as s:
        assert blocker(s) is None
        stack.stop.engage(operator="tester")  # 另一個連線提交
        assert blocker(s) == "global_stop_engaged"  # 同一 session 仍讀到最新值（populate_existing）


# ---- 審查修正 ----

def test_proposers_are_fail_closed_when_not_configured(session_factory):
    state = make_state(session_factory, authorized_proposers={})
    assert state.request_transition(req(proposer="nlp")).decision is Decision.REJECTED_UNAUTHORIZED
    bare = StateCoordinator(session_factory, EventPublisher(session_factory, EventBus()), clock=FakeClock())
    bare.set_runtime(RuntimeState.READY)
    assert bare.request_transition(req(proposer="nlp")).decision is Decision.REJECTED_UNAUTHORIZED
    assert bare.current(Axis.INTERACTION).version == 0


def test_project_event_only_trusts_expected_sources(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    activate_endpoint(stack.manager)
    granted = journal_events(session_factory, "presence.granted")[0]
    forged = granted.model_copy(update={"source": "core.evil"})
    assert stack.state.project_event(forged) is False
    assert stack.state.current(Axis.PRESENCE).version == 0
    assert stack.state.project_event(granted) is True
    health = stack.publisher.publish(
        EventDraft(event_type="core.component_health_changed", payload={"ref_id": "tts", "status": "unavailable"}),
        TrustedContext(source="adapter.fake", actor_id="x", scope_ref="scope_private_owner"),
    ).event
    assert stack.state.project_event(health) is False
    assert stack.state.health() == {}


def _health_event(stack, status: str, source: str = "core.health"):
    return stack.publisher.publish(
        EventDraft(event_type="core.component_health_changed", payload={"ref_id": "tts", "status": status}),
        TrustedContext(source=source, actor_id="uep:health", scope_ref="scope_private_owner"),
    ).event


def test_health_late_old_event_is_noop(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    old = _health_event(stack, "unavailable")
    new = _health_event(stack, "ready")
    assert stack.state.project_event(new) is True
    assert stack.state.project_event(old) is False  # 晚到的舊事件不得讓 ready 退回 unavailable
    assert stack.state.health() == {"tts": "ready"}


def test_projection_db_guards_state_change_pk_and_delete(session_factory):
    state = make_state(session_factory)
    state.request_transition(req())
    for sql in (
        "UPDATE state_projection SET state = 'THINKING' WHERE axis = 'Interaction'",  # 同 version 改 state
        "UPDATE state_projection SET subject = 'x' WHERE axis = 'Interaction'",  # 改主鍵
        "DELETE FROM state_projection",
    ):
        with session_factory() as s, pytest.raises(DBAPIError), s.begin():
            s.execute(text(sql))
    assert state.current(Axis.INTERACTION).state == "LISTENING"
