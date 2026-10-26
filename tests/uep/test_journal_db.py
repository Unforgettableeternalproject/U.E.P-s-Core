"""CC-1 journal／Inbox／Outbox 整合測試（需 PostgreSQL；未設 UEP_TEST_DATABASE_URL 時整檔 skip）。

涵蓋 VC-1-1（外部重送去重）、VC-1-2（Outbox 崩潰重送）、E-1、E-2、E-11。
"""
from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from uep.events import inbox, journal
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.models import EventRow, InboxRow, OutboxRow, PayloadBlob
from uep.events.outbox import DispatchStats, OutboxDispatcher
from uep.events.publisher import EventPublisher, UnsupportedDeliveryClass

pytestmark = pytest.mark.usefixtures("session_factory")

EXTERNAL = TrustedContext(source="adapter.discord", actor_id="guest_1", scope_ref="scope_public", external=True)
INTERNAL = TrustedContext(source="core.task", actor_id="core", scope_ref="scope_private_owner")


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def publisher(session_factory, bus):
    return EventPublisher(session_factory, bus)


def _count(session_factory, model):
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(model))


# ---- migration ----

def test_migration_downgrade_and_upgrade(alembic_cfg, engine):
    cfg = alembic_cfg
    try:
        command.downgrade(cfg, "base")
        assert not {"events", "outbox", "inbox", "payload_blobs"} & set(inspect(engine).get_table_names())
    finally:
        command.upgrade(cfg, "head")
    assert {"events", "outbox", "inbox", "payload_blobs"} <= set(inspect(engine).get_table_names())


# ---- E-1：journal 先於投遞 ----

def test_journal_written_before_bus_delivery(session_factory, bus, publisher):
    seen = {}

    def handler(event):
        # 以另一個 session 讀：能看到代表 journal 已提交
        with session_factory() as s:
            row = s.get(EventRow, event.event_id)
            seen["row"] = row is not None and row.journal_seq is not None

    bus.subscribe("turn.started", handler)
    recorded = publisher.publish(EventDraft(event_type="turn.started"), INTERNAL, sync=True)
    assert seen["row"] is True
    assert recorded.event.journal_seq is not None and recorded.event.received_at is not None


def test_journal_failure_means_no_delivery_and_no_row(session_factory, bus, publisher):
    delivered = []
    bus.subscribe("task.created", delivered.append)
    first = EventDraft(event_type="task.created", aggregate_type="task", aggregate_id="task_a", aggregate_version=1)
    publisher.publish(first, INTERNAL, sync=True)
    assert len(delivered) == 1

    clash = EventDraft(event_type="task.created", aggregate_type="task", aggregate_id="task_a", aggregate_version=1)
    with pytest.raises(IntegrityError):
        publisher.publish(clash, INTERNAL, sync=True)
    assert len(delivered) == 1  # 落地失敗不投遞
    assert _count(session_factory, EventRow) == 1


def test_rollback_leaves_no_journal_or_outbox(session_factory, publisher):
    with session_factory() as s:
        publisher.record(s, EventDraft(event_type="turn.started"), INTERNAL, outbox_targets=["a"])
        s.rollback()
    assert _count(session_factory, EventRow) == 0
    assert _count(session_factory, OutboxRow) == 0


def test_journal_and_outbox_share_transaction(session_factory, publisher):
    with session_factory() as s, s.begin():
        publisher.record(s, EventDraft(event_type="task.completed"), INTERNAL, outbox_targets=["a", "b"])
    assert _count(session_factory, EventRow) == 1
    assert _count(session_factory, OutboxRow) == 2


