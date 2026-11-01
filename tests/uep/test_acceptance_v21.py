"""V21 驗收：工具已做完但 response 遺失，能查 receipt 或轉 UNKNOWN，不盲目 retry（VC-6-2、T-3、T-15）。

全部使用 fake Adapter（FakeToolExecutor／FakeOutputAdapter）；需 PostgreSQL（UEP_TEST_DATABASE_URL）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.uep.acceptance_support import (
    HookedExecutor,
    LyingExecutor,
    drive,
    event_types,
    make_tm,
    receipt_count,
    setup_run,
    status_of,
)
from tests.uep.factories import make_intent
from uep.fakes.crash import CrashInjector, SimulatedCrash
from uep.fakes.output import FakeOutputAdapter
from uep.fakes.tool import FakeToolExecutor
from uep.output.delivery import DeliveryStatus as S
from uep.output.manager import DeliveryManager
from uep.tasks.manager import (
    ActionBlocked,
    ApprovalInvalid,
    ApprovalRequired,
    AuthenticatedPrincipal,
    IllegalTransition,
    params_hash,
)
from uep.tasks.models import ReceiptRow
from uep.tasks.states import RunStatus

pytestmark = pytest.mark.usefixtures("session_factory")
HUMAN = AuthenticatedPrincipal(actor_id="owner", kind="human", verified=True)


def _lost(session_factory, mode="lose_after_exec", idem="via_action_id", *, executor=None, **kw):
    """建立一個 action 並讓它在 mode 下 response 遺失，回傳 (tm, executor, item, run, task)。"""
    ex = executor or FakeToolExecutor()
    tm = make_tm(session_factory, ex)
    task, run, _step, (item,) = setup_run(tm, 1, idempotency=idem, **kw)
    ex.execute_behavior(item.action_id, mode)
    out = tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    assert out.status.value == "UNKNOWN" and out.executed  # response 遺失 -> UNKNOWN，不拋例外
    return tm, ex, item, run, task


# ---- (a) found：依 receipt 推進 ----

def test_v21a_lose_after_exec_then_found_receipt_succeeds_without_second_execution(session_factory):
    """V21／VC-6-2：已執行但回覆遺失 -> UNKNOWN -> 查核 found -> SUCCEEDED；execute 不再被呼叫；Task／Run 不受影響。"""
    tm, ex, item, run, task = _lost(session_factory)
    assert ex.execution_count(item.action_id) == 1
    assert tm.get_task(task.task_id).status == "RUNNING" and tm.get_run(run.run_id).status == "RUNNING"  # UNKNOWN 不連動
    out = tm.resolve_unknown(item.action_id)
    assert out.status.value == "SUCCEEDED" and not out.executed
    assert receipt_count(session_factory, item.action_id) == 1
    assert ex.execution_count(item.action_id) == 1 and ex.call_count(item.action_id) == 1
    assert tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch).executed is False
    assert ex.call_count(item.action_id) == 1


def test_v21a_lookup_raising_is_treated_as_undetermined(session_factory):
    """V21／T-15：lookup 拋例外視為 undetermined，action 維持 UNKNOWN，且不重送。"""
    tm, ex, item, _run, _ = _lost(session_factory)

    def boom(_action_id):
        raise ConnectionError("查核通道失聯")

    ex.lookup_receipt = boom
    assert tm.resolve_unknown(item.action_id).status.value == "UNKNOWN"
    assert ex.call_count(item.action_id) == 1 and ex.execution_count(item.action_id) == 1


# ---- (b) undetermined：維持 UNKNOWN，絕不重送 ----

def test_v21b_undetermined_stays_unknown_and_never_reexecutes(session_factory):
    """V21／T-3：undetermined 反覆查核、重啟、嘗試 execute 都維持 UNKNOWN；execute 呼叫數恆為 1。"""
    tm, ex, item, run, _task = _lost(session_factory)
    ex.lookup_outcome(item.action_id, "undetermined")
    for _ in range(3):
        assert tm.resolve_unknown(item.action_id).status.value == "UNKNOWN"
    with pytest.raises(ActionBlocked):  # 盲目 retry 被擋
        tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    # worker 重啟（新 epoch／新 manager）後依然如此
    tm2 = make_tm(session_factory, ex)
    plan = tm2.resume_run(run.run_id, stale_after=timedelta(0))
    assert plan.unknown_action_ids == [item.action_id]
    with pytest.raises(ActionBlocked):
        tm2.execute_action(item.action_id, item.params, worker_epoch=plan.run.worker_epoch)
    assert ex.call_count(item.action_id) == 1 and ex.execution_count(item.action_id) == 1
    # Run 不能在結果未明時宣稱完成（T-13）
    with pytest.raises(IllegalTransition):
        tm2.transition_run(run.run_id, RunStatus.SUCCEEDED, worker_epoch=plan.run.worker_epoch)
    # 查核恢復後可正常收斂
    ex.lookup_outcome(item.action_id, "found")
    assert tm2.resolve_unknown(item.action_id).status.value == "SUCCEEDED"
    assert ex.execution_count(item.action_id) == 1


# ---- (c) confirmed_not_executed 但條件不足：維持 UNKNOWN ----

@pytest.mark.parametrize("mode", ["lose_after_exec", "lose_before_exec"])
def test_v21c_idempotency_none_never_retried_even_if_authoritative(session_factory, mode):
    """V21／T-3：idempotency=none 即使 executor 權威且 confirmed_not_executed 也維持 UNKNOWN（交人工）。"""
    tm, ex, item, run, _ = _lost(session_factory, mode, idem="none")
    ex.lookup_outcome(item.action_id, "confirmed_not_executed")
    assert ex.lookup_authoritative is True
    assert tm.resolve_unknown(item.action_id).status.value == "UNKNOWN"
    with pytest.raises(ActionBlocked):
        tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    assert ex.call_count(item.action_id) == 1


@pytest.mark.parametrize("idem", ["native", "via_action_id"])
@pytest.mark.parametrize("forced", [True, False])
def test_v21c_non_authoritative_executor_never_retried(session_factory, idem, forced):
    """V21／T-3：executor 未宣告 lookup_authoritative 時，不論是否強制回 confirmed_not_executed，都不退回 INTENDED。"""
    tm, ex, item, run, _ = _lost(
        session_factory, "lose_before_exec", idem, executor=FakeToolExecutor(lookup_authoritative=False)
    )
    if forced:
        ex.lookup_outcome(item.action_id, "confirmed_not_executed")
    assert tm.resolve_unknown(item.action_id).status.value == "UNKNOWN"
    with pytest.raises(ActionBlocked):
        tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    assert ex.call_count(item.action_id) == 1


# ---- (d) 權威＋可冪等＋confirmed_not_executed：才重新執行，總效果仍 1 ----

@pytest.mark.parametrize("idem", ["native", "via_action_id"])
def test_v21d_authoritative_confirmed_not_executed_retries_exactly_once_effect(session_factory, idem):
    """V21／VC-6-2：lose_before_exec -> UNKNOWN -> 權威 confirmed_not_executed -> INTENDED -> 重跑，總效果 1。"""
    tm, ex, item, run, _ = _lost(session_factory, "lose_before_exec", idem)
    assert ex.execution_count(item.action_id) == 0
    assert tm.resolve_unknown(item.action_id).status.value == "INTENDED"
    ex.execute_behavior(item.action_id, "ok")
    out = tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    assert out.status.value == "SUCCEEDED" and out.executed
    assert ex.execution_count(item.action_id) == 1 and ex.call_count(item.action_id) == 2
    assert receipt_count(session_factory, item.action_id) == 1
    types = event_types(session_factory)
    assert types.count("action.unknown") == 1 and types.count("action.executed") == 1


def test_v21d_retry_of_external_effect_needs_fresh_approval(session_factory):
    """V21／T-4＋T-3：退回 INTENDED 的 external_effect 不能沿用已消耗的 approval，需新核准；總效果仍 1。"""
    ex = FakeToolExecutor()
    tm = make_tm(session_factory, ex)
    _task, run, _step, (item,) = setup_run(tm, 1, side_effect="external_effect")

    def approve():
        return tm.record_approval(
            principal=HUMAN, action_hash=params_hash("files.write", 1, item.params), target="files.write",
            scope="scope_private_owner", expiry=datetime.now(UTC) + timedelta(hours=1),
        )

    first = approve()
    ex.execute_behavior(item.action_id, "lose_before_exec")
    tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch, approval_id=first.approval_id)
    assert tm.resolve_unknown(item.action_id).status.value == "INTENDED"
    ex.execute_behavior(item.action_id, "ok")
    with pytest.raises(ApprovalInvalid) as err:  # 舊 approval 已單次消耗
        tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch,
                          approval_id=first.approval_id)
    assert err.value.code == "consumed"
    with pytest.raises(ApprovalRequired):
        tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    assert ex.execution_count(item.action_id) == 0
    out = tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch,
                            approval_id=approve().approval_id)
    assert out.status.value == "SUCCEEDED" and ex.execution_count(item.action_id) == 1


# ---- (e) 查核誤報：已執行卻回 confirmed_not_executed ----

@pytest.mark.parametrize(
    "idem,authoritative,executor_cls,expected_extra,expected_status",
    [
        ("via_action_id", True, FakeToolExecutor, 0, "SUCCEEDED"),  # executor 誠實去重：重跑吃回既有 receipt
        ("native", True, FakeToolExecutor, 0, "SUCCEEDED"),
        ("none", True, FakeToolExecutor, 0, "UNKNOWN"),             # Core 不重跑
        ("via_action_id", False, FakeToolExecutor, 0, "UNKNOWN"),   # 非權威：Core 不重跑
        ("via_action_id", True, LyingExecutor, 1, "SUCCEEDED"),     # 風險邊界：誤報＋宣稱可冪等卻不去重
    ],
)
def test_v21e_lookup_false_negative_duplicate_effect_matrix(
    session_factory, idem, authoritative, executor_cls, expected_extra, expected_status
):
    """V21／VC-6-2 風險邊界（量化）：已執行的 action 被誤報 confirmed_not_executed 時的重複效果次數。

    唯一會重複的組合是「executor 同時 (1) 查核誤報、(2) 宣告權威、(3) 宣稱可冪等卻實際不去重」，
    Core 無從得知（契約信任 executor 宣告）；其餘組合重複效果為 0。
    """
    ex = executor_cls(lookup_authoritative=authoritative)
    tm, ex, item, run, _ = _lost(session_factory, "lose_after_exec", idem, executor=ex)
    assert ex.execution_count(item.action_id) == 1
    ex.lookup_outcome(item.action_id, "confirmed_not_executed")  # 誤報
    resolved = tm.resolve_unknown(item.action_id)
    if resolved.status.value == "INTENDED":
        ex.execute_behavior(item.action_id, "ok")  # 這次回覆不再遺失
        tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    assert ex.execution_count(item.action_id) - 1 == expected_extra
    assert status_of(tm, item) == expected_status


# ---- (f) unverified receipt 不推進 ----

def test_v21f_unverified_receipt_never_advances(session_factory):
    """V21／T-3、M7：source_verified=False 的 receipt 不推進；execute 回的與 lookup 回的皆然；不重送。"""
    ex = FakeToolExecutor()
    tm = make_tm(session_factory, ex)
    _task, run, _step, (item,) = setup_run(tm, 1)
    ex.unverified_receipt(item.action_id)
    out = tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    assert out.status.value == "UNKNOWN"
    assert tm.get_receipt(item.action_id).source_verified is False
    for _ in range(2):
        assert tm.resolve_unknown(item.action_id).status.value == "UNKNOWN"
    with pytest.raises(ActionBlocked):
        tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    assert ex.call_count(item.action_id) == 1 and ex.execution_count(item.action_id) == 1


def test_v21f_unverified_lookup_receipt_after_lost_response_stays_unknown(session_factory):
    """V21／T-3：response 遺失後，lookup 找到的是未驗證 receipt -> 仍 UNKNOWN。"""
    ex = FakeToolExecutor()
    tm = make_tm(session_factory, ex)
    _task, run, _step, (item,) = setup_run(tm, 1)
    ex.execute_behavior(item.action_id, "lose_after_exec")
    ex.unverified_receipt(item.action_id)
    tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    assert tm.resolve_unknown(item.action_id).status.value == "UNKNOWN"
    assert ex.execution_count(item.action_id) == 1


# ---- crash 疊加：resolve 途中崩潰 ----

@pytest.mark.parametrize("outcome", ["found", "confirmed_not_executed"])
def test_v21_crash_in_resolve_before_commit_is_retryable(session_factory, outcome):
    """V21／VC-6-2：resolve_unknown 提交前崩潰，狀態不變（仍 UNKNOWN）；重試後收斂且總效果仍 1。"""
    crash, ex = CrashInjector(), FakeToolExecutor()
    mode = "lose_after_exec" if outcome == "found" else "lose_before_exec"
    tm = make_tm(session_factory, ex, crash=crash)
    _task, run, _step, (item,) = setup_run(tm, 1)
    ex.execute_behavior(item.action_id, mode)
    tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch)
    crash.arm("resolve.before_commit")
    with pytest.raises(SimulatedCrash):
        tm.resolve_unknown(item.action_id)
    tm2 = make_tm(session_factory, ex)
    assert status_of(tm2, item) == "UNKNOWN"
    ex.execute_behavior(item.action_id, "ok")
    drive(tm2, [item], run.worker_epoch)
    assert status_of(tm2, item) == "SUCCEEDED"
    assert ex.execution_count(item.action_id) == 1 and receipt_count(session_factory, item.action_id) == 1


def test_v21_failed_receipt_is_final_and_not_retried(session_factory):
    """V21：執行失敗的 receipt（succeeded=False）-> FAILED；再次 execute 不重跑。"""
    ex = FakeToolExecutor()
    tm = make_tm(session_factory, ex)
    _task, run, _step, (item,) = setup_run(tm, 1, idempotency="none")
    ex.execute_behavior(item.action_id, "fail")
    assert tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch).status.value == "FAILED"
    assert tm.execute_action(item.action_id, item.params, worker_epoch=run.worker_epoch).executed is False
    assert ex.call_count(item.action_id) == 1


def test_v21_zombie_worker_late_receipt_only_recorded_then_resolved(session_factory):
    """V21／T-9：舊 epoch worker 在新 worker 接手後才拿到 receipt -> 只記錄不推進，經 resolve_unknown 收斂；效果 1。"""
    holder: dict = {}

    def takeover(_call):  # executor 執行途中，另一個 supervisor 已接手同一 Run
        holder["plan"] = holder["new"].resume_run(holder["run_id"], stale_after=timedelta(0))

    ex = HookedExecutor(on_execute=takeover)
    old = make_tm(session_factory, ex)
    new = make_tm(session_factory, ex)
    _task, run, _step, (item,) = setup_run(old, 1)
    holder.update(new=new, run_id=run.run_id)
    out = old.execute_action(item.action_id, item.params, worker_epoch=1)
    assert holder["plan"].run.worker_epoch == 2
    assert out.status.value == "UNKNOWN"  # 舊 epoch 的結果不推進
    assert new.resolve_unknown(item.action_id).status.value == "SUCCEEDED"
    assert ex.execution_count(item.action_id) == 1 and receipt_count(session_factory, item.action_id) == 1


def test_v21_zombie_late_receipt_is_stored_immediately_then_resolve_prefers_stored_verified_receipt(session_factory):
    """V21／T-9：舊 epoch worker 的 execute_action 一回傳，receipt 就已入庫（已驗證）而 action 仍 UNKNOWN；

    resolve_unknown 以已存的已驗證 receipt 推進，不再信 executor 查核——即使 executor 此刻謊稱「確定未執行」，
    也不得退回 INTENDED 重做（效果仍只 1 次）。
    """
    holder: dict = {}

    def takeover(_call):
        holder["new"].resume_run(holder["run_id"], stale_after=timedelta(0))

    ex = HookedExecutor(on_execute=takeover)
    old = make_tm(session_factory, ex)
    new = make_tm(session_factory, ex)
    _task, run, _step, (item,) = setup_run(old, 1)
    holder.update(new=new, run_id=run.run_id)

    out = old.execute_action(item.action_id, item.params, worker_epoch=1)
    # execute_action 回傳的當下：證據已入庫、狀態仍 UNKNOWN（舊 epoch 不推進）
    assert out.status.value == "UNKNOWN"
    assert receipt_count(session_factory, item.action_id) == 1
    with session_factory() as s:
        stored = s.scalar(select(ReceiptRow).where(ReceiptRow.action_id == item.action_id))
        assert stored is not None and stored.source_verified is True and stored.succeeded is True
    assert status_of(new, item) == "UNKNOWN"
    # executor 查核被刻意設成謊言：若 resolve 還去問 executor，就會退回 INTENDED 並重做
    ex.lookup_outcome(item.action_id, "confirmed_not_executed")

    resolved = new.resolve_unknown(item.action_id)
    assert resolved.status.value == "SUCCEEDED"
    assert status_of(new, item) == "SUCCEEDED"
    assert ex.lookups[item.action_id] == 0  # 已有已驗證 receipt：不再向 executor 查核
    assert ex.execution_count(item.action_id) == 1 and receipt_count(session_factory, item.action_id) == 1


# ---- 輸出端類比：SendResponseLost -> unknown ----

def _dm(session_factory, **kw):
    adapter = FakeOutputAdapter(default_mode="lose_ack", **kw)
    return DeliveryManager(session_factory, adapter), adapter


def test_v21_output_lost_ack_non_authoritative_lookup_never_fails_or_resends(session_factory):
    """V21 輸出端類比／VC-5-3：ACK 遺失 -> unknown；非權威查核（含誤報 confirmed_not_sent）不標 failed、不重送。"""
    dm, adapter = _dm(session_factory)
    intent = make_intent()
    rec = dm.dispatch(intent)
    assert rec.status is S.UNKNOWN
    adapter.lookup_authoritative = False
    adapter.delivered.clear()  # 平台查不到
    assert dm.resolve_unknown(rec.delivery_id).status is S.UNKNOWN
    adapter.lookup_outcome(rec.dispatch_id, "confirmed_not_sent")  # 非權威卻說沒送
    assert dm.resolve_unknown(rec.delivery_id).status is S.UNKNOWN
    adapter.lookup_lost = True
    assert dm.resolve_unknown(rec.delivery_id).status is S.UNKNOWN
    assert dm.dispatch(intent).status is S.UNKNOWN  # 重複 dispatch 也不補送
    assert adapter.send_count(intent.output_id, 0) == 1 and adapter.effect_count(intent.output_id, 0) == 1


def test_v21_output_lost_ack_found_converges_to_sent_single_effect(session_factory):
    """V21 輸出端類比／VC-5-3：查得已送出 -> sent（或更高）；同一語意只留一筆 delivery、效果 1。"""
    dm, adapter = _dm(session_factory)
    intent = make_intent()
    rec = dm.dispatch(intent)
    resolved = dm.resolve_unknown(rec.delivery_id)
    assert resolved.status in (S.SENT, S.ACCEPTED, S.RENDERED_PLAYED)
    assert dm.dispatch(intent).delivery_id == rec.delivery_id
    assert adapter.effect_count(intent.output_id, 0) == 1 and adapter.send_count(intent.output_id, 0) == 1


def test_v21_output_authoritative_not_sent_fails_retryable_without_auto_resend(session_factory):
    """V21 輸出端類比：權威確認未送出 -> failed(retryable)；Core 不自動補送（send 次數不增加）。"""
    dm, adapter = _dm(session_factory)
    intent = make_intent()
    rec = dm.dispatch(intent)
    adapter.delivered.clear()
    resolved = dm.resolve_unknown(rec.delivery_id)
    assert resolved.status is S.FAILED and resolved.failure == {"code": "NOT_SENT", "retryable": True}
    assert dm.dispatch(intent).status is S.FAILED
    assert adapter.send_count(intent.output_id, 0) == 1
