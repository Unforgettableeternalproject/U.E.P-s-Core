"""Cycle 2 稽核區域 C（任務與上下文）的回歸測試：Task／Run／Action／Receipt（T-24，項目 1～7、9）。

每個測試對應稽核探針 P1～P13 的失敗情境；修正前皆會失敗。需 PostgreSQL（同 test_task_model）。
"""
# ruff: noqa: F811  fixture（mgr、executor）自 test_task_model 匯入後以參數名使用
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from tests.uep.test_task_model import (  # noqa: F401  fixture 與輔助函式沿用 test_task_model
    add_action,
    executor,
    mgr,
    new_task,
    running,
)
from uep.events.models import EventRow
from uep.events.payload_hash import PayloadHasher, PayloadHashKeyMissing
from uep.ids import is_valid_id, new_id
from uep.tasks.manager import (
    ActionBlocked,
    IllegalTransition,
    PolicyViolation,
    ResumeNotAllowed,
    StaleWorkerEpoch,
    TaskError,
    check_safe_value,
)
from uep.tasks.models import ReceiptRow, TaskRow
from uep.tasks.states import ActionStatus, PauseReason, RunStatus


def full(mgr, side="internal_read", idem="via_action_id"):
    task, run, step = running(mgr)
    params = {"a": 1}
    action = mgr.create_action(
        run_id=run.run_id, step_id=step.step_id, tool="files.write", executor="stub", params=params,
        side_effect_class=side, idempotency=idem, worker_epoch=run.worker_epoch,
    )
    return task, run, step, action, params


def events_of(session_factory, event_type):
    with session_factory() as s:
        return list(s.scalars(select(EventRow).where(EventRow.event_type == event_type).order_by(EventRow.journal_seq)))


# ---- 項目 1：Task／Run 脫鉤（P4、P4b） ----

def test_pause_task_via_transition_cascades_to_live_run(mgr):
    task, run, _step = running(mgr)
    mgr.transition_task(task.task_id, RunStatus.PAUSED, pause_reason=PauseReason.USER)
    got = mgr.get_run(run.run_id)
    assert got.status == "PAUSED" and got.pause_reason == "user"


def test_execute_action_blocked_when_task_paused_by_user(mgr, executor):
    task, run, _step, action, params = full(mgr)
    mgr.transition_task(task.task_id, RunStatus.PAUSED, pause_reason=PauseReason.USER)
    with pytest.raises(ActionBlocked):
        mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert executor.calls == [] and mgr.get_action(action.action_id).status == "INTENDED"


def test_execute_action_requires_task_running_even_if_run_diverged(mgr, executor, session_factory):
    """Task 已 PAUSED 但 Run 仍 RUNNING（脫鉤的歷史資料）：execute 仍須拒絕。"""
    task, run, _step, action, params = full(mgr)
    with session_factory.begin() as s:
        s.execute(update(TaskRow).where(TaskRow.task_id == task.task_id).values(status="PAUSED", pause_reason="user"))
    assert mgr.get_run(run.run_id).status == "RUNNING"
    with pytest.raises(ActionBlocked):
        mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert executor.calls == []


def test_start_run_rejected_on_paused_task(mgr):
    task = new_task(mgr)
    mgr.pause_task(task.task_id, PauseReason.USER)
    with pytest.raises(IllegalTransition):
        mgr.start_run(task.task_id, persona_version=1, policy_version=1)
    assert mgr.get_task(task.task_id).status == "PAUSED"


# ---- 項目 2：resume_run allowed_reasons（P3） ----

def test_resume_run_rejects_user_pause_by_default(mgr):
    task, run, _step = running(mgr)
    mgr.pause_task(task.task_id, PauseReason.USER)
    with pytest.raises(ResumeNotAllowed):
        mgr.resume_run(run.run_id, stale_after=timedelta(0))
    assert mgr.get_task(task.task_id).status == "PAUSED" and mgr.get_run(run.run_id).worker_epoch == 1


@pytest.mark.parametrize("reason", [PauseReason.COOLDOWN, PauseReason.RESOURCE_MISSING])
def test_resume_run_other_reasons_need_explicit_allow(mgr, reason):
    task, run, _step = running(mgr)
    mgr.pause_task(task.task_id, reason)
    with pytest.raises(ResumeNotAllowed):
        mgr.resume_run(run.run_id)
    plan = mgr.resume_run(run.run_id, allowed_reasons=frozenset({reason}))
    assert plan.run.status == "RUNNING" and plan.task.status == "RUNNING"


