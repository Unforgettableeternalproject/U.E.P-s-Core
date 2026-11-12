"""PresencePlan 行程持久化：meta、items、overrides、slot_marks

Revision ID: 0021_presence_plan
Revises: 0020_decision_log_chain
Create Date: 2026-11-12

- ``presence_plan_meta``：每 persona 一列，``revision``（任何行程寫入同交易 +1）與行程時區 ``tz``。
- ``presence_plan_items``：行程項（藍圖，soft delete）；CHECK 鎖住 location 為 NULL 時只能是 rest、rest_window 的形狀、
  once／weekly 欄位互斥；每 persona 至多一筆未刪除的 rest_window（partial unique）。
- ``presence_plan_overrides``：臨時覆蓋（overlay），``expires_at <= round_end_at``；location 不含 host；
  每 persona 至多一筆 PENDING／ACTIVE（partial unique）；``cause_request_id`` unique（冪等）。
- ``presence_plan_slot_marks``：暫停／放棄標記；同一出現至多一筆未結束的同 kind 標記（partial unique）。
- downgrade 依相反順序移除資料表（資料會遺失）。
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_presence_plan"
down_revision = "0020_decision_log_chain"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)
_LOCATIONS = ("desktop", "discord", "website", "host")
_OVERRIDE_LOCATIONS = ("desktop", "discord", "website")
_KINDS = ("rest", "work", "social")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "presence_plan_meta",
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tz", sa.String(), nullable=False, server_default="Asia/Taipei"),
        sa.Column("updated_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("persona_id", name="pk_presence_plan_meta"),
    )

    op.create_table(
        "presence_plan_items",
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="user"),
        sa.Column("activity_kind", sa.String(), nullable=False),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("recurrence", sa.String(), nullable=False),
        sa.Column("once_start_at", _TS, nullable=True),
        sa.Column("once_end_at", _TS, nullable=True),
        sa.Column("weekdays", postgresql.ARRAY(sa.SmallInteger()), nullable=True),
        sa.Column("local_start", sa.Time(), nullable=True),
        sa.Column("local_end", sa.Time(), nullable=True),
        sa.Column("tz", sa.String(), nullable=True),
        sa.Column("valid_from", _TS, nullable=True),
        sa.Column("valid_until", _TS, nullable=True),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="50"),
        sa.Column("flexibility", sa.String(), nullable=False, server_default="fixed"),
        sa.Column("interrupt_policy", sa.String(), nullable=True),
        sa.Column("suggested_behaviors", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("note_ref", sa.String(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("item_id", name="pk_presence_plan_items"),
        sa.CheckConstraint(_in("role", ("rest_window", "user")), name="ck_presence_plan_items_role"),
        sa.CheckConstraint(_in("activity_kind", _KINDS), name="ck_presence_plan_items_kind"),
        sa.CheckConstraint(f"location IS NULL OR {_in('location', _LOCATIONS)}", name="ck_presence_plan_items_location"),
        sa.CheckConstraint(_in("recurrence", ("once", "weekly")), name="ck_presence_plan_items_recurrence"),
        sa.CheckConstraint(_in("flexibility", ("fixed", "deferrable", "flexible")),
                           name="ck_presence_plan_items_flexibility"),
        sa.CheckConstraint(
            "interrupt_policy IS NULL OR " + _in("interrupt_policy", ("free", "safe_point", "owner_only")),
            name="ck_presence_plan_items_interrupt"),
        sa.CheckConstraint("priority BETWEEN 0 AND 99", name="ck_presence_plan_items_priority"),
        # X3：未指定地點＝主機＋休息
        sa.CheckConstraint("location IS NOT NULL OR activity_kind = 'rest'", name="ck_presence_plan_items_null_loc_rest"),
        sa.CheckConstraint(
            "role <> 'rest_window' OR (activity_kind = 'rest' AND location IS NULL AND recurrence = 'weekly')",
            name="ck_presence_plan_items_rest_window_shape"),
        # once／weekly 欄位互斥
        sa.CheckConstraint(
            "(recurrence = 'once' AND once_start_at IS NOT NULL AND once_end_at IS NOT NULL "
            "AND once_end_at > once_start_at AND weekdays IS NULL AND local_start IS NULL AND local_end IS NULL "
            "AND tz IS NULL) OR "
            "(recurrence = 'weekly' AND once_start_at IS NULL AND once_end_at IS NULL AND weekdays IS NOT NULL "
            "AND cardinality(weekdays) >= 1 AND local_start IS NOT NULL AND local_end IS NOT NULL "
            "AND local_start <> local_end AND tz IS NOT NULL)",
            name="ck_presence_plan_items_recurrence_fields"),
        sa.CheckConstraint("weekdays IS NULL OR weekdays <@ ARRAY[1,2,3,4,5,6,7]::smallint[]",
                           name="ck_presence_plan_items_weekdays"),
        sa.CheckConstraint("valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
                           name="ck_presence_plan_items_valid"),
    )
    op.create_index("ix_presence_plan_items_persona", "presence_plan_items", ["persona_id"],
                    postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index("uq_presence_plan_items_rest_window", "presence_plan_items", ["persona_id"], unique=True,
                    postgresql_where=sa.text("role = 'rest_window' AND deleted_at IS NULL"))

    op.create_table(
        "presence_plan_overrides",
        sa.Column("override_id", sa.String(), nullable=False),
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("cause_request_id", sa.String(), nullable=False),
        sa.Column("authority", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("location", sa.String(), nullable=False),
        sa.Column("activity_kind", sa.String(), nullable=False),
        sa.Column("started_at", _TS, nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("round_end_at", _TS, nullable=False),
        sa.Column("ended_at", _TS, nullable=True),
        sa.Column("end_reason", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("override_id", name="pk_presence_plan_overrides"),
        sa.UniqueConstraint("cause_request_id", name="uq_presence_plan_overrides_cause_request_id"),
        sa.CheckConstraint(_in("location", _OVERRIDE_LOCATIONS), name="ck_presence_plan_overrides_location"),
        sa.CheckConstraint(_in("activity_kind", _KINDS), name="ck_presence_plan_overrides_kind"),
        sa.CheckConstraint(_in("status", ("PENDING", "ACTIVE", "ENDED")), name="ck_presence_plan_overrides_status"),
        sa.CheckConstraint(
            "end_reason IS NULL OR " + _in("end_reason", (
                "completed", "idle_timeout", "round_ended", "expired", "owner_ended", "superseded", "emergency",
                "transfer_failed", "presence_lost", "restart")),
            name="ck_presence_plan_overrides_end_reason"),
        sa.CheckConstraint("expires_at <= round_end_at", name="ck_presence_plan_overrides_expires_in_round"),
        sa.CheckConstraint("expires_at > started_at", name="ck_presence_plan_overrides_expires_after_start"),
        sa.CheckConstraint(
            "(status = 'ENDED') = (ended_at IS NOT NULL AND end_reason IS NOT NULL)",
            name="ck_presence_plan_overrides_ended_consistent"),
    )
    op.create_index("uq_presence_plan_overrides_open", "presence_plan_overrides", ["persona_id"], unique=True,
                    postgresql_where=sa.text("status IN ('PENDING', 'ACTIVE')"))

    op.create_table(
        "presence_plan_slot_marks",
        sa.Column("mark_id", sa.String(), nullable=False),
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("item_ref", sa.String(), nullable=False),
        sa.Column("planned_start_at", _TS, nullable=False),
        sa.Column("cause_request_id", sa.String(), nullable=False),
        sa.Column("started_at", _TS, nullable=False),
        sa.Column("round_end_at", _TS, nullable=False),
        sa.Column("ended_at", _TS, nullable=True),
        sa.Column("end_reason", sa.String(), nullable=True),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("mark_id", name="pk_presence_plan_slot_marks"),
        sa.UniqueConstraint("cause_request_id", name="uq_presence_plan_slot_marks_cause_request_id"),
        sa.CheckConstraint(_in("kind", ("pause", "abandon")), name="ck_presence_plan_slot_marks_kind"),
        sa.CheckConstraint(
            "end_reason IS NULL OR " + _in("end_reason", ("owner_resumed", "item_ended", "round_ended", "superseded")),
            name="ck_presence_plan_slot_marks_end_reason"),
        sa.CheckConstraint("(ended_at IS NULL) = (end_reason IS NULL)", name="ck_presence_plan_slot_marks_ended"),
        sa.CheckConstraint("round_end_at > started_at", name="ck_presence_plan_slot_marks_round"),
    )
    op.create_index("uq_presence_plan_slot_marks_open", "presence_plan_slot_marks",
                    ["persona_id", "item_ref", "planned_start_at", "kind"], unique=True,
                    postgresql_where=sa.text("ended_at IS NULL"))


def downgrade() -> None:
    op.drop_index("uq_presence_plan_slot_marks_open", table_name="presence_plan_slot_marks")
    op.drop_table("presence_plan_slot_marks")
    op.drop_index("uq_presence_plan_overrides_open", table_name="presence_plan_overrides")
    op.drop_table("presence_plan_overrides")
    op.drop_index("uq_presence_plan_items_rest_window", table_name="presence_plan_items")
    op.drop_index("ix_presence_plan_items_persona", table_name="presence_plan_items")
    op.drop_table("presence_plan_items")
    op.drop_table("presence_plan_meta")
