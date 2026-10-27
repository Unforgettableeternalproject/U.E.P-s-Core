"""
Evidence Schema - 證據單位（EvidenceUnit）與來源契約

EvidenceUnit 只記錄「發生過什麼、誰說的、有沒有說出口」，不記錄「這是不是事實」。
來源由呼叫端以結構欄位宣告；引擎不得從文字內容推測來源（不使用 keyword / regex）。
EvidenceUnit 不可變：要修正就追加新 unit，不原地改寫。
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SOURCE_TYPES = ("user", "agent", "tool", "reflection", "dream")
DELIVERY_STATES = ("delivered", "partial", "not_delivered", "unknown")
PROVENANCE_STATUSES = ("known", "legacy_unknown")
# 內容真正的出處（僅 source_type="tool" 的 unit 使用）
CONTENT_ORIGINS = ("user", "agent", "tool", "unknown")
DELIVERY_UNITS = ("sentence", "char")

EVIDENCE_SCHEMA_VERSION = 1


def compute_content_hash(content: str) -> str:
    """sha256(NFC(content))。"""
    return hashlib.sha256(unicodedata.normalize("NFC", content).encode("utf-8")).hexdigest()


def _dt_to_str(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt_from_str(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class TextSpan:
    """content 在「原始來源文字」中的字元區間（半開 [start, end)）。"""

    start: int
    end: int
    total: int | None = None  # 原始來源文字總長（未知則 None）

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "total": self.total}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TextSpan:
        return cls(start=data["start"], end=data["end"], total=data.get("total"))


@dataclass(frozen=True)
class SourceRef:
    """來源指標。呼叫端自填，引擎只存不解讀。"""

    kind: str  # discord_message | discord_voice | tool_call | mcp | cli | episode | legacy
    channel: str | None = None  # origin：voice | dm | ...
    ref_ids: tuple[str, ...] = ()  # 訊息 id／音訊片段 id／tool call id
    name: str | None = None  # tool_call 時為工具名

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "channel": self.channel,
            "ref_ids": list(self.ref_ids),
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceRef:
        return cls(
            kind=data["kind"],
            channel=data.get("channel"),
            ref_ids=tuple(data.get("ref_ids") or ()),
            name=data.get("name"),
        )


def _validate_fields(
    source_type: str,
    delivery_state: str,
    delivery_unit: str | None,
    content_origin: str | None,
) -> None:
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"source_type 必須是 {SOURCE_TYPES} 之一，收到 {source_type!r}")
    if delivery_state not in DELIVERY_STATES:
        raise ValueError(f"delivery_state 必須是 {DELIVERY_STATES} 之一，收到 {delivery_state!r}")
    if delivery_unit is not None and delivery_unit not in DELIVERY_UNITS:
        raise ValueError(f"delivery_unit 必須是 {DELIVERY_UNITS} 之一，收到 {delivery_unit!r}")
    if content_origin is not None:
        if content_origin not in CONTENT_ORIGINS:
            raise ValueError(
                f"content_origin 必須是 {CONTENT_ORIGINS} 之一，收到 {content_origin!r}"
            )
        if source_type != "tool":
            raise ValueError("content_origin 只能用在 source_type='tool' 的 unit")


@dataclass(frozen=True)
class EvidenceUnit:
    """不可變的證據單位。"""

    # 身分與位置
    id: str
    episode_id: str | None
    source_type: str
    content: str  # 逐字原文，不摘要、不潤飾
    # 時間
    occurred_at: datetime
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # 說話者與來源
    speaker_id: str | None = None
    source_ref: SourceRef | None = None
    text_span: TextSpan | None = None
    # 交付狀態（主要對 source_type="agent" 有意義）
    delivery_state: str = "delivered"
    delivery_unit: str | None = None
    delivered_units: int | None = None  # None＝進度未知
    total_units: int | None = None
    # 範圍：來源範圍字串（namespace／session:<id>／channel:<origin>），只記錄不強制
    scope: str = "namespace"
    provenance_status: str = "known"
    # 工具操作與內容出處分離（僅 source_type="tool"）：
    # source_type="tool" 描述「操作」；content_origin 描述「內容真正出自誰」。
    content_origin: str | None = None
    origin_evidence_ids: tuple[str, ...] = ()
    # M2 預留
    content_hash: str = ""
    derived_from: tuple[str, ...] = ()
    tombstoned_at: datetime | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    schema_version: int = EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_fields(
            self.source_type, self.delivery_state, self.delivery_unit, self.content_origin
        )
        if self.provenance_status not in PROVENANCE_STATUSES:
            raise ValueError(f"provenance_status 無效: {self.provenance_status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "episode_id": self.episode_id,
            "source_type": self.source_type,
            "content": self.content,
            "occurred_at": _dt_to_str(self.occurred_at),
            "recorded_at": _dt_to_str(self.recorded_at),
            "speaker_id": self.speaker_id,
            "source_ref": self.source_ref.to_dict() if self.source_ref else None,
            "text_span": self.text_span.to_dict() if self.text_span else None,
            "delivery_state": self.delivery_state,
            "delivery_unit": self.delivery_unit,
            "delivered_units": self.delivered_units,
            "total_units": self.total_units,
            "scope": self.scope,
            "provenance_status": self.provenance_status,
            "content_origin": self.content_origin,
            "origin_evidence_ids": list(self.origin_evidence_ids),
            "content_hash": self.content_hash,
            "derived_from": list(self.derived_from),
            "tombstoned_at": _dt_to_str(self.tombstoned_at),
            "attrs": dict(self.attrs),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceUnit:
        ref = data.get("source_ref")
        span = data.get("text_span")
        return cls(
            id=data["id"],
            episode_id=data.get("episode_id"),
            source_type=data["source_type"],
            content=data["content"],
            occurred_at=_dt_from_str(data["occurred_at"]),
            recorded_at=_dt_from_str(data.get("recorded_at")) or datetime.now(timezone.utc),
            speaker_id=data.get("speaker_id"),
            source_ref=SourceRef.from_dict(ref) if ref else None,
            text_span=TextSpan.from_dict(span) if span else None,
            delivery_state=data.get("delivery_state", "unknown"),
            delivery_unit=data.get("delivery_unit"),
            delivered_units=data.get("delivered_units"),
            total_units=data.get("total_units"),
            scope=data.get("scope", "namespace"),
            # 缺值時保守降級，不升為 known
            provenance_status=data.get("provenance_status", "legacy_unknown"),
            content_origin=data.get("content_origin"),
            origin_evidence_ids=tuple(data.get("origin_evidence_ids") or ()),
            content_hash=data.get("content_hash", ""),
            derived_from=tuple(data.get("derived_from") or ()),
            tombstoned_at=_dt_from_str(data.get("tombstoned_at")),
            attrs=dict(data.get("attrs") or {}),
            schema_version=data.get("schema_version", EVIDENCE_SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class EvidenceInput:
    """呼叫端宣告用；id、episode_id、recorded_at、content_hash、provenance_status 由引擎填。"""

    source_type: str
    content: str
    speaker_id: str | None = None
    occurred_at: datetime | None = None
    source_ref: SourceRef | None = None
    text_span: TextSpan | None = None
    delivery_state: str = "delivered"
    delivery_unit: str | None = None
    delivered_units: int | None = None
    total_units: int | None = None
    scope: str = "namespace"
    # 僅 source_type="tool"：None 代表呼叫端沒說明出處，引擎記為 "unknown"
    content_origin: str | None = None
    origin_evidence_ids: tuple[str, ...] = ()
    derived_from: tuple[str, ...] = ()
    attrs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_fields(
            self.source_type, self.delivery_state, self.delivery_unit, self.content_origin
        )
        if self.source_type != "agent" and self.delivery_state != "delivered":
            raise ValueError("只有 agent unit 可以是 delivered 以外的 delivery_state")
        if self.source_type != "tool" and self.origin_evidence_ids:
            raise ValueError("origin_evidence_ids 只能用在 source_type='tool' 的 unit")

    def to_unit(
        self,
        *,
        unit_id: str,
        episode_id: str | None,
        recorded_at: datetime,
        provenance_status: str = "known",
    ) -> EvidenceUnit:
        content_origin = self.content_origin
        if self.source_type == "tool" and content_origin is None:
            content_origin = "unknown"  # 未說明出處不得推定為 user
        return EvidenceUnit(
            id=unit_id,
            episode_id=episode_id,
            source_type=self.source_type,
            content=self.content,
            occurred_at=self.occurred_at or recorded_at,
            recorded_at=recorded_at,
            speaker_id=self.speaker_id,
            source_ref=self.source_ref,
            text_span=self.text_span,
            delivery_state=self.delivery_state,
            delivery_unit=self.delivery_unit,
            delivered_units=self.delivered_units,
            total_units=self.total_units,
            scope=self.scope,
            provenance_status=provenance_status,
            content_origin=content_origin,
            origin_evidence_ids=tuple(self.origin_evidence_ids),
            content_hash=compute_content_hash(self.content),
            derived_from=tuple(self.derived_from),
            attrs={
                **self.attrs,
                **({"occurred_at_fallback": True} if self.occurred_at is None else {}),
            },
        )


# ─── 衍生判斷（純函式，不入庫，避免旗標過期）──────────────────────────────


def can_attest_user_claim(u: EvidenceUnit) -> bool:
    """該 unit 是否可作為「使用者確實說過這句話」的證據。"""
    return (
        u.source_type == "user"
        and u.provenance_status == "known"
        and u.delivery_state == "delivered"
        and u.tombstoned_at is None
    )


def can_attest_agent_said(u: EvidenceUnit) -> bool:
    """該 unit 是否可作為「Agent 確實說出口」的證據。

    partial 且進度未知、not_delivered、unknown 皆不算已交付。
    """
    return (
        u.source_type == "agent"
        and u.provenance_status == "known"
        and (
            u.delivery_state == "delivered"
            or (u.delivery_state == "partial" and u.delivered_units is not None)
        )
        and u.tombstoned_at is None
    )


def is_delivered(u: EvidenceUnit) -> bool:
    """該 unit 的內容是否確實已交付（agent partial 須有進度；其餘以 delivery_state 為準）。"""
    if u.tombstoned_at is not None:
        return False
    if u.delivery_state == "delivered":
        return True
    return u.delivery_state == "partial" and u.delivered_units is not None


def is_undelivered_agent(u: EvidenceUnit) -> bool:
    """agent unit 明確為「未說出口」：not_delivered，或被打斷但進度未知。

    後者的 content 可能是完整預定回覆，不可當成 Agent 說過。
    delivery_state="unknown"（舊資料）不在此列：無從得知，維持既有顯示行為。
    """
    return u.source_type == "agent" and (
        u.delivery_state == "not_delivered"
        or (u.delivery_state == "partial" and u.delivered_units is None)
    )
