"""切片 F-3：Supervisor 與 TaskManager 的 Supervisor 方法，對 PostgreSQL 的測試（需 UEP_TEST_DATABASE_URL；未設時 skip）。

涵蓋 VC-SV-2 兩個案例（心跳新鮮只設旗標、worker 讀到旗標自行收尾；超過 deadline_grace 才 force）、force 路徑的兩個條件
（EXECUTING -> UNKNOWN 與 run.ended 同一交易；force 遞增 worker_epoch 使舊 epoch 被 StaleWorkerEpoch 擋下）、核准過期
（同一 expiry 只標一次）、request_cancel 冪等、VC-SV-1 的 pause_all、migration 0013 往返。離線對照在 ``test_supervisor.py``。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select, update

from tests.uep.test_task_model import (
    POLICY,
    SCOPE,
    StubExecutor,
    add_action,
    make_manager,
)
from uep.events.models import EventRow
from uep.tasks.manager import ApprovalRequired, CancelSignal, StaleWorkerEpoch
from uep.tasks.models import ActionIntentRow, RunRow
from uep.tasks.states import ActionStatus, CancelReason, PauseReason, RunStatus
from uep.tasks.supervisor import Supervisor, approval_expired_key

pytestmark = pytest.mark.usefixtures("session_factory")
T0 = datetime.now(UTC).replace(microsecond=0)
GRACE = timedelta(seconds=30)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class Env:
    def __init__(self, session_factory, *, run_deadline=None, task_deadline=None, approval_expiry=None) -> None:
        self.sf = session_factory
        self.clock = Clock()
        self.executor = StubExecutor()
        self.mgr = make_manager(session_factory, self.executor, clock=self.clock, heartbeat_stale_after=timedelta(seconds=60))
        self.task = self.mgr.create_task(
            persona_id="uep", goal="整理報告", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY, deadline=task_deadline,
        )
        self.run = self.mgr.start_run(
            self.task.task_id, persona_version=1, policy_version=1, run_deadline=run_deadline, approval_expiry=approval_expiry,
        )
        self.epoch = self.run.worker_epoch
        self.mgr.heartbeat(self.run.run_id, worker_epoch=self.epoch)
        self.notified: list[tuple[str, str]] = []
        self.sup = Supervisor(
            self.mgr, deadline_grace=GRACE, clock=self.clock, notify=lambda run_id, key: self.notified.append((run_id, key)),
        )

    def run_row(self) -> RunRow:
        return self.mgr.get_run(self.run.run_id)

    def events(self, event_type: str) -> list[EventRow]:
        with self.sf() as s:
            return list(s.scalars(select(EventRow).where(EventRow.event_type == event_type).order_by(EventRow.journal_seq)))


def past(seconds=1):
    return T0 - timedelta(seconds=seconds)


# ---- VC-SV-2 ----

def test_fresh_heartbeat_sets_the_flag_only_and_the_worker_ends_the_run_itself(session_factory):
    env = Env(session_factory, run_deadline=past())
    stats = env.sup.tick(env.clock.now)
    assert (stats.cancel_requested, stats.forced, stats.tasks_failed) == (1, 0, 0)
    row = env.run_row()
    assert row.status == RunStatus.RUNNING.value and row.cancel_reason == "run_deadline" and row.cancel_requested_at is not None
    assert row.worker_epoch == env.epoch  # 沒有 force，epoch 不變
    signal = env.mgr.heartbeat(env.run.run_id, worker_epoch=env.epoch)  # worker 的下一次心跳讀到旗標
    assert signal == CancelSignal("run_deadline", row.cancel_requested_at)
    env.mgr.transition_run(env.run.run_id, RunStatus.FAILED, worker_epoch=env.epoch)  # worker 自行收尾
    after = env.sup.tick(env.clock.advance(5))
    assert (after.forced, after.tasks_failed) == (0, 1)
    assert env.mgr.get_task(env.task.task_id).status == RunStatus.FAILED.value


def test_unresponsive_worker_is_forced_after_deadline_grace_with_unresolved_actions(session_factory):
    env = Env(session_factory, run_deadline=past())
    action = add_action(env.mgr, env.run, env.mgr.begin_step(env.run.run_id, "work", worker_epoch=env.epoch), {"n": 1})
    with session_factory.begin() as s:
        s.execute(update(ActionIntentRow).where(ActionIntentRow.action_id == action.action_id).values(status="EXECUTING"))
    env.sup.tick(env.clock.now)
    env.mgr.heartbeat(env.run.run_id, worker_epoch=env.epoch)  # 心跳仍新鮮（卡在同步 executor 的 worker 其實無法 heartbeat）
    assert env.sup.tick(env.clock.advance(20)).forced == 0  # grace 內不 force
    env.mgr.heartbeat(env.run.run_id, worker_epoch=env.epoch)
    stats = env.sup.tick(env.clock.advance(11))  # 旗標設定後 31 秒 > deadline_grace
    assert (stats.forced, stats.tasks_failed) == (1, 1)
    assert env.run_row().status == RunStatus.FAILED.value
    assert env.mgr.get_task(env.task.task_id).status == RunStatus.FAILED.value
    (ended,) = env.events("run.ended")
    assert ended.payload["reason_code"] == "unresolved_actions" and ended.payload["action_id"] == action.action_id


def test_stale_heartbeat_forces_without_waiting_for_grace(session_factory):
    env = Env(session_factory, run_deadline=past())
    stats = env.sup.tick(env.clock.advance(61))  # 心跳已過期（T-12 條件成立）
    assert (stats.cancel_requested, stats.forced, stats.tasks_failed) == (1, 1, 1)


def test_task_deadline_flags_with_its_own_reason(session_factory):
    env = Env(session_factory, task_deadline=past())
    env.sup.tick(env.clock.now)
    assert env.run_row().cancel_reason == CancelReason.TASK_DEADLINE.value


# ---- force 路徑的兩個條件 ----

def test_force_turns_executing_into_unknown_in_the_same_transaction(session_factory):
    env = Env(session_factory)
    step = env.mgr.begin_step(env.run.run_id, "work", worker_epoch=env.epoch)
    action = add_action(env.mgr, env.run, step, {"n": 1})
    with session_factory.begin() as s:
        s.execute(update(ActionIntentRow).where(ActionIntentRow.action_id == action.action_id).values(status="EXECUTING"))
    env.mgr.force_transition_run(env.run.run_id, RunStatus.FAILED, reason="run_deadline")
    assert env.mgr.get_action(action.action_id).status == ActionStatus.UNKNOWN.value  # 不是 EXECUTING，也不是 FAILED
    (unknown,) = env.events("action.unknown")
    (ended,) = env.events("run.ended")
    assert unknown.txid == ended.txid  # 同一個 transaction
    assert ended.payload["reason_code"] == "unresolved_actions"


def test_force_increments_worker_epoch_so_a_slow_worker_is_fenced_off(session_factory):
    env = Env(session_factory)
    step = env.mgr.begin_step(env.run.run_id, "work", worker_epoch=env.epoch)
    action = add_action(env.mgr, env.run, step, {"n": 1})
    env.mgr.force_transition_run(env.run.run_id, RunStatus.FAILED, reason="run_deadline")
    assert env.run_row().worker_epoch == env.epoch + 1
    with pytest.raises(StaleWorkerEpoch):
        env.mgr.save_checkpoint(env.run.run_id, {"next_step_semantic": "think"}, worker_epoch=env.epoch)
    with pytest.raises(StaleWorkerEpoch):
        env.mgr.execute_action(action.action_id, {"n": 1}, worker_epoch=env.epoch)
    assert env.executor.calls == []


# ---- request_cancel ----

def test_request_cancel_is_idempotent_and_ignores_terminal_runs(session_factory):
    env = Env(session_factory)
    assert env.mgr.request_cancel(env.run.run_id, "run_deadline") is True
    first = env.run_row().cancel_requested_at
    env.clock.advance(10)
    assert env.mgr.request_cancel(env.run.run_id, CancelReason.TASK_DEADLINE) is False  # 不覆寫
    row = env.run_row()
    assert row.cancel_requested_at == first and row.cancel_reason == "run_deadline"
    with pytest.raises(ValueError):
        env.mgr.request_cancel(env.run.run_id, "not_a_reason")
    env.mgr.transition_run(env.run.run_id, RunStatus.FAILED, worker_epoch=env.epoch)
    env2 = Env(session_factory)
    env2.mgr.transition_run(env2.run.run_id, RunStatus.FAILED, worker_epoch=env2.epoch)
    assert env2.mgr.request_cancel(env2.run.run_id, "run_deadline") is False  # 終態不動作
    assert env2.run_row().cancel_requested_at is None


def test_request_cancel_does_not_check_worker_epoch_and_changes_no_status(session_factory):
    env = Env(session_factory)
    env.mgr.resume_run(env.run.run_id, stale_after=timedelta(0))  # epoch 已被取代
    assert env.mgr.request_cancel(env.run.run_id, "step_timeout") is True
    assert env.run_row().status == RunStatus.RUNNING.value


# ---- 核准過期 ----

def _waiting_env(session_factory, expiry):
    env = Env(session_factory, approval_expiry=expiry)
    step = env.mgr.begin_step(env.run.run_id, "work", worker_epoch=env.epoch)
    action = add_action(env.mgr, env.run, step, {"to": "a"}, side_effect="external_effect")
    with pytest.raises(ApprovalRequired):
        env.mgr.execute_action(action.action_id, {"to": "a"}, worker_epoch=env.epoch)
    assert env.run_row().status == RunStatus.WAITING_APPROVAL.value
    return env


def test_approval_expiry_is_marked_once_and_again_after_the_expiry_changes(session_factory):
    expiry = past(10)
    env = _waiting_env(session_factory, expiry)
    assert env.mgr.mark_approval_expired(env.run.run_id, expiry) is True
    assert env.mgr.mark_approval_expired(env.run.run_id, expiry) is False  # 同一 expiry 只設一次
    new_expiry = expiry + timedelta(hours=1)
    assert env.mgr.mark_approval_expired(env.run.run_id, new_expiry) is True  # expiry 更新後可再設
    assert env.run_row().approval_expired_at == new_expiry


def test_mark_approval_expired_requires_waiting_approval(session_factory):
    env = Env(session_factory)  # RUNNING
    assert env.mgr.mark_approval_expired(env.run.run_id, past()) is False
    assert env.run_row().approval_expired_at is None


def test_supervisor_notifies_an_expired_approval_once_per_expiry(session_factory):
    expiry = past(10)
    env = _waiting_env(session_factory, expiry)
    assert env.sup.tick(env.clock.now).notified == 1
    assert env.notified == [(env.run.run_id, approval_expired_key(env.run.run_id, expiry))]
    assert env.sup.tick(env.clock.advance(5)).notified == 0
    assert env.run_row().status == RunStatus.WAITING_APPROVAL.value  # 不自動轉失敗
    new_expiry = env.clock.now + timedelta(minutes=5)
    with session_factory.begin() as s:  # 使用者重新核准使 approval_expiry 更新
        s.execute(update(RunRow).where(RunRow.run_id == env.run.run_id).values(approval_expiry=new_expiry))
    assert env.sup.tick(env.clock.advance(60)).notified == 0
    assert env.sup.tick(env.clock.advance(300)).notified == 1
    assert len(env.notified) == 2


# ---- VC-SV-1 pause_all ----

def test_pause_all_pauses_running_and_waiting_input_and_skips_unknown(session_factory):
    env = Env(session_factory)  # RUNNING
    mgr = env.mgr
    other = mgr.create_task(persona_id="uep", goal="輸入", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY)
    other_run = mgr.start_run(other.task_id, persona_version=1, policy_version=1)
    mgr.transition_run(other_run.run_id, RunStatus.WAITING_INPUT, worker_epoch=other_run.worker_epoch)
    mgr.transition_task(other.task_id, RunStatus.WAITING_INPUT)
    unknown = mgr.create_task(persona_id="uep", goal="未知", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY)
    unknown_run = mgr.start_run(unknown.task_id, persona_version=1, policy_version=1)
    mgr.transition_run(unknown_run.run_id, RunStatus.UNKNOWN, worker_epoch=unknown_run.worker_epoch)
    mgr.transition_task(unknown.task_id, RunStatus.UNKNOWN)

    paused = env.sup.pause_all(PauseReason.CORE_SHUTDOWN)
    assert sorted(paused) == sorted([env.task.task_id, other.task_id])
    for task_id, run_id in ((env.task.task_id, env.run.run_id), (other.task_id, other_run.run_id)):
        assert mgr.get_task(task_id).status == RunStatus.PAUSED.value and mgr.get_task(task_id).pause_reason == "core_shutdown"
        assert mgr.get_run(run_id).status == RunStatus.PAUSED.value and mgr.get_run(run_id).pause_reason == "core_shutdown"
    assert mgr.get_task(unknown.task_id).status == RunStatus.UNKNOWN.value  # 略過
    assert env.events("task.failed") == [] and env.events("task.cancelled") == []
    # 注意：repo 尚未實作 SYS 契約 SA-7 的 paused_from（暫停前狀態），所以這裡只能斷言 PAUSED(core_shutdown)；
    # resume_run 目前一律回 RUNNING，不是暫停前的 WAITING_INPUT——契約與實作的落差，待裁決。


# ---- migration 0013 往返 ----

def test_db_migration_0013_upgrade_downgrade_roundtrip(alembic_cfg, engine):
    from alembic import command

    def columns():
        return {c["name"] for c in inspect(engine).get_columns("runs")}

    new = {"cancel_requested_at", "cancel_reason", "approval_expired_at"}
    assert new <= columns()
    try:
        command.downgrade(alembic_cfg, "0012_action_concurrency")
        assert not (new & columns())
    finally:
        command.upgrade(alembic_cfg, "head")
    assert new <= columns()
    assert "ck_runs_cancel_reason" in {c["name"] for c in inspect(engine).get_check_constraints("runs")}


def test_check_constraint_only_allows_the_three_cancel_reasons(session_factory):
    from sqlalchemy.exc import DBAPIError

    env = Env(session_factory)
    for reason in ("run_deadline", "task_deadline", "step_timeout"):
        with session_factory.begin() as s:
            s.execute(update(RunRow).where(RunRow.run_id == env.run.run_id).values(cancel_reason=reason))
    with pytest.raises(DBAPIError), session_factory.begin() as s:
        s.execute(update(RunRow).where(RunRow.run_id == env.run.run_id).values(cancel_reason="bogus"))
