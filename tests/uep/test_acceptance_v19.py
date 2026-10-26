"""V19 驗收：Task 在 GS／CS 結束後仍可恢復；回報不重做已完成副作用（VC-6-1、T-1、T-7、T-8、T-12、T-13）。

P1 沒有 GS 實體：以「建立 Task 的對話端物件被丟棄、全新 TaskManager／Bus／Publisher」模擬。
全部使用 fake Adapter；需 PostgreSQL（UEP_TEST_DATABASE_URL）。
"""
from __future__ import annotations

import gc
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from tests.uep.acceptance_support import (
    T0,
    action_row_count,
    arm_next,
    drive,
    event_types,
    make_tm,
    receipt_count,
    setup_run,
    status_of,
)
from tests.uep.factories import make_intent
from uep.events import journal
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.models import EventRow
from uep.events.outbox import OutboxDispatcher
from uep.fakes.crash import CrashInjector, SimulatedCrash
from uep.fakes.output import FakeOutputAdapter
from uep.fakes.tool import FakeToolExecutor
from uep.output.manager import DeliveryManager
from uep.output.models import DeliveryRow
from uep.tasks.manager import ActiveWorker, IllegalTransition, StaleWorkerEpoch
from uep.tasks.states import PauseReason, RunStatus

pytestmark = pytest.mark.usefixtures("session_factory")
CONV = TrustedContext(source="core.conversation", actor_id="core", scope_ref="scope_private_owner")


class FakeConversation:
    """對話端（GS／CS 等價物）：只持有建立 Task 的 TaskManager 參考；丟棄它就等於 GS 結束。"""

    def __init__(self, tm):
        self.tm = tm


# ---- GS 結束後的恢復 ----

def test_v19_task_survives_conversation_object_discard_and_resumes_on_new_manager(session_factory):
    """V19／VC-6-1：對話端物件丟棄、發 conversation.episode_ended、全新 manager 後，Task 可 list／resume 接續；
    已成功 action 不重做（execution_count 恆 1）；完成時只有一個 task.completed。"""
    ex = FakeToolExecutor()
    conv = FakeConversation(make_tm(session_factory, ex))
    task, run, step, items = setup_run(conv.tm, 3, origin_ref={"legacy_gs_id": "gs_1"})
    for item in items[:2]:
        conv.tm.execute_action(item.action_id, item.params, worker_epoch=1)
    conv.tm.save_checkpoint(
        run.run_id,
        {"completed_steps": [step.step_id], "consumed_action_ids": [i.action_id for i in items[:2]]},
        worker_epoch=1,
    )
    # GS 結束：事件落 journal，對話端物件與其 TaskManager 全部丟棄
    conv.tm._publisher.publish(
        EventDraft(event_type="conversation.episode_ended", payload={"conversation_id": "conv_1"}), CONV, sync=True
    )
    del conv
    gc.collect()

    tm2 = make_tm(session_factory, ex)
    assert [r.run_id for r in tm2.list_resumable_runs()] == [run.run_id]
    assert tm2.get_task(task.task_id).status == "RUNNING"
    plan = tm2.resume_run(run.run_id, stale_after=timedelta(0))
    assert plan.run.worker_epoch == 2
    assert plan.checkpoint["consumed_action_ids"] == [i.action_id for i in items[:2]]
    assert [a.action_id for a in plan.open_actions] == [items[2].action_id]
    drive(tm2, items, plan.run.worker_epoch)  # 全部再「回報」一次：前兩個不得重做
    assert [ex.execution_count(i.action_id) for i in items] == [1, 1, 1]
    assert [ex.call_count(i.action_id) for i in items[:2]] == [1, 1]  # 已完成者連 executor 都沒被叫
    tm2.transition_run(run.run_id, RunStatus.SUCCEEDED, worker_epoch=2)
    tm2.transition_task(task.task_id, RunStatus.SUCCEEDED)
    types = event_types(session_factory)
    assert types.count("task.completed") == 1
    assert not {"task.cancelled", "task.failed"} & set(types)


