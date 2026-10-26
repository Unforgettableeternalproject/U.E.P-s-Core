"""CC-6 Task／Run／Step／ActionIntent／Receipt／Approval 整合測試（需 PostgreSQL；未設 UEP_TEST_DATABASE_URL 時 skip）。

涵蓋 T-1～T-5、T-9、T-11、VC-6-1～VC-6-4。Executor 為本檔內的最小 stub（正式 fake 由 uep/fakes 提供）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from uep.adapters.tool import ReceiptLookup, ToolCall, ToolReceipt, ToolResponseLost
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.models import EventRow
from uep.events.publisher import EventPublisher
from uep.tasks.manager import (
    ActionBlocked,
    ActiveWorker,
    ApprovalInvalid,
    ApprovalRequired,
    AuthenticatedPrincipal,
    IllegalTransition,
    ParamsHashMismatch,
    PolicyViolation,
    StaleVersion,
    StaleWorkerEpoch,
    TaskManager,
    TaskPolicy,
    params_hash,
)
from uep.tasks.models import (
    ActionIntentRow,
    ActivityRow,
    ApprovalRow,
    ReceiptRow,
    StepRow,
    TaskRow,
)
from uep.tasks.states import (
    ACTION_TRANSITIONS,
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    TERMINAL_STATUSES,
    ActionStatus,
    InterruptPolicy,
    PauseReason,
    RunStatus,
)

SCOPE = "scope_private_owner"
HUMAN = AuthenticatedPrincipal(actor_id="owner", kind="human", verified=True)
POLICY = TaskPolicy(presence_bound=False, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_1")


class Crash(BaseException):
    """模擬程序在 executor 呼叫途中崩潰（不是 Exception，TaskManager 不應吞掉）。"""


class StubExecutor:
    """最小 executor：mode 控制 execute 行為，lookup_lost 控制查核是否可用。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.done: dict[str, ToolReceipt] = {}
        self.mode = "ok"  # ok | lose_after | lose_before | crash_after | unverified | slow
        self.lookup_lost = False
        self.lookup_authoritative = True
        self.delay = 0.0

    def _receipt(self, call: ToolCall) -> ToolReceipt:
        return ToolReceipt(
            receipt_id=f"rcpt_{call.action_id}", action_id=call.action_id, succeeded=True,
            result_ref=f"ref_{call.action_id}", recorded_at=datetime.now(UTC), source_verified=True,
        )

    def execute(self, call: ToolCall) -> ToolReceipt:
        self.calls.append(call.action_id)
        if self.delay:
            time.sleep(self.delay)
        if self.mode == "lose_before":
            raise ToolResponseLost
        receipt = self._receipt(call)
        if self.mode == "unverified":
            receipt = replace(receipt, source_verified=False)
        self.done[call.action_id] = receipt  # 副作用已發生
        if self.mode == "lose_after":
            raise ToolResponseLost
        if self.mode == "crash_after":
            raise Crash
        return receipt

    def lookup_receipt(self, action_id: str) -> ReceiptLookup:
        if self.lookup_lost:
            return ReceiptLookup("undetermined")
        if action_id in self.done:
            return ReceiptLookup("found", self.done[action_id])
        return ReceiptLookup("confirmed_not_executed")


@pytest.fixture
def executor() -> StubExecutor:
    return StubExecutor()


def make_manager(session_factory, executor, **kwargs) -> TaskManager:
    return TaskManager(session_factory, EventPublisher(session_factory, EventBus()), {"stub": executor}, **kwargs)


@pytest.fixture
def mgr(session_factory, executor) -> TaskManager:
    return make_manager(session_factory, executor)


def new_task(mgr: TaskManager, **kwargs) -> TaskRow:
    return mgr.create_task(
        persona_id="uep", goal="整理報告", owner_actor_id="owner", scope_ref=SCOPE,
        policy=kwargs.pop("policy", POLICY), **kwargs,
    )


def running(mgr: TaskManager):
    """建立 Task、啟動 Run、開一個 Step；回傳 (task, run, step)。"""
    task = new_task(mgr)
    run = mgr.start_run(task.task_id, persona_version=1, policy_version=1)
    step = mgr.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    return task, run, step


def add_action(mgr, run, step, params, *, side_effect="internal_read", **kwargs) -> ActionIntentRow:
    return mgr.create_action(
        run_id=run.run_id, step_id=step.step_id, tool="files.write", executor="stub", params=params,
        side_effect_class=side_effect, idempotency="via_action_id", worker_epoch=run.worker_epoch, **kwargs,
    )


def approve(mgr, params, *, target="files.write", scope=SCOPE, delta=timedelta(hours=1), **kwargs) -> ApprovalRow:
    return mgr.record_approval(
        principal=kwargs.pop("principal", HUMAN),
        action_hash=params_hash(params), target=target, scope=scope,
        expiry=datetime.now(UTC) + delta, **kwargs,
    )


def event_types(session_factory) -> list[str]:
    with session_factory() as s:
        return list(s.scalars(select(EventRow.event_type).order_by(EventRow.journal_seq)))


