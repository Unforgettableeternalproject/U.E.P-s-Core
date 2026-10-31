"""消費 presence.revoked／recovering／recovered 使 presence_bound Task 暫停（P-10、T-16 審查 (3)；需 PostgreSQL）。"""
from __future__ import annotations

import time

import pytest

from tests.uep.state_support import activate_endpoint, journal_events, make_stack
from uep.adapters.tool import ToolCall
from uep.contracts.presence import PresenceStatus
from uep.fakes.tool import FakeToolExecutor
from uep.fakes.tool_spec import FakeToolSpecResolver
from uep.tasks.manager import TaskManager, TaskPolicy
from uep.tasks.presence_bridge import PresenceTaskBridge
from uep.tasks.states import ActionStatus, InterruptPolicy, RunStatus

pytestmark = pytest.mark.usefixtures("session_factory")

SCOPE = "scope_private_owner"
BOUND = TaskPolicy(presence_bound=True, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_b")
BACKGROUND = TaskPolicy(presence_bound=False, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_g")


class HookExecutor(FakeToolExecutor):
    """execute 之前先執行 hook（模擬執行途中 Presence 離場）。"""

    def __init__(self) -> None:
        super().__init__()
        self.hook = lambda: None

    def execute(self, call: ToolCall):
        self.hook()
        return super().execute(call)


def build(session_factory, tmp_path, executor=None):
    stack = make_stack(session_factory, tmp_path)
    executors = {"fake": executor or FakeToolExecutor()}
    tasks = TaskManager(
        session_factory, stack.publisher, executors,
        presence_check=lambda persona: stack.manager.current().status is PresenceStatus.ACTIVE,
        tool_resolver=FakeToolSpecResolver(),
    )
    bridge = PresenceTaskBridge(stack.bus, tasks, lambda: stack.manager.current().status)
    return stack, tasks, bridge


def running_task(tasks: TaskManager, policy: TaskPolicy):
    task = tasks.create_task(persona_id="uep", goal="g", owner_actor_id="owner", scope_ref=SCOPE, policy=policy)
    run = tasks.start_run(task.task_id, persona_version=1, policy_version=1)
    return task, run


def wait_paused(tasks: TaskManager, task_id: str, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if tasks.get_task(task_id).status == RunStatus.PAUSED.value:
            return True
        time.sleep(0.02)
    return False


def test_revoke_event_pauses_only_presence_bound_tasks(session_factory, tmp_path):
    stack, tasks, bridge = build(session_factory, tmp_path)
    bridge.start()
    stack.bus.start()
    try:
        activate_endpoint(stack.manager)
        bound, bound_run = running_task(tasks, BOUND)
        background, _ = running_task(tasks, BACKGROUND)
        finished, _ = running_task(tasks, BOUND)
        tasks.transition_task(finished.task_id, RunStatus.CANCELLED)
        stack.manager.revoke("test")
        assert wait_paused(tasks, bound.task_id)
        task = tasks.get_task(bound.task_id)
        assert task.pause_reason == "presence_left"
        assert tasks.get_run(bound_run.run_id).status == RunStatus.PAUSED.value
        assert tasks.get_task(background.task_id).status == RunStatus.RUNNING.value
        assert tasks.get_task(finished.task_id).status == RunStatus.CANCELLED.value
    finally:
        stack.bus.stop()


def test_recovering_after_core_restart_is_covered_by_reconcile(session_factory, tmp_path):
    """presence.start() 的 recovering 事件早於訂閱：啟動後 reconcile 補暫停。"""
    stack, tasks, bridge = build(session_factory, tmp_path)
    activate_endpoint(stack.manager)
    bound, _ = running_task(tasks, BOUND)
    manager = stack.new_manager()
    assert manager.current().status is PresenceStatus.RECOVERING
    assert journal_events(session_factory, "presence.recovering")
    assert bridge.reconcile() == [bound.task_id]
    assert tasks.get_task(bound.task_id).status == RunStatus.PAUSED.value
    assert bridge.reconcile() == []  # 冪等


def test_recovered_does_not_resume_tasks(session_factory, tmp_path):
    stack, tasks, bridge = build(session_factory, tmp_path)
    bridge.start()
    stack.bus.start()
    try:
        activate_endpoint(stack.manager)
        bound, _ = running_task(tasks, BOUND)
        manager = stack.new_manager()
        stack.manager = manager
        bridge.reconcile()
        assert manager.resolve_recovery("operator") is True
        time.sleep(0.3)
        assert manager.current().status is PresenceStatus.NONE
        assert tasks.get_task(bound.task_id).status == RunStatus.PAUSED.value  # 回到 NONE 不等於人回來了
    finally:
        stack.bus.stop()


def test_stale_event_while_presence_active_again_does_not_pause(session_factory, tmp_path):
    stack, tasks, bridge = build(session_factory, tmp_path)
    activate_endpoint(stack.manager)
    stack.manager.revoke("first")
    revoked = journal_events(session_factory, "presence.revoked")[0]
    activate_endpoint(stack.manager, "ep_desktop")  # 又回來了
    bound, _ = running_task(tasks, BOUND)
    bridge._on_event(revoked)  # 晚到的舊事件
    assert tasks.get_task(bound.task_id).status == RunStatus.RUNNING.value


def test_inflight_action_finishes_and_records_receipt_after_pause(session_factory, tmp_path):
    """P-10：已提交的外部動作先完成，不假裝取消成功；Run 已 PAUSED。"""
    executor = HookExecutor()
    stack, tasks, bridge = build(session_factory, tmp_path, executor)
    activate_endpoint(stack.manager)
    task, run = running_task(tasks, BOUND)
    step = tasks.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    tasks.tool_resolver.register(
        "files.read", executor="fake", side_effect_class="internal_read", idempotency="via_action_id",
        presence_bound=False, tool_version=1,
    )
    action = tasks.create_action(
        run_id=run.run_id, step_id=step.step_id, tool="files.read", params={"p": 1},
        worker_epoch=run.worker_epoch,
    )

    def leave() -> None:
        stack.manager.revoke("leave")
        bridge.reconcile()

    executor.hook = leave
    outcome = tasks.execute_action(action.action_id, {"p": 1}, worker_epoch=run.worker_epoch)
    assert outcome.status is ActionStatus.SUCCEEDED and outcome.receipt is not None
    assert tasks.get_run(run.run_id).status == RunStatus.PAUSED.value
    assert tasks.get_task(task.task_id).status == RunStatus.PAUSED.value
