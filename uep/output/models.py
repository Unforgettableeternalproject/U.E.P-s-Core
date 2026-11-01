"""output_intents／deliveries 資料表與存取（migration 0003_context_output）。

計畫輸出與實際交付分表（O-1）。output_intents 不可變（trigger 禁止 UPDATE／DELETE）；
deliveries 的狀態推進由 ``advance_delivery`` 在列鎖內依 CC-5 狀態機檢查，DB trigger 另擋終態改寫。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from uep.db.base import Base
from uep.output.delivery import DeliveryEvidence, DeliveryRecord, DeliveryStatus
from uep.output.intent import OutputIntent


class OutputIntentRow(Base):
    __tablename__ = "output_intents"
    __table_args__ = (
        UniqueConstraint("output_id", "sequence", "kind", name="uq_output_intents_output_sequence_kind"),
        Index("ix_output_intents_turn_id", "turn_id"),
    )

    output_id: Mapped[str] = mapped_column(String, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    origin: Mapped[str] = mapped_column(String, nullable=False)
    persona_id: Mapped[str] = mapped_column(String, nullable=False)
    endpoint_id: Mapped[str] = mapped_column(String, nullable=False)
    lease_id: Mapped[str] = mapped_column(String, nullable=False)
    fencing_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    core_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    presence_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activity_id: Mapped[str | None] = mapped_column(String)
    turn_id: Mapped[str | None] = mapped_column(String)
    turn_generation: Mapped[int | None] = mapped_column(BigInteger)
    attempt_id: Mapped[str | None] = mapped_column(String)
    audience_scope: Mapped[str] = mapped_column(String, nullable=False)
    content_ref: Mapped[str] = mapped_column(String, nullable=False)
    content_format: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_text_ref: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DeliveryRow(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("output_id", "sequence", name="uq_deliveries_output_sequence"),
        UniqueConstraint("dispatch_id", name="uq_deliveries_dispatch_id"),
        ForeignKeyConstraint(  # kind 必須與 intent 一致，避免用錯合法路徑
            ["output_id", "sequence", "kind"],
            ["output_intents.output_id", "output_intents.sequence", "output_intents.kind"],
            name="fk_deliveries_output_id_output_intents",
        ),
        ForeignKeyConstraint(
            ["spoken_text_ref"], ["payload_blobs.payload_ref"],
            name="fk_deliveries_spoken_text_ref_payload_blobs",
        ),
    )

    delivery_id: Mapped[str] = mapped_column(String, primary_key=True)
    output_id: Mapped[str] = mapped_column(String, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    dispatch_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    delivery_evidence: Mapped[str] = mapped_column(String, nullable=False)
    played_range: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    cancel_position: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    spoken_text_ref: Mapped[str | None] = mapped_column(String)
    spoken_text_hash: Mapped[str | None] = mapped_column(String)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String)  # 送出權（單一送出者，N2）
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


_INTENT_FIELDS = tuple(OutputIntent.model_fields)
_DELIVERY_FIELDS = tuple(DeliveryRecord.model_fields)


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def save_intent(session: Session, intent: OutputIntent) -> None:
    session.add(OutputIntentRow(**{k: _plain(getattr(intent, k)) for k in _INTENT_FIELDS}))
    session.flush()


def load_intent(session: Session, output_id: str, sequence: int) -> OutputIntent | None:
    row = session.get(OutputIntentRow, (output_id, sequence))
    if row is None:
        return None
    return OutputIntent.model_validate({k: getattr(row, k) for k in _INTENT_FIELDS})


def record_from_row(row: DeliveryRow) -> DeliveryRecord:
    return DeliveryRecord.model_validate({k: getattr(row, k) for k in _DELIVERY_FIELDS})


def create_delivery(session: Session, record: DeliveryRecord) -> None:
    """建立交付紀錄；初始只能是 generated。"""
    if record.status is not DeliveryStatus.GENERATED:
        raise ValueError("DeliveryRecord 初始狀態必須是 generated")
    session.add(DeliveryRow(**{k: _plain(getattr(record, k)) for k in _DELIVERY_FIELDS}))
    session.flush()


def load_delivery(session: Session, delivery_id: str, *, lock: bool = False) -> DeliveryRecord | None:
    """讀取交付紀錄；``lock=True`` 時以 FOR UPDATE（populate_existing）重新載入，並行回報據此序列化。"""
    if lock:
        row = session.execute(
            select(DeliveryRow)
            .where(DeliveryRow.delivery_id == delivery_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
    else:
        row = session.get(DeliveryRow, delivery_id)
    return record_from_row(row) if row else None


@dataclass(frozen=True)
class ClaimResult:
    record: DeliveryRecord
    won: bool
    reclaimed: bool  # 贏得的是過期的舊 claim（前一個送出者可能已送出）


def claim_delivery(
    session: Session, delivery_id: str, worker: str, ttl: timedelta
) -> ClaimResult:
    """在列鎖內取得送出權（CAS）：未被 claim、或舊 claim 已超過 ttl 才能取得；輸家回現況。

    只有送出前狀態（generated／synthesized／enqueued）可被 claim。
    """
    row = session.execute(
        select(DeliveryRow)
        .where(DeliveryRow.delivery_id == delivery_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    now = session.scalar(select(func.now()))
    claimable = row.status in ("generated", "synthesized", "enqueued")
    stale = row.claimed_at is not None and row.claimed_at <= now - ttl
    if not claimable or (row.claimed_by is not None and not stale):
        return ClaimResult(record_from_row(row), won=False, reclaimed=False)
    reclaimed = row.claimed_by is not None
    row.claimed_by = worker
    row.claimed_at = now
    session.flush()
    return ClaimResult(record_from_row(row), won=True, reclaimed=reclaimed)


def update_played_range(session: Session, delivery_id: str, played_range: dict[str, Any]) -> DeliveryRecord:
    """在列鎖內更新播放進度（狀態不變）；位置只往前，倒退或同值為 no-op。"""
    row = session.execute(
        select(DeliveryRow)
        .where(DeliveryRow.delivery_id == delivery_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    current = record_from_row(row)
    updated = current.with_progress(played_range)
    if updated is current:
        return current
    row.played_range = updated.played_range
    session.flush()
    return updated


def advance_delivery(
    session: Session,
    delivery_id: str,
    to: DeliveryStatus,
    *,
    evidence: DeliveryEvidence | None = None,
    sent_at: datetime | None = None,
    reported_at: datetime | None = None,
    played_range: dict[str, Any] | None = None,
    cancel_position: dict[str, Any] | None = None,
    spoken_text_ref: str | None = None,
    spoken_text_hash: str | None = None,
    failure: dict[str, Any] | None = None,
) -> DeliveryRecord:
    """在列鎖內推進狀態；不合法拋 IllegalDeliveryTransition，資料不變。

    鎖定時以 populate_existing 重新載入，不使用同 session 先前讀到的舊狀態。
    目標等於當前狀態且證據不降級時為 no-op。
    """
    row = session.execute(
        select(DeliveryRow)
        .where(DeliveryRow.delivery_id == delivery_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    current = record_from_row(row)
    updated = current.advance(
        to, evidence=evidence, sent_at=sent_at, reported_at=reported_at, played_range=played_range,
        cancel_position=cancel_position, spoken_text_ref=spoken_text_ref,
        spoken_text_hash=spoken_text_hash, failure=failure,
    )
    if updated is current:
        return current
    row.status = updated.status.value
    row.delivery_evidence = updated.delivery_evidence.value
    row.sent_at = updated.sent_at
    row.reported_at = updated.reported_at
    row.played_range = updated.played_range
    row.cancel_position = updated.cancel_position
    row.spoken_text_ref = updated.spoken_text_ref
    row.spoken_text_hash = updated.spoken_text_hash
    row.failure = updated.failure
    session.flush()
    return updated