# ---- 狀態機表（不需 DB 的性質） ----

def test_transition_tables_are_total_and_terminal_states_are_sinks():
    assert set(RUN_TRANSITIONS) == set(RunStatus) and TASK_TRANSITIONS is RUN_TRANSITIONS
    for status in TERMINAL_STATUSES:
        assert not RUN_TRANSITIONS[status]
    assert not RUN_TRANSITIONS[RunStatus.PENDING] & {RunStatus.SUCCEEDED, RunStatus.WAITING_APPROVAL}
    assert set(ACTION_TRANSITIONS) == set(ActionStatus)
    # UNKNOWN 只能經查核離開；不可直接 EXECUTING／APPROVED
    assert ACTION_TRANSITIONS[ActionStatus.UNKNOWN] == {
        ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.INTENDED,
    }


# ---- Task／Run 狀態機 ----

def test_create_task_makes_task_activity_and_event(mgr, session_factory):
    task = new_task(mgr, origin_ref={"legacy_gs_id": "gs_1"})
    assert task.status == "PENDING" and task.task_id.startswith("task_")
    with session_factory() as s:
        activity = s.get(ActivityRow, task.activity_id)
        assert activity.kind == "task" and activity.status == "ACTIVE"
        assert activity.presence_bound == task.presence_bound
    assert event_types(session_factory) == ["task.created"]


def test_interrupt_policy_creator_can_only_tighten(mgr):
    policy = TaskPolicy(True, InterruptPolicy.CHECKPOINT_ONLY, "pol_1")
    assert new_task(mgr, policy=policy).interrupt_policy == "checkpoint_only"
    tightened = new_task(mgr, policy=policy, requested_interrupt=InterruptPolicy.NON_INTERRUPTIBLE)
    assert tightened.interrupt_policy == "non_interruptible"
    with pytest.raises(PolicyViolation):
        new_task(mgr, policy=policy, requested_interrupt=InterruptPolicy.INTERRUPTIBLE)


def test_legal_transitions_emit_registered_events(mgr, session_factory):
    task = new_task(mgr)
    run = mgr.start_run(task.task_id, persona_version=1, policy_version=1)
    assert mgr.get_task(task.task_id).status == "RUNNING"
    mgr.transition_task(task.task_id, RunStatus.WAITING_INPUT)
    mgr.transition_task(task.task_id, RunStatus.RUNNING)
    mgr.transition_run(run.run_id, RunStatus.SUCCEEDED)
    mgr.transition_task(task.task_id, RunStatus.SUCCEEDED)
    assert event_types(session_factory) == [
        "task.created", "run.started", "task.input_requested", "task.input_provided",
        "run.ended", "task.completed",
    ]
    with session_factory() as s:  # Task 終態後其 Activity 才結束
        assert s.get(ActivityRow, task.activity_id).status == "ENDED"


@pytest.mark.parametrize("target", [RunStatus.SUCCEEDED, RunStatus.WAITING_APPROVAL, RunStatus.UNKNOWN])
def test_illegal_transition_from_pending_rejected(mgr, session_factory, target):
    task = new_task(mgr)
    with pytest.raises(IllegalTransition):
        mgr.transition_task(task.task_id, target)
    assert mgr.get_task(task.task_id).status == "PENDING"
    assert event_types(session_factory) == ["task.created"]


def test_terminal_task_cannot_be_revived(mgr):
    task = new_task(mgr)
    mgr.transition_task(task.task_id, RunStatus.CANCELLED)
    for target in (RunStatus.RUNNING, RunStatus.PENDING, RunStatus.FAILED):
        with pytest.raises(IllegalTransition):
            mgr.transition_task(task.task_id, target)
    with pytest.raises(IllegalTransition):
        mgr.start_run(task.task_id, persona_version=1, policy_version=1)


def test_pause_reason_rules(mgr, session_factory):
    task = new_task(mgr)
    mgr.transition_task(task.task_id, RunStatus.RUNNING)
    with pytest.raises(IllegalTransition):  # PAUSED 必須帶 reason
        mgr.transition_task(task.task_id, RunStatus.PAUSED)
    with pytest.raises(IllegalTransition):  # 非 PAUSED 不可帶 reason
        mgr.transition_task(task.task_id, RunStatus.WAITING_INPUT, pause_reason=PauseReason.USER)
    paused = mgr.transition_task(task.task_id, RunStatus.PAUSED, pause_reason=PauseReason.RESOURCE_MISSING)
    assert paused.pause_reason == "resource_missing"
    resumed = mgr.transition_task(task.task_id, RunStatus.RUNNING)
    assert resumed.pause_reason is None
    with session_factory() as s, pytest.raises(IntegrityError):  # DB CHECK 也擋
        s.execute(text("UPDATE tasks SET status='PAUSED' WHERE task_id=:t"), {"t": task.task_id})
        s.commit()