@pytest.mark.parametrize("reason", [PauseReason.CORE_SHUTDOWN, PauseReason.PRESENCE_LEFT])
def test_resume_run_default_allows_shutdown_and_presence_left(mgr, reason):
    task, run, _step = running(mgr)
    mgr.pause_task(task.task_id, reason)
    assert mgr.resume_run(run.run_id).run.status == "RUNNING"


def test_list_resumable_runs_exposes_pause_reason(mgr):
    task, run, _step = running(mgr)
    mgr.pause_task(task.task_id, PauseReason.USER)
    listed = mgr.list_resumable_runs()
    assert [(r.run_id, r.pause_reason) for r in listed] == [(run.run_id, "user")]


# ---- 項目 3：transition_run 的 worker_epoch 必填（P10） ----

def test_transition_run_requires_worker_epoch(mgr):
    _task, run, _step = running(mgr)
    with pytest.raises(TypeError):
        mgr.transition_run(run.run_id, RunStatus.SUCCEEDED)  # type: ignore[call-arg]


def test_stale_worker_cannot_end_run_and_force_transition_needs_reason(mgr):
    _task, run, _step = running(mgr)
    mgr.resume_run(run.run_id, stale_after=timedelta(0))  # epoch -> 2
    assert mgr.transition_run(run.run_id, RunStatus.SUCCEEDED, worker_epoch=1).status == "RUNNING"
    with pytest.raises(PolicyViolation):
        mgr.force_transition_run(run.run_id, RunStatus.FAILED, reason="")
    forced = mgr.force_transition_run(run.run_id, RunStatus.FAILED, reason="supervisor_deadline")
    assert forced.status == "FAILED"


# ---- 項目 4：NaN／Infinity 與階段 3 失敗（P2） ----

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_check_safe_value_rejects_non_finite_float(bad):
    with pytest.raises(PolicyViolation):
        check_safe_value({"x": bad})
    check_safe_value({"x": 1.5})


def test_receipt_with_nan_meta_is_redacted_not_lost(mgr, executor):
    _task, run, step = running(mgr)
    params = {"a": 1}
    action = add_action(mgr, run, step, params)
    original = executor._receipt
    executor._receipt = lambda call: replace(original(call), meta={"x": float("nan")})
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert outcome.status is ActionStatus.SUCCEEDED
    assert outcome.receipt.meta["redacted"] is True and "meta" in outcome.receipt.meta["redactions"]


def test_stage3_failure_marks_unknown_in_new_transaction_then_raises(mgr, executor, monkeypatch):
    _task, run, _step, action, params = full(mgr)

    def boom(*_args, **_kwargs):
        raise RuntimeError("入庫失敗")

    monkeypatch.setattr(mgr, "_record_receipt", boom)
    with pytest.raises(RuntimeError, match="入庫失敗"):
        mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert mgr.get_action(action.action_id).status == "UNKNOWN"  # 不留在 EXECUTING
    assert executor.calls == [action.action_id]


def test_duplicate_receipt_id_across_actions_leaves_unknown(mgr, executor):
    _task, run, step = running(mgr)
    shared = new_id("rcpt")
    original = executor._receipt
    executor._receipt = lambda call: replace(original(call), receipt_id=shared)
    first = add_action(mgr, run, step, {"k": 1})
    second = add_action(mgr, run, step, {"k": 2})
    mgr.execute_action(first.action_id, {"k": 1}, worker_epoch=run.worker_epoch)
    with pytest.raises(TaskError):
        mgr.execute_action(second.action_id, {"k": 2}, worker_epoch=run.worker_epoch)
    assert mgr.get_action(second.action_id).status == "UNKNOWN"


# ---- 項目 5：resolve_unknown 先查 receipts（P1、P9） ----

def _stale_epoch_verified_receipt(mgr, executor, succeeded=True):
    _task, run, _step, action, params = full(mgr)
    original = executor.execute

    def execute(call):
        receipt = original(call)
        mgr.resume_run(run.run_id, stale_after=timedelta(0))  # 新 worker 接手，舊 epoch 回報只記證據
        return replace(receipt, succeeded=succeeded) if not succeeded else receipt

    executor.execute = execute
    mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert mgr.get_action(action.action_id).status == "UNKNOWN"
    assert mgr.get_receipt(action.action_id).source_verified is True
    return action


