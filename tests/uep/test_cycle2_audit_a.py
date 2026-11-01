"""core-cycle2-audit 區域 (A) 回歸測試（T-24）：M1／M2／M3／L1～L5。"""
import logging
import threading

import pytest
from alembic import command
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.uep.test_journal_db import EXTERNAL
from uep.config import get_settings
from uep.events import journal
from uep.events.bus import EventBus
from uep.events.envelope import (
    EventDraft,
    IdempotencyConflict,
    TrustedContext,
    scope_rank,
)
from uep.events.payload_hash import PayloadHasher, PayloadHashKeyMissing
from uep.events.publisher import EventPublisher
from uep.events.tailer import (
    JournalTailer,
    TailCursor,
    TailCursorMismatch,
    watermark_lag,
)
from uep.ids import is_valid_id, new_id


@pytest.fixture
def publisher(session_factory):
    return EventPublisher(session_factory, EventBus())


def _draft(**kw):
    kw.setdefault("event_type", "input.text_final")
    kw.setdefault("idempotency_key", "k")
    return EventDraft(**kw)


# ---- M1：idempotency 衝突 ----

def test_m1_same_key_different_event_type_raises(publisher):
    publisher.publish(_draft(), EXTERNAL)
    with pytest.raises(IdempotencyConflict):
        publisher.publish(_draft(event_type="input.attachment_provided"), EXTERNAL)


def test_m1_same_key_different_scope_raises(publisher):
    publisher.publish(_draft(), EXTERNAL)
    other = TrustedContext(source=EXTERNAL.source, actor_id=EXTERNAL.actor_id, scope_ref="scope_group", external=True)
    with pytest.raises(IdempotencyConflict):
        publisher.publish(_draft(), other)


def test_m1_same_key_different_aggregate_raises(publisher):
    publisher.publish(_draft(aggregate_type="t", aggregate_id="a1", aggregate_version=1), EXTERNAL)
    with pytest.raises(IdempotencyConflict):
        publisher.publish(_draft(aggregate_type="t", aggregate_id="a2", aggregate_version=1), EXTERNAL)


def test_m1_different_blob_raises_and_true_resend_still_duplicate(publisher):
    publisher.publish(_draft(payload_blob=b"one"), EXTERNAL)
    with pytest.raises(IdempotencyConflict):
        publisher.publish(_draft(payload_blob=b"two"), EXTERNAL)
    again = publisher.publish(_draft(payload_blob=b"one"), EXTERNAL)
    assert again.duplicate


# ---- M2：叢集變更偵測 ----

def test_m2_watermark_below_low_txid_raises(session_factory):
    tailer = JournalTailer(session_factory, cursor=TailCursor(low_txid=10**15))
    with pytest.raises(TailCursorMismatch):
        tailer.poll()


def test_m2_watermark_equal_low_txid_is_quiet(session_factory):
    tailer = JournalTailer.from_now(session_factory)
    assert tailer.poll() == []


def test_m2_watermark_lag_reports_gap(session_factory):
    tailer = JournalTailer.from_now(session_factory)
    with session_factory() as s:
        assert watermark_lag(s, tailer.cursor) == 0
        assert watermark_lag(s, TailCursor(low_txid=0)) > 0


# ---- M3：EventDraft 欄位驗證 ----

@pytest.mark.parametrize("field", ["task_id", "conversation_id", "segment_id", "turn_id"])
def test_m3_id_fields_reject_free_text(field):
    with pytest.raises(ValidationError):
        EventDraft(event_type="x", **{field: "我今天跟醫生說的病情" * 20})
    prefix = {"task_id": "task", "conversation_id": "conv", "segment_id": "seg", "turn_id": "turn"}[field]
    assert getattr(EventDraft(event_type="x", **{field: new_id(prefix)}), field)


