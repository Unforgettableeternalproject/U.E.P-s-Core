"""Supervisor 接線（切片 F-3 剩餘；D67、SV-1、VC-SV-1）：組裝層建立、背景 tick 迴圈、關機暫停。

離線：``SupervisorLoop`` 與 ``Supervisor.shutdown`` 對假 manager。DB（需 UEP_TEST_DATABASE_URL，未設時 skip）：組裝後
Supervisor 存在／被 tick／可關閉，以及 VC-SV-1 驗收（RUNNING／WAITING_INPUT／WAITING_APPROVAL 關機後 PAUSED 且
``paused_from`` 正確，重啟後 ``resume_run`` 回各自暫停前狀態；UNKNOWN 略過）。
"""
from __future__ import annotations

import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.uep.test_assembly import FREE, kwargs
from tests.uep.test_task_model import StubExecutor, make_manager
from uep.assembly import assemble_core
from uep.config import get_settings
from uep.events.models import EventRow
from uep.tasks.states import PauseReason, RunStatus
from uep.tasks.supervisor import Supervisor, SupervisorLoop
from uep.tools.cancel import ActionCanceller

SCOPE = "scope_private_owner"


# ---- 離線：SupervisorLoop ----

class _CountingSupervisor:
    def __init__(self, fail_first: int = 0) -> None:
        self.calls = 0
        self._fail_first = fail_first

    def tick(self) -> None:
        self.calls += 1
        if self.calls <= self._fail_first:
            raise RuntimeError("tick 爆了")


def _run_loop(sup, rounds: int) -> SupervisorLoop:
    """注入等待函式：前 ``rounds`` 次回 False（繼續），之後回 True（停止），執行緒因此確定性結束。"""
    left = [rounds]

    def wait(_interval: float) -> bool:
        left[0] -= 1
        return left[0] < 0

    loop = SupervisorLoop(sup, interval=0.01, wait=wait)
    loop.start()
    loop._thread.join(timeout=5)
    assert not loop._thread.is_alive()
    return loop


def test_loop_calls_tick_each_round():
    sup = _CountingSupervisor()
    loop = _run_loop(sup, 3)
    assert sup.calls == 3 and loop.ticks == 3 and loop.failures == 0


def test_loop_survives_tick_exceptions():
    sup = _CountingSupervisor(fail_first=2)
    loop = _run_loop(sup, 4)
    assert sup.calls == 4  # 前兩次爆了，執行緒仍繼續
    assert loop.failures == 2 and loop.ticks == 4


def test_loop_stop_ends_background_thread():
    sup = _CountingSupervisor()
    loop = SupervisorLoop(sup, interval=0.01)
    loop.start()
    deadline = time.monotonic() + 3
    while sup.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sup.calls > 0
    loop.stop()
    assert loop._thread is None
    assert not any(t.name == "UepSupervisor" for t in threading.enumerate())


def test_loop_rejects_non_positive_interval():
    with pytest.raises(ValueError):
        SupervisorLoop(_CountingSupervisor(), interval=0)


# ---- 離線：Supervisor.shutdown ----

class _FakeTasks:
    def __init__(self, *, executing: bool, pause_raises: bool = False) -> None:
        self.executing = executing
        self.pause_raises = pause_raises
        self.paused: list[tuple[str, PauseReason]] = []

    def list_resumable_runs(self):
        return [SimpleNamespace(run_id="run_1", task_id="task_1", status="running")]

    def list_live_tasks(self):
        if self.pause_raises:
            raise RuntimeError("DB 掛了")
        return [SimpleNamespace(task_id="task_1", status="running")]

    def pause_task(self, task_id, reason):
        self.paused.append((task_id, reason))

    def list_executing_actions(self, _run_id):
        return [object()] if self.executing else []


def test_shutdown_pauses_all_with_core_shutdown_and_returns_quickly_without_executing():
    tasks = _FakeTasks(executing=False)
    start = time.monotonic()
    paused = Supervisor(tasks, shutdown_grace=timedelta(seconds=10)).shutdown()
    assert paused == ["task_1"] and tasks.paused == [("task_1", PauseReason.CORE_SHUTDOWN)]
    assert time.monotonic() - start < 2


