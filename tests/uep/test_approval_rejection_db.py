"""切片 E2：人類否決核准的路徑，對真實 TaskManager／PostgreSQL 的測試（需 UEP_TEST_DATABASE_URL；未設時 skip）。

涵蓋：否決 ⇒ action ``FAILED(approval_denied)``、Run 回 RUNNING 且 executor 不被呼叫；park 中的 Run 被接手後模型看到
``tool rejected: approval_denied`` 並續跑完成；非人類／未驗證／非 approver／裸字串被拒；重複否決冪等；
核准後可反悔（撤銷核准、同 hash 的新 action 不能消耗）、已執行後拒絕（already_executing）、否決後才到的核准被拒；
INTENDED -> FAILED 只限 approval_denied、receipt 偽造不算否決、Run 非 WAITING_APPROVAL 時拒絕、park 前被否決的順序。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.uep.loop_support import Round
from tests.uep.test_agent_loop import call
from tests.uep.test_agent_loop_approval_db import (
    OWNER,
    PARK_ROUNDS,
    approval_row,
    approve_for,
    new_loop,
    parked_rig,
    post_action,
    resume,
    takeover,
)
from tests.uep.test_agent_loop_db import DbRig, _actions
from uep.agent.budget import (
    NO_PROGRESS_REJECTED,
    BudgetCounters,
    LoopBudget,
    observe_call,
)
from uep.fakes.loop_context import messages_from_json
from uep.tasks.manager import (
    ActionOutcome,
    ActiveWorker,
    ApprovalInvalid,
    AuthenticatedPrincipal,
    IllegalTransition,
    PolicyViolation,
    TaskManager,
)
from uep.tasks.models import ReceiptRow, RunRow
from uep.tasks.states import ACTION_TRANSITIONS, ActionStatus, RunStatus
from uep.tools.gateway import ToolGateway

pytestmark = pytest.mark.usefixtures("session_factory")


def _tool_texts(rig) -> list[str]:
    return [m.content for raw in rig.store.children.values() for m in messages_from_json(raw) if m.role == "tool"]


def _new_sibling(rig, action):
    """同 run、同 tool、同參數的新 action（同 params_hash，不同 action_id）。"""
    return rig.tm.create_action(
        run_id=rig.run.run_id, step_id=action.step_id, tool="post.ask", params={"path": "2"},
        worker_epoch=rig.run.worker_epoch,
    )


def _set_run_status(rig, status: str) -> None:
    with rig.tm._sf() as s, s.begin():
        run = s.get(RunRow, rig.run.run_id)
        run.status = status
        if status == "PAUSED":
            run.pause_reason = "user"


def test_reject_fails_action_with_approval_denied_and_run_back_to_running(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    out = rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    assert (out.status, out.executed) == (ActionStatus.FAILED, False)
    assert out.receipt.error_code == "approval_denied" and out.receipt.source_verified and out.receipt.succeeded is False
    assert out.receipt.meta["decision"] == "denied" and out.receipt.meta["principal"] == "owner"
    assert out.receipt.meta["origin"] == "manual" and out.receipt.meta["revoked"] == []
    assert rig.tm.get_action(action.action_id).status == ActionStatus.FAILED.value
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.RUNNING.value
    assert rig.executor.execution_count(action.action_id) == 0


def test_rejection_event_carries_reason_code(session_factory):
    from uep.events.models import EventRow

    rig = parked_rig(session_factory)
    action = post_action(rig)
    rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    with rig.tm._sf() as s:
        rows = [e for e in s.scalars(select(EventRow)) if e.event_type == "action.executed"]
    assert rows and any((e.payload or {}).get("reason_code") == "approval_denied" for e in rows)


def test_agent_sees_rejected_feedback_and_run_completes(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    plan, checkpoint = takeover(rig)  # 不帶 stale_after：park 已清心跳
    out = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch), checkpoint, {})
    assert (out.state, out.text) == ("completed", "完成")  # 否決不結束 Run，模型看到結果後繼續
    assert "tool rejected: approval_denied" in _tool_texts(rig)
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.SUCCEEDED.value
    assert rig.executor.execution_count(action.action_id) == 0  # 副作用從未發生
    assert rig.tm.get_action(action.action_id).status == ActionStatus.FAILED.value


def test_resume_with_stale_approval_id_after_revoked_denial_does_not_execute(session_factory):
    """核准先到、之後否決（核准被撤銷）：呼叫端 resume 仍帶那份核准 id，也只回 rejected，不執行、不消耗。"""
    rig = parked_rig(session_factory)
    action = post_action(rig)
    approval = approve_for(rig, action)
    rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    plan, checkpoint = takeover(rig)
    out = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch), checkpoint, {action.action_id: approval.approval_id})
    assert out.state == "completed"
    assert approval_row(rig, approval.approval_id).consumed_at is None
    assert rig.executor.execution_count(action.action_id) == 0
    assert "tool rejected: approval_denied" in _tool_texts(rig)


@pytest.mark.parametrize(
    "principal",
    [
        AuthenticatedPrincipal(actor_id="owner", kind="agent", verified=True),
        AuthenticatedPrincipal(actor_id="owner", kind="system", verified=True),
        AuthenticatedPrincipal(actor_id="owner", kind="human", verified=False),
        "owner",
    ],
    ids=["agent", "system", "unverified", "bare_string"],
)
def test_non_human_unverified_or_bare_principal_is_refused(session_factory, principal):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    with pytest.raises(PolicyViolation):
        rig.tm.record_rejection(principal=principal, action_id=action.action_id)
    assert rig.tm.get_action(action.action_id).status == ActionStatus.INTENDED.value
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.WAITING_APPROVAL.value


def test_human_who_is_not_an_approver_is_refused(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    with pytest.raises(PolicyViolation) as exc:
        rig.tm.record_rejection(
            principal=AuthenticatedPrincipal(actor_id="stranger", kind="human", verified=True), action_id=action.action_id,
        )
    assert exc.value.code == "unauthorized_approver"
    assert rig.tm.get_action(action.action_id).status == ActionStatus.INTENDED.value


def test_email_actor_id_is_hashed_in_receipt_meta(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    email = "owner@example.com"
    rig.tm._approvers = lambda task: frozenset({"owner", email})
    out = rig.tm.record_rejection(
        principal=AuthenticatedPrincipal(actor_id=email, kind="human", verified=True), action_id=action.action_id,
    )
    assert out.receipt.meta["principal"].startswith("h:") and email not in str(out.receipt.meta)
    assert out.receipt.meta["decision"] == "denied" and out.receipt.meta["origin"] == "manual"  # 沒被脫敏掉


def test_repeated_rejection_is_idempotent(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    first = rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    version = rig.tm.get_action(action.action_id).version
    second = rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    assert second.status is ActionStatus.FAILED and second.receipt.receipt_id == first.receipt.receipt_id
    assert rig.tm.get_action(action.action_id).version == version  # 不重複推進狀態


# ---- 核准後可反悔：撤銷 ----

def test_reject_after_approval_revokes_it_and_audits(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    approval = approve_for(rig, action)
    out = rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    assert out.receipt.meta["revoked"] == [approval.approval_id]
    row = approval_row(rig, approval.approval_id)
    assert row.consumed_at is None and row.revoked_at is not None
    assert (row.revoked_by, row.revoked_reason, row.revoked_by_action_id) == ("owner", "action_denied", action.action_id)
    assert rig.tm.get_action(action.action_id).status == ActionStatus.FAILED.value


def test_revoked_approval_cannot_be_consumed_by_new_action_with_same_hash(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    approval = approve_for(rig, action)
    rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    sibling = _new_sibling(rig, action)
    assert sibling.action_id != action.action_id and sibling.params_hash == action.params_hash
    with pytest.raises(ApprovalInvalid) as exc:
        rig.tm.execute_action(
            sibling.action_id, {"path": "2"}, worker_epoch=rig.run.worker_epoch, approval_id=approval.approval_id,
        )
    assert exc.value.code == "revoked"
    assert rig.executor.execution_count(sibling.action_id) == 0
    assert approval_row(rig, approval.approval_id).consumed_at is None


def test_rejection_does_not_revoke_unrelated_approvals(session_factory):
    rig = DbRig(
        session_factory, [Round("先發", (call("post.ask", "2"), call("post.ask", "3"))), Round("完成")],
        budget=LoopBudget(repeat_limit=99),
    )
    assert rig.go().state == "awaiting_approval"
    a2 = next(a for a in _actions(rig) if a.tool == "post.ask")
    other = rig.tm.create_action(
        run_id=rig.run.run_id, step_id=a2.step_id, tool="post.ask", params={"path": "不同"}, worker_epoch=rig.run.worker_epoch,
    )
    unrelated = approve_for(rig, other)
    rig.tm.record_rejection(principal=OWNER, action_id=a2.action_id)
    assert approval_row(rig, unrelated.approval_id).revoked_at is None


def test_reject_after_approval_consumed_and_executed_is_refused(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    approval = approve_for(rig, action)
    plan, checkpoint = takeover(rig)
    done = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch), checkpoint, {action.action_id: approval.approval_id})
    assert done.state == "completed"
    with pytest.raises(PolicyViolation) as exc:
        rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    assert exc.value.code == "already_executing"
    assert rig.tm.get_action(action.action_id).status == ActionStatus.SUCCEEDED.value
    assert rig.executor.execution_count(action.action_id) == 1


@pytest.mark.parametrize("status", [ActionStatus.APPROVED, ActionStatus.EXECUTING, ActionStatus.UNKNOWN])
def test_reject_when_approved_or_executing_or_unknown_is_refused(session_factory, status):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    with rig.tm._sf() as s, s.begin():
        s.get(type(action), action.action_id).status = status.value
    with pytest.raises(PolicyViolation) as exc:
        rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    assert exc.value.code == "already_executing"
    assert rig.tm.get_action(action.action_id).status == status.value


def test_approval_after_rejection_is_refused(session_factory):
    """否決後才到的同 hash 核准：沒有 INTENDED 的新 action ⇒ 拒絕寫入，沒有殘留核准列。"""
    rig = parked_rig(session_factory)
    action = post_action(rig)
    rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    with pytest.raises(PolicyViolation) as exc:
        approve_for(rig, action)
    assert exc.value.code == "approval_for_denied_action"
    with rig.tm._sf() as s:
        from uep.tasks.models import ApprovalRow

        assert s.scalar(select(ApprovalRow.approval_id).limit(1)) is None
    sibling = _new_sibling(rig, action)  # 新 action B 沒有核准，不能執行
    with pytest.raises(Exception) as blocked:
        rig.tm.execute_action(sibling.action_id, {"path": "2"}, worker_epoch=rig.run.worker_epoch)
    assert type(blocked.value).__name__ == "ApprovalRequired"
    assert rig.executor.execution_count(sibling.action_id) == 0


def test_approval_for_new_intended_action_after_denial_is_accepted(session_factory):
    """否決後重提 = 新 action；對它重新核准是合法的，且只有新 action 能消耗。"""
    rig = parked_rig(session_factory)
    action = post_action(rig)
    rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    sibling = _new_sibling(rig, action)
    approval = approve_for(rig, sibling)
    out = rig.tm.execute_action(
        sibling.action_id, {"path": "2"}, worker_epoch=rig.run.worker_epoch, approval_id=approval.approval_id,
    )
    assert out.status is ActionStatus.SUCCEEDED and out.executed is True
    assert rig.tm.get_action(action.action_id).status == ActionStatus.FAILED.value  # 被否決的那個仍是 FAILED


def test_reject_non_approval_action_is_refused(session_factory):
    rig = DbRig(session_factory, [Round("", (call("write.s", "1"),)), Round("完成")], budget=LoopBudget(repeat_limit=99))
    assert rig.go().state == "completed"
    (write,) = [a for a in _actions(rig) if a.tool == "write.s"]
    with pytest.raises(PolicyViolation) as exc:  # 已 SUCCEEDED
        rig.tm.record_rejection(principal=OWNER, action_id=write.action_id)
    assert exc.value.code == "already_executing"


@pytest.mark.parametrize("run_status", ["PAUSED", "FAILED", "CANCELLED"])
def test_reject_when_run_not_waiting_approval_is_refused(session_factory, run_status):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    _set_run_status(rig, run_status)
    with pytest.raises(IllegalTransition):
        rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    assert rig.tm.get_action(action.action_id).status == ActionStatus.INTENDED.value


# ---- INTENDED -> FAILED 只限 approval_denied ----

def test_intended_to_failed_transition_table_and_reason_guard(session_factory):
    assert ACTION_TRANSITIONS[ActionStatus.INTENDED] == {ActionStatus.APPROVED, ActionStatus.FAILED}
    rig = parked_rig(session_factory)
    action = post_action(rig)
    for reason in (None, "other"):  # 沒有 reason_code、其他 reason_code
        with pytest.raises(IllegalTransition), rig.tm._tx() as (s, pending):
            task, _run, locked = rig.tm._lock_action(s, action.action_id)
            rig.tm._set_action_status(s, pending, task, locked, ActionStatus.FAILED, "action.executed", reason_code=reason)
    assert rig.tm.get_action(action.action_id).status == ActionStatus.INTENDED.value


def test_failing_receipt_cannot_push_intended_action_to_failed(session_factory):
    """既有路徑（_apply_receipt）對 INTENDED 的 action 套用失敗 receipt，不會走到 INTENDED -> FAILED。"""
    from uep.adapters.tool import ToolReceipt

    rig = parked_rig(session_factory)
    action = post_action(rig)
    receipt = ToolReceipt(
        receipt_id="rcpt_x", action_id=action.action_id, succeeded=False, result_ref=None,
        recorded_at=datetime.now(UTC), source_verified=True, error_code="boom", meta={},
    )
    with pytest.raises(IllegalTransition), rig.tm._tx() as (s, pending):
        task, _run, locked = rig.tm._lock_action(s, action.action_id)
        rig.tm._apply_receipt(s, pending, task, locked, receipt)
    assert rig.tm.get_action(action.action_id).status == ActionStatus.INTENDED.value


def test_forged_error_code_without_manual_meta_is_not_a_denial(session_factory):
    outcome = lambda meta: ActionOutcome(
        "act_x", ActionStatus.FAILED, True,
        SimpleNamespace(error_code="approval_denied", meta=meta, source_verified=True, succeeded=False, result_ref=None),
    )
    gateway = ToolGateway.__new__(ToolGateway)
    assert ToolGateway.outcome_view(gateway, outcome({}))["status"] == "failed"
    assert ToolGateway.outcome_view(gateway, outcome({"origin": "executor", "decision": "denied"}))["status"] == "failed"
    assert ToolGateway.outcome_view(gateway, outcome({"origin": "manual"}))["status"] == "failed"
    genuine = ToolGateway.outcome_view(gateway, outcome({"origin": "manual", "decision": "denied"}))
    assert (genuine["status"], genuine["reason_code"]) == ("rejected", "approval_denied")


def test_executor_forged_denial_receipt_gives_failed_view(session_factory):
    """真實流程：executor 自己回 error_code=approval_denied 的失敗 receipt ⇒ 一般 failed，不是否決。"""
    rig = DbRig(session_factory, [Round("", (call("write.s", "1"),)), Round("完成")], budget=LoopBudget(repeat_limit=99))
    rig.go()
    (write,) = [a for a in _actions(rig) if a.tool == "write.s"]
    with rig.tm._sf() as s:
        receipt = s.scalar(select(ReceiptRow).where(ReceiptRow.action_id == write.action_id))
        assert receipt is not None and receipt.error_code != "approval_denied"
    from uep.tasks.manager import is_denial_receipt

    assert not is_denial_receipt(SimpleNamespace(error_code="approval_denied", meta={"origin": "executor"}))


# ---- 順序：park 前被否決 ----

def test_denied_before_park_run_returns_awaiting_approval_but_run_is_running(session_factory, monkeypatch):
    """loop 回 awaiting_approval 之前（checkpoint 之後、park_run 之前）被否決：park_run 看到 Run 已不是
    WAITING_APPROVAL ⇒ 不清心跳（記 warning）；loop 仍回 awaiting_approval，Run 是 RUNNING 且心跳新鮮。
    接手需等心跳過期（stale_after）；接手後 resume 不需 approvals，看到 rejected 回饋並完成。"""
    original = TaskManager.park_run
    state: dict = {}

    def deny_then_park(self, run_id, *, worker_epoch):
        action = next(a for a in _actions(state["rig"]) if a.tool == "post.ask")
        self.record_rejection(principal=OWNER, action_id=action.action_id)
        return original(self, run_id, worker_epoch=worker_epoch)

    monkeypatch.setattr(TaskManager, "park_run", deny_then_park)
    rig = DbRig(session_factory, list(PARK_ROUNDS), budget=LoopBudget(repeat_limit=99))
    state["rig"] = rig
    out = rig.go()
    assert out.state == "awaiting_approval"
    run = rig.tm.get_run(rig.run.run_id)
    assert run.status == RunStatus.RUNNING.value and run.worker_heartbeat_at is not None
    with pytest.raises(ActiveWorker):
        rig.tm.resume_run(rig.run.run_id, stale_after=timedelta(hours=1))
    plan = rig.tm.resume_run(rig.run.run_id, stale_after=timedelta(0))
    done = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch), plan.checkpoint or {}, {})
    assert (done.state, done.text) == ("completed", "完成")
    assert "tool rejected: approval_denied" in _tool_texts(rig)


# ---- 預算 ----

def test_denial_feedback_counts_as_rejected_in_batch_observation():
    """同批內的 rejected 視圖計入「連續被拒」並觸發 rejected_limit（resume 補齊路徑目前不重算此計數，見契約註記）。"""
    budget = LoopBudget(repeat_limit=99, rejected_limit=2)
    counters = BudgetCounters()
    assert observe_call(budget, counters, "post.ask", {"path": "1"}, "rejected") is None
    assert observe_call(budget, counters, "post.ask", {"path": "2"}, "rejected") == NO_PROGRESS_REJECTED
    assert counters.rejected_streak == 2
