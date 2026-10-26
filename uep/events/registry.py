"""事件類型註冊表（契約 §1.3）。delivery_class 與預設 priority 由此查得，不信任外部自稱。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeliveryClass(StrEnum):
    DURABLE = "durable"
    INTERACTION = "interaction"
    CONTROL = "control"
    TELEMETRY = "telemetry"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


# payload 預設允許的鍵：ID、狀態、計數、結果碼等小型結構化值（契約 §1.1：payload 不含私人原文）
DEFAULT_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "status", "reason_code", "result_code", "count", "len", "version", "kind",
        "ref_id", "action_id", "task_id", "run_id", "step_id", "turn_id",
        "segment_id", "conversation_id", "activity_id", "lease_id", "delivery_id",
    }
)


@dataclass(frozen=True)
class EventSpec:
    delivery_class: DeliveryClass
    priority: Priority = Priority.NORMAL
    # 私人 scope 的事件，payload 鍵必須在此白名單內
    payload_keys: frozenset[str] = DEFAULT_PAYLOAD_KEYS


class UnregisteredEventType(ValueError):
    """event_type 不在註冊表內（契約：被拒或進 dead letter）。"""


def _spec(cls: DeliveryClass, *types: str) -> dict[str, EventSpec]:
    return {t: EventSpec(cls) for t in types}


_D, _I, _C = DeliveryClass.DURABLE, DeliveryClass.INTERACTION, DeliveryClass.CONTROL

DEFAULT_EVENT_SPECS: dict[str, EventSpec] = {
    **_spec(_I, "input.text_final", "input.utterance_final", "input.attachment_provided"),
    **_spec(_D, "turn.started", "turn.generation_completed", "turn.completed", "turn.cancelled"),
    **_spec(
        _D,
        "conversation.episode_started", "conversation.episode_ended",
        "conversation.segment_started", "conversation.segment_ended",
    ),
    **_spec(_D, "state.transition_requested", "state.changed"),
    **_spec(
        _D,
        "task.created", "task.completed", "task.failed", "task.cancelled",
        "task.input_requested", "task.input_provided",
        "run.started", "run.ended", "step.completed",
    ),
    **_spec(_D, "action.intended", "action.approved", "action.executed", "action.unknown"),
    **_spec(
        _D,
        "presence.arrival_requested", "presence.departure_requested",
        "presence.transfer_requested", "presence.granted", "presence.activated",
        "presence.quiesced", "presence.revoked", "presence.recovering",
    ),
    **_spec(_D, "output.intent_created", "output.admitted", "output.rejected", "delivery.reported"),
    **_spec(_C, "control.stop", "control.abort", "control.revoke"),
    **_spec(_D, "memory.record_created", "memory.consolidated"),
    **_spec(_D, "proactive.trigger_observed", "proactive.candidate_decided"),
    **_spec(_D, "core.started", "core.ready", "core.stopping", "core.component_health_changed"),
}


class EventRegistry:
    """event_type -> EventSpec；可在測試或後續契約擴充。"""

    def __init__(self, specs: dict[str, EventSpec] | None = None):
        self._specs = dict(DEFAULT_EVENT_SPECS if specs is None else specs)

    def register(self, event_type: str, spec: EventSpec) -> None:
        self._specs[event_type] = spec

    def lookup(self, event_type: str) -> EventSpec:
        try:
            return self._specs[event_type]
        except KeyError:
            raise UnregisteredEventType(f"未註冊的事件類型：{event_type!r}") from None


DEFAULT_REGISTRY = EventRegistry()