def test_v19_paused_for_core_shutdown_then_restart_resumes_without_replaying_effects(session_factory):
    """V19／T-11、VC-6-1：core_shutdown 暫停後重啟，Run 回 RUNNING 並以新 epoch 續跑，已完成副作用不重播。"""
    ex = FakeToolExecutor()
    tm = make_tm(session_factory, ex)
    task, run, _step, items = setup_run(tm, 2)
    tm.execute_action(items[0].action_id, items[0].params, worker_epoch=1)
    tm.pause_task(task.task_id, PauseReason.CORE_SHUTDOWN)
    assert tm.get_task(task.task_id).status == "PAUSED"
    tm2 = make_tm(session_factory, ex)
    assert [r.run_id for r in tm2.list_resumable_runs()] == [run.run_id]  # PAUSED 的 Run 也列為可恢復
    plan = tm2.resume_run(run.run_id)  # PAUSED 且沒有 EXECUTING：不需等心跳
    assert plan.run.worker_epoch == 2 and tm2.get_task(task.task_id).status == "RUNNING"
    drive(tm2, items, 2)
    assert [ex.execution_count(i.action_id) for i in items] == [1, 1]


# ---- 每個 TaskManager／executor crash 點 ----

# (注入器, 命中點, 第 nth 次命中, 崩潰前 executor 是否已實際執行)
CRASH_POINTS = [
    ("tm", "action.after_executing_commit", 1, False),
    ("tm", "action.before_receipt_commit", 1, True),
    ("tm", "events.before_dispatch", 1, False),  # execute_action 階段 1 提交後（action.approved 事件）
    ("tm", "events.before_dispatch", 2, True),   # 階段 3 提交後（action.executed 事件；receipt 已入庫）
    ("tool", "tool.before_execute", 1, False),
    ("tool", "tool.after_execute", 1, True),
]


@pytest.mark.parametrize("idem", ["via_action_id", "native", "none"])
@pytest.mark.parametrize("which,point,nth,executed", CRASH_POINTS, ids=lambda v: str(v))
def test_v19_crash_at_every_point_never_redoes_succeeded_actions(session_factory, idem, which, point, nth, executed):
    """V19／VC-6-1、T-2、T-3：在每個崩潰點崩潰後重啟，已成功 action 的 execution_count 恆為 1；
    崩潰的 action 最多執行 1 次（idempotency=none 且未執行者不自動重跑，維持 UNKNOWN）；
    Run 在有 UNKNOWN 時不得轉 SUCCEEDED（T-13）。"""
    tm_crash, tool_crash = CrashInjector(), CrashInjector()
    ex = FakeToolExecutor(tool_crash)
    tm = make_tm(session_factory, ex, crash=tm_crash)
    task, run, _step, items = setup_run(tm, 3, idempotency=idem)
    a1, a2, a3 = items
    tm.execute_action(a1.action_id, a1.params, worker_epoch=1)  # 已成功並有 receipt
    arm_next(tool_crash if which == "tool" else tm_crash, point, nth)
    with pytest.raises(SimulatedCrash):
        tm.execute_action(a2.action_id, a2.params, worker_epoch=1)
    assert ex.execution_count(a2.action_id) == (1 if executed else 0)
    del tm

    tm2 = make_tm(session_factory, ex)  # 重啟
    assert [r.run_id for r in tm2.list_resumable_runs()] == [run.run_id]
    plan = tm2.resume_run(run.run_id, stale_after=timedelta(0))
    epoch = plan.run.worker_epoch
    assert epoch == 2
    drive(tm2, items, epoch)

    assert ex.execution_count(a1.action_id) == 1 and ex.call_count(a1.action_id) == 1
    assert ex.execution_count(a3.action_id) == 1 and status_of(tm2, a3) == "SUCCEEDED"
    stuck = idem == "none" and not executed
    if stuck:  # 無法證明未執行、也無冪等保證：寧可卡住也不盲目重跑
        assert status_of(tm2, a2) == "UNKNOWN" and ex.execution_count(a2.action_id) == 0
        with pytest.raises(IllegalTransition):
            tm2.transition_run(run.run_id, RunStatus.SUCCEEDED, worker_epoch=epoch)
    else:
        assert status_of(tm2, a2) == "SUCCEEDED" and ex.execution_count(a2.action_id) == 1
        assert all(receipt_count(session_factory, i.action_id) == 1 for i in items)
        tm2.transition_run(run.run_id, RunStatus.SUCCEEDED, worker_epoch=epoch)
        tm2.transition_task(task.task_id, RunStatus.SUCCEEDED)
        assert event_types(session_factory).count("task.completed") == 1
    # 舊 epoch 的 worker 復活也產生不了副作用
    with pytest.raises(StaleWorkerEpoch):
        make_tm(session_factory, ex).execute_action(a3.action_id, a3.params, worker_epoch=1)
    assert ex.execution_count(a3.action_id) == 1


