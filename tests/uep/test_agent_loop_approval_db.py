"""切片 E1：核准 park／resume 對真實 TaskManager／PostgreSQL 的測試（需 UEP_TEST_DATABASE_URL；未設時 skip）。

涵蓋：``park_run`` 清心跳使 ``resume_run`` 可立即接手（對照組沒有 park_run 會 ActiveWorker）、park 後換 worker 接手的全流程
（record_approval -> resume(approvals) -> 單次消耗、executor 只執行一次、Run SUCCEEDED）、resume 時三種「已變更」被拒
（approval 無效含對錯 action、tool_version 變了、能力撤銷）、WAITING_APPROVAL -> FAILED、崩潰在消耗之後不重複副作用，
以及 TaskManager 層 ``park_run`` 的單元測試。離線對照在 ``test_agent_loop_approval.py``。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.uep.acceptance_support import POLICY, SCOPE, arm_next, make_tm
from tests.uep.loop_support import Round, ScriptedRuntime, projector_for
from tests.uep.test_agent_loop import call, trigger
from tests.uep.test_agent_loop_db import DbRig, _actions
from uep.agent.budget import LoopBudget
from uep.agent.loop import AgentLoop
from uep.fakes.crash import CrashInjector, SimulatedCrash
from uep.tasks.manager import (
    ActiveWorker,
    AuthenticatedPrincipal,
    IllegalTransition,
    TaskManager,
)
from uep.tasks.models import ApprovalRow
from uep.tasks.states import ActionStatus, RunStatus
from uep.tools.gateway import ToolGateway
from uep.tools.registry import ToolRegistry

pytestmark = pytest.mark.usefixtures("session_factory")
OWNER = AuthenticatedPrincipal(actor_id="owner", kind="human", verified=True)
PARK_ROUNDS = [Round("先發", (call("post.ask", "2"),)), Round("完成")]


def parked_rig(session_factory, **kwargs) -> DbRig:
    rig = DbRig(session_factory, list(PARK_ROUNDS), budget=LoopBudget(repeat_limit=99), **kwargs)
    out = rig.go()
    assert out.state == "awaiting_approval"
    return rig


def post_action(rig: DbRig):
    (action,) = [a for a in _actions(rig) if a.tool == "post.ask"]
    return action


def approve_for(rig: DbRig, action, *, delta=timedelta(hours=1)) -> ApprovalRow:
    """直接讀 ActionIntentRow.params_hash 當 action_hash（與 Approval 通道簽發的內容一致）。"""
    return rig.tm.record_approval(
        principal=OWNER, action_hash=action.params_hash, target=action.tool, scope=SCOPE,
        expiry=datetime.now(UTC) + delta,
    )


def approval_row(rig: DbRig, approval_id: str) -> ApprovalRow:
    with rig.tm._sf() as s:
        return s.get(ApprovalRow, approval_id)


def new_loop(rig: DbRig, *, tm=None, registry=None, projector=None, epoch: int, rounds=None) -> AgentLoop:
    registry = registry or rig.registry
    return AgentLoop(
        runtime=ScriptedRuntime(list(rounds if rounds is not None else [Round("完成")])), tasks=tm or rig.tm,
        gateway=ToolGateway(registry), registry=registry, store=rig.store,
        projector=projector or projector_for(registry), budget=rig.budget, worker_epoch=epoch,
    )


def takeover(rig: DbRig, **kwargs):
    plan = rig.tm.resume_run(rig.run.run_id, **kwargs)
    return plan, plan.checkpoint or {}


def resume(rig: DbRig, loop: AgentLoop, checkpoint, approvals):
    return loop.resume(rig.run.run_id, checkpoint, trigger=trigger(), parent_id="ctx_parent", approvals=approvals)


# ---- park_run 與 resume_run 的配對 ----

def test_park_run_lets_resume_run_take_over_immediately(session_factory):
    rig = parked_rig(session_factory)
    run = rig.tm.get_run(rig.run.run_id)
    assert run.status == RunStatus.WAITING_APPROVAL.value and run.worker_heartbeat_at is None
    plan, _cp = takeover(rig)  # 不帶 stale_after 也能接手
    assert plan.run.worker_epoch == rig.run.worker_epoch + 1


def test_without_park_run_resume_run_raises_active_worker(session_factory, monkeypatch):
    """對照組：park_run 被換成不清心跳的空操作，心跳還新鮮 ⇒ 不帶 stale_after 的接手被擋。"""
    monkeypatch.setattr(TaskManager, "park_run", lambda self, run_id, *, worker_epoch: self.get_run(run_id))
    rig = parked_rig(session_factory)
    assert rig.tm.get_run(rig.run.run_id).worker_heartbeat_at is not None
    with pytest.raises(ActiveWorker):  # 明確傳大的 stale_after，不依賴預設值
        rig.tm.resume_run(rig.run.run_id, stale_after=timedelta(hours=1))


# ---- 全流程 ----

def test_park_then_new_worker_resumes_with_approval_and_completes(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    assert action.status == ActionStatus.INTENDED.value
    approval = approve_for(rig, action)
    plan, checkpoint = takeover(rig)
    loop = new_loop(rig, epoch=plan.run.worker_epoch)
    out = resume(rig, loop, checkpoint, {action.action_id: approval.approval_id})
    assert (out.state, out.text) == ("completed", "完成")
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.SUCCEEDED.value
    assert approval_row(rig, approval.approval_id).consumed_by_action_id == action.action_id
    assert rig.executor.execution_count(action.action_id) == 1  # 副作用只發生一次
    assert rig.tm.get_action(action.action_id).status == ActionStatus.SUCCEEDED.value


def test_replaying_the_same_resume_does_not_repeat_the_side_effect(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    approval = approve_for(rig, action)
    plan, checkpoint = takeover(rig)
    first = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch), checkpoint, {action.action_id: approval.approval_id})
    assert first.state == "completed"
    assert rig.executor.execution_count(action.action_id) == 1
    # 同一個 action 以同一份核准再執行：已有 receipt ⇒ 不重做、不再消耗
    outcome = rig.tm.execute_action(
        action.action_id, {"path": "2"}, worker_epoch=plan.run.worker_epoch, approval_id=approval.approval_id,
    )
    assert outcome.executed is False and rig.executor.execution_count(action.action_id) == 1


def test_expired_approval_parks_again_and_run_stays_waiting(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    approval = approve_for(rig, action, delta=timedelta(milliseconds=1))
    plan, checkpoint = takeover(rig)
    out = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch, rounds=[Round("不該到這裡")]), checkpoint,
                 {action.action_id: approval.approval_id})
    assert (out.state, out.reason_code) == ("awaiting_approval", "approval_required")
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.WAITING_APPROVAL.value
    assert rig.tm.get_action(action.action_id).status == ActionStatus.INTENDED.value
    assert approval_row(rig, approval.approval_id).consumed_at is None and rig.executor.execution_count(action.action_id) == 0


# ---- resume 時三種「已變更」各被拒一次 ----

def test_approval_for_another_action_is_rejected(session_factory):
    rig = DbRig(
        session_factory, [Round("先查再發", (call("write.s", "1"), call("post.ask", "2"))), Round("完成")],
        budget=LoopBudget(repeat_limit=99),
    )
    # write.s（reversible_local）不需核准：先執行；post.ask 缺核准 park
    assert rig.go().state == "awaiting_approval"
    action, other = post_action(rig), next(a for a in _actions(rig) if a.tool == "write.s")
    wrong = approve_for(rig, other)  # 對錯 action 的 hash
    plan, checkpoint = takeover(rig)
    out = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch, rounds=[Round("不該到這裡")]), checkpoint,
                 {action.action_id: wrong.approval_id})
    assert (out.state, out.reason_code) == ("failed", "approval_invalid")
    assert rig.tm.get_action(action.action_id).status == ActionStatus.INTENDED.value  # 沒有執行
    assert approval_row(rig, wrong.approval_id).consumed_at is None and rig.executor.execution_count(action.action_id) == 0
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.FAILED.value


def test_approval_for_same_tool_but_different_params_is_rejected(session_factory):
    """單獨鑑別 hash 綁定：target（工具名）、scope、核准者都對，只有 params 不同（同一個 post.ask）。"""
    rig = parked_rig(session_factory)
    action = post_action(rig)
    sibling = rig.tm.create_action(
        run_id=rig.run.run_id, step_id=action.step_id, tool="post.ask", params={"path": "不同的參數"},
        worker_epoch=rig.run.worker_epoch,
    )
    assert sibling.tool == action.tool and sibling.params_hash != action.params_hash
    wrong = approve_for(rig, sibling)
    plan, checkpoint = takeover(rig)
    out = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch, rounds=[Round("不該到這裡")]), checkpoint,
                 {action.action_id: wrong.approval_id})
    assert (out.state, out.reason_code) == ("failed", "approval_invalid")
    assert rig.tm.get_action(action.action_id).status == ActionStatus.INTENDED.value
    assert approval_row(rig, wrong.approval_id).consumed_at is None and rig.executor.execution_count(action.action_id) == 0


def test_unknown_approval_id_is_rejected(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    plan, checkpoint = takeover(rig)
    out = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch), checkpoint, {action.action_id: "appr_nonexistent"})
    assert (out.state, out.reason_code) == ("failed", "approval_invalid")
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.FAILED.value
    with rig.tm._sf() as s:
        assert s.scalar(select(ApprovalRow.approval_id).limit(1)) is None  # 迴圈沒有自己建立核准（TG-7）


def _registry_with_post_ask_v2(registry: ToolRegistry) -> ToolRegistry:
    entries = [
        replace(e, spec=replace(e.spec, tool_version=2) if e.name == "post.ask" else e.spec, schema_hash="")  # hash 由 build 重算
        for e in registry._entries.values()
    ]
    return ToolRegistry.build(entries)


@pytest.mark.parametrize("stale_projector", [False, True])
def test_tool_version_change_is_rejected_and_approval_not_consumed(session_factory, stale_projector):
    """stale_projector=False：預檢攔下（投影版本 ≠ 凍結版本）。True：投影仍是舊版，實際攔下的是 Manager 的 create 層
    （``create_action`` 對既有 action 的 ``_assert_same_action`` 版本檢查，``ActionBlocked(code=tool_version_changed)``）；
    ``execute_action`` 內的同名檢查在 create 層之後，是更後面的防線。"""
    rig = parked_rig(session_factory)
    action = post_action(rig)
    approval = approve_for(rig, action)
    v2 = _registry_with_post_ask_v2(rig.registry)
    tm2 = make_tm(session_factory, rig.executor, tool_resolver=v2)  # 註冊表已升版
    plan, checkpoint = takeover(rig)
    loop = new_loop(
        rig, tm=tm2, registry=rig.registry if stale_projector else v2, epoch=plan.run.worker_epoch,
        rounds=[Round("不該到這裡")],
    )
    out = resume(rig, loop, checkpoint, {action.action_id: approval.approval_id})
    assert (out.state, out.reason_code) == ("failed", "tool_version_changed")
    assert approval_row(rig, approval.approval_id).consumed_at is None and rig.executor.execution_count(action.action_id) == 0
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.FAILED.value


def test_capability_revoked_is_rejected_and_waiting_run_ends_failed(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    approval = approve_for(rig, action)
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.WAITING_APPROVAL.value
    base = projector_for(rig.registry)
    plan, checkpoint = takeover(rig)
    loop = new_loop(
        rig, epoch=plan.run.worker_epoch, rounds=[Round("不該到這裡")],
        projector=lambda tags, prior: {"tools": [t for t in base(tags, prior)["tools"] if t["tool"] != "post.ask"]},
    )
    out = resume(rig, loop, checkpoint, {action.action_id: approval.approval_id})
    assert (out.state, out.reason_code) == ("failed", "capability_revoked")
    assert approval_row(rig, approval.approval_id).consumed_at is None and rig.executor.execution_count(action.action_id) == 0
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.FAILED.value  # WAITING_APPROVAL -> FAILED 允許


def test_waiting_approval_run_cannot_succeed_only_fail(session_factory):
    """為什麼重驗失敗一律結束 Run：WAITING_APPROVAL 不能轉 SUCCEEDED（states.py），只能 FAILED。"""
    rig = parked_rig(session_factory)
    plan, _cp = takeover(rig)
    with pytest.raises(IllegalTransition):
        rig.tm.transition_run(rig.run.run_id, RunStatus.SUCCEEDED, worker_epoch=plan.run.worker_epoch)
    rig.tm.transition_run(rig.run.run_id, RunStatus.FAILED, worker_epoch=plan.run.worker_epoch)
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.FAILED.value


# ---- 崩潰在消耗核准之後 ----

def test_crash_after_approval_consumed_does_not_repeat_the_side_effect(session_factory):
    """階段 1（消耗核准、action 轉 EXECUTING）已提交、executor 尚未呼叫就崩潰：新 worker 接手時 EXECUTING 轉 UNKNOWN，
    resume 不重做（UNKNOWN 禁止盲目重試，Manager 回 ActionBlocked），pending 項沒有執行 ⇒ Run 直接 FAILED(action_blocked)，由 resolve_unknown 查核。"""
    crash = CrashInjector()
    rig = DbRig(session_factory, list(PARK_ROUNDS), budget=LoopBudget(repeat_limit=99))
    rig.tm = make_tm(session_factory, rig.executor, crash=crash, tool_resolver=rig.registry)
    task = rig.tm.create_task(persona_id="uep", goal="查資料", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY)
    rig.run = rig.tm.start_run(task.task_id, persona_version=1, policy_version=1)
    assert rig.go().state == "awaiting_approval"
    action = post_action(rig)
    approval = approve_for(rig, action)
    plan, checkpoint = takeover(rig)
    arm_next(crash, "action.after_executing_commit")
    with pytest.raises(SimulatedCrash):
        resume(rig, new_loop(rig, epoch=plan.run.worker_epoch), checkpoint, {action.action_id: approval.approval_id})
    assert approval_row(rig, approval.approval_id).consumed_by_action_id == action.action_id
    assert rig.executor.execution_count(action.action_id) == 0  # executor 還沒被呼叫

    plan2 = rig.tm.resume_run(rig.run.run_id, stale_after=timedelta(0))  # 再換一個 worker
    assert plan2.unknown_action_ids == [action.action_id]
    out = resume(rig, new_loop(rig, epoch=plan2.run.worker_epoch), plan2.checkpoint or {}, {action.action_id: approval.approval_id})
    assert (out.state, out.reason_code) == ("failed", "action_blocked")
    assert rig.executor.execution_count(action.action_id) == 0  # 沒有因為重放而執行
    assert rig.tm.get_action(action.action_id).status == ActionStatus.UNKNOWN.value


# ---- TaskManager 層 park_run ----

def _waiting_run(session_factory):
    rig = parked_rig(session_factory)
    return rig, rig.run.run_id, rig.run.worker_epoch


def test_park_run_clears_heartbeat_and_keeps_status(session_factory):
    rig, run_id, epoch = _waiting_run(session_factory)
    rig.tm.heartbeat(run_id, worker_epoch=epoch)  # 先讓心跳有值
    assert rig.tm.get_run(run_id).worker_heartbeat_at is not None
    before = rig.tm.get_run(run_id)
    run = rig.tm.park_run(run_id, worker_epoch=epoch)
    after = rig.tm.get_run(run_id)
    assert run.worker_epoch == epoch and after.worker_heartbeat_at is None
    assert after.status == RunStatus.WAITING_APPROVAL.value and after.version == before.version + 1
    assert after.worker_epoch == before.worker_epoch and after.checkpoint == before.checkpoint


def test_park_run_ignores_stale_epoch(session_factory):
    rig, run_id, epoch = _waiting_run(session_factory)
    rig.tm.heartbeat(run_id, worker_epoch=epoch)
    before = rig.tm.get_run(run_id)
    run = rig.tm.park_run(run_id, worker_epoch=epoch + 5)  # 不拋例外
    after = rig.tm.get_run(run_id)
    assert run.worker_epoch == epoch and after.worker_heartbeat_at == before.worker_heartbeat_at and after.version == before.version


def test_park_run_does_nothing_when_not_waiting_approval(session_factory):
    rig = DbRig(session_factory, [])  # Run 是 RUNNING
    epoch, run_id = rig.run.worker_epoch, rig.run.run_id
    rig.tm.heartbeat(run_id, worker_epoch=epoch)
    before = rig.tm.get_run(run_id)
    rig.tm.park_run(run_id, worker_epoch=epoch)
    after = rig.tm.get_run(run_id)
    assert after.status == RunStatus.RUNNING.value and after.worker_heartbeat_at == before.worker_heartbeat_at
    assert after.version == before.version


def test_park_run_emits_no_journal_event(session_factory):
    from sqlalchemy import func

    from uep.events.models import EventRow

    rig, run_id, epoch = _waiting_run(session_factory)
    with rig.tm._sf() as s:
        before = s.scalar(select(func.count()).select_from(EventRow))
    rig.tm.park_run(run_id, worker_epoch=epoch)
    with rig.tm._sf() as s:
        assert s.scalar(select(func.count()).select_from(EventRow)) == before
