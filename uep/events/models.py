"""CC-1 的資料表：events（journal）、payload_blobs、outbox、inbox。

migration 手寫於 ``uep/db/migrations/versions``，此處須與之保持一致。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from uep.db.base import Base


class PayloadBlob(Base):
    """可刪除的私人原文 blob（D33）。刪除後 content 為 NULL，保留雜湊與刪除時間。"""

    __tablename__ = "payload_blobs"

    payload_ref: Mapped[str] = mapped_column(String, primary_key=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EventRow(Base):
    """journal：append-only，只存元資料、小型 payload、payload_ref／hash 與墓碑。"""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("source", "idempotency_key", name="uq_events_source_idempotency"),
        UniqueConstraint("aggregate_id", "aggregate_version", name="uq_events_aggregate_version"),
        Index("ix_events_trace_id", "trace_id"),
        Index("ix_events_event_type", "event_type"),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    journal_seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    delivery_class: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    persona_id: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String)
    segment_id: Mapped[str | None] = mapped_column(String)
    activity_id: Mapped[str | None] = mapped_column(String)
    task_id: Mapped[str | None] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(String)
    turn_id: Mapped[str | None] = mapped_column(String)
    legacy_ref: Mapped[dict | None] = mapped_column(JSONB)
    trace_id: Mapped[str] = mapped_column(String, nullable=False)
    causation_id: Mapped[str | None] = mapped_column(String)
    idempotency_key: Mapped[str | None] = mapped_column(String)
    aggregate_type: Mapped[str | None] = mapped_column(String)
    aggregate_id: Mapped[str | None] = mapped_column(String)
    aggregate_version: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope_ref: Mapped[str] = mapped_column(String, nullable=False)
    fence: Mapped[dict | None] = mapped_column(JSONB)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_ref: Mapped[str | None] = mapped_column(ForeignKey("payload_blobs.payload_ref"))
    payload_hash: Mapped[str | None] = mapped_column(String)
    tombstone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tombstone_reason: Mapped[str | None] = mapped_column(Text)


class OutboxRow(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("event_id", "target", name="uq_outbox_event_target"),
        Index(
            "ix_outbox_pending", "seq",
            postgresql_where=text("acked_at IS NULL AND dead_letter_reason IS NULL"),
        ),
    )

    outbox_id: Mapped[str] = mapped_column(String, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True, nullable=False)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id"), nullable=False)
    target: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_letter_reason: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InboxRow(Base):
    __tablename__ = "inbox"

    consumer_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    result: Mapped[dict | None] = mapped_column(JSONB)
