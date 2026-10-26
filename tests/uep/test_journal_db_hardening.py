"""CC-1 加固項目的整合測試（需 PostgreSQL；未設 UEP_TEST_DATABASE_URL 時 skip）。"""
import logging
import threading
from datetime import timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from uep.db.base import Base
from uep.events import inbox, journal
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.models import EventRow, InboxRow, OutboxRow, PayloadBlob
from uep.events.outbox import OutboxDispatcher
from uep.events.publisher import EventPublisher

pytestmark = pytest.mark.usefixtures("session_factory")

EXTERNAL = TrustedContext(source="adapter.discord", actor_id="guest_1", scope_ref="scope_public", external=True)
INTERNAL = TrustedContext(source="core.task", actor_id="core", scope_ref="scope_private_owner")
SECRET = "私人字串-不可外洩-xyz"

_EVENT_INSERT = (
    "INSERT INTO events (event_id, {extra_cols} schema_version, event_type, delivery_class, occurred_at,"
    " source, persona_id, actor_id, trace_id, priority, scope_ref, payload{ref_col})"
    " VALUES ('evt_manual', {extra_vals} 1, 'turn.started', 'durable', now(), 's', 'uep', 'a', 't',"
    " 'normal', 'scope_public', '{{}}'{ref_val})"
)


@pytest.fixture
def publisher(session_factory):
    return EventPublisher(session_factory, EventBus())


def _count(session_factory, model):
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(model))


def _dispatcher(session_factory, **kw):
    kw.setdefault("base_delay", timedelta(0))
    kw.setdefault("jitter", 0.0)
    return OutboxDispatcher(session_factory, **kw)


def _run_threads(n, target):
    barrier, results, errors = threading.Barrier(n), [None] * n, []

    def runner(i):
        try:
            barrier.wait(timeout=10)
            results[i] = target(i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    return results


# ---- Identity always ----

def test_seq_columns_reject_explicit_values(session_factory, publisher):
    event = publisher.publish(EventDraft(event_type="turn.started"), INTERNAL).event
    sql = _EVENT_INSERT.format(extra_cols="journal_seq,", extra_vals="999,", ref_col="", ref_val="")
    with pytest.raises(DBAPIError), session_factory() as s, s.begin():
        s.execute(text(sql))
    with pytest.raises(DBAPIError), session_factory() as s, s.begin():
        s.execute(
            text("INSERT INTO outbox (outbox_id, seq, event_id, target) VALUES ('obx_manual', 999, :e, 'x')"),
            {"e": event.event_id},
        )


def test_payload_ref_requires_existing_blob(session_factory):
    sql = _EVENT_INSERT.format(extra_cols="", extra_vals="", ref_col=", payload_ref", ref_val=", 'blob_missing'")
    with pytest.raises(IntegrityError), session_factory() as s, s.begin():
        s.execute(text(sql))


# ---- 墓碑 ----

def test_tombstone_is_immutable(session_factory, publisher):
    event = publisher.publish(EventDraft(event_type="turn.started"), INTERNAL).event
    sql = "UPDATE events SET {} WHERE event_id = :i"
    with pytest.raises(DBAPIError), session_factory() as s, s.begin():  # 無 tombstone_at 時不可只改 reason
        s.execute(text(sql.format("tombstone_reason = 'only reason'")), {"i": event.event_id})
    with session_factory() as s, s.begin():
        s.execute(text(sql.format("tombstone_at = now(), tombstone_reason = 'r1'")), {"i": event.event_id})
    for assignment in ("tombstone_at = NULL", "tombstone_reason = 'r2'", "tombstone_reason = NULL"):
        with pytest.raises(DBAPIError), session_factory() as s, s.begin():
            s.execute(text(sql.format(assignment)), {"i": event.event_id})


def test_delete_blob_is_idempotent(session_factory, publisher):
    draft = EventDraft(event_type="input.text_final", idempotency_key="k", payload_blob=b"abc")
    event = publisher.publish(draft, EXTERNAL).event
    with session_factory() as s, s.begin():
        assert journal.delete_blob(s, event.payload_ref, "first") == 1
    with session_factory() as s, s.begin():
        assert journal.delete_blob(s, event.payload_ref, "second") == 0
    with session_factory() as s:
        assert s.get(EventRow, event.event_id).tombstone_reason == "first"


# ---- 錯誤訊息不外洩 ----

def test_handler_exception_message_not_stored_or_logged(session_factory, publisher, caplog):
    publisher.publish(EventDraft(event_type="task.failed"), INTERNAL, outbox_targets=["t"])

    def fail(event):
        raise ValueError(SECRET)

    dispatcher = _dispatcher(session_factory, max_attempts=2)
    dispatcher.register("t", fail)
    with caplog.at_level(logging.DEBUG):
        dispatcher.dispatch_once()
        with session_factory() as s:
            assert s.execute(select(OutboxRow)).scalar_one().last_error == "ValueError"
        dispatcher.dispatch_once()
    with session_factory() as s:
        row = s.execute(select(OutboxRow)).scalar_one()
        assert row.dead_letter_reason == "ValueError" and SECRET not in (row.last_error or "")
    assert SECRET not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_inbox_result_restricted(session_factory):
    for bad in ({"text": "x" * 500}, {"k": ["a"]}, {"k": {"n": 1}}):
        with pytest.raises(ValueError), session_factory() as s, s.begin():
            inbox.process_once(s, "c", "evt_r", lambda bad=bad: bad)
    assert _count(session_factory, InboxRow) == 0
    with session_factory() as s, s.begin():
        assert inbox.process_once(s, "c", "evt_r", lambda: {"status": "ok", "n": 1})


def test_resend_with_different_blob_logs_warning(session_factory, publisher, caplog):
    def draft(blob):
        return EventDraft(event_type="input.text_final", idempotency_key="k", payload_blob=blob)

    publisher.publish(draft(b"one"), EXTERNAL)
    with caplog.at_level(logging.WARNING, logger="uep.events.journal"):
        publisher.publish(draft(b"two"), EXTERNAL)
    assert "不一致" in caplog.text and "two" not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="uep.events.journal"):
        publisher.publish(draft(b"one"), EXTERNAL)
    assert caplog.text == ""


