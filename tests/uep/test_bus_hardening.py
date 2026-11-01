"""Event Bus 契約 §8.4 補強（T-23）：佇列上限與丟棄統計、stop() 排空、get_stats；不需要資料庫。"""
import logging
import threading

import pytest

from tests.uep.test_envelope_and_bus import _event
from uep.events.bus import EventBus


def test_full_queue_drops_with_backpressure_stats_and_log(caplog):
    bus = EventBus(max_queue=2, put_timeout=0.01)  # 未 start：沒有消費者，佇列會滿
    events = [_event(idempotency_key=f"k{i}") for i in range(4)]
    with caplog.at_level(logging.ERROR, logger="uep.events.bus"):
        for event in events:
            bus.publish(event)
    stats = bus.get_stats()
    assert stats["published"] == 4 and stats["dropped"] == 2
    assert stats["dropped_by_type"] == {"input.text_final": 2}
    assert stats["queue_size"] == 2 and stats["max_queue"] == 2
    # 丟棄不得靜默：log 帶 event_id，供 outbox／journal 補送
    assert all(events[i].event_id in caplog.text for i in (2, 3))


def test_publish_blocks_then_succeeds_when_consumer_drains():
    bus = EventBus(max_queue=1, put_timeout=5.0)
    gate, seen = threading.Event(), []

    def handler(event):
        gate.wait(timeout=5)
        seen.append(event.event_id)

    bus.subscribe("input.text_final", handler)
    bus.start()
    first, second, third = (_event(idempotency_key=k) for k in "abc")
    try:
        bus.publish(first)  # 被消費者取走並卡在 handler
        while bus.get_stats()["queue_size"]:
            threading.Event().wait(0.01)
        bus.publish(second)  # 佇列剩這一個
        threading.Timer(0.1, gate.set).start()  # 稍後放行 -> 第三個的背壓等待得以成功
        bus.publish(third)
    finally:
        gate.set()
        bus.stop()
    assert seen == [first.event_id, second.event_id, third.event_id]
    assert bus.get_stats()["dropped"] == 0


def test_handler_publish_on_consumer_thread_never_blocks_when_full():
    bus = EventBus(max_queue=1, put_timeout=30.0)  # 若在消費者線程等待會卡 30 秒
    done = threading.Event()

    def handler(event):
        bus.publish(_event(idempotency_key="x1"))  # 填滿佇列
        bus.publish(_event(idempotency_key="x2"))  # 已滿 -> 立即丟棄而不是自我死結
        done.set()

    bus.subscribe("input.text_final", handler)
    bus.start()
    try:
        bus.publish(_event(idempotency_key="root"))
        assert done.wait(timeout=5)
    finally:
        bus.stop()
    assert bus.get_stats()["dropped"] >= 1


def test_stop_drains_queue_before_exit():
    bus, seen = EventBus(), []
    bus.subscribe("input.text_final", lambda e: seen.append(e.idempotency_key))
    for i in range(50):
        bus.publish(_event(idempotency_key=f"k{i}"))
    bus.start()
    bus.stop()  # 排空後才回來
    assert seen == [f"k{i}" for i in range(50)]
    stats = bus.get_stats()
    assert stats["dispatched"] == 50 and stats["undelivered_on_stop"] == 0 and stats["queue_size"] == 0


def test_stop_drain_timeout_records_unprocessed(caplog):
    bus = EventBus(drain_timeout=0.1)
    release = threading.Event()
    started = threading.Event()

    def slow(event):
        started.set()
        release.wait(timeout=5)

    bus.subscribe("input.text_final", slow)
    bus.start()
    for i in range(5):
        bus.publish(_event(idempotency_key=f"k{i}"))
    assert started.wait(timeout=5)
    with caplog.at_level(logging.ERROR, logger="uep.events.bus"):
        threading.Timer(0.5, release.set).start()
        bus.stop()
    assert bus.get_stats()["undelivered_on_stop"] == 4  # 一個執行中，其餘四個沒處理
    assert "排空逾時" in caplog.text


def test_stats_count_dispatch_and_handler_errors():
    bus = EventBus()

    def boom(event):
        raise RuntimeError("x")

    bus.subscribe("input.text_final", boom)
    bus.publish(_event(idempotency_key="a"), sync=True)
    bus.publish(_event(idempotency_key="b"), sync=True)
    stats = bus.get_stats()
    assert (stats["published"], stats["dispatched"], stats["handler_errors"]) == (2, 2, 2)


def test_stats_updated_consistently_under_threads():
    bus = EventBus(max_queue=100_000)
    bus.subscribe("input.text_final", lambda e: None)
    event = _event()

    def worker():
        for _ in range(500):
            bus.publish(event, sync=True)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stats = bus.get_stats()
    assert stats["published"] == 4000 and stats["dispatched"] == 4000


def test_max_queue_must_be_positive():
    with pytest.raises(ValueError):
        EventBus(max_queue=0)


def test_publish_after_stop_is_dropped_immediately_without_waiting(caplog):
    bus = EventBus(put_timeout=30.0)  # 若等待 put_timeout 會卡 30 秒
    bus.start()
    bus.stop()
    with caplog.at_level(logging.ERROR, logger="uep.events.bus"):
        bus.publish(_event(idempotency_key="late"))
    stats = bus.get_stats()
    assert stats["dropped"] == 1 and stats["queue_size"] == 0
    assert "已停止" in caplog.text and "JournalTailer" in caplog.text
    bus.start()  # 重新 start 後恢復接收
    bus.publish(_event(idempotency_key="again"))
    bus.stop()
    assert bus.get_stats()["dispatched"] == 1