@pytest.mark.parametrize("field", ["trace_id", "causation_id", "aggregate_id", "idempotency_key"])
def test_m3_safe_fields_reject_free_text_and_overlong(field):
    for bad in ("含中文的私人文字", "a b", "x" * 300, "ok\n"):
        with pytest.raises(ValidationError):
            EventDraft(event_type="x", **{field: bad})
    assert getattr(EventDraft(event_type="x", **{field: "disc:msg_991.v-2"}), field) == "disc:msg_991.v-2"


def test_m3_idempotency_key_limit_256():
    assert EventDraft(event_type="x", idempotency_key="k" * 256)
    with pytest.raises(ValidationError):
        EventDraft(event_type="x", idempotency_key="k" * 257)


# ---- L1：尾端換行 ----

def test_l1_trailing_newline_rejected():
    assert not is_valid_id(new_id("evt") + "\n", "evt")
    assert not is_valid_id(new_id("evt") + "\n")
    with pytest.raises(ValueError):
        scope_rank("scope_private\n")


# ---- L2：空白金鑰 ----

def test_l2_whitespace_key_rejected():
    with pytest.raises(PayloadHashKeyMissing):
        PayloadHasher(SecretStr(" " * 16))
    with pytest.raises(PayloadHashKeyMissing):
        PayloadHasher(SecretStr("\t \n" * 8))
    assert PayloadHasher(SecretStr("x" * 16))


def test_l2_whitespace_settings_key_rejected(monkeypatch):
    monkeypatch.setattr(get_settings(), "payload_hash_key", SecretStr(" " * 20))
    with pytest.raises(PayloadHashKeyMissing):
        journal.put_blob(None, b"secret")  # 先取密鑰：失敗時不會碰 session


# ---- L3：handler 例外 log ----

def test_l3_handler_error_log_has_type_only(caplog):
    from tests.uep.test_envelope_and_bus import _event

    bus = EventBus()
    secret = "機密訊息內文-XYZZY"

    def handler(event):
        raise ValueError(secret)

    bus.subscribe("input.text_final", handler)
    with caplog.at_level(logging.ERROR, logger="uep.events.bus"):
        bus.publish(_event(idempotency_key="l3"), sync=True)
    assert "ValueError" in caplog.text
    assert secret not in caplog.text
    assert bus.get_stats()["handler_errors"] == 1


# ---- L4：stop 逾時後拒絕重啟 ----

def test_l4_start_refused_while_old_consumer_alive():
    from tests.uep.test_envelope_and_bus import _event

    release = threading.Event()
    entered = threading.Event()

    def handler(event):
        entered.set()
        release.wait(timeout=10)

    bus = EventBus(drain_timeout=0.1)
    bus.subscribe("input.text_final", handler)
    bus.start()
    try:
        bus.publish(_event(idempotency_key="l4"))
        assert entered.wait(timeout=5)
        bus.stop()  # 排空逾時，handler 仍卡住 -> 舊線程存活
        with pytest.raises(RuntimeError):
            bus.start()
    finally:
        release.set()
    # 舊線程結束後即可重新啟動
    for _ in range(100):
        try:
            bus.start()
            break
        except RuntimeError:
            threading.Event().wait(0.05)
    else:
        pytest.fail("舊消費者結束後仍無法啟動")
    bus.stop()


# ---- L5：TRUNCATE trigger ----

def test_l5_events_truncate_blocked(alembic_cfg, engine, session_factory):
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(text("TRUNCATE events CASCADE"))


def test_l5_migration_0007_reversible(alembic_cfg, engine, session_factory):
    def triggers():
        with engine.connect() as conn:
            return {r[0] for r in conn.execute(text(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = 'events'::regclass AND NOT tgisinternal"))}

    assert "events_no_truncate" in triggers()
    try:
        command.downgrade(alembic_cfg, "0006_journal_txid")
        assert "events_no_truncate" not in triggers()
    finally:
        command.upgrade(alembic_cfg, "head")
    assert "events_no_truncate" in triggers()
