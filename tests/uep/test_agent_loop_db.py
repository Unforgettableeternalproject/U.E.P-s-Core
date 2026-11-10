"""AgentLoop 對真實 TaskManager 的 DB 測試（切片 D1 審查修正；需 PostgreSQL，沒有 UEP_TEST_DATABASE_URL 時自動 skip）。

這些測試是 ``loop_support.FakeTasks`` 每個守衛的真實對照：
- B1：Run 內有 UNKNOWN 時，``transition_run(SUCCEEDED)`` 被 ``_assert_no_inflight`` 拒絕 ⇒ 迴圈收尾轉 ``FAILED(unresolved_actions)``
  （FakeTasks：``unknown`` 集合＋``IllegalTransition``）。
- B2：缺核准時 resume 再次 park，不執行需核准的工具、不重做已完成的唯讀項
  （FakeTasks：需核准的工具一律 ``ApprovalRequired``）。D1 的 resume 還沒有核准執行路徑（歸切片 E）。
- executor 例外 ⇒ action 持久化為 UNKNOWN，同 id 重放回 ``ActionBlocked``。
- 舊 epoch 的 ``transition_run`` 記 warning 後忽略、不拋例外。
- 已有 receipt 的 action 重放不重做（``executed`` 為 False）。
- ``save_checkpoint`` 接受 D1 擴充的鍵、拒絕契約外的鍵；JSONB 往返後 resume 仍可用。
- ``TaskManager.supports_parallel_execution`` 為 True（D1b）；並行的 DB 測試在 ``test_tc3_parallel_db.py``。
"""
from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest

from tests.uep.acceptance_support import POLICY, SCOPE, make_tm
from tests.uep.loop_support import Round, ScriptedRuntime, make_registry, projector_for
from tests.uep.test_agent_loop import PARENT, call, trigger
from uep.agent.budget import LoopBudget
from uep.agent.core import stable_action_id
from uep.agent.loop import AgentLoop
from uep.fakes.loop_context import InMemoryLoopContextStore
from uep.fakes.tool import FakeToolExecutor
from uep.tasks.manager import (
    ActionBlocked,
    IllegalTransition,
    PolicyViolation,
    StaleWorkerEpoch,
    TaskManager,
)
from uep.tasks.states import ActionStatus, RunStatus
from uep.tools.gateway import ToolGateway

pytestmark = pytest.mark.usefixtures("session_factory")


class LosingExecutor(FakeToolExecutor):
    """對指定工具回應失聯（副作用已發生、回應遺失 ⇒ UNKNOWN）。"""

    def __init__(self, lose_tool: str | None = None) -> None:
        super().__init__()
        self.lose_tool = lose_tool

    def execute(self, call):
        if call.tool == self.lose_tool:
            self.execute_behavior(call.action_id, "lose_after_exec")
        return super().execute(call)


class DbRig:
    def __init__(
        self, session_factory, rounds, *, lose_tool: str | None = None, budget: LoopBudget | None = None,
        registry=None, executor: FakeToolExecutor | None = None, **loop_kwargs,
    ) -> None:
        self.registry = registry or make_registry()
        self.loop_kwargs = loop_kwargs
        self.executor = executor or LosingExecutor(lose_tool)
        self.tm = make_tm(session_factory, self.executor, tool_resolver=self.registry)
        task = self.tm.create_task(persona_id="uep", goal="查資料", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY)
        self.run = self.tm.start_run(task.task_id, persona_version=1, policy_version=1)
        self.runtime = ScriptedRuntime(list(rounds))
        self.store = InMemoryLoopContextStore()
        self.budget = budget or LoopBudget()

    def loop(self, *, runtime=None, epoch: int | None = None) -> AgentLoop:
        return AgentLoop(
            runtime=runtime or self.runtime, tasks=self.tm, gateway=ToolGateway(self.registry), registry=self.registry,
            store=self.store, projector=projector_for(self.registry), budget=self.budget,
            worker_epoch=self.run.worker_epoch if epoch is None else epoch, **self.loop_kwargs,
        )

    def go(self):
        return self.loop().run(trigger(), self.run.run_id, parent=PARENT, system_text="你是助理", trigger_text="幫我查")


def test_db_unknown_action_makes_run_fail_with_unresolved_actions(session_factory):
    rig = DbRig(
        session_factory, [Round("", (call("write.s", "1"),)), Round("全部完成")], lose_tool="write.s",
    )
    out = rig.go()
    assert (out.state, out.reason_code) == ("failed", "unresolved_actions")
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.FAILED.value  # 不是卡在 RUNNING
    unknown = [a for a in _actions(rig) if a.status == ActionStatus.UNKNOWN.value]
    assert len(unknown) == 1


