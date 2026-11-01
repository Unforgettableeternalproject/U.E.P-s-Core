"""V20 驗收：重送事件、Outbox 重播、worker 重啟不產生重複 action／通知（VC-1-1、VC-1-2、T-2、E-2）。

外部效果以 FakeToolExecutor.execution_count、FakeOutputAdapter.effect_count 衡量（與呼叫次數分開）。
全部使用 fake Adapter；需 PostgreSQL（UEP_TEST_DATABASE_URL）。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from tests.uep.acceptance_support import (
    HookedExecutor,
    action_row_count,
    declare_tool,
    drive,
    event_types,
    make_tm,
    receipt_count,
    setup_run,
    status_of,
)
from tests.uep.factories import make_intent
from uep.events import inbox
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.models import EventRow, InboxRow, OutboxRow
from uep.events.outbox import OutboxDispatcher
from uep.events.publisher import EventPublisher
from uep.fakes.crash import CrashInjector, SimulatedCrash
from uep.fakes.output import FakeOutputAdapter
from uep.fakes.tool import FakeToolExecutor
from uep.output.delivery import DeliveryStatus as S
from uep.output.manager import DeliveryManager
from uep.output.models import DeliveryRow
from uep.tasks.manager import ActionBlocked

pytestmark = pytest.mark.usefixtures("session_factory")

EXTERNAL = TrustedContext(source="adapter.discord", actor_id="guest_1", scope_ref="scope_public", external=True)
INTERNAL = TrustedContext(source="core.task", actor_id="core", scope_ref="scope_private_owner")


def _count(session_factory, model) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(model))


def _dispatcher(session_factory, **kw) -> OutboxDispatcher:
    kw.setdefault("base_delay", timedelta(0))
    kw.setdefault("jitter", 0.0)
    return OutboxDispatcher(session_factory, **kw)


class Consumer:
    """帶 Inbox 的 consumer：calls＝handler 被叫次數（含重播），effects＝實際效果次數。"""

    def __init__(self, session_factory, consumer_id="consumer_a", *, crash_after_effect=False, delay=0.0):
        self._sf, self.consumer_id = session_factory, consumer_id
        self.calls = 0
        self.effects: list[str] = []
        self.crash_after_effect = crash_after_effect
        self.delay = delay
        self._lock = Lock()

    def __call__(self, event) -> None:
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        with self._sf() as s, s.begin():
            inbox.process_once(s, self.consumer_id, event.event_id, lambda: self.effects.append(event.event_id))
        if self.crash_after_effect:  # 效果已提交、ACK 前崩潰
            self.crash_after_effect = False
            raise SimulatedCrash("before_ack")


# ---- 同一 durable 事件重送（VC-1-1） ----

def test_v20_resend_same_idempotency_key_single_event_single_effect(session_factory):
    """V20／VC-1-1：同 idempotency_key 重送 -> journal 1 筆、outbox 1 筆、Bus 只投遞 1 次、consumer 效果 1。"""
    bus, seen = EventBus(), []
    bus.subscribe("input.text_final", seen.append)
    publisher = EventPublisher(session_factory, bus)
    draft = EventDraft(event_type="input.text_final", idempotency_key="disc:msg_991", payload={"len": 3})
    first = publisher.publish(draft, EXTERNAL, outbox_targets=["consumer_a"], sync=True)
    again = [publisher.publish(draft, EXTERNAL, outbox_targets=["consumer_a"], sync=True) for _ in range(3)]
    assert all(r.duplicate and r.event.event_id == first.event.event_id for r in again)
    assert _count(session_factory, EventRow) == 1 and _count(session_factory, OutboxRow) == 1
    assert len(seen) == 1
    consumer = Consumer(session_factory)
    dispatcher = _dispatcher(session_factory)
    dispatcher.register("consumer_a", consumer)
    dispatcher.dispatch_once()
    dispatcher.dispatch_once()
    assert len(consumer.effects) == 1


def test_v20_concurrent_resend_race_yields_one_event_and_one_effect(session_factory):
    """V20／VC-1-1：8 個執行緒同時以同 key 重送 -> 恰 1 個新事件、其餘 duplicate；outbox 1；效果 1。"""
    publisher = EventPublisher(session_factory, EventBus())
    draft = EventDraft(event_type="input.text_final", idempotency_key="disc:msg_race", payload={"len": 3})
    barrier = Barrier(8)

    def send(_):
        barrier.wait()
        return publisher.publish(draft, EXTERNAL, outbox_targets=["consumer_a"], sync=True)

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(send, range(8)))
    assert sum(not r.duplicate for r in results) == 1
    assert len({r.event.event_id for r in results}) == 1
    assert _count(session_factory, EventRow) == 1 and _count(session_factory, OutboxRow) == 1
    consumer = Consumer(session_factory)
    dispatcher = _dispatcher(session_factory)
    dispatcher.register("consumer_a", consumer)
    dispatcher.dispatch_once()
    assert len(consumer.effects) == 1


def test_v20_resend_with_different_payload_does_not_create_second_event(session_factory):
    """V20／VC-1-1：同 key 但內容被改過的重送，仍回原事件（不新增列、不改原內容）。"""
    publisher = EventPublisher(session_factory, EventBus())
    first = publisher.publish(
        EventDraft(event_type="input.text_final", idempotency_key="k", payload={"len": 1}), EXTERNAL
    )
    second = publisher.publish(
        EventDraft(event_type="input.text_final", idempotency_key="k", payload={"len": 999}), EXTERNAL
    )
    assert second.duplicate and second.event.event_id == first.event.event_id
    assert second.event.payload == {"len": 1} and _count(session_factory, EventRow) == 1


# ---- Outbox 崩潰重播（VC-1-2） ----

@pytest.mark.parametrize("replays", [1, 3])
def test_v20_outbox_crash_before_ack_replay_is_blocked_by_inbox(session_factory, replays):
    """V20／VC-1-2：handler 成功（效果已提交）但 ACK 前崩潰（BaseException 直接穿出 dispatcher）；
    重啟重播 replays 次，Inbox 擋住，效果 1，outbox 最終 acked。"""
    publisher = EventPublisher(session_factory, EventBus())
    publisher.publish(EventDraft(event_type="task.completed"), INTERNAL, outbox_targets=["consumer_a"])
    consumer = Consumer(session_factory, crash_after_effect=True)
    d1 = _dispatcher(session_factory)
    d1.register("consumer_a", consumer)
    with pytest.raises(SimulatedCrash):
        d1.dispatch_once()
    with session_factory() as s:
        row = s.execute(select(OutboxRow)).scalar_one()
        assert row.acked_at is None and row.attempts == 0  # 崩潰回滾：尚未 ACK
    for _ in range(replays):  # 重啟後的新 dispatcher
        d2 = _dispatcher(session_factory)
        d2.register("consumer_a", consumer)
        d2.dispatch_once()
    assert len(consumer.effects) == 1 and consumer.calls == 2  # 重播確實發生過，但效果只一次
    with session_factory() as s:
        assert s.execute(select(OutboxRow)).scalar_one().acked_at is not None
        assert s.scalar(select(func.count()).select_from(InboxRow)) == 1


def test_v20_outbox_crash_before_effect_is_not_lost(session_factory):
    """V20／E-2：handler 在效果前崩潰（at-least-once）：重播後效果仍恰 1 次，不會漏。"""
    publisher = EventPublisher(session_factory, EventBus())
    publisher.publish(EventDraft(event_type="task.completed"), INTERNAL, outbox_targets=["consumer_a"])
    crashed = {"n": 0}
    consumer = Consumer(session_factory)

    def flaky(event):
        if crashed["n"] == 0:
            crashed["n"] += 1
            raise SimulatedCrash("before_effect")
        consumer(event)

    d1 = _dispatcher(session_factory)
    d1.register("consumer_a", flaky)
    with pytest.raises(SimulatedCrash):
        d1.dispatch_once()
    d2 = _dispatcher(session_factory)
    d2.register("consumer_a", flaky)
    d2.dispatch_once()
    assert len(consumer.effects) == 1


def test_v20_two_dispatchers_concurrently_each_event_handled_once(session_factory):
    """V20／E-2：兩個 dispatcher 併發處理 20 筆 outbox，handler 呼叫數與效果數皆恰為 20（SKIP LOCKED 互斥）。"""
    publisher = EventPublisher(session_factory, EventBus())
    n = 20
    for _ in range(n):
        publisher.publish(EventDraft(event_type="task.completed"), INTERNAL, outbox_targets=["consumer_a"])
    consumer = Consumer(session_factory, delay=0.01)
    barrier = Barrier(2)

    def run(_):
        d = _dispatcher(session_factory)
        d.register("consumer_a", consumer)
        barrier.wait()
        return d.dispatch_once(limit=100).delivered

    with ThreadPoolExecutor(2) as pool:
        delivered = list(pool.map(run, range(2)))
    assert sum(delivered) == n
    assert consumer.calls == n and len(consumer.effects) == n and len(set(consumer.effects)) == n
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(OutboxRow).where(OutboxRow.acked_at.is_(None))) == 0


def test_v20_inbox_concurrent_same_event_only_one_winner(session_factory):
    """V20／E-2：同 (consumer, event) 併發 process_once，恰 1 個執行效果、其餘回 False。"""
    effects, barrier = [], Barrier(6)

    def run(_):
        barrier.wait()
        with session_factory() as s, s.begin():
            return inbox.process_once(s, "c1", "evt_same", lambda: effects.append(1))

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(run, range(6)))
    assert results.count(True) == 1 and len(effects) == 1


# ---- Outbox／重送驅動 action：外部效果 execution_count ----

def test_v20_event_driven_action_resend_replay_and_two_dispatchers_execute_once(session_factory):
    """V20／T-2、VC-1-2：同一 action 被「重送的兩個不同事件」「Outbox 重播」「兩個 dispatcher 併發」驅動，
    FakeToolExecutor.execution_count 恆為 1；撞到 EXECUTING 的 handler 失敗後以 outbox 重試收斂。"""
    ex = HookedExecutor(on_execute=lambda _c: time.sleep(0.2))
    tm = make_tm(session_factory, ex)
    _task, run, _step, (item,) = setup_run(tm, 1)
    publisher = EventPublisher(session_factory, EventBus())
    for key in ("k1", "k2"):  # 兩個不同事件要求同一 action（例如上游重複產生）
        publisher.publish(
            EventDraft(event_type="action.intended", idempotency_key=key, payload={"action_id": item.action_id}),
            EXTERNAL, outbox_targets=["worker"],
        )

    def handler(event):
        tm.execute_action(event.payload["action_id"], item.params, worker_epoch=run.worker_epoch)

    barrier = Barrier(2)

    def run_dispatcher(_):
        d = _dispatcher(session_factory, max_attempts=50)
        d.register("worker", handler)
        barrier.wait()
        return d.dispatch_once()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(run_dispatcher, range(2)))
    d = _dispatcher(session_factory, max_attempts=50)
    d.register("worker", handler)
    for _ in range(5):  # 撞到 EXECUTING 的那筆被標失敗並退避，之後重試收斂
        d.dispatch_once()
    assert status_of(tm, item) == "SUCCEEDED"
    assert ex.execution_count(item.action_id) == 1 and receipt_count(session_factory, item.action_id) == 1
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(OutboxRow).where(OutboxRow.acked_at.is_(None))) == 0
        assert s.scalar(select(func.count()).select_from(OutboxRow).where(OutboxRow.dead_letter_reason.isnot(None))) == 0


def test_v20_concurrent_execute_action_same_id_single_effect(session_factory):
    """V20／T-2：4 個 worker 同時 execute_action 同一 action_id：執行 1 次，其餘 ActionBlocked 或回既有結果。"""
    ex = HookedExecutor(on_execute=lambda _c: time.sleep(0.2))
    tm = make_tm(session_factory, ex)
    _task, _run, _step, (item,) = setup_run(tm, 1)
    barrier = Barrier(4)

    def attempt(_):
        mgr = make_tm(session_factory, ex)
        barrier.wait()
        try:
            return mgr.execute_action(item.action_id, item.params, worker_epoch=1)
        except ActionBlocked:
            return None

    with ThreadPoolExecutor(4) as pool:
        outcomes = list(pool.map(attempt, range(4)))
    assert ex.execution_count(item.action_id) == 1 and ex.call_count(item.action_id) == 1
    assert sum(1 for o in outcomes if o is not None and o.executed) == 1
    assert status_of(tm, item) == "SUCCEEDED" and receipt_count(session_factory, item.action_id) == 1
    assert event_types(session_factory).count("action.executed") == 1


def test_v20_concurrent_create_action_same_id_creates_one_row(session_factory):
    """V20／T-2：併發以同 action_id 建立 action：只有 1 筆 action、1 個 action.intended。
    輸家回傳既有列（不拋 IntegrityError）。"""
    ex = FakeToolExecutor()
    tm = make_tm(session_factory, ex)
    _task, run, step, _ = setup_run(tm, 0)
    declare_tool(tm, "files.write")
    barrier = Barrier(4)
    params = {"path": "same"}

    def create(_):
        mgr = make_tm(session_factory, ex)
        barrier.wait()
        try:
            return mgr.create_action(
                run_id=run.run_id, step_id=step.step_id, tool="files.write", params=params,
                worker_epoch=1,
                action_id="act_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            ).action_id
        except IntegrityError:  # 不應再發生：輸家回傳既有列
            return "lost_race"

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(create, range(4)))
    assert action_row_count(session_factory) == 1
    assert event_types(session_factory).count("action.intended") == 1
    assert results == ["act_01ARZ3NDEKTSV4RRFFQ69G5FAV"] * 4  # 輸家也回傳同一個 action


def test_v20_create_action_second_layer_integrity_error_still_dedupes(session_factory, monkeypatch):
    """V20／T-2：第一層「先查既有列」失效（模擬過期讀取）時，唯一約束的 IntegrityError 第二層仍保證去重。

    以 monkeypatch 讓 create_action 的 ``with_for_update`` 查詢回 None（第二層用的是不帶鎖的 get，不受影響），
    重放同 action_id：必須回傳既有列、不多一筆 action、不多一個事件；參數改變仍拒絕。
    """
    from sqlalchemy.orm import Session

    from uep.tasks.manager import ParamsHashMismatch
    from uep.tasks.models import ActionIntentRow

    ex = FakeToolExecutor()
    tm = make_tm(session_factory, ex)
    _task, run, step, (item,) = setup_run(tm, 1)
    first_layer_misses: list[str] = []
    original_get = Session.get

    def blind_first_layer(self, entity, ident, **kw):
        if entity is ActionIntentRow and kw.get("with_for_update"):
            first_layer_misses.append(str(ident))
            return None  # 第一層看不到既有列
        return original_get(self, entity, ident, **kw)

    monkeypatch.setattr(Session, "get", blind_first_layer)
    kwargs = {
        "run_id": run.run_id, "step_id": step.step_id, "tool": "files.write",
        "worker_epoch": 1, "action_id": item.action_id,
    }
    for _ in range(3):
        row = make_tm(session_factory, ex).create_action(params=item.params, **kwargs)
        assert row.action_id == item.action_id
    assert len(first_layer_misses) == 3  # 確實走了「第一層沒擋到、由第二層接手」的路徑
    with pytest.raises(ParamsHashMismatch):  # 第二層也會比對參數：不是任何同 id 都放行
        make_tm(session_factory, ex).create_action(params={"path": "other"}, **kwargs)
    monkeypatch.undo()
    assert action_row_count(session_factory) == 1
    assert event_types(session_factory).count("action.intended") == 1


def test_v20_repeated_worker_restarts_never_redo_effects(session_factory):
    """V20／T-2：連續重啟 5 次（每次新 manager、新 epoch、全部再 drive 一遍），每個 action 的執行數恆為 1。"""
    crash, ex = CrashInjector(), FakeToolExecutor()
    tm = make_tm(session_factory, ex, crash=crash)
    _task, run, _step, items = setup_run(tm, 3)
    tm.execute_action(items[0].action_id, items[0].params, worker_epoch=1)
    crash.arm("action.before_receipt_commit")
    with pytest.raises(SimulatedCrash):
        tm.execute_action(items[1].action_id, items[1].params, worker_epoch=1)
    for expected_epoch in range(2, 7):
        mgr = make_tm(session_factory, ex)
        plan = mgr.resume_run(run.run_id, stale_after=timedelta(0))
        assert plan.run.worker_epoch == expected_epoch
        drive(mgr, items, expected_epoch)
        assert [ex.execution_count(i.action_id) for i in items] == [1, 1, 1]
    assert [receipt_count(session_factory, i.action_id) for i in items] == [1, 1, 1]


# ---- worker 在 delivery 各 crash 點重啟（輸出效果） ----

def _crash_at(point):
    def hook(p):
        if p == point:
            raise SimulatedCrash(p)

    return hook


@pytest.mark.parametrize("mode", ["played", "transport_only", "lose_ack"])
@pytest.mark.parametrize("idempotent", [False, True])
@pytest.mark.parametrize("point", ["delivery.after_prepare", "delivery.after_claim", "delivery.after_send"])
def test_v20_delivery_worker_restart_at_each_crash_point_single_effect(session_factory, point, idempotent, mode):
    """V20／VC-5-3、O-1：delivery 各崩潰點重啟（舊 claim 過期後接手）-> 外部效果恰 1（adapter 不靠冪等也成立）；
    只有 1 筆 delivery。"""
    adapter = FakeOutputAdapter(default_mode=mode, idempotent_by_dispatch=idempotent)
    intent = make_intent()
    with pytest.raises(SimulatedCrash):
        DeliveryManager(session_factory, adapter, crash_hook=_crash_at(point)).dispatch(intent)
    rec = DeliveryManager(session_factory, adapter, claim_ttl=timedelta(0)).dispatch(intent)
    if rec.status is S.UNKNOWN:  # lose_ack 路徑可能停在 unknown：以查核收斂
        rec = DeliveryManager(session_factory, adapter).resolve_unknown(rec.delivery_id)
    assert rec.status in (S.SENT, S.ACCEPTED, S.RENDERED_PLAYED)
    assert adapter.effect_count(intent.output_id, 0) == 1
    assert _count(session_factory, DeliveryRow) == 1


def test_v20_delivery_repeated_crashes_then_recovery_single_effect(session_factory):
    """V20／O-1：連續在 after_claim、after_send 崩潰後第三次重啟才完成：外部效果仍 1。"""
    adapter = FakeOutputAdapter(default_mode="transport_only")
    intent = make_intent()
    with pytest.raises(SimulatedCrash):
        DeliveryManager(session_factory, adapter, crash_hook=_crash_at("delivery.after_claim")).dispatch(intent)
    with pytest.raises(SimulatedCrash):
        DeliveryManager(
            session_factory, adapter, claim_ttl=timedelta(0), crash_hook=_crash_at("delivery.after_send")
        ).dispatch(intent)
    assert adapter.effect_count(intent.output_id, 0) == 1  # 第二次崩潰前已真的送出
    rec = DeliveryManager(session_factory, adapter, claim_ttl=timedelta(0)).dispatch(intent)
    assert rec.status is S.SENT and adapter.effect_count(intent.output_id, 0) == 1


def test_v20_delivery_reclaim_with_non_authoritative_not_sent_does_not_resend(session_factory):
    """V20／O-6：after_send 崩潰後接手，非權威 adapter 說「沒送」不可採信 -> unknown，不重送。"""
    adapter = FakeOutputAdapter(default_mode="transport_only")
    intent = make_intent()
    with pytest.raises(SimulatedCrash):
        DeliveryManager(session_factory, adapter, crash_hook=_crash_at("delivery.after_send")).dispatch(intent)
    adapter.lookup_authoritative = False
    adapter.lookup_outcome(adapter.sends[0][1], "confirmed_not_sent")
    rec = DeliveryManager(session_factory, adapter, claim_ttl=timedelta(0)).dispatch(intent)
    assert rec.status is S.UNKNOWN and adapter.effect_count(intent.output_id, 0) == 1
    assert adapter.send_count(intent.output_id, 0) == 1


@pytest.mark.parametrize("idempotent,expected_effects", [(False, 2), (True, 1)])
def test_v20_delivery_authoritative_false_negative_after_crash_duplicate_boundary(
    session_factory, idempotent, expected_effects
):
    """V20／O-6 風險邊界（量化）：after_send 崩潰後，權威 adapter 的 lookup 誤報 confirmed_not_sent，
    Core 會重送：非冪等 adapter 重複 1 次（效果 2）；idempotent_by_dispatch adapter 仍 1。
    契約信任權威宣告，故此為已知邊界而非實作缺陷；此測試鎖定目前行為。"""
    adapter = FakeOutputAdapter(default_mode="transport_only", idempotent_by_dispatch=idempotent)
    intent = make_intent()
    with pytest.raises(SimulatedCrash):
        DeliveryManager(session_factory, adapter, crash_hook=_crash_at("delivery.after_send")).dispatch(intent)
    adapter.lookup_outcome(adapter.sends[0][1], "confirmed_not_sent")  # 誤報
    DeliveryManager(session_factory, adapter, claim_ttl=timedelta(0)).dispatch(intent)
    assert adapter.effect_count(intent.output_id, 0) == expected_effects