def test_expected_version_guards_concurrent_writers(mgr):
    task = new_task(mgr)
    mgr.transition_task(task.task_id, RunStatus.RUNNING, expected_version=task.version)
    with pytest.raises(StaleVersion):
        mgr.transition_task(task.task_id, RunStatus.CANCELLED, expected_version=task.version)


def test_transition_guard_hook_can_veto_and_rolls_back(session_factory, executor):
    seen = []

    def guard(request):
        seen.append((request.entity, request.from_status, request.to_status))
        if request.to_status is RunStatus.CANCELLED:
            raise PermissionError("CC-7 拒絕")

    mgr = make_manager(session_factory, executor, transition_guard=guard)
    task = new_task(mgr)
    with pytest.raises(PermissionError):
        mgr.transition_task(task.task_id, RunStatus.CANCELLED)
    assert mgr.get_task(task.task_id).status == "PENDING"
    assert ("task", RunStatus.PENDING, RunStatus.CANCELLED) in seen
    assert event_types(session_factory) == ["task.created"]


def test_only_one_active_run_per_task(mgr):
    task = new_task(mgr)
    mgr.start_run(task.task_id, persona_version=1, policy_version=1)
    with pytest.raises(IllegalTransition):
        mgr.start_run(task.task_id, persona_version=1, policy_version=1)


def test_stale_epoch_reports_do_not_change_state(mgr):
    _task, run, step = running(mgr)
    mgr.resume_run(run.run_id, stale_after=timedelta(0))  # epoch 1 -> 2
    assert mgr.transition_run(run.run_id, RunStatus.SUCCEEDED, worker_epoch=1).status == "RUNNING"
    assert mgr.complete_step(step.step_id, worker_epoch=1).status == "RUNNING"
    assert mgr.save_checkpoint(run.run_id, {"completed_steps": ["x"]}, worker_epoch=1).checkpoint is None
    with pytest.raises(StaleWorkerEpoch):
        mgr.create_action(
            run_id=run.run_id, step_id=step.step_id, tool="t", executor="stub", params={},
            side_effect_class="internal_read", idempotency="none", worker_epoch=1,
        )


def test_checkpoint_only_accepts_contract_keys(mgr):
    _task, run, _step = running(mgr)
    with pytest.raises(PolicyViolation):
        mgr.save_checkpoint(run.run_id, {"python_object": 1}, worker_epoch=1)
    saved = mgr.save_checkpoint(
        run.run_id, {"completed_steps": ["step_a"], "next_step_semantic": "send_report"}, worker_epoch=1
    )
    assert saved.checkpoint["completed_steps"] == ["step_a"]


# ---- action_id 去重（T-2） ----

def test_same_action_id_not_executed_twice(mgr, executor):
    _task, run, step = running(mgr)
    params = {"path": "a.txt"}
    action = add_action(mgr, run, step, params, action_id="act_01JABCDEFGHJKMNPQRSTVWXYZ0")
    first = mgr.execute_action(action.action_id, params, worker_epoch=1)
    second = mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert first.status is second.status is ActionStatus.SUCCEEDED
    assert first.executed and not second.executed
    assert executor.calls == [action.action_id]
    assert second.receipt.receipt_id == first.receipt.receipt_id
    # create_action 以同 action_id 重呼叫回傳同一列；改參數須新 action
    assert add_action(mgr, run, step, params, action_id=action.action_id).action_id == action.action_id
    with pytest.raises(ParamsHashMismatch):
        add_action(mgr, run, step, {"path": "b.txt"}, action_id=action.action_id)
    with pytest.raises(ParamsHashMismatch):
        mgr.execute_action(action.action_id, {"path": "b.txt"}, worker_epoch=1)


def test_action_events_follow_state(mgr, session_factory):
    _task, run, step = running(mgr)
    action = add_action(mgr, run, step, {"k": 1})
    mgr.execute_action(action.action_id, {"k": 1}, worker_epoch=1)
    mgr.complete_step(step.step_id, worker_epoch=1)
    types = event_types(session_factory)
    assert types[-4:] == ["action.intended", "action.approved", "action.executed", "step.completed"]


# ---- Approval（T-4、T-5、VC-6-3、VC-6-4） ----

def test_external_effect_without_approval_stays_intended_and_task_waits(mgr, executor):
    task, run, step = running(mgr)
    params = {"to": "boss"}
    action = add_action(mgr, run, step, params, side_effect="external_effect")
    with pytest.raises(ApprovalRequired):
        mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert mgr.get_action(action.action_id).status == "INTENDED"
    assert mgr.get_task(task.task_id).status == "WAITING_APPROVAL"
    assert mgr.get_run(run.run_id).status == "WAITING_APPROVAL"
    assert executor.calls == []


def test_llm_claimed_approval_is_invalid(mgr, executor):
    _task, run, step = running(mgr)
    params = {"to": "boss"}
    action = add_action(mgr, run, step, params, side_effect="high_impact")
    with pytest.raises(ApprovalInvalid) as err:
        mgr.execute_action(action.action_id, params, worker_epoch=1, approval_id="appr_LLM_SAID_YES")
    assert err.value.code == "not_found"
    assert executor.calls == [] and mgr.get_action(action.action_id).status == "INTENDED"