def test_shutdown_stops_waiting_for_receipts_after_grace():
    tasks = _FakeTasks(executing=True)  # receipt 永遠不來
    start = time.monotonic()
    paused = Supervisor(tasks, shutdown_grace=timedelta(milliseconds=300)).shutdown()
    elapsed = time.monotonic() - start
    assert paused == ["task_1"]
    assert 0.25 <= elapsed < 3  # 等滿 grace 即放棄，不無限期阻擋關機


def test_shutdown_never_raises_when_pause_fails():
    sup = Supervisor(_FakeTasks(executing=False, pause_raises=True), shutdown_grace=timedelta(milliseconds=100))
    assert sup.shutdown() == []
    assert sup.failures == 1


# ---- DB：組裝 ----

@pytest.fixture
def build(session_factory, tmp_path):
    made = []

    def _build(**over):
        core = assemble_core(session_factory, **kwargs(session_factory, tmp_path, **over))
        made.append(core)
        return core

    yield _build
    for core in made:
        core.shutdown()


def test_assembly_builds_supervisor_with_action_canceller(build):
    core = build()
    assert core.supervisor is not None
    cancel_actions = core.supervisor._cancel_actions
    assert isinstance(cancel_actions.__self__, ActionCanceller)
    # 第 4 點：迴圈與 maintenance_interval 無關（kwargs 傳 maintenance_interval=None 仍建立），supervisor_interval=0 才不建
    assert core.supervisor_loop is not None
    assert build(supervisor_interval=0).supervisor_loop is None


def test_assembly_rejects_orphan_older_than_not_above_turn_max_duration(build, monkeypatch):
    """B：older_than 必須大於 TURN_MAX_DURATION，組裝時就拒絕（不是等到第一個 tick 才炸）。"""
    from uep.assembly import AssemblyError
    from uep.tasks.manager import TURN_MAX_DURATION

    monkeypatch.setattr(get_settings(), "supervisor_orphan_older_than_s", TURN_MAX_DURATION.total_seconds())
    with pytest.raises(AssemblyError, match="TURN_MAX_DURATION"):
        build()
    assert build(supervisor=False).supervisor is None  # 停用時不驗證
    monkeypatch.setattr(get_settings(), "supervisor_orphan_older_than_s", TURN_MAX_DURATION.total_seconds() + 1)
    assert build().supervisor._older_than == TURN_MAX_DURATION + timedelta(seconds=1)
    assert get_settings().supervisor_orphan_older_than_s > 0


def test_assembly_supervisor_disabled_by_flag_or_setting(build, monkeypatch):
    assert build(supervisor=False).supervisor is None
    monkeypatch.setattr(get_settings(), "supervisor_enabled", False)
    off = build()
    assert off.supervisor is None and off.supervisor_loop is None
    assert build(supervisor=True).supervisor is not None  # 明示參數優先於設定


def test_assembly_supervisor_uses_settings_graces(build, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "supervisor_shutdown_grace_s", 3.0)
    monkeypatch.setattr(settings, "supervisor_deadline_grace_s", 7.0)
    monkeypatch.setattr(settings, "supervisor_cancel_timeout_s", 2.0)
    sup = build().supervisor
    assert sup._shutdown_grace == timedelta(seconds=3) and sup._grace == timedelta(seconds=7)
    assert sup._cancel_actions.__self__._timeout_s == 2.0


def test_assembly_starts_loop_ticks_and_stops_with_shutdown(build, monkeypatch):
    monkeypatch.setattr(get_settings(), "supervisor_interval_s", 0.05)
    core = build()  # maintenance_interval=None：Supervisor 迴圈仍照設定啟動
    assert core.maintenance is None and core.supervisor_loop is not None
    core.start()
    assert "supervisor.start" in core.start_log
    deadline = time.monotonic() + 5
    while core.supervisor_loop.ticks == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert core.supervisor.ticks > 0 and core.supervisor_loop.failures == 0
    core.shutdown()
    assert "supervisor.shutdown" in core.start_log
    assert not any(t.name == "UepSupervisor" for t in threading.enumerate())


def test_assembly_without_supervisor_shuts_down_without_pausing(build):
    core = build(supervisor=False)
    core.start()
    task = core.tasks.create_task(persona_id="uep", goal="g", owner_actor_id="o", scope_ref=SCOPE, policy=FREE)
    core.tasks.start_run(task.task_id, persona_version=1, policy_version=1)
    core.shutdown()
    assert "supervisor.shutdown" not in core.start_log
    assert core.tasks.get_task(task.task_id).status == RunStatus.RUNNING.value