# ---- 併發 ----

def test_concurrent_same_idempotency_key_single_row(session_factory, publisher):
    draft = EventDraft(event_type="input.text_final", idempotency_key="race", payload_blob=b"x")
    results = _run_threads(4, lambda i: publisher.publish(draft, EXTERNAL))
    assert sum(not r.duplicate for r in results) == 1
    assert len({r.event.event_id for r in results}) == 1
    assert _count(session_factory, EventRow) == 1
    assert _count(session_factory, PayloadBlob) == 1


def test_concurrent_inbox_same_key_single_effect(session_factory):
    effects = []

    def work(i):
        with session_factory() as s, s.begin():
            return inbox.process_once(s, "c1", "evt_race", lambda: effects.append(i))

    results = _run_threads(4, work)
    assert results.count(True) == 1 and len(effects) == 1
    assert _count(session_factory, InboxRow) == 1


def test_two_dispatchers_cover_all_rows(session_factory, publisher):
    n = 40
    for _ in range(n):
        publisher.publish(EventDraft(event_type="task.completed"), INTERNAL, outbox_targets=["t"])
    handled, lock = [], threading.Lock()

    def handler(event):
        with lock:
            handled.append(event.event_id)

    def run(i):
        d = _dispatcher(session_factory)
        d.register("t", handler)
        return d.dispatch_once(limit=100)

    stats = _run_threads(2, run)
    assert len(set(handled)) == n  # 無遺漏（至少一次）
    assert sum(s.delivered for s in stats) == n
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(OutboxRow).where(OutboxRow.acked_at.is_(None))) == 0


class _Crash(BaseException):
    """模擬程序在 ACK 前崩潰（不被 dispatcher 的 Exception 隔離攔下）。"""


def test_crash_before_ack_then_resend_blocked_by_inbox(session_factory, publisher):
    publisher.publish(EventDraft(event_type="task.completed"), INTERNAL, outbox_targets=["a"])
    effects, armed = [], {"crash": True}

    def consumer(event):
        with session_factory() as s, s.begin():
            inbox.process_once(s, "a", event.event_id, lambda: effects.append(1))
        if armed["crash"]:
            armed["crash"] = False
            raise _Crash()

    dispatcher = _dispatcher(session_factory)
    dispatcher.register("a", consumer)
    with pytest.raises(_Crash):
        dispatcher.dispatch_once()
    with session_factory() as s:
        row = s.execute(select(OutboxRow)).scalar_one()
        assert row.attempts == 0 and row.acked_at is None  # 崩潰：outbox 狀態未變
    assert dispatcher.dispatch_once().delivered == 1
    assert effects == [1]


# ---- schema 無漂移 ----

def test_migration_matches_models(engine):
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
