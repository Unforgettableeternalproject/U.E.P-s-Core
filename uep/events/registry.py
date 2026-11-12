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
        "segment_id", "conversation_id", "activity_id", "lease_id", "delivery_id", "resumed",
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
        "presence.quiesced", "presence.revoked", "presence.recovering", "presence.recovered",
    ),
    **_spec(_D, "output.intent_created", "output.admitted", "output.rejected", "delivery.reported"),
    **_spec(_C, "control.stop", "control.abort", "control.revoke"),
    **_spec(_D, "memory.record_created", "memory.consolidated"),
    **_spec(_D, "proactive.trigger_observed", "proactive.candidate_decided"),
    **_spec(_D, "core.started", "core.ready", "core.stopping", "core.component_health_changed"),
}

# CC-7 state.* 事件的 payload 鍵（契約 6A.2：{axis, from, to, version, cause_event_id}）；全域停止事件（D36）
_STATE_KEYS = DEFAULT_PAYLOAD_KEYS | {"axis", "from_state", "to_state", "cause_event_id", "subject"}
DEFAULT_EVENT_SPECS["state.transition_requested"] = EventSpec(_D, payload_keys=_STATE_KEYS)
DEFAULT_EVENT_SPECS["state.changed"] = EventSpec(_D, payload_keys=_STATE_KEYS)
DEFAULT_EVENT_SPECS["core.global_stop_engaged"] = EventSpec(_D, Priority.URGENT)
DEFAULT_EVENT_SPECS["core.global_stop_released"] = EventSpec(_D)

# P3 新增 kind（P3接入方案 §2.2、D81）。私人 scope 的 payload 只收下列鍵，`summary` 等自由文字不在內（D78）
# 疊加在預設鍵上（方案 §2.2「新增」語意）；summary、expires_at 一律扣除（D78）
_FORBIDDEN_KEYS = frozenset({"summary", "expires_at"})


def _keys(*extra: str) -> frozenset[str]:
    return (DEFAULT_PAYLOAD_KEYS | frozenset(extra)) - _FORBIDDEN_KEYS


_MEMORY_KEYS = _keys("evidence_refs", "derived_from", "weight", "privacy_scope", "schema_version")
MEMORY_EVENT_TYPES: tuple[str, ...] = tuple(
    f"memory.{kind}" for kind in (
        "dream_completed", "reflection_completed", "hypothesis_supported", "hypothesis_contradicted",
        "contradiction_found", "episode_flagged", "self_observation_proposed",
    )
)
for _t in MEMORY_EVENT_TYPES:
    DEFAULT_EVENT_SPECS[_t] = EventSpec(_D, payload_keys=_MEMORY_KEYS)
# expires_at 對映信封欄位、summary 轉入可刪 payload_ref blob，兩者都不在白名單
DEFAULT_EVENT_SPECS["proactive.trigger_observed"] = EventSpec(
    _D,
    payload_keys=_keys(
        "trigger_kind", "dedupe_key", "source_idempotency_keys", "evidence_refs", "memory_weight",
        "confidence", "privacy_scope", "schema_version",
    ),
)
# 不新增 turn.cancelled kind：以 payload.reason_code 區分取消原因
DEFAULT_EVENT_SPECS["turn.attempt_started"] = EventSpec(_D, payload_keys=_keys("attempt_id"))
# V18 缺席端輸入 shadow 紀錄也走 turn.skipped：只存來源、身分與長度，不存原文
DEFAULT_EVENT_SPECS["turn.skipped"] = EventSpec(
    _D,
    payload_keys=_keys(
        "reply_mode", "proposal_ref", "utterance_ref", "speaker_ref", "actor_id", "source_location",
        "source_endpoint_id", "is_owner", "presence_request", "actor_authenticated", "presence_intent",
        "target_location",
    ),
)
# ES 轉錄進 journal（§1.5 (a)）：payload 只放 ID、語言、信心、毫秒統計與 speaker_ref（平台字串），
# 文字與 Utterance.metadata 的私人部分走 payload_blob；S3 補註冊（S1 當時只有預設鍵）
DEFAULT_EVENT_SPECS["input.utterance_final"] = EventSpec(
    _I, payload_keys=_keys("speaker_ref", "language", "confidence", "duration_ms", "utterance_seq")
)
_PARTICIPANT_KEYS = _keys("speaker_ref", "role")
DEFAULT_EVENT_SPECS["conversation.participant_joined"] = EventSpec(_D, payload_keys=_PARTICIPANT_KEYS)
DEFAULT_EVENT_SPECS["conversation.participant_left"] = EventSpec(_D, payload_keys=_PARTICIPANT_KEYS)
# module coordinator 事件（D108 R2；Module Coordinator 設計稿 §8）。鍵須與 ``uep.lifecycle.states`` 的
# ``STATE_CHANGED_KEYS``／``PROVISION_RESOLVED_KEYS`` 一致（由測試鎖住；events 不 import lifecycle 以免循環）
DEFAULT_EVENT_SPECS["module.state_changed"] = EventSpec(
    _D,
    payload_keys=_keys(
        "module_id", "revision", "epoch", "state", "prev_state", "readiness", "reason_code", "cause", "decision_id",
        "attempt",
    ),
)
DEFAULT_EVENT_SPECS["module.provision_resolved"] = EventSpec(
    _D,
    payload_keys=_keys("module_id", "decision_id", "plan_version", "action", "outcome", "reason_code"),
)

# 行程事件（PresencePlan 契約 §10；durable，source ``core.schedule``，不得用 ``presence.*``）。payload 只放 ID／列舉／數字，
# 不含自由文字與備註。override_*、slot_marked／unmarked、abandon_work 先只登記（等仲裁器接上才發布）
SCHEDULE_EVENT_KEYS: dict[str, frozenset[str]] = {
    "schedule.plan_changed": _keys("revision", "change_kind", "ref_id"),
    "schedule.slot_changed": _keys("slot_ref", "activity_kind", "source", "location", "plan_revision"),
    "schedule.slot_entered": _keys(
        "slot_ref", "activity_kind", "source", "location", "slot_state", "reason", "plan_revision"),
    "schedule.slot_left": _keys(
        "slot_ref", "activity_kind", "source", "location", "slot_state", "reason", "plan_revision"),
    "schedule.directive_issued": _keys("directive_id", "reason", "drop_reason", "next_eval_at"),
    "schedule.directive_dropped": _keys("directive_id", "reason", "drop_reason", "next_eval_at"),
    "schedule.divergence_observed": _keys("plan_location", "actual_location", "override_state", "override_id"),
    "schedule.override_started": _keys("override_id", "cause_request_id", "end_reason"),
    "schedule.override_ended": _keys("override_id", "cause_request_id", "end_reason"),
    "schedule.slot_marked": _keys("mark_id", "kind", "item_ref", "cause_request_id", "end_reason"),
    "schedule.slot_unmarked": _keys("mark_id", "kind", "item_ref", "cause_request_id", "end_reason"),
    "schedule.abandon_work": _keys(
        "signal_id", "item_ref", "result", "reason_code", "path", "decider_id", "decider_version"),
    # 仲裁判決落地（AR-3a）：只放 ID／列舉；executed＝這個判決的動作是否真的被執行
    "schedule.arbitration_decided": _keys(
        "source", "outcome", "band", "override_action", "transfer_target", "request_id", "directive_id", "executed",
        "exec_reason", "mark_written"),
}
for _t, _k in SCHEDULE_EVENT_KEYS.items():
    DEFAULT_EVENT_SPECS[_t] = EventSpec(_D, payload_keys=_k)


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
