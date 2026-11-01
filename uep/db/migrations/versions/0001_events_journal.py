"""CC-1：events journal、payload_blobs、outbox、inbox

Revision ID: 0001_events_journal
Revises:
Create Date: 2026-10-26
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_events_journal"
down_revision = None
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)

# journal append-only：禁止 DELETE；UPDATE 只允許改墓碑欄位（D33、E-11）
_GUARD_FUNCTION = """
CREATE FUNCTION events_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'events journal 為 append-only，不可刪除';
    END IF;
    IF (to_jsonb(NEW) - 'tombstone_at' - 'tombstone_reason')
       IS DISTINCT FROM (to_jsonb(OLD) - 'tombstone_at' - 'tombstone_reason') THEN
        RAISE EXCEPTION 'events journal 為 append-only，只允許更新墓碑欄位';
    END IF;
    -- 墓碑一旦設定不可清除或改寫；尚無墓碑時不可只改 reason
    IF OLD.tombstone_at IS NOT NULL
       AND (NEW.tombstone_at IS DISTINCT FROM OLD.tombstone_at
            OR NEW.tombstone_reason IS DISTINCT FROM OLD.tombstone_reason) THEN
        RAISE EXCEPTION 'events 墓碑設定後不可變更';
    END IF;
    IF OLD.tombstone_at IS NULL AND NEW.tombstone_at IS NULL
       AND NEW.tombstone_reason IS DISTINCT FROM OLD.tombstone_reason THEN
        RAISE EXCEPTION 'events 不可在未設 tombstone_at 時只寫 tombstone_reason';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "payload_blobs",
        sa.Column("payload_ref", sa.String(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("payload_ref", name="pk_payload_blobs"),
    )

    op.create_table(
        "events",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("journal_seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("delivery_class", sa.String(), nullable=False),
        sa.Column("occurred_at", _TS, nullable=False),
        sa.Column("received_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("segment_id", sa.String(), nullable=True),
        sa.Column("activity_id", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("turn_id", sa.String(), nullable=True),
        sa.Column("legacy_ref", postgresql.JSONB(), nullable=True),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("causation_id", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("aggregate_type", sa.String(), nullable=True),
        sa.Column("aggregate_id", sa.String(), nullable=True),
        sa.Column("aggregate_version", sa.Integer(), nullable=True),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("expires_at", _TS, nullable=True),
        sa.Column("scope_ref", sa.String(), nullable=False),
        sa.Column("fence", postgresql.JSONB(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_ref", sa.String(), nullable=True),
        sa.Column("payload_hash", sa.String(), nullable=True),
        sa.Column("tombstone_at", _TS, nullable=True),
        sa.Column("tombstone_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name="pk_events"),
        sa.UniqueConstraint("journal_seq", name="uq_events_journal_seq"),
        sa.UniqueConstraint("source", "idempotency_key", name="uq_events_source_idempotency"),
        sa.UniqueConstraint("aggregate_id", "aggregate_version", name="uq_events_aggregate_version"),
        sa.ForeignKeyConstraint(
            ["payload_ref"], ["payload_blobs.payload_ref"],
            name="fk_events_payload_ref_payload_blobs",
        ),
    )
    op.create_index("ix_events_trace_id", "events", ["trace_id"])
    op.create_index("ix_events_event_type", "events", ["event_type"])

    op.execute(_GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events "
        "FOR EACH ROW EXECUTE FUNCTION events_append_only()"
    )

    op.create_table(
        "outbox",
        sa.Column("outbox_id", sa.String(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_at", _TS, nullable=True),
        sa.Column("acked_at", _TS, nullable=True),
        sa.Column("dead_letter_reason", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("outbox_id", name="pk_outbox"),
        sa.UniqueConstraint("seq", name="uq_outbox_seq"),
        sa.UniqueConstraint("event_id", "target", name="uq_outbox_event_target"),
        sa.ForeignKeyConstraint(["event_id"], ["events.event_id"], name="fk_outbox_event_id_events"),
    )
    op.create_index(
        "ix_outbox_pending", "outbox", ["seq"],
        postgresql_where=sa.text("acked_at IS NULL AND dead_letter_reason IS NULL"),
    )

    op.create_table(
        "inbox",
        sa.Column("consumer_id", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("claimed_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("consumer_id", "event_id", name="pk_inbox"),
    )


def downgrade() -> None:
    op.drop_table("inbox")
    op.drop_index("ix_outbox_pending", table_name="outbox")
    op.drop_table("outbox")
    op.execute("DROP TRIGGER events_append_only ON events")
    op.drop_index("ix_events_event_type", table_name="events")
    op.drop_index("ix_events_trace_id", table_name="events")
    op.drop_table("events")
    op.execute("DROP FUNCTION events_append_only()")
    op.drop_table("payload_blobs")