# ---- DB：VC-SV-1 ----

def _live(core, goal: str, to: RunStatus | None):
    task = core.tasks.create_task(persona_id="uep", goal=goal, owner_actor_id="o", scope_ref=SCOPE, policy=FREE)
    run = core.tasks.start_run(task.task_id, persona_version=1, policy_version=1)
    if to is not None:
        core.tasks.transition_run(run.run_id, to, worker_epoch=run.worker_epoch)
        core.tasks.transition_task(task.task_id, to)
    return task, run


def test_vc_sv_1_shutdown_pauses_live_tasks_with_paused_from_and_resume_restores(build, session_factory):
    core = build()
    core.start()
    cases = {
        "running": _live(core, "running", None),
        "input": _live(core, "input", RunStatus.WAITING_INPUT),
        "approval": _live(core, "approval", RunStatus.WAITING_APPROVAL),
    }
    unknown_task, unknown_run = _live(core, "unknown", RunStatus.UNKNOWN)
    expected = {
        "running": RunStatus.RUNNING.value, "input": RunStatus.WAITING_INPUT.value,
        "approval": RunStatus.WAITING_APPROVAL.value,
    }

    start = time.monotonic()
    core.shutdown()
    assert time.monotonic() - start < get_settings().supervisor_shutdown_grace_s  # 無 EXECUTING action：不必等滿 grace

    # 以全新 manager 檢查（模擬重啟後）
    mgr = make_manager(session_factory, StubExecutor())
    for key, (task, run) in cases.items():
        t, r = mgr.get_task(task.task_id), mgr.get_run(run.run_id)
        assert t.status == RunStatus.PAUSED.value and t.pause_reason == PauseReason.CORE_SHUTDOWN.value
        assert r.status == RunStatus.PAUSED.value and r.pause_reason == PauseReason.CORE_SHUTDOWN.value
        assert t.paused_from == expected[key] and r.paused_from == expected[key]
    assert mgr.get_task(unknown_task.task_id).status == RunStatus.UNKNOWN.value  # 略過
    assert mgr.get_run(unknown_run.run_id).status == RunStatus.UNKNOWN.value

    with session_factory() as s:
        types = set(s.scalars(select(EventRow.event_type).where(EventRow.event_type.like("task.%"))))
    assert "task.cancelled" not in types and "task.failed" not in types

    for key, (task, run) in cases.items():
        mgr.resume_run(run.run_id, stale_after=timedelta(0))
        assert mgr.get_run(run.run_id).status == expected[key]
        assert mgr.get_task(task.task_id).status == expected[key]


# ---- A：回合 Run 不會被 Supervisor force ----

def test_turn_run_without_heartbeat_is_never_forced_however_old(session_factory):
    """A：direct_chat 回合 Run 沒有 heartbeat、沒有 deadline；比 stale_after（與孤兒門檻以內）久也不會被 force。"""
    from datetime import UTC, datetime

    mgr = make_manager(session_factory, StubExecutor(), heartbeat_stale_after=timedelta(seconds=60))
    task, run = mgr.get_or_create_turn_task(
        "turn_a1", persona_id="uep", owner_actor_id="o", scope_ref=SCOPE, policy_ref="pol_t",
        persona_version=1, policy_version=1,
    )
    assert run.run_deadline is None and task.deadline is None  # 回合 Run 只有建立時的一次心跳（之後不再更新），也沒有 deadline
    sup = Supervisor(mgr, shutdown_grace=timedelta(seconds=1))
    later = datetime.now(UTC) + timedelta(minutes=20)  # 遠大於 stale_after，仍小於孤兒門檻（30 分鐘）
    stats = sup.tick(later)
    assert (stats.forced, stats.cancel_requested, stats.tasks_failed, stats.failures) == (0, 0, 0, 0)
    assert mgr.get_run(run.run_id).status == RunStatus.RUNNING.value
    assert mgr.get_run(run.run_id).worker_epoch == run.worker_epoch  # 沒有 epoch 遞增（沒 force）
    assert mgr.get_task(task.task_id).status == RunStatus.RUNNING.value
    assert sup.tick(later + timedelta(hours=1)).orphans_closed == 1  # 真的孤兒才由掃描收尾（FAILED，非 force）


# ---- M1：進行中的回合 Task 與關機 ----

