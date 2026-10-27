"""UEPEvent 信封（契約 §1.1）與 Gateway 重建（E-3、E-6）。

產生者只能提供 ``EventDraft``；信任欄位（source、actor_id、persona_id、scope_ref、
priority、delivery_class）一律由 ``TrustedContext`` 與註冊表決定，payload 內同名鍵
只當作不被信任的資料保留。
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uep.events.registry import DEFAULT_REGISTRY, DeliveryClass, EventRegistry, Priority
from uep.ids import is_valid_id, new_id

_SCOPE_PATTERN = re.compile(r"scope_(private|group|public)(_[A-Za-z0-9]+)*")
_SAFE_REF_PATTERN = re.compile(r"[A-Za-z0-9_:.\-]{1,128}")  # 外部 trace／聚合鍵等：安全字元集，防止夾帶私人文字
MAX_IDEMPOTENCY_KEY = 256
_SCOPE_RANK = {"private": 0, "group": 1, "public": 2}

MAX_PAYLOAD_STR = 256  # payload 內任何字串的長度上限；超長視為可能夾帶原文
MAX_ID_STR = 128
LEGACY_REF_KEYS = frozenset({"gs_id", "cs_id", "ws_id", "cycle_index", "turn_n", "step_n", "memory_token"})
FENCE_KEYS = frozenset(
    {"persona_id", "endpoint_id", "lease_id", "fencing_epoch", "core_epoch",
     "presence_generation", "turn_generation"}
)


class PayloadPolicyError(ValueError):
    """payload 不符合小型非私人結構化內容的限制（E-11、D33）。"""


def _check_scalar_tree(value: Any, path: str, depth: int = 0) -> None:
    if depth > 4:
        raise PayloadPolicyError(f"{path} 巢狀過深")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if len(value) > MAX_PAYLOAD_STR:
            raise PayloadPolicyError(f"{path} 字串超過 {MAX_PAYLOAD_STR} 字，私人文字請走 payload_blob")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check_scalar_tree(item, f"{path}[{i}]", depth + 1)
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or len(k) > MAX_ID_STR:
                raise PayloadPolicyError(f"{path} 含不合法的鍵")
            _check_scalar_tree(v, f"{path}.{k}", depth + 1)
        return
    raise PayloadPolicyError(f"{path} 型別不允許：{type(value).__name__}")


def validate_payload(payload: dict[str, Any], scope_ref: str, allowed_keys: frozenset[str]) -> None:
    """所有 scope：字串長度與型別限制；私人 scope：鍵必須在白名單內。"""
    _check_scalar_tree(payload, "payload")
    if scope_rank(scope_ref) == 0:
        extra = sorted(set(payload) - allowed_keys)
        if extra:
            raise PayloadPolicyError(f"私人事件的 payload 含非白名單鍵：{extra}")


def _validate_id_map(value: dict[str, Any] | None, allowed: frozenset[str], name: str) -> dict[str, Any] | None:
    if value is None:
        return value
    extra = sorted(set(value) - allowed)
    if extra:
        raise ValueError(f"{name} 含不允許的鍵：{extra}")
    for k, v in value.items():
        if isinstance(v, bool) or not (v is None or isinstance(v, (int, str))):
            raise ValueError(f"{name}.{k} 只允許字串或整數")
        if isinstance(v, str) and len(v) > MAX_ID_STR:
            raise ValueError(f"{name}.{k} 過長")
    return value


class IdempotencyConflict(ValueError):
    """(source, idempotency_key) 命中既有事件，但 event_type／scope／aggregate／payload 內容不一致（M1）。"""


class ScopeWideningError(ValueError):
    """衍生事件的 scope 比起因事件寬（E-6）。"""


def scope_rank(scope_ref: str) -> int:
    match = _SCOPE_PATTERN.fullmatch(scope_ref)
    if not match:
        raise ValueError(f"scope_ref 格式不合法：{scope_ref!r}")
    return _SCOPE_RANK[match.group(1)]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EventDraft(BaseModel):
    """產生者可填的欄位。信任欄位不在此，extra=forbid 使其無法被夾帶。"""

    model_config = ConfigDict(extra="forbid")

    event_type: str
    schema_version: int = 1
    occurred_at: datetime = Field(default_factory=_utcnow)
    conversation_id: str | None = None
    segment_id: str | None = None
    activity_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    legacy_ref: dict[str, Any] | None = None
    trace_id: str | None = None
    causation_id: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1)
    aggregate_type: str | None = None
    aggregate_id: str | None = None
    aggregate_version: int | None = None
    expires_at: datetime | None = None
    fence: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_id", "activity_id", "run_id", "conversation_id", "segment_id", "turn_id")
    @classmethod
    def _stable_id(cls, v):
        if v is not None and not is_valid_id(v):
            raise ValueError("ID 欄位必須是 前綴_ULID 格式")
        return v

    @field_validator("trace_id", "causation_id", "aggregate_id")
    @classmethod
    def _safe_ref(cls, v):
        if v is not None and not _SAFE_REF_PATTERN.fullmatch(v):
            raise ValueError("欄位只允許 [A-Za-z0-9_:.-]、長度 1～128")
        return v

    @field_validator("idempotency_key")
    @classmethod
    def _idempotency_key(cls, v):
        if v is not None and not (
            len(v) <= MAX_IDEMPOTENCY_KEY and re.fullmatch(r"[A-Za-z0-9_:.\-]+", v)
        ):
            raise ValueError(f"idempotency_key 只允許 [A-Za-z0-9_:.-]、長度 1～{MAX_IDEMPOTENCY_KEY}")
        return v

    @field_validator("legacy_ref")
    @classmethod
    def _legacy_ref(cls, v):
        return _validate_id_map(v, LEGACY_REF_KEYS, "legacy_ref")

    @field_validator("fence")
    @classmethod
    def _fence(cls, v):
        return _validate_id_map(v, FENCE_KEYS, "fence")

    # 含私人原文的內容：由 publisher 寫入可刪 blob，不進 journal（E-11）
    payload_blob: bytes | None = Field(default=None, exclude=True, repr=False)


class TrustedContext(BaseModel):
    """Gateway 認證後的來源資訊。"""

    model_config = ConfigDict(extra="forbid")

    source: str
    actor_id: str
    scope_ref: str
    persona_id: str = "uep"
    external: bool = False  # 外部來源（Adapter／平台／worker）必須帶 idempotency_key
    parent_scope_ref: str | None = None  # 起因事件的 scope，用於 E-6 檢查

    @model_validator(mode="after")
    def _check_scope(self) -> TrustedContext:
        child = scope_rank(self.scope_ref)
        if self.parent_scope_ref is not None and child > scope_rank(self.parent_scope_ref):
            raise ScopeWideningError(
                f"scope 不得比起因事件寬：{self.parent_scope_ref} -> {self.scope_ref}"
            )
        return self


class UEPEvent(BaseModel):
    """完整信封。journal_seq／received_at 於落地後由 DB 填入。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    event_id: str
    event_type: str
    delivery_class: DeliveryClass
    occurred_at: datetime
    received_at: datetime | None = None
    journal_seq: int | None = None
    source: str
    persona_id: str
    actor_id: str
    conversation_id: str | None = None
    segment_id: str | None = None
    activity_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    legacy_ref: dict[str, Any] | None = None
    trace_id: str
    causation_id: str | None = None
    idempotency_key: str | None = None
    aggregate_type: str | None = None
    aggregate_id: str | None = None
    aggregate_version: int | None = None
    priority: Priority
    expires_at: datetime | None = None
    scope_ref: str
    fence: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_ref: str | None = None
    payload_hash: str | None = None
    tombstone_at: datetime | None = None

    @model_validator(mode="after")
    def _check_ids(self) -> UEPEvent:
        if not is_valid_id(self.event_id, "evt"):
            raise ValueError(f"event_id 格式不合法：{self.event_id!r}")
        return self


