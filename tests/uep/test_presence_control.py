"""獨立控制通道（P-8、E-12、VC-4-5）：不經 outbox、不等 Bus queue；journal 補記不阻塞送達。

通道本身的測試不需 DB；journal 與積壓測試需 PostgreSQL（未設 UEP_TEST_DATABASE_URL 時 skip）。
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from tests.uep.presence_support import count_events, count_outbox, make_rig
from uep.control.channel import InProcessControlChannel
from uep.control.journal import ControlJournal
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext, build_event
from uep.events.models import EventRow
from uep.events.publisher import EventPublisher, UnsupportedDeliveryClass
from uep.ids import new_id
from uep.output.intent import ControlKind, ControlScope, ControlSignal

WAIT = 2.0


def make_signal(kind: ControlKind = ControlKind.STOP, **kw) -> ControlSignal:
    data = {
        "control_id": new_id("ctl"), "kind": kind, "scope": ControlScope.PROCESS,
        "target_fence": {"core_epoch": 1, "presence_generation": 1}, "issued_at": datetime.now(UTC),
    }
    data.update(kw)
    return ControlSignal(**data)


class Collector:
    def __init__(self) -> None:
        self.q: queue.Queue[ControlSignal] = queue.Queue()

    def __call__(self, signal: ControlSignal) -> None:
        self.q.put(signal)

    def get(self, timeout: float = WAIT) -> ControlSignal:
        return self.q.get(timeout=timeout)


# ---- 通道行為（不需 DB）----

def test_send_reaches_registered_endpoint():
    channel, got = InProcessControlChannel(), Collector()
    channel.subscribe("ep_desktop", got)
    signal = make_signal(ControlKind.REVOKE)
    result = channel.send(signal, endpoint_id="ep_desktop")
    assert result.delivered_to == ("ep_desktop",) and not result.duplicate
    assert got.get() == signal


def test_unregistered_endpoint_is_not_an_error_but_not_delivered():
    channel = InProcessControlChannel()
    result = channel.send(make_signal(), endpoint_id="ep_missing")
    assert result.delivered_to == () and not result.duplicate


def test_broadcast_reaches_all_endpoints_only_once_per_control_id():
    channel, a, b = InProcessControlChannel(), Collector(), Collector()
    channel.subscribe("ep_a", a)
    channel.subscribe("ep_b", b)
    signal = make_signal()
    assert set(channel.send(signal).delivered_to) == {"ep_a", "ep_b"}
    assert a.get() == signal and b.get() == signal
    again = channel.send(signal)
    assert again.duplicate
    with pytest.raises(queue.Empty):
        a.get(timeout=0.2)


def test_resubscribe_replaces_old_connection():
    channel, old, new = InProcessControlChannel(), Collector(), Collector()
    channel.subscribe("ep_desktop", old)
    channel.subscribe("ep_desktop", new)
    channel.send(make_signal(), endpoint_id="ep_desktop")
    new.get()
    with pytest.raises(queue.Empty):
        old.get(timeout=0.2)


def test_closed_subscription_stops_receiving():
    channel, got = InProcessControlChannel(), Collector()
    sub = channel.subscribe("ep_desktop", got)
    sub.close()
    assert channel.send(make_signal(), endpoint_id="ep_desktop").delivered_to == ()


def test_blocked_handler_does_not_block_sender_or_other_endpoints():
    channel, other = InProcessControlChannel(), Collector()
    release = threading.Event()
    entered = threading.Event()

    def stuck(_signal: ControlSignal) -> None:
        entered.set()
        release.wait(10)

    channel.subscribe("ep_stuck", stuck)
    channel.subscribe("ep_ok", other)
    start = time.monotonic()
    channel.send(make_signal(), endpoint_id="ep_stuck")
    assert entered.wait(WAIT)
    for _ in range(5):  # 卡住的端點後面再排訊號，送出者仍不被擋
        channel.send(make_signal(), endpoint_id="ep_stuck")
    signal = make_signal(ControlKind.ABORT)
    channel.send(signal, endpoint_id="ep_ok")
    assert other.get() == signal
    assert time.monotonic() - start < WAIT
    release.set()


def test_handler_exception_is_isolated():
    channel, got = InProcessControlChannel(), Collector()
    calls = []

    def flaky(signal: ControlSignal) -> None:
        calls.append(signal)
        if len(calls) == 1:
            raise RuntimeError("boom")
        got(signal)

    channel.subscribe("ep_desktop", flaky)
    channel.send(make_signal(), endpoint_id="ep_desktop")
    second = make_signal()
    channel.send(second, endpoint_id="ep_desktop")
    assert got.get() == second


def test_vc_4_5_revoke_unblocks_writer_when_audio_queue_is_full():
    """VC-4-5：音訊 queue 已滿、writer 阻塞時，control.revoke 不經音訊通道，Sink 立即停止、writer 解鎖。"""
    audio: queue.Queue[bytes] = queue.Queue(maxsize=1)
    audio.put(b"chunk")  # queue 已滿
    stopped = threading.Event()
    writer_done = threading.Event()

    def writer() -> None:
        while not stopped.is_set():
            try:
                audio.put(b"chunk", timeout=0.05)
            except queue.Full:
                continue
        writer_done.set()

    def on_control(signal: ControlSignal) -> None:
        if signal.kind is ControlKind.REVOKE:
            stopped.set()  # Sink 立即停止，不 drain 舊佇列

    threading.Thread(target=writer, daemon=True).start()
    channel = InProcessControlChannel()
    channel.subscribe("ep_desktop", on_control)
    time.sleep(0.1)
    assert not writer_done.is_set() and audio.full()
    channel.send(make_signal(ControlKind.REVOKE), endpoint_id="ep_desktop")
    assert writer_done.wait(WAIT) and audio.full()  # 舊資料仍在佇列，但沒有人等它播完


# ---- 與 journal／outbox／Bus 的關係（需 DB）----

def test_control_events_are_not_publishable_through_outbox_path(session_factory):
    publisher = EventPublisher(session_factory, EventBus())
    draft = EventDraft(event_type="control.stop", payload={"kind": "stop"})
    trusted = TrustedContext(source="core.control", actor_id="uep", scope_ref="scope_public")
    with pytest.raises(UnsupportedDeliveryClass):
        publisher.publish(draft, trusted)


def test_control_delivered_immediately_while_bus_and_outbox_are_backed_up(session_factory):
    """outbox 大量積壓、Bus handler 卡住時，control 仍立即送達（不經 outbox、不排 Bus queue）。"""
    bus = EventBus()
    blocker, entered = threading.Event(), threading.Event()

    def stuck(_event) -> None:
        entered.set()
        blocker.wait(30)

    bus.subscribe("task.created", stuck)
    bus.start()
    publisher = EventPublisher(session_factory, bus)
    trusted = TrustedContext(source="core.test", actor_id="uep", scope_ref="scope_public")
    try:
        for _ in range(300):  # outbox 積壓：沒有 dispatcher，列一直是待投遞
            publisher.publish(EventDraft(event_type="task.created"), trusted, outbox_targets=("slow",))
        assert entered.wait(WAIT)
        for _ in range(5000):  # Bus queue 積壓
            bus.publish(build_event(EventDraft(event_type="task.created"), trusted))
        assert bus._queue.qsize() > 1000 and count_outbox(session_factory) == 300

        journal = ControlJournal(session_factory)
        channel, got = InProcessControlChannel(journal), Collector()
        channel.subscribe("ep_desktop", got)
        signal = make_signal(ControlKind.STOP, scope=ControlScope.TURN,
                             target_fence={"core_epoch": 1, "turn_generation": 3})
        start = time.monotonic()
        channel.send(signal, endpoint_id="ep_desktop")
        assert got.get(timeout=1.0) == signal
        assert time.monotonic() - start < 0.5
        assert bus._queue.qsize() > 1000  # 送達時 Bus 仍積壓，證明沒有排在它後面
        assert journal.flush(WAIT)
        assert count_outbox(session_factory) == 300  # control 沒有寫 outbox
        journal.close()
    finally:
        blocker.set()
        bus.stop()


def test_control_is_journaled_after_delivery_and_idempotent(session_factory):
    journal = ControlJournal(session_factory)
    channel, got = InProcessControlChannel(journal), Collector()
    channel.subscribe("ep_desktop", got)
    signal = make_signal(ControlKind.REVOKE, target_fence={"core_epoch": 2, "presence_generation": 7})
    channel.send(signal, endpoint_id="ep_desktop")
    channel.send(signal, endpoint_id="ep_desktop")  # 重送：不重複記
    assert got.get() == signal and journal.flush(WAIT)
    assert count_events(session_factory, "control.revoke") == 1
    with session_factory() as s:
        row = s.scalars(select(EventRow).where(EventRow.event_type == "control.revoke")).one()
    assert row.delivery_class == "control" and row.idempotency_key == signal.control_id
    assert row.fence == {"core_epoch": 2, "presence_generation": 7}
    assert row.payload["kind"] == "revoke" and row.payload["scope"] == "process"
    assert count_outbox(session_factory) == 0
    journal.close()


def test_slow_or_failing_journal_never_blocks_delivery(session_factory):
    journal = ControlJournal(session_factory)
    gate, entered = threading.Event(), threading.Event()
    original = journal._write

    def slow_write(job):
        entered.set()
        gate.wait(10)
        return original(job)

    journal._write = slow_write  # type: ignore[method-assign]
    channel, got = InProcessControlChannel(journal), Collector()
    channel.subscribe("ep_desktop", got)
    first, second = make_signal(), make_signal()
    channel.send(first, endpoint_id="ep_desktop")
    assert entered.wait(WAIT)  # journal 工作執行緒卡住
    channel.send(second, endpoint_id="ep_desktop")
    assert got.get() == first and got.get() == second  # 送達不受影響
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(EventRow)
                        .where(EventRow.event_type == "control.stop")) == 0  # 尚未補記
    gate.set()
    assert journal.flush(WAIT) and count_events(session_factory, "control.stop") == 2

    def failing_write(job):
        raise RuntimeError("db down")

    journal._write = failing_write  # type: ignore[method-assign]
    third = make_signal()
    channel.send(third, endpoint_id="ep_desktop")
    assert got.get() == third and journal.flush(WAIT)
    assert journal.failed == 1
    journal.close()


def test_revoke_through_presence_manager_sends_control_to_old_endpoint(session_factory, tmp_path):
    channel, got = InProcessControlChannel(), Collector()
    channel.subscribe("ep_desktop", got)
    rig = make_rig(session_factory, tmp_path, control=channel)
    grant = rig.manager.acquire("ep_desktop").grant
    assert grant is not None
    rig.manager.confirm_activated(grant.lease_id)
    outcome = rig.manager.revoke("global_stop")
    signal = got.get()
    assert signal.kind is ControlKind.REVOKE and signal.target_fence["presence_generation"] == outcome.generation
    assert signal.target_fence["core_epoch"] == grant.authorization.core_epoch


# ---- 審查補強：優先 lane、重連競態、journal 佇列上限 ----

def test_stuck_stop_handler_does_not_block_revoke_or_abort():
    """H3：同一端點 stop 的 handler 卡住時，revoke／abort 仍立即送達。"""
    channel = InProcessControlChannel()
    release, entered = threading.Event(), threading.Event()
    seen: queue.Queue[ControlSignal] = queue.Queue()

    def handler(signal: ControlSignal) -> None:
        if signal.kind is ControlKind.STOP:
            entered.set()
            release.wait(10)  # 違規的阻塞 handler
        else:
            seen.put(signal)

    channel.subscribe("ep_desktop", handler)
    channel.send(make_signal(ControlKind.STOP), endpoint_id="ep_desktop")
    assert entered.wait(WAIT)
    channel.send(make_signal(ControlKind.STOP), endpoint_id="ep_desktop")  # 後續 stop 排在卡住的 stop 後面
    revoke, abort = make_signal(ControlKind.REVOKE), make_signal(ControlKind.ABORT)
    start = time.monotonic()
    channel.send(revoke, endpoint_id="ep_desktop")
    channel.send(abort, endpoint_id="ep_desktop")
    assert {seen.get(timeout=1.0).control_id, seen.get(timeout=1.0).control_id} == {
        revoke.control_id, abort.control_id,
    }
    assert time.monotonic() - start < 0.5
    release.set()


def test_revoke_reaches_new_connection_when_subscription_replaced_mid_send():
    """M1：send 解析到的信箱在放入前被取代（重連競態），重新解析並送到新連線。"""
    channel, old_got, new_got = InProcessControlChannel(), Collector(), Collector()
    channel.subscribe("ep_desktop", old_got)
    old_box = channel._mailboxes["ep_desktop"]
    real_put = old_box.put

    def racing_put(signal):
        channel.subscribe("ep_desktop", new_got)  # 在 send 放入之前，連線被取代
        return real_put(signal)

    old_box.put = racing_put  # type: ignore[method-assign]
    signal = make_signal(ControlKind.REVOKE)
    result = channel.send(signal, endpoint_id="ep_desktop")
    assert result.delivered_to == ("ep_desktop",) and not result.duplicate
    assert new_got.get() == signal
    with pytest.raises(queue.Empty):
        old_got.get(timeout=0.2)


def test_send_does_not_claim_delivery_when_mailbox_closed_and_no_replacement():
    channel = InProcessControlChannel()
    sub = channel.subscribe("ep_desktop", Collector())
    box = channel._mailboxes["ep_desktop"]
    sub.close()
    result = channel._deliver("ep_desktop", box, make_signal())
    assert result.value == "closed"
    assert channel.send(make_signal(), endpoint_id="ep_desktop").delivered_to == ()


def test_duplicate_flag_only_when_every_target_already_had_it():
    channel, a, b = InProcessControlChannel(), Collector(), Collector()
    channel.subscribe("ep_a", a)
    signal = make_signal()
    channel.send(signal, endpoint_id="ep_a")
    channel.subscribe("ep_b", b)  # 新訂閱者尚未收過
    mixed = channel.send(signal)
    assert set(mixed.delivered_to) == {"ep_a", "ep_b"} and not mixed.duplicate
    assert channel.send(signal).duplicate


def test_journal_queue_is_bounded_and_drops_without_blocking(session_factory):
    journal = ControlJournal(session_factory, max_pending=2)
    gate, entered = threading.Event(), threading.Event()
    original = journal._write

    def slow_write(job):
        entered.set()
        gate.wait(10)
        return original(job)

    journal._write = slow_write  # type: ignore[method-assign]
    channel, got = InProcessControlChannel(journal), Collector()
    channel.subscribe("ep_desktop", got)
    channel.send(make_signal(), endpoint_id="ep_desktop")
    assert entered.wait(WAIT)  # 工作執行緒卡住，佇列容量 2
    start = time.monotonic()
    for _ in range(6):
        channel.send(make_signal(), endpoint_id="ep_desktop")
    assert time.monotonic() - start < 0.5  # 佇列滿也不阻塞送達
    for _ in range(7):
        got.get()
    assert journal.dropped == 4
    gate.set()
    assert journal.flush(WAIT)
    assert count_events(session_factory, "control.stop") == 3
    journal.close()
