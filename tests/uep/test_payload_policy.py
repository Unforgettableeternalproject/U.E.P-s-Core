"""payload／legacy_ref／fence 約束（E-11、D33）；不需要資料庫。"""
import pytest
from pydantic import ValidationError

from uep.events.envelope import (
    EventDraft,
    PayloadPolicyError,
    TrustedContext,
    build_event,
)
from uep.events.registry import DeliveryClass, EventRegistry, EventSpec

PRIVATE = TrustedContext(source="core.task", actor_id="core", scope_ref="scope_private_owner")
PUBLIC = TrustedContext(source="adapter.web", actor_id="guest", scope_ref="scope_public", external=True)


def test_private_text_in_payload_rejected_by_key():
    with pytest.raises(PayloadPolicyError):
        build_event(EventDraft(event_type="input.text_final", payload={"text": "我今天很難過"}), PRIVATE)


def test_long_string_rejected_in_any_scope():
    long = "字" * 257
    with pytest.raises(PayloadPolicyError):
        build_event(EventDraft(event_type="turn.completed", payload={"status": long}), PRIVATE)
    with pytest.raises(PayloadPolicyError):
        build_event(
            EventDraft(event_type="input.text_final", idempotency_key="k", payload={"x": {"y": [long]}}),
            PUBLIC,
        )


def test_private_whitelisted_small_payload_ok():
    event = build_event(
        EventDraft(event_type="turn.completed", payload={"status": "ok", "turn_id": "turn_x", "count": 3}),
        PRIVATE,
    )
    assert event.payload["status"] == "ok"


def test_per_type_whitelist_override():
    registry = EventRegistry({"x.done": EventSpec(DeliveryClass.DURABLE, payload_keys=frozenset({"note"}))})
    build_event(EventDraft(event_type="x.done", payload={"note": "ok"}), PRIVATE, registry)
    with pytest.raises(PayloadPolicyError):
        build_event(EventDraft(event_type="x.done", payload={"status": "ok"}), PRIVATE, registry)


def test_legacy_ref_and_fence_constrained():
    EventDraft(event_type="turn.started", legacy_ref={"gs_id": "gs_1", "turn_n": 3},
               fence={"lease_id": "lease_x", "core_epoch": 2})
    bad_cases = (
        {"legacy_ref": {"note": "x"}},
        {"legacy_ref": {"gs_id": "x" * 200}},
        {"legacy_ref": {"gs_id": ["a"]}},
        {"fence": {"text": "x"}},
        {"fence": {"lease_id": {"a": 1}}},
    )
    for bad in bad_cases:
        with pytest.raises(ValidationError):
            EventDraft(event_type="turn.started", **bad)


def test_empty_idempotency_key_rejected():
    with pytest.raises(ValidationError):
        EventDraft(event_type="input.text_final", idempotency_key="")
    EventDraft(event_type="input.text_final", idempotency_key=None)