def test_valid_approval_is_consumed_once_and_resumes_task(mgr, executor):
    task, run, step = running(mgr)
    params = {"to": "boss"}
    first = add_action(mgr, run, step, params, side_effect="external_effect")
    second = add_action(mgr, run, step, params, side_effect="external_effect")  # 同參數的另一個 action
    approval = approve(mgr, params)
    with pytest.raises(ApprovalRequired):
        mgr.execute_action(first.action_id, params, worker_epoch=1)
    outcome = mgr.execute_action(first.action_id, params, worker_epoch=1, approval_id=approval.approval_id)
    assert outcome.status is ActionStatus.SUCCEEDED
    assert mgr.get_task(task.task_id).status == "RUNNING"
    assert mgr.get_action(first.action_id).approval_ref == approval.approval_id
    # 再次引用同一 approval：被拒，且不執行
    with pytest.raises(ApprovalInvalid) as err:
        mgr.execute_action(second.action_id, params, worker_epoch=1, approval_id=approval.approval_id)
    assert err.value.code == "consumed"
    assert executor.calls == [first.action_id]


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"params": {"to": "attacker"}}, "hash_mismatch"),
        ({"target": "mail.send"}, "target_mismatch"),
        ({"scope": "scope_public"}, "scope_mismatch"),
        ({"delta": timedelta(seconds=-1)}, "expired"),
    ],
)
def test_approval_must_match_action(mgr, executor, override, code):
    _task, run, step = running(mgr)
    params = {"to": "boss"}
    action = add_action(mgr, run, step, params, side_effect="external_effect")
    approval_params = override.pop("params", params)
    approval = approve(mgr, approval_params, **override)
    with pytest.raises(ApprovalInvalid) as err:
        mgr.execute_action(action.action_id, params, worker_epoch=1, approval_id=approval.approval_id)
    assert err.value.code == code
    assert executor.calls == []
    with mgr._sf() as s:  # 不合格的嘗試不消耗 approval
        assert s.get(ApprovalRow, approval.approval_id).consumed_at is None


def test_approval_single_consumption_under_concurrency(session_factory, executor):
    mgr = make_manager(session_factory, executor)
    _task, run, step = running(mgr)
    params = {"to": "boss"}
    actions = [add_action(mgr, run, step, params, side_effect="external_effect") for _ in range(2)]
    approval = approve(mgr, params)
    results: list[object] = []
    barrier = threading.Barrier(2)

    def attempt(action_id: str) -> None:
        barrier.wait()
        try:
            results.append(mgr.execute_action(action_id, params, worker_epoch=1, approval_id=approval.approval_id))
        except ApprovalInvalid as exc:
            results.append(exc)

    threads = [threading.Thread(target=attempt, args=(a.action_id,)) for a in actions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if not isinstance(r, ApprovalInvalid)) == 1
    assert len(executor.calls) == 1


# ---- 回覆遺失（T-3、VC-6-2） ----

def _lost_action(mgr, executor, mode):
    task, run, step = running(mgr)
    params = {"x": 1}
    action = add_action(mgr, run, step, params)
    executor.mode = mode
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert outcome.status is ActionStatus.UNKNOWN
    return task, run, action, params


def test_unknown_blocks_blind_retry(mgr, executor):
    _task, _run, action, params = _lost_action(mgr, executor, "lose_after")
    executor.mode = "ok"
    with pytest.raises(ActionBlocked):
        mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert executor.calls == [action.action_id]


def test_unknown_lookup_finds_receipt(mgr, executor, session_factory):
    _task, _run, action, params = _lost_action(mgr, executor, "lose_after")
    outcome = mgr.resolve_unknown(action.action_id)
    assert outcome.status is ActionStatus.SUCCEEDED and outcome.receipt.source_verified
    assert executor.calls == [action.action_id]  # 查核不重送
    again = mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert again.status is ActionStatus.SUCCEEDED and not again.executed
    assert executor.calls == [action.action_id]
    assert "action.unknown" in event_types(session_factory)


def test_unknown_confirmed_not_executed_returns_to_intended(mgr, executor):
    _task, _run, action, params = _lost_action(mgr, executor, "lose_before")
    outcome = mgr.resolve_unknown(action.action_id)
    assert outcome.status is ActionStatus.INTENDED
    executor.mode = "ok"
    final = mgr.execute_action(action.action_id, params, worker_epoch=1)  # 同 action_id 重新執行
    assert final.status is ActionStatus.SUCCEEDED
    assert executor.calls == [action.action_id, action.action_id]


