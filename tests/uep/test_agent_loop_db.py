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
- ``TaskManager.supports_parallel_execution`` 為 False。
"""
from __future__ import annotations

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
    def __init__(self, session_factory, rounds, *, lose_tool: str | None = None, budget: LoopBudget | None = None) -> None:
        self.registry = make_registry()
        self.executor = LosingExecutor(lose_tool)
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
            worker_epoch=self.run.worker_epoch if epoch is None else epoch,
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
    with pytest.raises(Exception):  # noqa: B017  executor 失聯：Manager 標 UNKNOWN 並重新拋出
        rig.tm.execute_action(action.action_id, {"path": "x"}, worker_epoch=rig.run.worker_epoch)
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


def test_db_task_manager_does_not_support_parallel_execution(session_factory):
    assert TaskManager.supports_parallel_execution is False
    rig = DbRig(session_factory, [], budget=LoopBudget(max_parallel=2))
    with pytest.raises(ValueError, match="supports_parallel_execution"):
        rig.loop()


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

