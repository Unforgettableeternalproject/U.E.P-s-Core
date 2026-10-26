"""presence_current／presence_transfers／presence_leases（migration 0004_presence）。

``presence_current`` 以 persona_id 為 PK，是有效授權的唯一權威 row（P-1）；
``presence_leases`` 保存每次核發的 Lease（歷史不構成授權）；``presence_transfers`` 存移轉階段。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from uep.db.base import Base

STATUSES = ("NONE", "ACTIVE", "RECOVERING")
LOCATIONS = ("desktop", "discord", "website")
PHASES = ("PREPARING", "QUIESCING", "CHECKPOINTED", "ACTIVATING", "DONE", "ABORTED")
LEASE_STATES = ("ISSUED", "ACTIVE", "EXPIRED", "RELEASED", "REVOKED", "SUPERSEDED")


def in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class PresenceCurrentRow(Base):
    __tablename__ = "presence_current"
    __table_args__ = (
        CheckConstraint(in_list("status", STATUSES), name="status"),
        CheckConstraint("location IS NULL OR " + in_list("location", LOCATIONS), name="location"),
        # ACTIVE 必須有完整授權與到期時間（契約 4.1）
        CheckConstraint(
            "status <> 'ACTIVE' OR (lease_id IS NOT NULL AND endpoint_id IS NOT NULL "
            "AND location IS NOT NULL AND expires_at IS NOT NULL)",
            name="active_complete",
        ),
        CheckConstraint("status <> 'RECOVERING' OR recovery_reason IS NOT NULL", name="recovering_has_reason"),
    )

    persona_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str | None] = mapped_column(String)
    endpoint_id: Mapped[str | None] = mapped_column(String)
    owner_activity_id: Mapped[str | None] = mapped_column(String)
    lease_id: Mapped[str | None] = mapped_column(String)
    core_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fencing_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False)
    transfer_id: Mapped[str | None] = mapped_column(String)
    # 草案補充：Lease 過期但舊端未證明已停止時，記下舊端；其他端 acquire 會被擋（P-6）
    unquiesced_endpoint_id: Mapped[str | None] = mapped_column(String)
    # 草案補充：無 transfer 時（如 Core 重啟）進 RECOVERING 的原因
    recovery_reason: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PresenceTransferRow(Base):
    __tablename__ = "presence_transfers"
    __table_args__ = (
        CheckConstraint(in_list("phase", PHASES), name="phase"),
        # 每個 persona 最多一個未結束移轉
        Index(
            "uq_presence_transfers_open", "persona_id", unique=True,
            postgresql_where=text("phase NOT IN ('DONE', 'ABORTED')"),
        ),
    )

    transfer_id: Mapped[str] = mapped_column(String, primary_key=True)
    persona_id: Mapped[str] = mapped_column(String, nullable=False)
    from_endpoint_id: Mapped[str | None] = mapped_column(String)
    to_endpoint_id: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    lease_id: Mapped[str | None] = mapped_column(String)
    checkpoint_ref: Mapped[str | None] = mapped_column(String)
    stop_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recovery_reason: Mapped[str | None] = mapped_column(String)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PresenceLeaseRow(Base):
    __tablename__ = "presence_leases"
    __table_args__ = (
        UniqueConstraint("persona_id", "generation", name="uq_presence_leases_persona_generation"),
        CheckConstraint(in_list("state", LEASE_STATES), name="state"),
    )

    lease_id: Mapped[str] = mapped_column(String, primary_key=True)
    persona_id: Mapped[str] = mapped_column(String, nullable=False)
    endpoint_id: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str] = mapped_column(String, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    core_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fencing_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String)
