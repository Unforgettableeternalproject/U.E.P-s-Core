"""審查補強：CC-2／CC-5 DB 行為（需 PostgreSQL；未設 UEP_TEST_DATABASE_URL 時 skip）。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.uep.factories import make_delivery, make_intent, make_snapshot
from uep.context.models import StaleRevisionError, save_snapshot
from uep.ids import new_id
from uep.output.delivery import DeliveryEvidence as E
from uep.output.delivery import DeliveryStatus as S
from uep.output.models import (
    DeliveryRow,
    advance_delivery,
    create_delivery,
    load_delivery,
    save_intent,
)

pytestmark = pytest.mark.usefixtures("session_factory")


def _persist(session_factory):
    intent = make_intent()
    record = make_delivery(intent)
    with session_factory.begin() as s:
        save_intent(s, intent)
        create_delivery(s, record)
    return intent, record


def test_advance_reloads_locked_row_instead_of_stale_session_state(session_factory):
    _, rec = _persist(session_factory)
    with session_factory() as s:
        assert load_delivery(s, rec.delivery_id).status is S.GENERATED  # 同 session 先讀到舊狀態
        with session_factory.begin() as other:
            advance_delivery(other, rec.delivery_id, S.ENQUEUED)
        # 若沿用舊狀態，generated -> sent 會被判跳級；正確應以鎖內重新載入的 enqueued 為基準
        got = advance_delivery(s, rec.delivery_id, S.SENT, evidence=E.TRANSPORT_SENT)
        s.commit()
    assert got.status is S.SENT


def test_advance_same_status_is_noop_in_db(session_factory):
    _, rec = _persist(session_factory)
    with session_factory.begin() as s:
        advance_delivery(s, rec.delivery_id, S.ENQUEUED)
        again = advance_delivery(s, rec.delivery_id, S.ENQUEUED)
    assert again.status is S.ENQUEUED


def test_unknown_resolves_to_failed_in_db_and_evidence_never_drops(session_factory):
    _, rec = _persist(session_factory)
    with session_factory.begin() as s:
        advance_delivery(s, rec.delivery_id, S.ENQUEUED)
        advance_delivery(s, rec.delivery_id, S.SENT, evidence=E.REMOTE_RENDER)
        advance_delivery(s, rec.delivery_id, S.UNKNOWN)
        done = advance_delivery(s, rec.delivery_id, S.FAILED, failure={"code": "NOT_SENT", "retryable": True})
    assert done.failure == {"code": "NOT_SENT", "retryable": True}
    _, rec2 = _persist(session_factory)
    with session_factory.begin() as s:
        advance_delivery(s, rec2.delivery_id, S.ENQUEUED)
        advance_delivery(s, rec2.delivery_id, S.SENT, evidence=E.REMOTE_RENDER)
    with pytest.raises(DBAPIError), session_factory.begin() as s:  # 繞過應用層降級證據
        s.execute(
            text("UPDATE deliveries SET delivery_evidence='transport_sent' WHERE delivery_id=:i"),
            {"i": rec2.delivery_id},
        )


def test_db_rejects_failure_with_extra_keys_and_spoken_text_is_only_a_ref(session_factory):
    _, rec = _persist(session_factory)
    bad_failure = '{"code":"X","detail":"private"}'
    with pytest.raises(IntegrityError), session_factory.begin() as s:
        s.execute(
            text("UPDATE deliveries SET status='failed', failure=CAST(:f AS jsonb) WHERE delivery_id=:i"),
            {"f": bad_failure, "i": rec.delivery_id},
        )
    cols = {c.name for c in DeliveryRow.__table__.columns}
    assert "spoken_text" not in cols and {"spoken_text_ref", "spoken_text_hash"} <= cols
    with pytest.raises(IntegrityError), session_factory.begin() as s:  # ref 必須指向 payload_blobs
        s.execute(
            text("UPDATE deliveries SET spoken_text_ref='blob_missing' WHERE delivery_id=:i"),
            {"i": rec.delivery_id},
        )


def test_delivery_kind_must_match_intent(session_factory):
    intent, _ = _persist(session_factory)
    wrong = make_delivery(intent, kind="audio_stream", dispatch_id=new_id("dsp"))
    with pytest.raises(IntegrityError), session_factory.begin() as s:
        create_delivery(s, wrong)


def test_background_snapshots_scope_by_run_then_persona_purpose(session_factory):
    bg = {"purpose": "proactive_eval", "conversation_id": None, "segment_id": None, "turn_id": None}
    with session_factory.begin() as s:  # 不同 run 各自從 revision 1 起
        save_snapshot(s, make_snapshot(context_revision=1, run_id="run_a", **bg))
        save_snapshot(s, make_snapshot(context_revision=1, run_id="run_b", **bg))
        save_snapshot(s, make_snapshot(context_revision=2, run_id="run_a", **bg))
    with pytest.raises(StaleRevisionError), session_factory.begin() as s:
        save_snapshot(s, make_snapshot(context_revision=2, run_id="run_a", **bg))
    # 無 run_id：退到 (persona_id, purpose)
    with session_factory.begin() as s:
        save_snapshot(s, make_snapshot(context_revision=1, **bg))
        save_snapshot(s, make_snapshot(context_revision=1, persona_id="persona_other", **bg))
    with pytest.raises(StaleRevisionError), session_factory.begin() as s:
        save_snapshot(s, make_snapshot(context_revision=1, **bg))
    # 互動範圍不受 run_id 影響：同 conversation／segment 共用一條序列
    conv, seg = new_id("conv"), new_id("seg")
    with session_factory.begin() as s:
        save_snapshot(s, make_snapshot(conversation_id=conv, segment_id=seg, context_revision=1))
    with pytest.raises(StaleRevisionError), session_factory.begin() as s:
        save_snapshot(
            s, make_snapshot(conversation_id=conv, segment_id=seg, context_revision=1, purpose="task_run", run_id="run_x")
        )


def test_scope_key_matches_python_helper(session_factory):
    from uep.context.models import ContextSnapshotRow, scope_key_of

    snaps = [
        make_snapshot(),
        make_snapshot(purpose="task_run", conversation_id=None, segment_id=None, turn_id=None, run_id="run_k"),
        make_snapshot(purpose="proactive_eval", conversation_id=None, segment_id=None, turn_id=None),
    ]
    with session_factory.begin() as s:
        for snap in snaps:
            save_snapshot(s, snap)
    with session_factory() as s:
        for snap in snaps:
            assert s.get(ContextSnapshotRow, snap.snapshot_id).scope_key == scope_key_of(snap)


def test_concurrent_snapshot_saves_same_revision_only_one_wins(session_factory):
    conv, seg = new_id("conv"), new_id("seg")

    def attempt(_: int) -> str:
        try:
            with session_factory.begin() as s:
                save_snapshot(s, make_snapshot(conversation_id=conv, segment_id=seg, context_revision=1))
            return "ok"
        except StaleRevisionError:
            return "stale"

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sorted(results) == ["ok", "stale", "stale", "stale"]