def test_journal_seq_monotonic(session_factory, publisher):
    seqs = [publisher.publish(EventDraft(event_type="turn.started"), INTERNAL).event.journal_seq for _ in range(5)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 5


def test_control_and_telemetry_rejected_on_journal_path(publisher):
    with pytest.raises(UnsupportedDeliveryClass):
        publisher.publish(EventDraft(event_type="control.stop"), INTERNAL)


# ---- VC-1-1 ----

def test_vc_1_1_external_resend_is_deduplicated(session_factory, bus, publisher):
    turns = []
    bus.subscribe("input.text_final", lambda e: turns.append(e.event_id))
    draft = EventDraft(event_type="input.text_final", idempotency_key="disc:msg_991")

    first = publisher.publish(draft, EXTERNAL, sync=True)
    second = publisher.publish(draft, EXTERNAL, sync=True)

    assert first.duplicate is False and second.duplicate is True
    assert second.event.event_id == first.event.event_id
    assert _count(session_factory, EventRow) == 1
    assert turns == [first.event.event_id]  # 下游效果只發生一次


def test_idempotency_key_is_scoped_per_source(session_factory, publisher):
    draft = EventDraft(event_type="input.text_final", idempotency_key="same")
    other = EXTERNAL.model_copy(update={"source": "adapter.web"})
    publisher.publish(draft, EXTERNAL)
    publisher.publish(draft, other)
    assert _count(session_factory, EventRow) == 2


# ---- VC-1-3（落地部分）----

def test_vc_1_3_journal_stores_trusted_values(session_factory, publisher):
    draft = EventDraft(event_type="input.text_final", idempotency_key="k", payload={"priority": "urgent", "actor_id": "owner"})
    event = publisher.publish(draft, EXTERNAL).event
    with session_factory() as s:
        row = s.get(EventRow, event.event_id)
        assert (row.priority, row.actor_id, row.source) == ("normal", "guest_1", "adapter.discord")
        assert row.payload == {"priority": "urgent", "actor_id": "owner"}


# ---- Inbox ----

def test_inbox_dedups_by_consumer_and_event(session_factory):
    effects = []
    with session_factory() as s, s.begin():
        assert inbox.process_once(s, "c1", "evt_x", lambda: effects.append(1)) is True
        assert inbox.process_once(s, "c1", "evt_x", lambda: effects.append(2)) is False
        assert inbox.process_once(s, "c2", "evt_x", lambda: effects.append(3)) is True  # 不同 consumer 各自處理
    assert effects == [1, 3]
    assert _count(session_factory, InboxRow) == 2


def test_inbox_dedup_across_sessions(session_factory):
    effects = []
    for _ in range(3):
        with session_factory() as s, s.begin():
            inbox.process_once(s, "c1", "evt_y", lambda: effects.append(1))
    assert effects == [1]


def test_inbox_effect_failure_does_not_mark_done(session_factory):
    def boom():
        raise RuntimeError("effect failed")

    with pytest.raises(RuntimeError), session_factory() as s, s.begin():
        inbox.process_once(s, "c1", "evt_z", boom)
    assert _count(session_factory, InboxRow) == 0
    with session_factory() as s, s.begin():
        assert inbox.process_once(s, "c1", "evt_z", lambda: {"ok": True}) is True
    with session_factory() as s:
        assert s.get(InboxRow, ("c1", "evt_z")).result == {"ok": True}


# ---- Outbox / VC-1-2 ----

def _dispatcher(session_factory, **kw):
    kw.setdefault("base_delay", timedelta(0))
    kw.setdefault("jitter", 0.0)
    return OutboxDispatcher(session_factory, **kw)


def test_vc_1_2_outbox_resend_after_crash_is_deduplicated(session_factory, publisher):
    publisher.publish(EventDraft(event_type="task.completed"), INTERNAL, outbox_targets=["consumer_a"])
    effects, crash = [], {"armed": True}

    def consumer_a(event):
        with session_factory() as s, s.begin():
            inbox.process_once(s, "consumer_a", event.event_id, lambda: effects.append(event.event_id))
        if crash["armed"]:  # 效果已提交，但 ACK 前崩潰
            crash["armed"] = False
            raise RuntimeError("crash before ack")

    dispatcher = _dispatcher(session_factory)
    dispatcher.register("consumer_a", consumer_a)

    first = dispatcher.dispatch_once()
    assert (first.delivered, first.failed) == (0, 1)
    second = dispatcher.dispatch_once()
    assert second.delivered == 1

    assert len(effects) == 1  # 去重：效果只一次
    with session_factory() as s:
        row = s.execute(select(OutboxRow)).scalar_one()
        assert row.acked_at is not None and row.attempts == 2 and row.last_error is None


def test_outbox_acked_row_not_redelivered(session_factory, publisher):
    publisher.publish(EventDraft(event_type="task.completed"), INTERNAL, outbox_targets=["t"])
    calls = []
    dispatcher = _dispatcher(session_factory)
    dispatcher.register("t", calls.append)
    dispatcher.dispatch_once()
    dispatcher.dispatch_once()
    assert len(calls) == 1


def test_outbox_retry_respects_retry_at(session_factory, publisher):
    publisher.publish(EventDraft(event_type="task.completed"), INTERNAL, outbox_targets=["t"])
    calls = []

    def fail(event):
        calls.append(1)
        raise RuntimeError("down")

    dispatcher = _dispatcher(session_factory, base_delay=timedelta(hours=1))
    dispatcher.register("t", fail)
    dispatcher.dispatch_once()
    dispatcher.dispatch_once()  # retry_at 尚未到
    assert len(calls) == 1
    with session_factory() as s:
        assert s.execute(select(OutboxRow)).scalar_one().retry_at is not None


def test_outbox_dead_letter_after_max_attempts(session_factory, publisher):
    publisher.publish(EventDraft(event_type="task.failed"), INTERNAL, outbox_targets=["t"])

    def fail(event):
        raise RuntimeError("poison")

    dispatcher = _dispatcher(session_factory, max_attempts=3)
    dispatcher.register("t", fail)
    stats = [dispatcher.dispatch_once() for _ in range(5)]
    assert sum(s.dead_lettered for s in stats) == 1
    with session_factory() as s:
        row = s.execute(select(OutboxRow)).scalar_one()
        assert row.attempts == 3 and row.dead_letter_reason == "RuntimeError" and row.acked_at is None


def test_outbox_target_without_handler_stays_pending(session_factory, publisher):
    publisher.publish(EventDraft(event_type="task.completed"), INTERNAL, outbox_targets=["nobody"])
    dispatcher = _dispatcher(session_factory)
    assert dispatcher.dispatch_once() == DispatchStats()  # 沒有任何 handler
    dispatcher.register("other", lambda e: None)
    assert dispatcher.dispatch_once() == DispatchStats()
    with session_factory() as s:
        row = s.execute(select(OutboxRow)).scalar_one()
        assert row.attempts == 0 and row.acked_at is None and row.dead_letter_reason is None
    dispatcher.register("nobody", lambda e: None)  # 之後註冊 handler 即可投遞
    assert dispatcher.dispatch_once().delivered == 1


def test_duplicate_publish_does_not_enqueue_again(session_factory, publisher):
    draft = EventDraft(event_type="input.text_final", idempotency_key="k")
    publisher.publish(draft, EXTERNAL, outbox_targets=["t"])
    publisher.publish(draft, EXTERNAL, outbox_targets=["t"])
    assert _count(session_factory, OutboxRow) == 1


# ---- E-11 / D33：私人原文只在可刪 blob ----

def test_private_text_lives_in_deletable_blob_only(session_factory, publisher):
    secret = "私人對話內容-不應進入 journal"
    draft = EventDraft(event_type="input.text_final", idempotency_key="k", payload={"len": 12}, payload_blob=secret.encode())
    event = publisher.publish(draft, EXTERNAL).event
    digest = journal.sha256_hex(secret.encode())
    assert event.payload_ref and event.payload_hash == digest

    with session_factory() as s:
        dump = s.execute(text("SELECT to_jsonb(e)::text FROM events e")).scalar_one()
        assert secret not in dump
        assert journal.read_blob(s, event.payload_ref) == secret.encode()

    with session_factory() as s, s.begin():
        assert journal.delete_blob(s, event.payload_ref, "使用者要求刪除") == 1

    with session_factory() as s:
        assert journal.read_blob(s, event.payload_ref) is None
        assert s.get(PayloadBlob, event.payload_ref).deleted_at is not None
        row = s.get(EventRow, event.event_id)
        assert row.payload_hash == digest  # 雜湊保留
        assert row.tombstone_at is not None and row.tombstone_reason == "使用者要求刪除"


def test_duplicate_resend_does_not_leave_orphan_blob(session_factory, publisher):
    draft = EventDraft(event_type="input.text_final", idempotency_key="k", payload_blob=b"abc")
    publisher.publish(draft, EXTERNAL)
    publisher.publish(draft, EXTERNAL)
    assert _count(session_factory, PayloadBlob) == 1


# ---- append-only ----

def test_journal_is_append_only(session_factory, publisher):
    event = publisher.publish(EventDraft(event_type="turn.started"), INTERNAL).event
    with pytest.raises(DBAPIError), session_factory() as s, s.begin():
        s.execute(text("DELETE FROM events WHERE event_id = :i"), {"i": event.event_id})
    with pytest.raises(DBAPIError), session_factory() as s, s.begin():
        s.execute(text("UPDATE events SET event_type = 'x' WHERE event_id = :i"), {"i": event.event_id})
    assert _count(session_factory, EventRow) == 1