def test_db_replay_of_unknown_action_is_blocked(session_factory):
    rig = DbRig(session_factory, [Round("", (call("write.s", "1"),)), Round("")], lose_tool="write.s", budget=LoopBudget(max_steps=1, finalize_on_exhausted=False))
    rig.go()
    (action,) = [a for a in _actions(rig) if a.status == ActionStatus.UNKNOWN.value]
    unknown_run = rig.tm.get_run(action.run_id)
    assert unknown_run.status in (RunStatus.FAILED.value, RunStatus.RUNNING.value)
    with pytest.raises(ActionBlocked):  # 同 id 重放：不重做、不改變狀態
        rig.tm.execute_action(action.action_id, {"path": "1"}, worker_epoch=unknown_run.worker_epoch)


def test_db_illegal_transition_when_unknown_blocks_succeeded(session_factory):
    rig = DbRig(session_factory, [Round("", (call("write.s", "1"),)), Round("")], lose_tool="write.s")
    # 直接以「還沒收尾」的方式製造 UNKNOWN：迴圈跑一個 Step 後不收尾（預算 1 步、不 finalize）會轉 FAILED，
    # 所以改用 manager 層直接驗證守衛本身
    step = rig.tm.begin_step(rig.run.run_id, "think", worker_epoch=rig.run.worker_epoch)
    action = rig.tm.create_action(
        run_id=rig.run.run_id, step_id=step.step_id, tool="write.s", params={"path": "x"}, worker_epoch=rig.run.worker_epoch,
    )
    rig.executor.execute_behavior(action.action_id, "lose_after_exec")
    # executor 回應失聯（ToolResponseLost）：Manager 不拋出，回傳 UNKNOWN outcome
    outcome = rig.tm.execute_action(action.action_id, {"path": "x"}, worker_epoch=rig.run.worker_epoch)
    assert outcome.status is ActionStatus.UNKNOWN
    assert rig.tm.get_action(action.action_id).status == ActionStatus.UNKNOWN.value
    with pytest.raises(IllegalTransition):
        rig.tm.transition_run(rig.run.run_id, RunStatus.SUCCEEDED, worker_epoch=rig.run.worker_epoch)


def test_db_resume_without_approval_parks_again_without_side_effects(session_factory):
    """B2：缺核准 ⇒ resume 再次 park；需核准的工具不執行、已完成的唯讀項不重做。"""
    rig = DbRig(
        session_factory,
        [Round("先查再發", (call("read.lone", "1"), call("post.ask", "2"), call("read.lone", "3"))), Round("全部完成")],
        budget=LoopBudget(repeat_limit=99),
    )
    out = rig.go()
    assert out.state == "awaiting_approval"
    run_id = rig.run.run_id
    assert rig.tm.get_run(run_id).status == RunStatus.WAITING_APPROVAL.value
    stored = rig.tm.get_run(run_id).checkpoint  # JSONB 往返後的 checkpoint
    assert {"child_context_ref", "child_revision", "history_hash", "frozen_tools", "frozen_tags", "step_id",
            "budget_counters", "visible_tools"} <= set(stored)
    reads_before = sum(rig.executor.executions.values())

    plan = rig.tm.resume_run(run_id, stale_after=timedelta(0))  # 「崩潰」後以新 epoch 接手
    assert plan.run.worker_epoch == rig.run.worker_epoch + 1
    resumed = rig.loop(runtime=ScriptedRuntime([Round("不該到這裡")]), epoch=plan.run.worker_epoch)
    again = resumed.resume(run_id, plan.checkpoint or {}, trigger=trigger(), parent_id="ctx_parent")
    assert again.state == "awaiting_approval" and again.reason_code == "approval_required"
    assert sum(rig.executor.executions.values()) == reads_before  # 沒有重做、也沒有執行 post.ask
    post = [a for a in _actions(rig) if a.tool == "post.ask"]
    assert post and all(a.status == ActionStatus.INTENDED.value for a in post)


def test_db_already_executed_action_is_not_redone_on_replay(session_factory):
    rig = DbRig(session_factory, [Round("", (call("read.lone", "1"),)), Round("完成")])
    out = rig.go()
    assert out.state == "completed" and rig.tm.get_run(rig.run.run_id).status == RunStatus.SUCCEEDED.value
    (action,) = [a for a in _actions(rig) if a.tool == "read.lone"]
    outcome = rig.tm.execute_action(action.action_id, {"path": "1"}, worker_epoch=rig.run.worker_epoch)
    assert outcome.executed is False and rig.executor.execution_count(action.action_id) == 1


def test_db_stale_epoch_transition_is_ignored(session_factory):
    rig = DbRig(session_factory, [])
    result = rig.tm.transition_run(rig.run.run_id, RunStatus.SUCCEEDED, worker_epoch=rig.run.worker_epoch + 5)
    assert result.status == RunStatus.RUNNING.value  # 記 warning、忽略、不拋例外
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.RUNNING.value