def build_event(
    draft: EventDraft,
    trusted: TrustedContext,
    registry: EventRegistry = DEFAULT_REGISTRY,
) -> UEPEvent:
    """Gateway 重建：驗證 draft，補上信任欄位與 ID。"""
    spec = registry.lookup(draft.event_type)  # 未註冊 -> UnregisteredEventType

    if trusted.external and not draft.idempotency_key:
        raise ValueError("外部來源事件必須提供 idempotency_key")
    agg = (draft.aggregate_type, draft.aggregate_id, draft.aggregate_version)
    if any(v is not None for v in agg) and any(v is None for v in agg):
        raise ValueError("aggregate_type／aggregate_id／aggregate_version 必須同時提供")

    validate_payload(draft.payload, trusted.scope_ref, spec.payload_keys)

    return UEPEvent(
        schema_version=draft.schema_version,
        event_id=new_id("evt"),
        event_type=draft.event_type,
        delivery_class=spec.delivery_class,
        occurred_at=draft.occurred_at,
        source=trusted.source,
        persona_id=trusted.persona_id,
        actor_id=trusted.actor_id,
        conversation_id=draft.conversation_id,
        segment_id=draft.segment_id,
        activity_id=draft.activity_id,
        task_id=draft.task_id,
        run_id=draft.run_id,
        turn_id=draft.turn_id,
        legacy_ref=draft.legacy_ref,
        trace_id=draft.trace_id or new_id("trc"),
        causation_id=draft.causation_id,
        idempotency_key=draft.idempotency_key,
        aggregate_type=draft.aggregate_type,
        aggregate_id=draft.aggregate_id,
        aggregate_version=draft.aggregate_version,
        priority=spec.priority,
        expires_at=draft.expires_at,
        scope_ref=trusted.scope_ref,
        fence=draft.fence,
        payload=draft.payload,
    )