def test_v19_crash_during_resume_itself_then_resume_again(session_factory):
    """V19／T-12：resume_run 提交後（epoch 已 +1、EXECUTING -> UNKNOWN）、投遞事件前崩潰；再次接手仍收斂，效果 1。"""
    tm_crash, ex = CrashInjector(), FakeToolExecutor()
    tm = make_tm(session_factory, ex, crash=tm_crash)
    _task, run, _step, (item,) = setup_run(tm, 1)
    tm_crash.arm("action.before_receipt_commit")
    with pytest.raises(SimulatedCrash):
        tm.execute_action(item.action_id, item.params, worker_epoch=1)
    sup = make_tm(session_factory, ex, crash=tm_crash)
    arm_next(tm_crash, "events.before_dispatch")
    with pytest.raises(SimulatedCrash):
        sup.resume_run(run.run_id, stale_after=timedelta(0))
    assert status_of(sup, item) == "UNKNOWN"  # 已提交
    plan = make_tm(session_factory, ex).resume_run(run.run_id, stale_after=timedelta(0))
    assert plan.run.worker_epoch == 3 and plan.unknown_action_ids == [item.action_id]
    drive(make_tm(session_factory, ex), [item], 3)
    assert ex.execution_count(item.action_id) == 1 and status_of(sup, item) == "SUCCEEDED"


def test_v19_concurrent_resume_only_one_supervisor_wins(session_factory):
    """V19／T-12：心跳過期後多個 supervisor 同時接手，只有一個成功（epoch 恰 +1），其餘 ActiveWorker。"""
    now = [T0]
    ex = FakeToolExecutor()
    tm = make_tm(session_factory, ex, clock=lambda: now[0])
    _task, run, _step, _items = setup_run(tm, 1)
    now[0] = T0 + timedelta(seconds=120)
    barrier = Barrier(4)

    def attempt(_):
        mgr = make_tm(session_factory, ex, clock=lambda: now[0])
        barrier.wait()
        try:
            return mgr.resume_run(run.run_id, stale_after=timedelta(seconds=60)).run.worker_epoch
        except ActiveWorker:
            return "blocked"

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sorted(map(str, results)) == ["2", "blocked", "blocked", "blocked"]
    assert tm.get_run(run.run_id).worker_epoch == 2


# ---- Task 完成通知 OutputIntent 只送一次 ----

def _intent_for(task_id: str):
    """測試端把 Task 完成接到 Output：output_id 由 task_id 決定，重複觸發得到同一個 intent 身分（契約 VC-6-1）。"""
    return make_intent(output_id="out_" + task_id.split("_", 1)[1], sequence=0)


class Notifier:
    def __init__(self, dm: DeliveryManager) -> None:
        self.dm = dm

    def __call__(self, event) -> None:
        self.dm.dispatch(_intent_for(event.task_id))


def _drain(bus: EventBus) -> None:
    bus._queue.join()  # 等 Bus 消費者線程處理完已入列的事件（測試用，存取內部佇列）


def test_v19_task_completion_notifies_exactly_once_even_with_replayed_events(session_factory):
    """V19／VC-6-1、T-8：Task 完成 -> OutputIntent 送出一次；task.completed 被重複投遞、DeliveryManager 重建後仍只有 1 個效果、1 筆 delivery。"""
    bus = EventBus()
    bus.start()
    try:
        adapter = FakeOutputAdapter(default_mode="transport_only")  # 非冪等 adapter：重複 send 會真的多一次效果
        dm = DeliveryManager(session_factory, adapter)
        bus.subscribe("task.completed", Notifier(dm))
        ex = FakeToolExecutor()
        tm = make_tm(session_factory, ex, bus=bus)
        task, run, _step, items = setup_run(tm, 1)
        tm.execute_action(items[0].action_id, items[0].params, worker_epoch=1)
        tm.transition_run(run.run_id, RunStatus.SUCCEEDED, worker_epoch=1)
        tm.transition_task(task.task_id, RunStatus.SUCCEEDED)
        _drain(bus)
        intent = _intent_for(task.task_id)
        assert adapter.effect_count(intent.output_id, 0) == 1

        with session_factory() as s:
            event = journal.row_to_event(
                s.scalars(select(EventRow).where(EventRow.event_type == "task.completed")).one()
            )
        for _ in range(3):  # 重複投遞同一事件
            bus.publish(event)
        _drain(bus)
        Notifier(DeliveryManager(session_factory, adapter))(event)  # 重啟後的新 DeliveryManager 再被觸發
        assert adapter.effect_count(intent.output_id, 0) == 1 and adapter.send_count(intent.output_id, 0) == 1
        with session_factory() as s:
            assert s.scalar(select(func.count()).select_from(DeliveryRow)) == 1
    finally:
        bus.stop()


