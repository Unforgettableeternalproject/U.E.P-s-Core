"""CC-2 ContextSnapshot、CC-5 OutputIntent／DeliveryRecord／ControlSignal 的不變條件（無 DB）。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.uep.factories import NOW, make_delivery, make_intent, make_snapshot
from uep.context.snapshot import scope_admits
from uep.output.delivery import (
    TERMINAL,
    IllegalDeliveryTransition,
    check_transition,
)
from uep.output.delivery import (
    DeliveryEvidence as E,
)
from uep.output.delivery import (
    DeliveryStatus as S,
)
from uep.output.intent import ControlSignal, OutputKind


def _memory(scope: str):
    return {"memory_id": "mem_1", "scope": scope, "retrieved_at": NOW, "relevance": 0.9}


# ---- CC-2 ----
def test_snapshot_is_frozen():
    snap = make_snapshot()
    with pytest.raises(ValidationError):
        snap.context_revision = 2  # type: ignore[misc]


def test_scope_admits_rules():
    assert scope_admits("scope_private_owner", "scope_public")  # public 內容可進 private audience
    assert not scope_admits("scope_public", "scope_private_owner")  # VC-2-1
    assert scope_admits("scope_private_owner", "scope_private_owner")
    assert not scope_admits("scope_private_owner", "scope_private_other")  # 同級須同歸屬
    assert not scope_admits("scope_group_a", "scope_group_b")


def test_private_memory_cannot_enter_public_snapshot():
    with pytest.raises(ValidationError, match="不可進入"):
        make_snapshot(audience_scope="scope_public", memory_slices=[_memory("scope_private_owner")])


def test_private_history_cannot_enter_public_snapshot():
    entry = {
        "turn_id": "turn_x", "role": "user", "generated_text_ref": "blob_1",
        "scope": "scope_private_owner", "delivery_state": "rendered_played",
    }
    with pytest.raises(ValidationError, match="不可進入"):
        make_snapshot(audience_scope="scope_public", history_entries=[entry])


def test_public_snapshot_accepts_public_memory_and_records_exclusions():
    snap = make_snapshot(
        audience_scope="scope_public",
        memory_slices=[_memory("scope_public")],
        excluded_summary={"scope": 3},
    )
    assert len(snap.memory_slices) == 1 and snap.excluded_summary == {"scope": 3}


def test_degraded_memory_status_forbids_slices():
    with pytest.raises(ValidationError):
        make_snapshot(memory_status="timeout", memory_slices=[_memory("scope_private_owner")])
    assert make_snapshot(memory_status="timeout").memory_slices == ()  # VC-2-3


def test_cancelled_history_cannot_claim_complete_speech():
    entry = {
        "turn_id": "turn_x", "role": "assistant", "generated_text_ref": "blob_1",
        "scope": "scope_private_owner", "delivery_state": "cancelled",
        "delivered_range": {"complete": True},
    }
    with pytest.raises(ValidationError):
        make_snapshot(history_entries=[entry])


def test_purpose_requirements():
    with pytest.raises(ValidationError):
        make_snapshot(turn_id=None)  # conversation_turn 需 turn_id
    with pytest.raises(ValidationError):
        make_snapshot(purpose="task_run", run_id=None)
    bg = make_snapshot(purpose="proactive_eval", conversation_id=None, segment_id=None, turn_id=None)
    assert bg.purpose.value == "proactive_eval"


def test_revision_must_be_positive_and_bad_scope_rejected():
    with pytest.raises(ValidationError):
        make_snapshot(context_revision=0)
    with pytest.raises(ValueError):
        make_snapshot(audience_scope="everyone")


# ---- CC-5 OutputIntent／ControlSignal ----
def test_intent_requires_turn_or_activity():
    with pytest.raises(ValidationError):
        make_intent(turn_id=None, turn_generation=None, attempt_id=None)
    assert make_intent(turn_id=None, turn_generation=None, attempt_id=None, activity_id="actv_1")
    with pytest.raises(ValidationError):
        make_intent(attempt_id=None)  # 互動欄位須成組


def test_audio_intent_needs_format_and_system_origin_has_no_generated_text():
    with pytest.raises(ValidationError):
        make_intent(kind="audio_stream")
    fmt = {"sample_rate": 24000, "channels": 1, "codec": "pcm"}
    assert make_intent(kind="audio_stream", content_format=fmt)
    with pytest.raises(ValidationError):
        make_intent(origin="system", generated_text_ref="blob_gen")


def test_control_signal_fence_requirements():
    base = {"control_id": "ctl_1", "kind": "stop", "scope": "turn", "issued_at": NOW}
    with pytest.raises(ValidationError):
        ControlSignal.model_validate({**base, "target_fence": {"core_epoch": 1}})
    assert ControlSignal.model_validate({**base, "target_fence": {"core_epoch": 1, "turn_generation": 5}})
    with pytest.raises(ValidationError):
        ControlSignal.model_validate({**base, "scope": "process", "target_fence": {}})


# ---- CC-5 狀態機 ----
def _walk(record, steps):
    for to, ev in steps:
        record = record.advance(to, evidence=ev)
    return record


def test_text_path_full_and_no_synthesized():
    rec = make_delivery(make_intent())
    done = _walk(rec, [(S.ENQUEUED, E.UNKNOWN), (S.SENT, E.TRANSPORT_SENT),
                       (S.ACCEPTED, E.REMOTE_RENDER), (S.RENDERED_PLAYED, E.REMOTE_RENDER)])
    assert done.status is S.RENDERED_PLAYED
    with pytest.raises(IllegalDeliveryTransition):
        rec.advance(S.SYNTHESIZED)  # text 沒有 synthesized


def test_audio_path_requires_synthesized():
    intent = make_intent(kind="audio_stream", content_format={"sample_rate": 24000, "channels": 1, "codec": "pcm"})
    rec = make_delivery(intent)
    with pytest.raises(IllegalDeliveryTransition):
        rec.advance(S.ENQUEUED)  # 不得略過 synthesized
    done = _walk(rec, [(S.SYNTHESIZED, E.UNKNOWN), (S.ENQUEUED, E.UNKNOWN), (S.SENT, E.TRANSPORT_SENT),
                       (S.ACCEPTED, E.LOCAL_PLAYBACK), (S.RENDERED_PLAYED, E.LOCAL_PLAYBACK)])
    assert done.status is S.RENDERED_PLAYED


@pytest.mark.parametrize("to", [S.SENT, S.ACCEPTED, S.RENDERED_PLAYED])
def test_no_skipping_levels(to):
    with pytest.raises(IllegalDeliveryTransition):
        make_delivery(make_intent()).advance(to, evidence=E.LOCAL_PLAYBACK)


def test_transport_sent_evidence_caps_at_sent():
    rec = _walk(make_delivery(make_intent()), [(S.ENQUEUED, E.UNKNOWN), (S.SENT, E.TRANSPORT_SENT)])
    for to in (S.ACCEPTED, S.RENDERED_PLAYED):
        with pytest.raises(IllegalDeliveryTransition):
            rec.advance(to, evidence=E.TRANSPORT_SENT)  # VC-5-2 / O-2
    with pytest.raises(IllegalDeliveryTransition):
        rec.advance(S.ACCEPTED, evidence=E.UNKNOWN)


def test_sent_needs_evidence():
    enq = make_delivery(make_intent()).advance(S.ENQUEUED)
    with pytest.raises(IllegalDeliveryTransition):
        enq.advance(S.SENT)  # 證據仍 unknown


def test_terminal_states_are_final():
    for terminal in (S.FAILED, S.CANCELLED):
        rec = make_delivery(make_intent()).advance(terminal)
        for to in (x for x in S if x is not terminal):
            with pytest.raises(IllegalDeliveryTransition):
                rec.advance(to, evidence=E.LOCAL_PLAYBACK)
    assert {S.RENDERED_PLAYED, S.FAILED, S.CANCELLED, S.REJECTED, S.SUPPRESSED} == TERMINAL


def test_rejected_only_from_generated_and_suppressed_only_pre_send():
    gen = make_delivery(make_intent())
    assert gen.advance(S.REJECTED).status is S.REJECTED
    enq = gen.advance(S.ENQUEUED)
    with pytest.raises(IllegalDeliveryTransition):
        enq.advance(S.REJECTED)
    assert enq.advance(S.SUPPRESSED).status is S.SUPPRESSED
    sent = enq.advance(S.SENT, evidence=E.TRANSPORT_SENT)
    with pytest.raises(IllegalDeliveryTransition):
        sent.advance(S.SUPPRESSED)


def test_cancel_keeps_played_range_and_spoken_text_separate_from_generated():
    # VC-5-1：句 1–2 已播，句 3 取消；歷史只記 played_range 內已交付部分
    rec = _walk(make_delivery(make_intent()), [(S.ENQUEUED, E.UNKNOWN), (S.SENT, E.TRANSPORT_SENT)])
    cancelled = rec.advance(
        S.CANCELLED, played_range={"to_chunk": 2}, cancel_position={"chunk": 2}, spoken_text_ref="blob_spoken", spoken_text_hash="h1",
    )
    assert cancelled.status is S.CANCELLED
    assert cancelled.spoken_text_ref == "blob_spoken" and cancelled.played_range == {"to_chunk": 2}


def test_unknown_resolves_only_to_sent():
    # VC-5-3：ACK 遺失 -> unknown；查核到已送出才可回到 sent
    rec = _walk(make_delivery(make_intent()), [(S.ENQUEUED, E.UNKNOWN), (S.SENT, E.TRANSPORT_SENT)])
    unknown = rec.advance(S.UNKNOWN)
    assert unknown.advance(S.SENT, evidence=E.TRANSPORT_SENT).status is S.SENT
    with pytest.raises(IllegalDeliveryTransition):
        unknown.advance(S.RENDERED_PLAYED, evidence=E.LOCAL_PLAYBACK)
    with pytest.raises(IllegalDeliveryTransition):
        unknown.advance(S.CANCELLED)


def test_unknown_to_failed_requires_lookup_failure():
    rec = _walk(make_delivery(make_intent()), [(S.ENQUEUED, E.UNKNOWN), (S.SENT, E.TRANSPORT_SENT)]).advance(S.UNKNOWN)
    with pytest.raises(IllegalDeliveryTransition):
        rec.advance(S.FAILED)  # 缺查核結果
    failed = rec.advance(S.FAILED, failure={"code": "NOT_SENT", "retryable": True})
    assert failed.status is S.FAILED


def test_late_strong_evidence_resolves_unknown_via_sent_then_ladder():
    rec = _walk(make_delivery(make_intent()), [(S.ENQUEUED, E.UNKNOWN), (S.SENT, E.TRANSPORT_SENT)]).advance(S.UNKNOWN)
    sent = rec.advance(S.SENT, evidence=E.REMOTE_RENDER)  # 晚到強證據：先解析為 sent，不跳級
    assert sent.delivery_evidence is E.REMOTE_RENDER
    assert sent.advance(S.ACCEPTED).advance(S.RENDERED_PLAYED).status is S.RENDERED_PLAYED
    done = _walk(make_delivery(make_intent()), [(S.CANCELLED, E.UNKNOWN)])
    with pytest.raises(IllegalDeliveryTransition):
        done.advance(S.RENDERED_PLAYED, evidence=E.LOCAL_PLAYBACK)  # 終態後晚到的證據不改狀態


def test_advance_same_status_is_idempotent_noop():
    rec = _walk(make_delivery(make_intent()), [(S.ENQUEUED, E.UNKNOWN), (S.SENT, E.TRANSPORT_SENT)])
    assert rec.advance(S.SENT, evidence=E.TRANSPORT_SENT) is rec
    assert rec.advance(S.SENT, evidence=E.REMOTE_RENDER) is rec  # 同狀態不改（升級證據須走推進）
    done = rec.advance(S.CANCELLED)
    assert done.advance(S.CANCELLED) is done  # 終態重複同一目標也是 no-op
    with pytest.raises(IllegalDeliveryTransition):
        rec.advance(S.SENT, evidence=E.UNKNOWN)  # 證據不得降級


def test_evidence_never_downgrades():
    rec = _walk(make_delivery(make_intent()), [(S.ENQUEUED, E.UNKNOWN), (S.SENT, E.REMOTE_RENDER)])
    with pytest.raises(IllegalDeliveryTransition):
        rec.advance(S.ACCEPTED, evidence=E.TRANSPORT_SENT)


def test_value_whitelists():
    base = make_delivery(make_intent())
    with pytest.raises(ValidationError):
        base.model_copy().model_validate({**base.model_dump(), "failure": {"code": "X", "detail": "原文"}})
    with pytest.raises(ValidationError):
        base.model_validate({**base.model_dump(), "failure": {"code": "x" * 100}})
    with pytest.raises(ValidationError):
        base.model_validate({**base.model_dump(), "played_range": {"text": "你好"}})
    with pytest.raises(ValidationError):
        base.model_validate({**base.model_dump(), "cancel_position": {"chunk": "二"}})
    with pytest.raises(ValidationError):
        make_snapshot(commitments=[{"commitment_id": "c1", "note": "私人內容", "scope": "scope_private_owner"}])
    with pytest.raises(ValidationError):
        make_snapshot(commitments=[{"commitment_id": "c" * 300, "scope": "scope_private_owner"}])
    assert make_snapshot(commitments=[{"commitment_id": "c1", "kind": "reminder", "scope": "scope_private_owner"}])


def test_any_non_terminal_can_fail_unknown_cancel():
    intent = make_intent(kind="audio_stream", content_format={"sample_rate": 1, "channels": 1, "codec": "pcm"})
    for stop in (S.FAILED, S.UNKNOWN, S.CANCELLED):
        assert make_delivery(intent).advance(stop).status is stop


def test_check_transition_pure():
    check_transition(OutputKind.TEXT, S.GENERATED, S.ENQUEUED, E.UNKNOWN)
