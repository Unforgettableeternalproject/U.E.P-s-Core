"""切片 F-3（離線）：Supervisor 元件（SYS 契約 §2.7 SV-1～SV-4、VC-SV-1／VC-SV-2）。fake clock + 記憶體版 TaskManager 替身。

打真實 Manager／PostgreSQL 的對照測試在 ``test_supervisor_db.py``（需 DB，未設 UEP_TEST_DATABASE_URL 時 skip）。
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.uep.loop_support import FakeTasks, make_registry
from uep.tasks.manager import TURN_MAX_DURATION, CancelSignal, IllegalTransition
from uep.tasks.states import PauseReason
from uep.tasks.supervisor import Supervisor, approval_expired_key

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
STALE = timedelta(seconds=60)
GRACE = timedelta(seconds=30)


class FakeManager:
    """Supervisor 用到的 TaskManager 公開方法的記憶體版（語意對齊真實 Manager，DB 對照見 test_supervisor_db.py）。"""

    heartbeat_stale_after = STALE

    def __init__(self) -> None:
        self.runs: dict[str, SimpleNamespace] = {}
        self.tasks: dict[str, SimpleNamespace] = {}
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()  # 方法名 -> 拋 RuntimeError
        self.swept: list[tuple] = []

    # ---- 建構輔助 ----
    def add(self, run_id="run_1", *, status="RUNNING", run_deadline=None, task_deadline=None, beat=None, task_status=None, **extra):
        task_id = f"task_{run_id}"
        self.tasks[task_id] = SimpleNamespace(task_id=task_id, status=task_status or status, deadline=task_deadline)
        self.runs[run_id] = SimpleNamespace(
            run_id=run_id, task_id=task_id, status=status, run_deadline=run_deadline, worker_heartbeat_at=beat,
            cancel_requested_at=None, cancel_reason=None, approval_expiry=None, approval_expired_at=None, **extra,
        )
        return self.runs[run_id]

    def _maybe_fail(self, name):
        self.calls.append((name,))
        if name in self.fail_on:
            raise RuntimeError(name)

    # ---- Supervisor 使用的介面 ----
    def list_runs_due(self, now):
        self._maybe_fail("list_runs_due")
        return [SimpleNamespace(**vars(r)) for r in self.runs.values() if self.tasks[r.task_id].status not in ("FAILED", "SUCCEEDED", "CANCELLED")]

    def get_task(self, task_id):
        return self.tasks[task_id]

    def request_cancel(self, run_id, reason):
        self._maybe_fail("request_cancel")
        run = self.runs[run_id]
        if run.status in ("FAILED", "SUCCEEDED", "CANCELLED") or run.cancel_requested_at is not None:
            return False
        run.cancel_requested_at, run.cancel_reason = self.now, reason.value
        self.calls.append(("request_cancel", run_id, reason.value))
        return True

    def mark_approval_expired(self, run_id, expiry):
        self._maybe_fail("mark_approval_expired")
        run = self.runs[run_id]
        if run.status != "WAITING_APPROVAL" or run.approval_expired_at == expiry:
            return False
        run.approval_expired_at = expiry
        return True

    def force_transition_run(self, run_id, to, *, reason, **_):
        self._maybe_fail("force_transition_run")
        run = self.runs[run_id]
        if run.status in ("FAILED", "SUCCEEDED", "CANCELLED"):
            raise IllegalTransition("終態")
        run.status = to.value
        self.calls.append(("force", run_id, to.value, reason))
        return run

    def transition_task(self, task_id, to, **_):
        self._maybe_fail("transition_task")
        task = self.tasks[task_id]
        if task.status in ("FAILED", "SUCCEEDED", "CANCELLED"):
            raise IllegalTransition("終態")
        task.status = to.value
        self.calls.append(("transition_task", task_id, to.value))
        return task

    def sweep_orphan_turn_tasks(self, older_than, *, now=None, limit=100):
        self._maybe_fail("sweep_orphan_turn_tasks")
        self.swept.append((older_than, now))
        return ["task_orphan"]

    def list_live_tasks(self):
        return [t for t in self.tasks.values() if t.status not in ("FAILED", "SUCCEEDED", "CANCELLED")]

    def list_resumable_runs(self):
        return list(self.runs.values())

    def pause_task(self, task_id, reason):
        self.calls.append(("pause_task", task_id, reason.value))
        if "pause_task" in self.fail_on and task_id in self.fail_on:
            raise RuntimeError(task_id)
        self.tasks[task_id].status = "PAUSED"

    now = T0


def make(**kwargs):
    manager = FakeManager()
    notified: list[tuple[str, str]] = []
    sup = Supervisor(manager, clock=lambda: manager.now, notify=lambda run_id, key: notified.append((run_id, key)), **kwargs)  # type: ignore[arg-type]
    return manager, sup, notified


def at(manager, seconds):
    manager.now = T0 + timedelta(seconds=seconds)
    return manager.now


def names(manager):
    return [c[0] for c in manager.calls]


# ---- SV-2 deadline ----

def test_fresh_heartbeat_only_sets_the_flag_and_never_forces():
    manager, sup, _ = make()
    manager.add(run_deadline=T0 - timedelta(seconds=1), beat=T0 - timedelta(seconds=5))
    stats = sup.tick(T0)
    assert (stats.cancel_requested, stats.forced, stats.tasks_failed) == (1, 0, 0)
    assert manager.runs["run_1"].cancel_reason == "run_deadline" and manager.runs["run_1"].status == "RUNNING"
    # 旗標已設、仍在 grace 內、心跳仍新鮮：之後的 tick 也不 force
    manager.runs["run_1"].worker_heartbeat_at = T0 + timedelta(seconds=20)
    assert sup.tick(at(manager, 25)).forced == 0
    assert "force" not in names(manager)


def test_force_only_after_deadline_grace_then_task_is_failed():
    manager, sup, _ = make()
    manager.add(run_deadline=T0 - timedelta(seconds=1), beat=T0)
    sup.tick(T0)
    manager.runs["run_1"].worker_heartbeat_at = T0 + timedelta(seconds=30)  # 心跳仍新鮮，但已超過 grace
    stats = sup.tick(at(manager, 31))
    assert stats.forced == 1 and stats.tasks_failed == 1
    assert ("force", "run_1", "FAILED", "run_deadline") in manager.calls
    assert manager.calls.index(("force", "run_1", "FAILED", "run_deadline")) < manager.calls.index(("transition_task", "task_run_1", "FAILED"))
    assert manager.tasks["task_run_1"].status == "FAILED"


def test_grace_boundary_is_exclusive():
    manager, sup, _ = make()
    manager.add(run_deadline=T0 - timedelta(seconds=1), beat=T0)
    sup.tick(T0)
    manager.runs["run_1"].worker_heartbeat_at = T0 + timedelta(seconds=30)
    assert sup.tick(at(manager, 30)).forced == 0  # 剛好 grace 不 force（要「超過」）


def test_stale_heartbeat_forces_in_the_same_tick():
    manager, sup, _ = make()
    manager.add(run_deadline=T0 - timedelta(seconds=1), beat=T0 - STALE - timedelta(seconds=1))
    stats = sup.tick(T0)
    assert (stats.cancel_requested, stats.forced, stats.tasks_failed) == (1, 1, 1)


def test_parked_run_without_heartbeat_counts_as_worker_gone():
    manager, sup, _ = make()
    manager.add(status="WAITING_APPROVAL", run_deadline=T0 - timedelta(seconds=1), beat=None)  # park_run 清了心跳
    assert sup.tick(T0).forced == 1


def test_task_deadline_uses_its_own_reason():
    manager, sup, _ = make()
    manager.add(task_deadline=T0 - timedelta(seconds=1), beat=T0)
    sup.tick(T0)
    assert manager.runs["run_1"].cancel_reason == "task_deadline"
    manager.runs["run_1"].worker_heartbeat_at = T0 + timedelta(seconds=40)
    sup.tick(at(manager, 41))
    assert ("force", "run_1", "FAILED", "task_deadline") in manager.calls


def test_run_that_the_worker_already_ended_only_needs_task_closure():
    manager, sup, _ = make()
    run = manager.add(status="FAILED", task_status="RUNNING", beat=T0)
    run.cancel_reason, run.cancel_requested_at = "run_deadline", T0
    stats = sup.tick(T0)
    assert (stats.forced, stats.tasks_failed) == (0, 1) and "force" not in names(manager)


def test_step_timeout_flag_never_fails_the_run():
    manager, sup, _ = make()
    run = manager.add(beat=T0 - timedelta(hours=1))  # 心跳早就過期也不 force：step_timeout 只取消 Step
    run.cancel_requested_at, run.cancel_reason = T0 - timedelta(hours=1), "step_timeout"
    assert sup.tick(T0).forced == 0 and manager.runs["run_1"].status == "RUNNING"


def test_paused_run_is_not_cancelled_by_deadline():
    manager, sup, _ = make()
    manager.add(status="PAUSED", run_deadline=T0 - timedelta(hours=1), beat=None)
    assert sup.tick(T0).cancel_requested == 0


def test_no_deadline_no_action():
    manager, sup, _ = make()
    manager.add(run_deadline=T0 + timedelta(hours=1), beat=T0)
    stats = sup.tick(T0)
    assert (stats.cancel_requested, stats.forced, stats.tasks_failed, stats.approvals_expired) == (0, 0, 0, 0)


def test_force_losing_the_race_to_the_worker_is_benign():
    manager, sup, _ = make()
    manager.add(run_deadline=T0 - timedelta(seconds=1), beat=None)
    original = manager.force_transition_run

    def lost(run_id, to, **kw):
        manager.runs[run_id].status = "FAILED"  # worker 剛好自己轉成終態
        return original(run_id, to, **kw)

    manager.force_transition_run = lost
    stats = sup.tick(T0)
    assert stats.failures == 0 and stats.forced == 0 and stats.tasks_failed == 1


# ---- SV-2(5) 核准過期 ----

def test_approval_expiry_notifies_once_per_expiry_and_again_after_it_changes():
    manager, sup, notified = make()
    expiry = T0 - timedelta(minutes=1)
    run = manager.add(status="WAITING_APPROVAL", beat=None)
    run.approval_expiry = expiry
    assert sup.tick(T0).notified == 1
    assert notified == [("run_1", f"appr_expired:run_1:{int(expiry.timestamp())}")]
    assert sup.tick(at(manager, 5)).notified == 0 and len(notified) == 1  # 同一 expiry 只通知一次
    new_expiry = T0 + timedelta(minutes=10)  # 使用者重新核准使 approval_expiry 更新
    manager.runs["run_1"].approval_expiry = new_expiry
    assert sup.tick(at(manager, 60)).notified == 0  # 還沒到期
    assert sup.tick(at(manager, 11 * 60)).notified == 1
    assert notified[-1][1] == f"appr_expired:run_1:{int(new_expiry.timestamp())}" and len(notified) == 2
    assert manager.runs["run_1"].status == "WAITING_APPROVAL"  # 不自動轉失敗（SA-8）


def test_approval_key_format_matches_e20_charset():
    key = approval_expired_key("run_abc123", datetime(2026, 11, 6, 12, 0, 0, 999999, tzinfo=UTC))
    assert re.fullmatch(r"[A-Za-z0-9_:.\-]+", key) and key == "appr_expired:run_abc123:1793966400"
    assert "T" not in key.split(":")[-1]  # 不放 ISO 時間字串


def test_notify_failure_is_counted_and_does_not_stop_the_tick():
    manager = FakeManager()
    sup = Supervisor(manager, clock=lambda: T0, notify=lambda *_: (_ for _ in ()).throw(RuntimeError("通道壞了")))  # type: ignore[arg-type]
    run = manager.add(status="WAITING_APPROVAL", beat=None)
    run.approval_expiry = T0 - timedelta(seconds=1)
    manager.add("run_2", run_deadline=T0 - timedelta(seconds=1), beat=T0)
    stats = sup.tick()
    assert stats.failures == 1 and stats.cancel_requested == 1 and sup.failures == 1


def test_default_notify_is_a_noop():
    manager = FakeManager()
    sup = Supervisor(manager, clock=lambda: T0)  # type: ignore[arg-type]
    run = manager.add(status="WAITING_APPROVAL", beat=None)
    run.approval_expiry = T0 - timedelta(seconds=1)
    assert sup.tick().notified == 1


def test_non_waiting_run_is_never_marked():
    manager, sup, notified = make()
    run = manager.add(status="RUNNING", beat=T0)
    run.approval_expiry = T0 - timedelta(seconds=1)
    assert sup.tick(T0).approvals_expired == 0 and notified == []


# ---- 孤兒掃描與失敗隔離 ----

def test_orphan_sweep_is_called_with_older_than_and_now():
    manager, sup, _ = make(older_than=timedelta(minutes=45))
    stats = sup.tick(T0)
    assert manager.swept == [(timedelta(minutes=45), T0)] and stats.orphans_closed == 1


def test_one_failing_run_is_counted_and_the_rest_continue():
    manager, sup, _ = make()
    manager.add("run_1", run_deadline=T0 - timedelta(seconds=1), beat=T0)
    manager.add("run_2", run_deadline=T0 - timedelta(seconds=1), beat=T0)
    original = manager.request_cancel

    def flaky(run_id, reason):
        if run_id == "run_1":
            raise RuntimeError("DB 暫時失敗")
        return original(run_id, reason)

    manager.request_cancel = flaky
    stats = sup.tick(T0)
    assert stats.failures == 1 and stats.cancel_requested == 1 and manager.runs["run_2"].cancel_reason == "run_deadline"
    manager.request_cancel = original  # 下個 tick 重試成功
    assert sup.tick(at(manager, 1)).cancel_requested == 1 and sup.ticks == 2


def test_query_and_sweep_failures_are_isolated():
    manager, sup, _ = make()
    manager.fail_on = {"list_runs_due", "sweep_orphan_turn_tasks"}
    stats = sup.tick(T0)
    assert stats.failures == 2 and sup.ticks == 1


# ---- 參數驗證 ----

@pytest.mark.parametrize("older_than", [TURN_MAX_DURATION, TURN_MAX_DURATION - timedelta(seconds=1), timedelta(0)])
def test_older_than_must_exceed_turn_max_duration(older_than):
    with pytest.raises(ValueError):
        Supervisor(FakeManager(), older_than=older_than)  # type: ignore[arg-type]


def test_deadline_grace_must_be_positive():
    with pytest.raises(ValueError):
        Supervisor(FakeManager(), deadline_grace=timedelta(0))  # type: ignore[arg-type]
    assert Supervisor(FakeManager()).__class__ is Supervisor  # 預設值合法（30 秒、3 倍 TURN_MAX_DURATION）  # type: ignore[arg-type]


# ---- SV-1 pause_all ----

def test_pause_all_skips_unknown_paused_and_isolates_failures():
    manager, sup, _ = make()
    manager.add("run_a", status="RUNNING")
    manager.add("run_b", status="WAITING_INPUT")
    manager.add("run_c", status="UNKNOWN")  # Task 與 Run 皆 UNKNOWN：無 PAUSED 邊
    manager.add("run_d", status="PAUSED")
    manager.add("run_e", status="RUNNING", task_status="RUNNING")
    manager.runs["run_e"].status = "UNKNOWN"  # 只有 Run UNKNOWN 也略過
    manager.add("run_f", status="RUNNING")
    manager.fail_on = {"pause_task", "task_run_a"}  # 單一失敗：run_a 的 Task 暫停拋例外
    paused = sup.pause_all(PauseReason.CORE_SHUTDOWN)
    assert paused == ["task_run_b", "task_run_f"] and sup.failures == 1
    assert [c[1] for c in manager.calls if c[0] == "pause_task"] == ["task_run_a", "task_run_b", "task_run_f"]
    assert all(c[2] == "core_shutdown" for c in manager.calls if c[0] == "pause_task")
    assert not any(c[0] in ("transition_task", "force") for c in manager.calls)  # 不寫 task.failed／cancelled


# ---- FakeTasks 對齊 ----

def test_fake_tasks_heartbeat_can_return_a_cancel_signal():
    tasks = FakeTasks(make_registry())
    assert tasks.heartbeat("run_1", worker_epoch=1) is None
    tasks.cancel_signal = CancelSignal("run_deadline", T0)
    assert tasks.heartbeat("run_1", worker_epoch=1) == CancelSignal("run_deadline", T0)
