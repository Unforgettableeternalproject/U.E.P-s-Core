"""P3 S1：新增 kind 與 payload 白名單、記憶端範例草稿轉譯（P3接入方案 §2.2、§5 S1）。

純註冊表／信封測試不需資料庫；journal 相關案例需 PostgreSQL（未設 UEP_TEST_DATABASE_URL 時 skip）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.uep.memory_translate_support import load_samples, translate
from uep.assembly import DEFAULT_PROPOSERS
from uep.events import journal
from uep.events.bus import EventBus
from uep.events.envelope import (
    EventDraft,
    PayloadPolicyError,
    TrustedContext,
    build_event,
)
from uep.events.publisher import EventPublisher
from uep.events.registry import DEFAULT_REGISTRY, DeliveryClass, UnregisteredEventType
from uep.state.coordinator import Axis

SAMPLES = load_samples()
ALL_SAMPLES = SAMPLES["memory"] + SAMPLES["candidate"]
PRIVATE = TrustedContext(source="core.turn", actor_id="core", scope_ref="scope_private_owner")

NEW_KINDS = (
    "turn.attempt_started", "turn.skipped", "conversation.participant_joined",
    "conversation.participant_left", "proactive.trigger_observed",
    "memory.dream_completed", "memory.reflection_completed", "memory.hypothesis_supported",
    "memory.hypothesis_contradicted", "memory.contradiction_found", "memory.episode_flagged",
    "memory.self_observation_proposed",
)


def _sample_id(raw):
    return raw["idempotency_key"][:28]


def test_all_new_kinds_registered_durable():
    for kind in NEW_KINDS:
        assert DEFAULT_REGISTRY.lookup(kind).delivery_class is DeliveryClass.DURABLE


def test_turn_cancelled_unchanged_and_no_extra_cancel_kind():
    with pytest.raises(UnregisteredEventType):
        DEFAULT_REGISTRY.lookup("turn.cancel_requested")
    spec = DEFAULT_REGISTRY.lookup("turn.cancelled")
    assert "reason_code" in spec.payload_keys  # 取消原因以 reason_code 區分


def test_unregistered_kind_rejected():
    for bad in ("memory.dreamt_up", "memory.dream_complete", "turn.attempt_finished"):
        with pytest.raises(UnregisteredEventType):
            build_event(EventDraft(event_type=bad, idempotency_key="k"), PRIVATE)


def test_proposers_include_turn_and_decision_on_interaction_only():
    assert {"core.turn", "core.decision"} <= DEFAULT_PROPOSERS[Axis.INTERACTION]
    assert not {"core.turn", "core.decision"} & DEFAULT_PROPOSERS[Axis.BEHAVIOR]


def test_private_payload_whitelist_for_new_kinds():
    ok = EventDraft(event_type="turn.attempt_started", payload={"attempt_id": "attempt_2", "reason_code": "regen"})
    assert build_event(ok, PRIVATE).payload["attempt_id"] == "attempt_2"
    for event_type, bad in (
        ("turn.attempt_started", {"text": "x"}),
        ("turn.skipped", {"summary": "x"}),
        ("conversation.participant_joined", {"display_name": "x"}),
        ("memory.dream_completed", {"summary": "x"}),
        ("proactive.trigger_observed", {"summary": "x"}),
        ("proactive.trigger_observed", {"expires_at": "2026-11-05T00:00:00+00:00"}),  # 信封欄位，不入 payload
    ):
        with pytest.raises(PayloadPolicyError):
            build_event(EventDraft(event_type=event_type, payload=bad), PRIVATE)


@pytest.mark.parametrize("raw", ALL_SAMPLES, ids=_sample_id)
def test_echo_memory_samples_pass_gateway_rebuild(raw):
    draft, trusted = translate(raw)
    event = build_event(draft, trusted)
    assert event.event_type == raw["event_type"] and event.scope_ref == raw["scope_ref"]
    assert event.source == "core.memory" and event.actor_id == "uep:memory"
    assert "summary" not in event.payload and "expires_at" not in event.payload


def test_untranslated_candidate_is_rejected_with_summary_in_payload():
    """未轉譯（summary 仍在 payload）的候選草稿不得進私人 scope。"""
    raw = SAMPLES["candidate"][0]
    draft = EventDraft(event_type=raw["event_type"], idempotency_key=raw["idempotency_key"], payload=raw["payload"])
    with pytest.raises(PayloadPolicyError):
        build_event(draft, translate(raw)[1])


def test_candidate_expires_at_moves_to_envelope():
    draft, trusted = translate(SAMPLES["candidate"][0])
    event = build_event(draft, trusted)
    assert event.expires_at is not None and event.expires_at.year == 2026


# ---- journal（需 PostgreSQL）----

@pytest.fixture
def publisher(session_factory):
    return EventPublisher(session_factory, EventBus())


@pytest.mark.parametrize("raw", ALL_SAMPLES, ids=_sample_id)
def test_samples_accepted_and_resend_same_key_is_duplicate(publisher, raw):
    draft, trusted = translate(raw)
    first = publisher.publish(draft, trusted)
    again = publisher.publish(draft, trusted)
    assert not first.duplicate and again.duplicate
    assert again.event.event_id == first.event.event_id


def test_summary_never_reaches_journal_but_lives_in_deletable_blob(session_factory, publisher):
    raw = SAMPLES["candidate"][0]
    summary = raw["payload"]["summary"]
    draft, trusted = translate(raw)
    event = publisher.publish(draft, trusted).event
    assert event.payload_ref and event.payload_hash
    with session_factory() as s:
        dump = s.execute(text("SELECT to_jsonb(e)::text FROM events e")).scalar_one()
        assert summary not in dump and "summary" not in dump
        assert journal.read_blob(s, event.payload_ref) == summary.encode("utf-8")
    with session_factory() as s, s.begin():
        assert journal.delete_blob(s, event.payload_ref, "使用者要求刪除") == 1
    with session_factory() as s:
        assert journal.read_blob(s, event.payload_ref) is None


def test_unregistered_kind_not_journaled(session_factory, publisher):
    with pytest.raises(UnregisteredEventType):
        publisher.publish(EventDraft(event_type="memory.unknown_kind", idempotency_key="k"), PRIVATE)
    with session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM events")).scalar_one() == 0