def test_db_checkpoint_keys_are_enforced(session_factory):
    rig = DbRig(session_factory, [])
    epoch = rig.run.worker_epoch
    rig.tm.save_checkpoint(rig.run.run_id, {
        "child_context_ref": "loop:run_x", "child_revision": 2, "history_hash": "a" * 64, "frozen_tools": {"read.a": 1},
        "frozen_tags": ["memory.read"], "step_id": "step_x", "budget_counters": {"steps": 1}, "visible_tools": ["read.a"],
        "next_step_semantic": "think",
    }, worker_epoch=epoch)
    assert rig.tm.get_run(rig.run.run_id).checkpoint["frozen_tools"] == {"read.a": 1}
    with pytest.raises(PolicyViolation):
        rig.tm.save_checkpoint(rig.run.run_id, {"not_in_contract": 1}, worker_epoch=epoch)
    with pytest.raises(PolicyViolation):  # 值層級白名單：自由文字不得進 checkpoint
        rig.tm.save_checkpoint(rig.run.run_id, {"next_step_semantic": "這是 自由文字"}, worker_epoch=epoch)


def test_db_task_manager_supports_parallel_execution(session_factory):
    assert TaskManager.supports_parallel_execution is True
    rig = DbRig(session_factory, [], budget=LoopBudget(max_parallel=2))
    rig.loop()  # D1b：不再拒絕 max_parallel > 1


def test_db_budget_and_tags_sanity(session_factory):
    """真實註冊表下的投影與 stable_action_id 與離線測試一致（防止 fake 與真實的 step_id 假設分歧）。"""
    rig = DbRig(session_factory, [Round("", (call("read.lone", "1"),)), Round("完成")])
    rig.go()
    (action,) = [a for a in _actions(rig) if a.tool == "read.lone"]
    assert action.action_id == stable_action_id(rig.run.run_id, action.step_id, 0)


# ---- helpers ----

def _actions(rig: DbRig):
    from sqlalchemy import select

    from uep.tasks.models import ActionIntentRow

    with rig.tm._sf() as s:
        return list(s.scalars(select(ActionIntentRow).where(ActionIntentRow.run_id == rig.run.run_id)))



# ───────────────────────── parallel_one_shot：真實 Manager 的狀態轉移（RA-11／12、§6.4） ─────────────────────────

class _OneShotExecutor(FakeToolExecutor):
    """路徑在 ``slow_paths`` 內的呼叫會卡住（模擬逾時的慢工具）；``cancel_mode`` 決定 ``cancel`` 的回應。
    同時記錄 execute 內的最大並行數，證明兩個 one_shot 真的在 DB 內同時 EXECUTING。"""

    def __init__(self, slow_paths=(), cancel_mode: str = "stopped", work_s: float = 0.0) -> None:
        super().__init__()
        self.slow_paths, self.cancel_mode, self.work_s = set(slow_paths), cancel_mode, work_s
        self.active = self.peak = 0
        self._guard = threading.Lock()

    def execute(self, call):
        if call.params.get("path") in self.slow_paths:
            self.block_execution(call.action_id)
        with self._guard:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.work_s)
            return super().execute(call)
        finally:
            with self._guard:
                self.active -= 1

    def cancel(self, action_id):
        if self.cancel_mode == "stopped":
            return super().cancel(action_id)
        self.cancels.append(action_id)
        if self.cancel_mode == "raise":
            raise RuntimeError("cancel 失敗")
        return self.cancel_mode

    def release_all(self) -> None:
        for gate in list(self._blocks.values()):
            gate.set()


def _one_shot_rig(session_factory, rounds, executor, *, timeout_s: float = 0.3, **kw):
    from tests.uep.test_agent_loop import one_shot_registry

    kw.setdefault("budget", LoopBudget(max_parallel=2, repeat_limit=99))
    kw.setdefault("executors", {"fake": executor})
    kw.setdefault("cancel_grace_s", 2.0)
    return DbRig(session_factory, rounds, registry=one_shot_registry(timeout_s), executor=executor, **kw)


def _by_order(rig):
    """該 Run 的 action，依建立順序（＝波內序號）。"""
    return sorted(_actions(rig), key=lambda a: a.created_at)


def test_db_two_one_shot_items_execute_concurrently_and_both_succeed(session_factory):
    executor = _OneShotExecutor(work_s=0.2)
    rig = _one_shot_rig(
        session_factory, [Round("", (call("once.a", "1"), call("once.b", "2"))), Round("完成")], executor, timeout_s=5.0,
    )
    out = rig.go()
    assert out.state == "completed" and executor.peak == 2  # 真實 _check_single_executing 放行 one_shot 並行
    rows = _by_order(rig)
    assert [r.status for r in rows] == [ActionStatus.SUCCEEDED.value] * 2
    assert {r.concurrency for r in rows} == {"parallel_one_shot"}


