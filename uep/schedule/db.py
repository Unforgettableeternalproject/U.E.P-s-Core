"""行程資料表的 ORM model（migration 0021_presence_plan）。

``PlanStore`` 以 ``text()`` 存取這四張表；這裡的 model 讓 ``Base.metadata`` 與 migration 一致
（``compare_metadata`` 無差異）並供測試清表使用。CHECK 條件文字與 migration 相同（約束名稱經命名慣例加上資料表前綴）。
"""
from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from uep.db.base import Base


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


_LOCATIONS = ("desktop", "discord", "website", "host")
_OVERRIDE_LOCATIONS = ("desktop", "discord", "website")
_KINDS = ("rest", "work", "social")


class PlanMetaRow(Base):
    __tablename__ = "presence_plan_meta"

    persona_id: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    tz: Mapped[str] = mapped_column(String, nullable=False, server_default="Asia/Taipei")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PlanItemRow(Base):
    __tablename__ = "presence_plan_items"

    item_id: Mapped[str] = mapped_column(String, primary_key=True)  # plan_<ULID>
    persona_id: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, server_default="user")
    activity_kind: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str | None] = mapped_column(String)
    recurrence: Mapped[str] = mapped_column(String, nullable=False)
    once_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    once_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    weekdays: Mapped[list[int] | None] = mapped_column(ARRAY(SmallInteger))
    local_start: Mapped[time | None] = mapped_column(Time)
    local_end: Mapped[time | None] = mapped_column(Time)
    tz: Mapped[str | None] = mapped_column(String)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="50")
    flexibility: Mapped[str] = mapped_column(String, nullable=False, server_default="fixed")
    interrupt_policy: Mapped[str | None] = mapped_column(String)
    suggested_behaviors: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    note_ref: Mapped[str | None] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    updated_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in("role", ("rest_window", "user")), name="role"),
        CheckConstraint(_in("activity_kind", _KINDS), name="kind"),
        CheckConstraint(f"location IS NULL OR {_in('location', _LOCATIONS)}", name="location"),
        CheckConstraint(_in("recurrence", ("once", "weekly")), name="recurrence"),
        CheckConstraint(_in("flexibility", ("fixed", "deferrable", "flexible")), name="flexibility"),
        CheckConstraint("interrupt_policy IS NULL OR " + _in("interrupt_policy", ("free", "safe_point", "owner_only")),
                        name="interrupt"),
        CheckConstraint("priority BETWEEN 0 AND 99", name="priority"),
        CheckConstraint("location IS NOT NULL OR activity_kind = 'rest'", name="null_loc_rest"),
        CheckConstraint(
            "role <> 'rest_window' OR (activity_kind = 'rest' AND location IS NULL AND recurrence = 'weekly')",
            name="rest_window_shape"),
        CheckConstraint(
            "(recurrence = 'once' AND once_start_at IS NOT NULL AND once_end_at IS NOT NULL "
            "AND once_end_at > once_start_at AND weekdays IS NULL AND local_start IS NULL AND local_end IS NULL "
            "AND tz IS NULL) OR "
            "(recurrence = 'weekly' AND once_start_at IS NULL AND once_end_at IS NULL AND weekdays IS NOT NULL "
            "AND cardinality(weekdays) >= 1 AND local_start IS NOT NULL AND local_end IS NOT NULL "
            "AND local_start <> local_end AND tz IS NOT NULL)",
            name="recurrence_fields"),
        CheckConstraint("weekdays IS NULL OR weekdays <@ ARRAY[1,2,3,4,5,6,7]::smallint[]", name="weekdays"),
        CheckConstraint("valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from", name="valid"),
        Index("ix_presence_plan_items_persona", "persona_id", postgresql_where=text("deleted_at IS NULL")),
        Index("uq_presence_plan_items_rest_window", "persona_id", unique=True,
              postgresql_where=text("role = 'rest_window' AND deleted_at IS NULL")),
    )


class PlanOverrideRow(Base):
    __tablename__ = "presence_plan_overrides"

    override_id: Mapped[str] = mapped_column(String, primary_key=True)  # povr_<ULID>
    persona_id: Mapped[str] = mapped_column(String, nullable=False)
    cause_request_id: Mapped[str] = mapped_column(String, nullable=False)
    authority: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    location: Mapped[str] = mapped_column(String, nullable=False)
    activity_kind: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    round_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("cause_request_id"),
        CheckConstraint(_in("location", _OVERRIDE_LOCATIONS), name="location"),
        CheckConstraint(_in("activity_kind", _KINDS), name="kind"),
        CheckConstraint(_in("status", ("PENDING", "ACTIVE", "ENDED")), name="status"),
        CheckConstraint("end_reason IS NULL OR " + _in("end_reason", (
            "completed", "idle_timeout", "round_ended", "expired", "owner_ended", "superseded", "emergency",
            "transfer_failed", "presence_lost", "restart")), name="end_reason"),
        CheckConstraint("expires_at <= round_end_at", name="expires_in_round"),
        CheckConstraint("expires_at > started_at", name="expires_after_start"),
        CheckConstraint("(status = 'ENDED') = (ended_at IS NOT NULL AND end_reason IS NOT NULL)",
                        name="ended_consistent"),
        Index("uq_presence_plan_overrides_open", "persona_id", unique=True,
              postgresql_where=text("status IN ('PENDING', 'ACTIVE')")),
    )


class PlanSlotMarkRow(Base):
    __tablename__ = "presence_plan_slot_marks"

    mark_id: Mapped[str] = mapped_column(String, primary_key=True)  # pmark_<ULID>
    persona_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    item_ref: Mapped[str] = mapped_column(String, nullable=False)
    planned_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cause_request_id: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    round_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("cause_request_id"),
        CheckConstraint(_in("kind", ("pause", "abandon")), name="kind"),
        CheckConstraint("end_reason IS NULL OR " + _in(
            "end_reason", ("owner_resumed", "item_ended", "round_ended", "superseded")), name="end_reason"),
        CheckConstraint("(ended_at IS NULL) = (end_reason IS NULL)", name="ended"),
        CheckConstraint("round_end_at > started_at", name="round"),
        Index("uq_presence_plan_slot_marks_open", "persona_id", "item_ref", "planned_start_at", "kind", unique=True,
              postgresql_where=text("ended_at IS NULL")),
    )
