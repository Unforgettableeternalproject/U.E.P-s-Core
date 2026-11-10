"""切片 E2：人類否決核准的路徑，對真實 TaskManager／PostgreSQL 的測試（需 UEP_TEST_DATABASE_URL；未設時 skip）。

涵蓋：否決 ⇒ action ``FAILED(approval_denied)``、Run 回 RUNNING 且 executor 不被呼叫；park 中的 Run 被接手後模型看到
``tool rejected: approval_denied`` 並續跑完成；非人類／未驗證／非 approver／裸字串被拒；重複否決冪等；
核准與否決衝突（先核准再否決、已消耗再否決、先否決再核准）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.uep.acceptance_support import SCOPE
from tests.uep.loop_support import Round
from tests.uep.test_agent_loop import call
from tests.uep.test_agent_loop_approval_db import (
    OWNER,
    approval_row,
    approve_for,
    new_loop,
    parked_rig,
    post_action,
    resume,
    takeover,
)
from tests.uep.test_agent_loop_db import DbRig, _actions
from uep.agent.budget import LoopBudget
from uep.fakes.loop_context import messages_from_json
from uep.tasks.manager import (
    AuthenticatedPrincipal,
    IllegalTransition,
    PolicyViolation,
)
from uep.tasks.states import ActionStatus, RunStatus

pytestmark = pytest.mark.usefixtures("session_factory")


def _tool_texts(rig) -> list[str]:
    return [m.content for raw in rig.store.children.values() for m in messages_from_json(raw) if m.role == "tool"]


def test_reject_fails_action_with_approval_denied_and_run_back_to_running(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    out = rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    assert (out.status, out.executed) == (ActionStatus.FAILED, False)
    assert out.receipt.error_code == "approval_denied" and out.receipt.source_verified and out.receipt.succeeded is False
    assert out.receipt.meta["decision"] == "denied" and out.receipt.meta["principal"] == "owner"
    assert rig.tm.get_action(action.action_id).status == ActionStatus.FAILED.value
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.RUNNING.value
    assert rig.executor.execution_count(action.action_id) == 0


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


def test_resume_with_late_approval_after_denial_does_not_execute(session_factory):
    """否決之後即使呼叫端仍帶一份有效核准，也只回 rejected 視圖，不執行、不消耗核准。"""
    rig = parked_rig(session_factory)
    action = post_action(rig)
    rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    approval = approve_for(rig, action)  # 否決後才到的核准
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


def test_repeated_rejection_is_idempotent(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    first = rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    version = rig.tm.get_action(action.action_id).version
    second = rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    assert second.status is ActionStatus.FAILED and second.receipt.receipt_id == first.receipt.receipt_id
    assert rig.tm.get_action(action.action_id).version == version  # 不重複推進狀態


def test_reject_after_valid_approval_granted_is_refused(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    approval = approve_for(rig, action)
    with pytest.raises(PolicyViolation) as exc:
        rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    assert exc.value.code == "approval_already_granted"
    assert rig.tm.get_action(action.action_id).status == ActionStatus.INTENDED.value
    assert approval_row(rig, approval.approval_id).consumed_at is None


def test_reject_after_approval_consumed_and_executed_is_refused(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    approval = approve_for(rig, action)
    plan, checkpoint = takeover(rig)
    done = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch), checkpoint, {action.action_id: approval.approval_id})
    assert done.state == "completed"
    with pytest.raises(IllegalTransition):
        rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    assert rig.tm.get_action(action.action_id).status == ActionStatus.SUCCEEDED.value
    assert rig.executor.execution_count(action.action_id) == 1


def test_approval_after_rejection_is_not_consumed_and_never_executes(session_factory):
    rig = parked_rig(session_factory)
    action = post_action(rig)
    rig.tm.record_rejection(principal=OWNER, action_id=action.action_id)
    approval = rig.tm.record_approval(
        principal=OWNER, action_hash=action.params_hash, target=action.tool, scope=SCOPE,
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    out = rig.tm.execute_action(
        action.action_id, {"path": "2"}, worker_epoch=rig.run.worker_epoch, approval_id=approval.approval_id,
    )
    assert (out.status, out.executed) == (ActionStatus.FAILED, False) and out.receipt.error_code == "approval_denied"
    assert approval_row(rig, approval.approval_id).consumed_at is None
    assert rig.executor.execution_count(action.action_id) == 0


def test_reject_non_approval_action_is_refused(session_factory):
    rig = DbRig(session_factory, [Round("", (call("write.s", "1"),)), Round("完成")], budget=LoopBudget(repeat_limit=99))
    assert rig.go().state == "completed"
    (write,) = [a for a in _actions(rig) if a.tool == "write.s"]
    with pytest.raises(IllegalTransition):  # 已 SUCCEEDED；也不需要核准
        rig.tm.record_rejection(principal=OWNER, action_id=write.action_id)