class _FakeEcho:
    """代替 ES Host：stop() 時像 Agent Core 一樣把進行中的回合 Task 收尾（finish_turn_task），並記下呼叫序。"""

    def __init__(self, tasks, task_id, log) -> None:
        self._tasks, self._task_id, self._log = tasks, task_id, log

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self._log.append("echo.stop")
        self._tasks.finish_turn_task(self._task_id)


def test_inflight_turn_task_finishes_before_pause_and_no_task_failed(build, session_factory):
    core = build()
    core.start()
    task, _run = core.tasks.get_or_create_turn_task(
        "turn_m1", persona_id="uep", owner_actor_id="o", scope_ref=SCOPE, policy_ref="pol_t",
        persona_version=1, policy_version=1,
    )
    calls: list[str] = []
    core.echo = _FakeEcho(core.tasks, task.task_id, calls)
    real_shutdown = core.supervisor.shutdown
    core.supervisor.shutdown = lambda *a, **k: (calls.append("supervisor.shutdown"), real_shutdown(*a, **k))[1]
    other = core.tasks.create_task(persona_id="uep", goal="other", owner_actor_id="o", scope_ref=SCOPE, policy=FREE)
    core.tasks.start_run(other.task_id, persona_version=1, policy_version=1)
    core.shutdown()
    assert calls == ["echo.stop", "supervisor.shutdown"]  # 入口先停、再暫停
    assert core.tasks.get_task(task.task_id).status == RunStatus.SUCCEEDED.value  # 回合收尾在 pause 之前：不是 FAILED
    assert core.tasks.get_task(other.task_id).status == RunStatus.PAUSED.value
    with session_factory() as s:
        failed = list(s.scalars(select(EventRow.event_type).where(EventRow.event_type == "task.failed")))
    assert failed == []


# ---- m1：啟動失敗的收尾路徑會停 Supervisor 迴圈 ----

def test_start_failure_stops_supervisor_loop(build):
    core = build()

    class Boom:
        def start(self):
            raise RuntimeError("echo 起不來")

        def stop(self):
            pass

    core.echo = Boom()
    with pytest.raises(RuntimeError):
        core.start()
    assert core.supervisor_loop._thread is None  # 已停止


# ---- m2：stop() 逾時保留執行緒參照 ----

def test_loop_stop_timeout_keeps_thread_reference_and_does_not_double_start():
    gate = threading.Event()

    class Stuck:
        def tick(self):
            gate.wait(10)

    loop = SupervisorLoop(Stuck(), interval=0.01)
    loop.start()
    time.sleep(0.1)
    try:
        loop.stop(timeout=0.1)
        first = loop._thread
        assert first is not None and first.is_alive()
        loop.start()
        assert loop._thread is first  # 沒有第二條 tick 執行緒
    finally:
        gate.set()
        loop.stop(timeout=5)
    assert loop._thread is None

def test_turn_task_and_run_never_carry_a_deadline(session_factory):
    """鎖住 A 的地雷：回合 Task／Run（``get_or_create_turn_task`` 實際建立路徑）的 deadline 一律為 None。

    回合 Run 只在建立時寫一次心跳，之後不再更新；若有人替它設 ``run_deadline``（或 Task.deadline），
    心跳很快過期，Supervisor 到期時會立刻 force 一個仍在進行的回合。要給回合加 deadline，須先讓回合 Run 持續送心跳。"""
    mgr = make_manager(session_factory, StubExecutor())
    for turn_id, extra in (("turn_dl1", {}), ("turn_dl2", {"conversation_id": "conv_1", "segment_id": "seg_1"})):
        task, run = mgr.get_or_create_turn_task(
            turn_id, persona_id="uep", owner_actor_id="o", scope_ref=SCOPE, policy_ref="pol_t",
            persona_version=1, policy_version=1, **extra,
        )
        assert run.run_deadline is None and task.deadline is None
        again_task, again_run = mgr.get_or_create_turn_task(  # 冪等回讀的路徑同樣沒有 deadline
            turn_id, persona_id="uep", owner_actor_id="o", scope_ref=SCOPE, policy_ref="pol_t",
            persona_version=1, policy_version=1,
        )
        assert again_run.run_deadline is None and again_task.deadline is None
        assert mgr.get_run(run.run_id).run_deadline is None and mgr.get_task(task.task_id).deadline is None