def test_db_serial_action_is_still_blocked_while_one_shot_is_executing(session_factory):
    executor = _OneShotExecutor(work_s=0.3)
    rig = _one_shot_rig(
        session_factory, [Round("", (call("once.a", "1"), call("write.s", "2"))), Round("完成")], executor, timeout_s=5.0,
    )
    out = rig.go()
    assert out.state == "completed" and executor.peak == 1  # serial 是屏障：不與 one_shot 同時執行
    assert [r.status for r in _by_order(rig)] == [ActionStatus.SUCCEEDED.value] * 2


def test_db_one_shot_timeout_with_stopped_cancel_lands_failed_timeout(session_factory):
    executor = _OneShotExecutor(slow_paths={"slow"})
    rig = _one_shot_rig(session_factory, [Round("", (call("once.a", "slow"), call("once.b", "fast"))), Round("完成")], executor)
    try:
        out = rig.go()
    finally:
        executor.release_all()
    a, b = _by_order(rig)
    assert out.state == "completed"
    assert (a.status, b.status) == (ActionStatus.FAILED.value, ActionStatus.SUCCEEDED.value)
    assert executor.cancels == [a.action_id]
    receipt = rig.tm.get_receipt(a.action_id)
    assert receipt.succeeded is False and receipt.error_code == "timeout"
    fed = [v for _, v in next(iter(rig.runtime.submitted.values()))]
    assert (fed[0]["status"], fed[0]["reason_code"]) == ("failed", "timeout") and fed[1]["status"] == "ok"
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.SUCCEEDED.value


def test_db_one_shot_timeout_with_isolated_cancel_lands_unknown_and_late_receipt_does_not_change_it(session_factory):
    executor = _OneShotExecutor(slow_paths={"slow"}, cancel_mode="isolated")
    rig = _one_shot_rig(session_factory, [Round("", (call("once.a", "slow"), call("once.b", "fast"))), Round("完成")], executor)
    try:
        out = rig.go()
        a, b = _by_order(rig)
        assert (a.status, b.status) == (ActionStatus.UNKNOWN.value, ActionStatus.SUCCEEDED.value)
        assert (out.state, out.reason_code) == ("failed", "unresolved_actions")
        executor.release_all()  # 卡住的 execute 之後才回來：action 不再是 EXECUTING，晚到的 receipt 不推進狀態
        deadline = time.monotonic() + 3
        while executor.active and time.monotonic() < deadline:
            time.sleep(0.02)
        assert rig.tm.get_action(a.action_id).status == ActionStatus.UNKNOWN.value
        assert executor.executions[a.action_id] == 1  # 沒有重送
    finally:
        executor.release_all()


def test_db_run_cancel_cancels_all_in_flight_one_shot_actions(session_factory):
    executor = _OneShotExecutor(slow_paths={"p1", "p2"})
    flag = threading.Event()
    rig = _one_shot_rig(
        session_factory, [Round("", (call("once.a", "p1"), call("once.b", "p2"))), Round("完成")], executor, timeout_s=30.0,
        cancelled=flag.is_set,
    )
    timer = threading.Timer(0.3, flag.set)
    timer.start()
    try:
        out = rig.go()
    finally:
        timer.cancel()
        executor.release_all()
    rows = _by_order(rig)
    assert out.state == "cancelled" and sorted(executor.cancels) == sorted(r.action_id for r in rows)
    assert [r.status for r in _by_order(rig)] == [ActionStatus.FAILED.value] * 2  # stopped：executor 簽回失敗 receipt


def test_db_stale_worker_epoch_cancels_other_in_flight_one_shot_actions(session_factory):
    executor = _OneShotExecutor(slow_paths={"p1"})
    rig = _one_shot_rig(
        session_factory, [Round("", (call("once.a", "p1"), call("once.b", "p2"))), Round("完成")], executor, timeout_s=30.0,
    )
    real_execute = rig.tm.execute_action
    calls = []

    def execute_action(action_id, params, **kw):
        calls.append(action_id)
        if params["path"] == "p2":  # 第二項在第一項進入執行後被接手：以真實 StaleWorkerEpoch 回報
            time.sleep(0.3)
            raise StaleWorkerEpoch("run 已被接手")
        return real_execute(action_id, params, **kw)

    rig.tm.execute_action = execute_action  # type: ignore[method-assign]
    try:
        out = rig.go()
    finally:
        executor.release_all()
    first = _by_order(rig)[0]
    assert out.state == "stale" and executor.cancels == [first.action_id]