@pytest.mark.parametrize("point", ["delivery.after_prepare", "delivery.after_claim", "delivery.after_send"])
def test_v19_completion_notification_survives_delivery_worker_crash(session_factory, point):
    """V19／VC-6-1：通知送出途中 worker 崩潰，重啟後同一 intent 補完，外部效果仍 1。"""
    adapter = FakeOutputAdapter(default_mode="transport_only")

    def hook(p):
        if p == point:
            raise SimulatedCrash(p)

    task_id = "task_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    intent = _intent_for(task_id)
    with pytest.raises(SimulatedCrash):
        DeliveryManager(session_factory, adapter, crash_hook=hook).dispatch(intent)
    rec = DeliveryManager(session_factory, adapter, claim_ttl=timedelta(0)).dispatch(intent)
    assert rec.status.value in ("sent", "rendered_played")
    assert adapter.effect_count(intent.output_id, 0) == 1


def test_v19_completed_event_is_journaled_even_when_crash_precedes_bus_dispatch(session_factory):
    """V19／E-1：task.completed 在 Bus 投遞前崩潰，journal 仍有該事件、Task 已 SUCCEEDED（可據以對帳）。"""
    crash, ex = CrashInjector(), FakeToolExecutor()
    tm = make_tm(session_factory, ex, crash=crash)
    task, run, _step, _items = setup_run(tm, 1)
    tm.transition_run(run.run_id, RunStatus.SUCCEEDED, worker_epoch=1)
    arm_next(crash, "events.before_dispatch")
    with pytest.raises(SimulatedCrash):
        tm.transition_task(task.task_id, RunStatus.SUCCEEDED)
    assert make_tm(session_factory, ex).get_task(task.task_id).status == "SUCCEEDED"
    assert event_types(session_factory).count("task.completed") == 1


def test_v19_completion_notification_recovered_via_outbox_after_crash_before_bus(session_factory):
    """V19／VC-1-2、VC-6-1、T-8（預期失敗）：完成後、Bus 投遞前崩潰，重啟由 Outbox 重送必須補送通知恰好一次。"""
    crash, ex = CrashInjector(), FakeToolExecutor()
    tm = make_tm(session_factory, ex, crash=crash)
    task, run, _step, _items = setup_run(tm, 1)
    tm.transition_run(run.run_id, RunStatus.SUCCEEDED, worker_epoch=1)
    arm_next(crash, "events.before_dispatch")
    with pytest.raises(SimulatedCrash):
        tm.transition_task(task.task_id, RunStatus.SUCCEEDED)

    adapter = FakeOutputAdapter(default_mode="transport_only")
    notifier = Notifier(DeliveryManager(session_factory, adapter))
    dispatcher = OutboxDispatcher(session_factory, base_delay=timedelta(0), jitter=0.0)
    dispatcher.register("notification", notifier)  # 重啟後的消費者
    dispatcher.dispatch_once()
    assert adapter.effect_count(_intent_for(task.task_id).output_id, 0) == 1


def test_v19_action_creation_replay_is_idempotent(session_factory):
    """V19／T-2：worker 重啟後重放同一 create_action（同 action_id、同參數）不產生第二筆 action／第二個事件；參數改變則拒絕。"""
    from uep.tasks.manager import ParamsHashMismatch

    ex = FakeToolExecutor()
    tm = make_tm(session_factory, ex)
    _task, run, step, (item,) = setup_run(tm, 1)
    tm2 = make_tm(session_factory, ex)
    for _ in range(3):
        row = tm2.create_action(
            run_id=run.run_id, step_id=step.step_id, tool="files.write", executor="fake", params=item.params,
            side_effect_class="reversible_local", idempotency="via_action_id", worker_epoch=1,
            action_id=item.action_id,
        )
        assert row.action_id == item.action_id
    assert action_row_count(session_factory) == 1
    assert event_types(session_factory).count("action.intended") == 1
    with pytest.raises(ParamsHashMismatch):
        tm2.create_action(
            run_id=run.run_id, step_id=step.step_id, tool="files.write", executor="fake", params={"path": "other"},
            side_effect_class="reversible_local", idempotency="via_action_id", worker_epoch=1,
            action_id=item.action_id,
        )