def test_unknown_confirmed_not_executed_external_needs_fresh_approval(mgr, executor):
    _task, run, step = running(mgr)
    params = {"to": "boss"}
    action = add_action(mgr, run, step, params, side_effect="external_effect")
    approval = approve(mgr, params)
    executor.mode = "lose_before"
    mgr.execute_action(action.action_id, params, worker_epoch=1, approval_id=approval.approval_id)
    assert mgr.resolve_unknown(action.action_id).status is ActionStatus.INTENDED
    executor.mode = "ok"
    with pytest.raises(ApprovalInvalid) as err:  # 舊批准已消耗
        mgr.execute_action(action.action_id, params, worker_epoch=1, approval_id=approval.approval_id)
    assert err.value.code == "consumed"
    fresh = approve(mgr, params)
    assert mgr.execute_action(
        action.action_id, params, worker_epoch=1, approval_id=fresh.approval_id
    ).status is ActionStatus.SUCCEEDED


def test_unknown_stays_unknown_when_lookup_unavailable(mgr, executor):
    _task, _run, action, params = _lost_action(mgr, executor, "lose_after")
    executor.lookup_lost = True
    assert mgr.resolve_unknown(action.action_id).status is ActionStatus.UNKNOWN
    assert mgr.get_action(action.action_id).status == "UNKNOWN"
    with pytest.raises(ActionBlocked):
        mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert executor.calls == [action.action_id]


# ---- 重啟與恢復（T-1、T-11、VC-6-1） ----

def test_restart_resumes_without_redoing_side_effects(session_factory, executor):
    old = make_manager(session_factory, executor)
    task = new_task(old, origin_ref={"legacy_gs_id": "gs_1"}, policy=TaskPolicy(True, InterruptPolicy.CHECKPOINT_ONLY, "p"))
    run = old.start_run(task.task_id, persona_version=1, policy_version=1)
    step1 = old.begin_step(run.run_id, "send", worker_epoch=1)
    done_params = {"to": "boss"}
    done = add_action(old, run, step1, done_params, side_effect="external_effect")
    old.execute_action(done.action_id, done_params, worker_epoch=1, approval_id=approve(old, done_params).approval_id)
    old.complete_step(step1.step_id, worker_epoch=1, result_ref="ref_1")
    old.save_checkpoint(
        run.run_id, {"completed_steps": [step1.step_id], "consumed_action_ids": [done.action_id]}, worker_epoch=1
    )
    step2 = old.begin_step(run.run_id, "write", worker_epoch=1)
    crashed_params = {"path": "b"}
    crashed = add_action(old, run, step2, crashed_params)
    executor.mode = "crash_after"  # 副作用已發生，但程序在收到回覆前崩潰
    with pytest.raises(Crash):
        old.execute_action(crashed.action_id, crashed_params, worker_epoch=1)
    assert old.get_action(crashed.action_id).status == "EXECUTING"
    executor.mode = "ok"

    # GS 結束事件不影響 Task（T-1）
    EventPublisher(session_factory, EventBus()).publish(
        EventDraft(event_type="conversation.episode_ended", payload={"conversation_id": "conv_1"}),
        TrustedContext(source="core.conversation", actor_id="core", scope_ref=SCOPE),
    )

    # 模擬重啟：全新 TaskManager／Session／Bus，只靠 DB
    fresh = make_manager(session_factory, executor)
    resumable = fresh.list_resumable_runs()
    assert [r.run_id for r in resumable] == [run.run_id]
    plan = fresh.resume_run(run.run_id, stale_after=timedelta(0))
    assert plan.run.worker_epoch == 2 and plan.task.status == "RUNNING"
    assert plan.checkpoint["completed_steps"] == [step1.step_id]
    assert [s.step_id for s in plan.open_steps] == [step2.step_id]
    assert plan.unknown_action_ids == [crashed.action_id]  # EXECUTING -> UNKNOWN（T-11）

    # 已有 receipt 的 action 不重做
    calls_before = list(executor.calls)
    again = fresh.execute_action(done.action_id, done_params, worker_epoch=2)
    assert again.status is ActionStatus.SUCCEEDED and not again.executed
    assert executor.calls == calls_before
    # 舊 worker 不能再產生副作用
    with pytest.raises(StaleWorkerEpoch):
        old.execute_action(done.action_id, done_params, worker_epoch=1)
    # 結果未知的 action 只能經查核推進，且不重送
    assert fresh.resolve_unknown(crashed.action_id).status is ActionStatus.SUCCEEDED
    assert executor.calls == calls_before

    fresh.complete_step(step2.step_id, worker_epoch=2)
    fresh.transition_run(run.run_id, RunStatus.SUCCEEDED, worker_epoch=2)
    fresh.transition_task(task.task_id, RunStatus.SUCCEEDED)
    types = event_types(session_factory)
    assert types.count("task.completed") == 1
    assert "task.cancelled" not in types and "task.failed" not in types


def test_core_shutdown_pause_then_resume_keeps_task_alive(mgr, session_factory):
    task, run, _step = running(mgr)
    paused = mgr.pause_task(task.task_id, PauseReason.CORE_SHUTDOWN)
    assert paused.status == "PAUSED" and paused.pause_reason == "core_shutdown"
    assert mgr.get_run(run.run_id).status == "PAUSED"
    plan = make_manager(session_factory, StubExecutor()).resume_run(run.run_id)
    assert plan.task.status == "RUNNING" and plan.run.status == "RUNNING"
    assert not {"task.cancelled", "task.failed"} & set(event_types(session_factory))