@pytest.mark.parametrize("lookup", ["not_executed", "undetermined"])
def test_resolve_unknown_uses_stored_verified_receipt_never_back_to_intended(mgr, executor, lookup):
    action = _stale_epoch_verified_receipt(mgr, executor)
    if lookup == "not_executed":
        executor.done.clear()  # executor 誤報「未執行」
    else:
        executor.lookup_lost = True
    outcome = mgr.resolve_unknown(action.action_id)
    assert outcome.status is ActionStatus.SUCCEEDED and outcome.receipt.source_verified is True
    assert mgr.get_action(action.action_id).status == "SUCCEEDED"


def test_resolve_unknown_follows_failed_verified_receipt(mgr, executor):
    action = _stale_epoch_verified_receipt(mgr, executor)
    with mgr._sf.begin() as s:  # 把已存 receipt 改成失敗結果，驗證依 receipt 結果推進
        s.execute(update(ReceiptRow).where(ReceiptRow.action_id == action.action_id).values(succeeded=False))
    executor.done.clear()
    assert mgr.resolve_unknown(action.action_id).status is ActionStatus.FAILED


# ---- 項目 6：T-13（P6、P7） ----

def test_task_succeeded_blocked_by_unknown_action_in_earlier_run(mgr, executor):
    task, run, _step, action, params = full(mgr)
    executor.mode = "lose_after"
    mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert mgr.get_action(action.action_id).status == "UNKNOWN"
    mgr.transition_run(run.run_id, RunStatus.FAILED, worker_epoch=run.worker_epoch)
    run2 = mgr.start_run(task.task_id, persona_version=1, policy_version=1)
    mgr.transition_run(run2.run_id, RunStatus.SUCCEEDED, worker_epoch=run2.worker_epoch)
    with pytest.raises(IllegalTransition):
        mgr.transition_task(task.task_id, RunStatus.SUCCEEDED)
    assert mgr.get_task(task.task_id).status == "RUNNING"


def test_run_failed_with_unknown_action_leaves_queryable_event(mgr, executor, session_factory):
    _task, run, _step, action, params = full(mgr)
    executor.mode = "lose_after"
    mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    mgr.transition_run(run.run_id, RunStatus.CANCELLED, worker_epoch=run.worker_epoch)
    ended = events_of(session_factory, "run.ended")
    payload = ended[-1].payload
    assert payload["reason_code"] == "unresolved_actions" and payload["count"] == 1
    assert payload["action_id"] == action.action_id


def test_run_failed_without_leftover_has_no_unresolved_marker(mgr, session_factory):
    _task, run, _step = running(mgr)
    mgr.transition_run(run.run_id, RunStatus.FAILED, worker_epoch=run.worker_epoch)
    assert "reason_code" not in events_of(session_factory, "run.ended")[-1].payload


# ---- 項目 7：receipt 脫敏（P13） ----

def _noisy(executor, **changes):
    original = executor._receipt
    executor._receipt = lambda call: replace(original(call), **changes)


def test_receipt_id_must_be_rcpt_ulid_else_replaced(mgr, executor):
    _task, run, step = running(mgr)
    params = {"a": 1}
    action = add_action(mgr, run, step, params)
    _noisy(executor, receipt_id="my password is hunter2 and ssn 123")
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    rid = outcome.receipt.receipt_id
    assert is_valid_id(rid, "rcpt") and "hunter2" not in rid
    assert "receipt_id" in outcome.receipt.meta["redactions"]


def test_receipt_reserved_meta_keys_are_not_trusted(mgr, executor):
    _task, run, step = running(mgr)
    params = {"a": 1}
    action = add_action(mgr, run, step, params)
    fake = {"fake": {"sha256": "0" * 64, "reason": "x"}}
    _noisy(executor, meta={"redactions": fake, "redacted": True})
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert "fake" not in outcome.receipt.meta["redactions"] and "meta" in outcome.receipt.meta["redactions"]


