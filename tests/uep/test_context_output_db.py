"""CC-2／CC-5 DB 落地（需 PostgreSQL 與 0002_task_model；未設 UEP_TEST_DATABASE_URL 時 skip）。"""
from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.uep.factories import NOW, make_delivery, make_intent, make_snapshot
from uep.context.models import (
    ContextSnapshotRow,
    StaleRevisionError,
    load_snapshot,
    save_snapshot,
)
from uep.ids import new_id
from uep.output.delivery import DeliveryEvidence as E
from uep.output.delivery import DeliveryStatus as S
from uep.output.delivery import IllegalDeliveryTransition
from uep.output.models import (
    DeliveryRow,
    advance_delivery,
    create_delivery,
    load_delivery,
    load_intent,
    save_intent,
)

pytestmark = pytest.mark.usefixtures("session_factory")


# ---- CC-2 ----
def test_snapshot_roundtrip_without_raw_text(session_factory):
    snap = make_snapshot(
        memory_slices=[{"memory_id": "mem_1", "scope": "scope_private_owner",
                        "retrieved_at": NOW, "relevance": 0.5}],
        history_entries=[{"turn_id": "turn_a", "role": "user", "generated_text_ref": "blob_1",
                          "scope": "scope_private_owner", "delivery_state": "rendered_played"}],
    )
    with session_factory.begin() as s:
        save_snapshot(s, snap)
    with session_factory() as s:
        assert load_snapshot(s, snap.snapshot_id) == snap
        body = s.scalar(select(ContextSnapshotRow.body).where(ContextSnapshotRow.snapshot_id == snap.snapshot_id))
        assert "generated_text_ref" in str(body) and "text" not in body  # 只有引用，無原文欄位


def test_snapshot_revision_must_increase(session_factory):
    conv, seg = new_id("conv"), new_id("seg")
    with session_factory.begin() as s:
        save_snapshot(s, make_snapshot(conversation_id=conv, segment_id=seg, context_revision=1))
        save_snapshot(s, make_snapshot(conversation_id=conv, segment_id=seg, context_revision=2))
    with session_factory() as s, pytest.raises(StaleRevisionError):
        save_snapshot(s, make_snapshot(conversation_id=conv, segment_id=seg, context_revision=2))
    with session_factory() as s, pytest.raises(StaleRevisionError):
        save_snapshot(s, make_snapshot(conversation_id=conv, segment_id=seg, context_revision=1))


def test_snapshot_db_unique_revision(session_factory):
    conv, seg = new_id("conv"), new_id("seg")
    first = make_snapshot(conversation_id=conv, segment_id=seg, context_revision=1)
    with session_factory.begin() as s:
        save_snapshot(s, first)
    cols = ("context_revision, purpose, persona_id, persona_version, policy_version, conversation_id, "
            "segment_id, audience_scope, actor_ref, memory_status, history_owner, built_at, body")
    with pytest.raises(IntegrityError), session_factory.begin() as s:  # 繞過應用層直接複製同 revision
        s.execute(text(f"INSERT INTO context_snapshots (snapshot_id, {cols}) "
                       f"SELECT :new, {cols} FROM context_snapshots WHERE snapshot_id=:old"),
                  {"new": new_id("snap"), "old": first.snapshot_id})


def test_snapshot_immutable_except_sdk_session_ref_backfill(session_factory):
    snap = make_snapshot()
    with session_factory.begin() as s:
        save_snapshot(s, snap)
    with session_factory.begin() as s:  # 回填一次允許
        s.execute(text("UPDATE context_snapshots SET sdk_session_ref='sdk_1' WHERE snapshot_id=:i"),
                  {"i": snap.snapshot_id})
    for sql in (
        "UPDATE context_snapshots SET sdk_session_ref='sdk_2' WHERE snapshot_id=:i",
        "UPDATE context_snapshots SET context_revision=9 WHERE snapshot_id=:i",
        "UPDATE context_snapshots SET audience_scope='scope_public' WHERE snapshot_id=:i",
        "DELETE FROM context_snapshots WHERE snapshot_id=:i",
    ):
        with pytest.raises(DBAPIError), session_factory.begin() as s:
            s.execute(text(sql), {"i": snap.snapshot_id})


# ---- CC-5 ----
def _persist(session_factory, **intent_kw):
    intent = make_intent(**intent_kw)
    record = make_delivery(intent)
    with session_factory.begin() as s:
        save_intent(s, intent)
        create_delivery(s, record)
    return intent, record


def test_intent_roundtrip_and_immutable(session_factory):
    intent, _ = _persist(session_factory)
    with session_factory() as s:
        assert load_intent(s, intent.output_id, intent.sequence) == intent
    with pytest.raises(DBAPIError), session_factory.begin() as s:
        s.execute(text("UPDATE output_intents SET content_ref='x' WHERE output_id=:i"), {"i": intent.output_id})


def test_delivery_advances_with_evidence_and_persists(session_factory):
    _, rec = _persist(session_factory)
    with session_factory.begin() as s:
        advance_delivery(s, rec.delivery_id, S.ENQUEUED)
        advance_delivery(s, rec.delivery_id, S.SENT, evidence=E.TRANSPORT_SENT, reported_at=NOW)
    with session_factory() as s:
        got = load_delivery(s, rec.delivery_id)
    assert got.status is S.SENT and got.delivery_evidence is E.TRANSPORT_SENT


def test_delivery_illegal_transition_leaves_row_unchanged(session_factory):
    _, rec = _persist(session_factory)
    with pytest.raises(IllegalDeliveryTransition), session_factory.begin() as s:
        advance_delivery(s, rec.delivery_id, S.RENDERED_PLAYED, evidence=E.LOCAL_PLAYBACK)
    with session_factory() as s:
        assert load_delivery(s, rec.delivery_id).status is S.GENERATED


def test_delivery_initial_must_be_generated(session_factory):
    intent = make_intent()
    bad = make_delivery(intent, status="sent", delivery_evidence="transport_sent")
    with pytest.raises(ValueError), session_factory.begin() as s:
        save_intent(s, intent)
        create_delivery(s, bad)


def test_db_blocks_terminal_rewrite_and_weak_evidence(session_factory):
    _, rec = _persist(session_factory)
    with session_factory.begin() as s:
        advance_delivery(s, rec.delivery_id, S.CANCELLED, cancel_position={"chunk": 1})
    with pytest.raises(DBAPIError), session_factory.begin() as s:  # 終態不可再改（繞過應用層）
        s.execute(text("UPDATE deliveries SET status='sent' WHERE delivery_id=:i"), {"i": rec.delivery_id})
    _, rec2 = _persist(session_factory)
    with pytest.raises(IntegrityError), session_factory.begin() as s:  # O-2 CHECK
        s.execute(text("UPDATE deliveries SET status='rendered_played', delivery_evidence='transport_sent' "
                       "WHERE delivery_id=:i"), {"i": rec2.delivery_id})


def test_one_delivery_per_intent_and_fk(session_factory):
    intent, _ = _persist(session_factory)
    with pytest.raises(IntegrityError), session_factory.begin() as s:
        create_delivery(s, make_delivery(intent))  # 同一輸出只留一筆 deliveries
    orphan = make_delivery(make_intent())
    with pytest.raises(IntegrityError), session_factory.begin() as s:
        create_delivery(s, orphan)  # 沒有對應 intent


def test_delivery_row_columns_match_model(session_factory):
    cols = {c.name for c in DeliveryRow.__table__.columns}
    from uep.output.delivery import DeliveryRecord
    assert cols == set(DeliveryRecord.model_fields)