def test_resume_rejects_terminal_run(mgr):
    task, run, _step = running(mgr)
    mgr.transition_run(run.run_id, RunStatus.FAILED)
    mgr.transition_task(task.task_id, RunStatus.FAILED)
    with pytest.raises(IllegalTransition):
        mgr.resume_run(run.run_id)
    assert mgr.list_resumable_runs() == []


# ---- 資料層約束 ----

def test_receipt_unique_per_action_and_models_registered(mgr, session_factory):
    _task, run, step = running(mgr)
    action = add_action(mgr, run, step, {"k": 1})
    mgr.execute_action(action.action_id, {"k": 1}, worker_epoch=1)
    with session_factory() as s:
        assert s.scalar(select(ReceiptRow).where(ReceiptRow.action_id == action.action_id)) is not None
        assert s.scalar(select(StepRow).where(StepRow.run_id == run.run_id)) is not None
        s.add(ReceiptRow(
            receipt_id="rcpt_dup", action_id=action.action_id, succeeded=True, source_verified=True,
            recorded_at=datetime.now(UTC),
        ))
        with pytest.raises(IntegrityError):
            s.commit()


# ---- H3：resolve_unknown 只在權威且可冪等時才退回 INTENDED ----

def test_confirmed_not_executed_with_idempotency_none_stays_unknown(mgr, executor):
    _task, run, step = running(mgr)
    params = {"x": 1}
    action = mgr.create_action(
        run_id=run.run_id, step_id=step.step_id, tool="files.write", executor="stub", params=params,
        side_effect_class="internal_read", idempotency="none", worker_epoch=1,
    )
    executor.mode = "lose_before"
    mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert mgr.resolve_unknown(action.action_id).status is ActionStatus.UNKNOWN
    executor.mode = "ok"
    with pytest.raises(ActionBlocked):
        mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert executor.calls == [action.action_id]


def test_confirmed_not_executed_from_non_authoritative_executor_stays_unknown(mgr, executor):
    _task, _run, action, params = _lost_action(mgr, executor, "lose_before")
    executor.lookup_authoritative = False
    assert mgr.resolve_unknown(action.action_id).status is ActionStatus.UNKNOWN
    with pytest.raises(ActionBlocked):
        mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert executor.calls == [action.action_id]


def test_unverified_receipt_keeps_action_unknown(mgr, executor):
    _task, run, step = running(mgr)
    params = {"x": 1}
    action = add_action(mgr, run, step, params)
    executor.mode = "unverified"
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert outcome.status is ActionStatus.UNKNOWN and outcome.receipt.source_verified is False
    # 查核仍只拿到未驗證 receipt：維持 UNKNOWN；改為已驗證後可推進
    assert mgr.resolve_unknown(action.action_id).status is ActionStatus.UNKNOWN
    executor.done[action.action_id] = replace(executor.done[action.action_id], source_verified=True)
    assert mgr.resolve_unknown(action.action_id).status is ActionStatus.SUCCEEDED
    assert executor.calls == [action.action_id]


# ---- H4：Task 終態、in-flight action ----

def test_cancelled_task_closes_runs_and_blocks_actions(mgr, session_factory, executor):
    task, run, step = running(mgr)
    params = {"x": 1}
    action = add_action(mgr, run, step, params)
    mgr.transition_task(task.task_id, RunStatus.CANCELLED)
    assert mgr.get_run(run.run_id).status == "CANCELLED"
    assert "run.ended" in event_types(session_factory)
    with pytest.raises(ActionBlocked):
        mgr.execute_action(action.action_id, params, worker_epoch=1)
    with pytest.raises(ActionBlocked):
        add_action(mgr, run, step, {"y": 2})
    assert executor.calls == []


def test_cannot_succeed_with_unknown_action(mgr, executor):
    task, run, step = running(mgr)
    params = {"x": 1}
    action = add_action(mgr, run, step, params)
    executor.mode = "lose_after"
    mgr.execute_action(action.action_id, params, worker_epoch=1)
    with pytest.raises(IllegalTransition):
        mgr.transition_run(run.run_id, RunStatus.SUCCEEDED)
    with pytest.raises(IllegalTransition):
        mgr.transition_task(task.task_id, RunStatus.SUCCEEDED)
    assert mgr.get_task(task.task_id).status == "RUNNING"
    mgr.resolve_unknown(action.action_id)
    mgr.transition_task(task.task_id, RunStatus.SUCCEEDED)  # 查核完成後，Task 終態連帶收尾 Run
    assert mgr.get_run(run.run_id).status == "SUCCEEDED"


# ---- H2：鎖定後重讀，併發 ----