@pytest.mark.parametrize("field_name,value", [
    ("result_ref", "alice@example.com"), ("result_ref", "a%20b"), ("error_code", "SGVsbG8=="),
    ("error_code", "a+b"),
])
def test_ref_charset_excludes_at_percent_equals_plus(mgr, executor, field_name, value):
    _task, run, step = running(mgr)
    params = {"a": 1}
    action = add_action(mgr, run, step, params)
    _noisy(executor, **{field_name: value})
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert getattr(outcome.receipt, field_name) == "redacted" and field_name in outcome.receipt.meta["redactions"]


def test_ref_with_newline_rejected_by_fullmatch(mgr, executor):
    _task, run, step = running(mgr)
    params = {"a": 1}
    action = add_action(mgr, run, step, params)
    _noisy(executor, result_ref="blob/abc\n")
    assert mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch).receipt.result_ref == "redacted"


def test_redaction_digest_is_hmac_and_fail_closed_without_key(mgr, executor, monkeypatch, payload_hash_key):
    from pydantic import SecretStr

    from uep.config import get_settings

    _task, run, step = running(mgr)
    params = {"a": 1}
    action = add_action(mgr, run, step, params)
    _noisy(executor, error_code="完全是中文說明")
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    digest = outcome.receipt.meta["redactions"]["error_code"]["sha256"]
    assert digest == PayloadHasher(SecretStr(payload_hash_key)).hash("完全是中文說明".encode())
    assert digest.startswith("hmac-sha256:")

    # 沒有金鑰：與 payload blob 一致 fail-closed；副作用已發生，action 轉 UNKNOWN 不留 EXECUTING
    monkeypatch.setattr(get_settings(), "payload_hash_key", None)
    params2 = {"a": 2}
    action2 = add_action(mgr, run, step, params2)
    _noisy(executor, error_code="完全是中文說明")
    with pytest.raises(PayloadHashKeyMissing):
        mgr.execute_action(action2.action_id, params2, worker_epoch=run.worker_epoch)
    assert mgr.get_action(action2.action_id).status == "UNKNOWN"


def test_receipt_records_worker_epoch(mgr, executor):
    _task, run, step = running(mgr)
    params = {"a": 1}
    action = add_action(mgr, run, step, params)
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert outcome.receipt.worker_epoch == run.worker_epoch == 1


# ---- 項目 9：次要 ----

def test_begin_step_rejected_on_terminal_run(mgr):
    _task, run, _step = running(mgr)
    mgr.transition_run(run.run_id, RunStatus.SUCCEEDED, worker_epoch=run.worker_epoch)
    with pytest.raises(IllegalTransition):
        mgr.begin_step(run.run_id, "late", worker_epoch=run.worker_epoch)


def test_create_action_with_existing_id_must_match_run_tool_and_class(mgr):
    _task, run, step, action, params = full(mgr, side="external_effect")
    _t2, run2, step2 = running(mgr)

    def again(**over):
        kw = {
            "run_id": run.run_id, "step_id": step.step_id, "tool": "files.write", "executor": "stub",
            "params": params, "side_effect_class": "external_effect", "idempotency": "via_action_id",
            "worker_epoch": run.worker_epoch, "action_id": action.action_id,
        }
        kw.update(over)
        return mgr.create_action(**kw)

    assert again().action_id == action.action_id  # 完全相同仍冪等
    with pytest.raises(PolicyViolation):
        again(run_id=run2.run_id, step_id=step2.step_id, worker_epoch=run2.worker_epoch)
    with pytest.raises(PolicyViolation):
        again(tool="OTHER")
    with pytest.raises(PolicyViolation):
        again(side_effect_class="internal_read")


def test_stale_epoch_heartbeat_checkpoint_complete_step_raise(mgr):
    _task, run, step = running(mgr)
    mgr.resume_run(run.run_id, stale_after=timedelta(0))
    with pytest.raises(StaleWorkerEpoch):
        mgr.heartbeat(run.run_id, worker_epoch=1)
    with pytest.raises(StaleWorkerEpoch):
        mgr.save_checkpoint(run.run_id, {"completed_steps": ["x"]}, worker_epoch=1)
    with pytest.raises(StaleWorkerEpoch):
        mgr.complete_step(step.step_id, worker_epoch=1)
    assert mgr.get_run(run.run_id).checkpoint is None


def test_safe_str_uses_fullmatch():
    with pytest.raises(PolicyViolation):
        check_safe_value("abc\n")
    with pytest.raises(PolicyViolation):
        check_safe_value({"k\n": 1})
