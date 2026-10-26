"""CC-2／CC-5 測試共用工廠。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from uep.context.snapshot import ContextSnapshot
from uep.ids import new_id
from uep.output.delivery import DeliveryRecord
from uep.output.intent import OutputIntent

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def make_snapshot(**overrides: Any) -> ContextSnapshot:
    data: dict[str, Any] = {
        "snapshot_id": new_id("snap"),
        "context_revision": 1,
        "purpose": "conversation_turn",
        "persona_id": "persona_main",
        "persona_version": 1,
        "policy_version": 1,
        "conversation_id": new_id("conv"),
        "segment_id": new_id("seg"),
        "turn_id": new_id("turn"),
        "audience_scope": "scope_private_owner",
        "actor_ref": "actor_owner",
        "history_window": {"from_turn": None, "to_turn": None},
        "memory_status": "ok",
        "excluded_summary": {},
        "capabilities": {"text_stream": True},
        "data_egress": {"providers": ["fake"]},
        "token_budget": {"max": 4000, "per_section": {}},
        "history_owner": "core_projection",
        "built_at": NOW,
    }
    data.update(overrides)
    return ContextSnapshot.model_validate(data)


def make_intent(**overrides: Any) -> OutputIntent:
    data: dict[str, Any] = {
        "output_id": new_id("out"),
        "sequence": 0,
        "kind": "text",
        "origin": "persona",
        "persona_id": "persona_main",
        "endpoint_id": "ep_desktop",
        "lease_id": new_id("lease"),
        "fencing_epoch": 1,
        "core_epoch": 1,
        "presence_generation": 1,
        "turn_id": new_id("turn"),
        "turn_generation": 5,
        "attempt_id": "attempt_1",
        "audience_scope": "scope_private_owner",
        "content_ref": "blob_text_1",
        "valid_until": NOW + timedelta(seconds=30),
    }
    data.update(overrides)
    return OutputIntent.model_validate(data)


def make_delivery(intent: OutputIntent, **overrides: Any) -> DeliveryRecord:
    data: dict[str, Any] = {
        "delivery_id": new_id("dlv"),
        "output_id": intent.output_id,
        "sequence": intent.sequence,
        "kind": intent.kind,
        "dispatch_id": new_id("dsp"),
        "sent_at": NOW,
    }
    data.update(overrides)
    return DeliveryRecord.model_validate(data)