def test_concurrent_execute_same_action_calls_executor_once(session_factory, executor):
    mgr = make_manager(session_factory, executor)
    _task, run, step = running(mgr)
    params = {"x": 1}
    action = add_action(mgr, run, step, params)
    executor.delay = 0.3
    results: list[object] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        barrier.wait()
        try:
            results.append(mgr.execute_action(action.action_id, params, worker_epoch=1))
        except Exception as exc:  # noqa: BLE001  收集例外型別供斷言
            results.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert executor.calls == [action.action_id]
    blocked = [r for r in results if isinstance(r, ActionBlocked)]
    done = [r for r in results if not isinstance(r, Exception)]
    assert len(blocked) == 1 and len(done) == 1 and done[0].status is ActionStatus.SUCCEEDED


def test_resume_during_execute_fences_old_epoch(session_factory, executor):
    mgr = make_manager(session_factory, executor)
    _task, run, step = running(mgr)
    params = {"x": 1}
    action = add_action(mgr, run, step, params)
    executor.delay = 0.5
    holder: list[object] = []
    worker = threading.Thread(
        target=lambda: holder.append(mgr.execute_action(action.action_id, params, worker_epoch=1))
    )
    worker.start()
    while not executor.calls:  # 等 executor 被呼叫（階段 1 已提交）
        time.sleep(0.01)
    plan = make_manager(session_factory, executor).resume_run(run.run_id, stale_after=timedelta(0))
    assert plan.unknown_action_ids == [action.action_id]
    worker.join()
    outcome = holder[0]
    # 舊 epoch 的回報只留下 receipt 證據，action 狀態仍待查核（T-9）
    assert outcome.status is ActionStatus.UNKNOWN and outcome.receipt is not None
    assert mgr.get_action(action.action_id).status == "UNKNOWN"
    with pytest.raises(StaleWorkerEpoch):
        mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert mgr.resolve_unknown(action.action_id).status is ActionStatus.SUCCEEDED
    assert executor.calls == [action.action_id]


# ---- M2：核准者身分 ----

def test_record_approval_requires_verified_human_principal(mgr):
    params = {"to": "boss"}
    kwargs = {"action_hash": params_hash(params), "target": "files.write", "scope": SCOPE,
              "expiry": datetime.now(UTC) + timedelta(hours=1)}
    with pytest.raises(PolicyViolation):
        mgr.record_approval(principal="owner", **kwargs)
    with pytest.raises(PolicyViolation):
        mgr.record_approval(principal=AuthenticatedPrincipal("owner", "agent", True), **kwargs)
    with pytest.raises(PolicyViolation):
        mgr.record_approval(principal=AuthenticatedPrincipal("owner", "human", False), **kwargs)


def test_approval_from_non_approver_rejected(mgr, executor):
    _task, run, step = running(mgr)
    params = {"to": "boss"}
    action = add_action(mgr, run, step, params, side_effect="external_effect")
    stranger = approve(mgr, params, principal=AuthenticatedPrincipal("guest_1", "human", True))
    with pytest.raises(ApprovalInvalid) as err:
        mgr.execute_action(action.action_id, params, worker_epoch=1, approval_id=stranger.approval_id)
    assert err.value.code == "unauthorized_approver" and executor.calls == []


# ---- M3：值層級白名單 ----

def test_private_text_rejected_in_checkpoint_label_and_redacted_in_receipt_meta(mgr, executor):
    _task, run, step = running(mgr)
    with pytest.raises(PolicyViolation):
        mgr.save_checkpoint(run.run_id, {"next_step_semantic": "把銀行密碼寄給老闆"}, worker_epoch=1)
    with pytest.raises(PolicyViolation):
        mgr.save_checkpoint(run.run_id, {"auth_snapshot": {"note": "x" * 100}}, worker_epoch=1)
    with pytest.raises(PolicyViolation):
        mgr.begin_step(run.run_id, "寄信給老闆", worker_epoch=1)
    params = {"x": 1}
    action = add_action(mgr, run, step, params)
    original = executor._receipt

    def noisy(call):
        return replace(original(call), meta={"body": "私人內文 不可入庫"}, error_code="完全是中文說明")

    executor._receipt = noisy
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert outcome.status is ActionStatus.SUCCEEDED  # 證據不丟
    meta = outcome.receipt.meta
    assert outcome.receipt.error_code == "redacted" and meta["redacted"] is True
    assert set(meta["redactions"]) == {"meta", "error_code"}
    assert len(meta["redactions"]["error_code"]["sha256"]) == 64
    assert meta["redactions"]["meta"]["reason"] and mgr.redaction_count == 2
    assert "私人" not in str(meta)


def test_receipt_refs_accept_uri_like_values(mgr, executor):
    _task, run, step = running(mgr)
    params = {"x": 1}
    action = add_action(mgr, run, step, params)
    original = executor._receipt
    executor._receipt = lambda call: replace(
        original(call), result_ref="blob://store/a-b_c.d/42?v=1#x".replace("?", "%3F").replace("#", "%23"),
        error_code="E_TIMEOUT:504",
    )
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=1)
    assert outcome.receipt.result_ref.startswith("blob://store/") and outcome.receipt.error_code == "E_TIMEOUT:504"
    assert outcome.receipt.meta is None and mgr.redaction_count == 0


