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
    assert core.supervisor_loop is None  # maintenance_interval=None：不起背景執行緒


def test_assembly_supervisor_disabled_by_flag_or_setting(build, monkeypatch):
    assert build(supervisor=False).supervisor is None
    monkeypatch.setattr(get_settings(), "supervisor_enabled", False)
    off = build(maintenance_interval=0.05)
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
    core = build(maintenance_interval=0.05)
    assert core.supervisor_loop is not None
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