# ---- L1／L2／L3 與崩潰 hook ----

def test_pause_task_is_idempotent(mgr):
    task, run, _step = running(mgr)
    mgr.pause_task(task.task_id, PauseReason.PRESENCE_LEFT)
    again = mgr.pause_task(task.task_id, PauseReason.PRESENCE_LEFT)
    assert again.status == "PAUSED" and mgr.get_run(run.run_id).status == "PAUSED"


def test_resume_requires_stale_heartbeat(mgr):
    _task, run, _step = running(mgr)
    with pytest.raises(ActiveWorker):
        mgr.resume_run(run.run_id)  # 預設 60 秒內有心跳
    mgr.heartbeat(run.run_id, worker_epoch=1)
    with pytest.raises(ActiveWorker):
        mgr.resume_run(run.run_id, stale_after=timedelta(minutes=5))
    assert mgr.resume_run(run.run_id, stale_after=timedelta(0)).run.worker_epoch == 2


def test_dispatch_failure_does_not_break_committed_transition(session_factory, executor):
    publisher = EventPublisher(session_factory, EventBus())

    def boom(_recorded, **_kw):
        raise RuntimeError("bus down")

    publisher.dispatch = boom
    mgr = TaskManager(session_factory, publisher, {"stub": executor})
    task = new_task(mgr)
    assert mgr.transition_task(task.task_id, RunStatus.RUNNING).status == "RUNNING"
    assert event_types(session_factory) == ["task.created"]


def test_crash_hook_points(session_factory, executor):
    points: list[str] = []

    def hook(point: str) -> None:
        points.append(point)
        if point == crash_at:
            raise Crash

    crash_at = "action.before_receipt_commit"
    mgr = make_manager(session_factory, executor, crash_hook=hook)
    _task, run, step = running(mgr)
    params = {"x": 1}
    action = add_action(mgr, run, step, params)
    with pytest.raises(Crash):
        mgr.execute_action(action.action_id, params, worker_epoch=1)
    # 階段 3 在提交前崩潰：副作用已發生，但 DB 仍是 EXECUTING，receipt 未入庫
    assert mgr.get_action(action.action_id).status == "EXECUTING" and mgr.get_receipt(action.action_id) is None
    assert "action.after_executing_commit" in points
    crash_at = ""
    task2 = new_task(mgr)
    crash_at = "events.before_dispatch"
    with pytest.raises(Crash):
        mgr.transition_task(task2.task_id, RunStatus.CANCELLED)
    assert mgr.get_task(task2.task_id).status == "CANCELLED"  # 已提交，只是沒投遞


def test_lookup_exception_is_undetermined(mgr, executor):
    _task, _run, action, params = _lost_action(mgr, executor, "lose_before")

    def boom(_action_id):
        raise RuntimeError("lookup crashed")

    executor.lookup_receipt = boom
    assert mgr.resolve_unknown(action.action_id).status is ActionStatus.UNKNOWN
    with pytest.raises(ActionBlocked):
        mgr.execute_action(action.action_id, params, worker_epoch=1)


def test_unverified_receipt_replaced_by_verified_with_different_receipt_id(mgr, executor):
    _task, run, step = running(mgr)
    params = {"x": 1}
    action = add_action(mgr, run, step, params)
    executor.mode = "unverified"
    mgr.execute_action(action.action_id, params, worker_epoch=1)
    executor.done[action.action_id] = replace(
        executor.done[action.action_id], receipt_id="rcpt_second", source_verified=True
    )
    outcome = mgr.resolve_unknown(action.action_id)
    assert outcome.status is ActionStatus.SUCCEEDED and outcome.receipt.receipt_id == "rcpt_second"


def test_paused_run_with_executing_action_waits_for_heartbeat_window(session_factory, executor):
    mgr = make_manager(session_factory, executor)
    task, run, step = running(mgr)
    params = {"x": 1}
    action = add_action(mgr, run, step, params)
    executor.mode = "crash_after"
    with pytest.raises(Crash):
        mgr.execute_action(action.action_id, params, worker_epoch=1)
    mgr.pause_task(task.task_id, PauseReason.CORE_SHUTDOWN)
    assert mgr.get_run(run.run_id).paused_at is not None
    with pytest.raises(ActiveWorker):
        mgr.resume_run(run.run_id)  # 暫停時仍有 EXECUTING action，預設視窗內不可接手
    plan = mgr.resume_run(run.run_id, stale_after=timedelta(0))
    assert plan.unknown_action_ids == [action.action_id] and plan.run.paused_at is None


def test_resume_window_uses_injected_clock(session_factory, executor):
    now = [datetime.now(UTC)]
    mgr = make_manager(session_factory, executor, clock=lambda: now[0])
    _task, run, _step = running(mgr)
    with pytest.raises(ActiveWorker):
        mgr.resume_run(run.run_id)
    now[0] += timedelta(seconds=61)
    assert mgr.resume_run(run.run_id).run.worker_epoch == 2
