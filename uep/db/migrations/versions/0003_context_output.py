"""CC-2／CC-5：context_snapshots、output_intents、deliveries

Revision ID: 0003_context_output
Revises: 0002_task_model
Create Date: 2026-10-26
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_context_output"
down_revision = "0002_task_model"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)

_PURPOSES = ("conversation_turn", "task_run", "proactive_eval", "summarize")
_MEMORY_STATUSES = ("ok", "degraded_no_memory", "timeout", "unavailable")
_HISTORY_OWNERS = ("core_projection", "sdk_store")
_OUTPUT_KINDS = (
    "text", "audio_stream", "typing", "avatar_enter_leave", "animation",
    "desktop_ui_action", "system_notice",
)
_DELIVERY_STATUSES = (
    "generated", "synthesized", "enqueued", "sent", "accepted", "rendered_played",
    "failed", "unknown", "cancelled", "rejected", "suppressed",
)
_EVIDENCES = ("local_playback", "remote_render", "transport_sent", "unknown")


# revision 範圍鍵：conversation／segment，否則 run_id，否則 (persona_id, purpose)；須與 uep.context.models 一致
_SCOPE_KEY_SQL = (
    "CASE WHEN conversation_id IS NOT NULL OR segment_id IS NOT NULL "
    "THEN 'c:' || coalesce(conversation_id, '') || ':' || coalesce(segment_id, '') "
    "WHEN run_id IS NOT NULL THEN 'r:' || run_id "
    "ELSE 'p:' || persona_id || ':' || purpose END"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


# 快照不可變：禁止 DELETE；UPDATE 只允許把 sdk_session_ref 由 NULL 回填一次（Runtime 回填、Core 記錄）
_SNAPSHOT_GUARD = """
CREATE FUNCTION context_snapshots_immutable() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'context_snapshots 不可變，不可刪除';
    END IF;
    IF (to_jsonb(NEW) - 'sdk_session_ref' - 'scope_key') IS DISTINCT FROM (to_jsonb(OLD) - 'sdk_session_ref' - 'scope_key') THEN
        RAISE EXCEPTION 'context_snapshots 不可變，只允許回填 sdk_session_ref';
    END IF;
    IF OLD.sdk_session_ref IS NOT NULL AND NEW.sdk_session_ref IS DISTINCT FROM OLD.sdk_session_ref THEN
        RAISE EXCEPTION 'sdk_session_ref 回填後不可變更';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_INTENT_GUARD = """
CREATE FUNCTION output_intents_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'output_intents 不可變（計畫輸出與交付結果分表，O-1）';
END;
$$ LANGUAGE plpgsql;
"""

# deliveries：終態不得再改；unknown 只能被解析為 sent（VC-5-3）
_DELIVERY_GUARD = """
CREATE FUNCTION deliveries_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'deliveries 不可刪除';
    END IF;
    IF OLD.status IN ('rendered_played', 'failed', 'cancelled', 'rejected', 'suppressed') THEN
        RAISE EXCEPTION 'deliveries 終態 % 不得再改', OLD.status;
    END IF;
    IF OLD.status = 'unknown' AND NEW.status NOT IN ('unknown', 'sent', 'failed') THEN
        RAISE EXCEPTION 'deliveries unknown 只能解析為 sent 或 failed';
    END IF;
    IF (CASE NEW.delivery_evidence WHEN 'unknown' THEN 0 WHEN 'transport_sent' THEN 1 ELSE 2 END)
       < (CASE OLD.delivery_evidence WHEN 'unknown' THEN 0 WHEN 'transport_sent' THEN 1 ELSE 2 END) THEN
        RAISE EXCEPTION 'deliveries 證據強度只升不降';
    END IF;
    IF NEW.output_id <> OLD.output_id OR NEW.sequence <> OLD.sequence OR NEW.kind <> OLD.kind
       OR NEW.dispatch_id <> OLD.dispatch_id OR NEW.delivery_id <> OLD.delivery_id THEN
        RAISE EXCEPTION 'deliveries 識別欄位不可變更';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "context_snapshots",
        sa.Column("snapshot_id", sa.String(), nullable=False),
        sa.Column("context_revision", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("persona_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("segment_id", sa.String(), nullable=True),
        sa.Column("turn_id", sa.String(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("audience_scope", sa.String(), nullable=False),
        sa.Column("actor_ref", sa.String(), nullable=False),
        sa.Column("memory_status", sa.String(), nullable=False),
        sa.Column("history_owner", sa.String(), nullable=False),
        sa.Column("sdk_session_ref", sa.String(), nullable=True),
        sa.Column("built_at", _TS, nullable=False),
        sa.Column("valid_until", _TS, nullable=True),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("scope_key", sa.String(), sa.Computed(_SCOPE_KEY_SQL, persisted=True), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id", name="pk_context_snapshots"),
        sa.UniqueConstraint("scope_key", "context_revision", name="uq_context_snapshots_scope_revision"),
        sa.CheckConstraint(_in("purpose", _PURPOSES), name="ck_context_snapshots_purpose"),
        sa.CheckConstraint(_in("memory_status", _MEMORY_STATUSES), name="ck_context_snapshots_memory_status"),
        sa.CheckConstraint(_in("history_owner", _HISTORY_OWNERS), name="ck_context_snapshots_history_owner"),
        sa.CheckConstraint("context_revision >= 1", name="ck_context_snapshots_revision_positive"),
    )
    op.create_index("ix_context_snapshots_run_id", "context_snapshots", ["run_id"])
    op.execute(_SNAPSHOT_GUARD)
    op.execute(
        "CREATE TRIGGER context_snapshots_immutable BEFORE UPDATE OR DELETE ON context_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION context_snapshots_immutable()"
    )

    op.create_table(
        "output_intents",
        sa.Column("output_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("endpoint_id", sa.String(), nullable=False),
        sa.Column("lease_id", sa.String(), nullable=False),
        sa.Column("fencing_epoch", sa.BigInteger(), nullable=False),
        sa.Column("core_epoch", sa.BigInteger(), nullable=False),
        sa.Column("presence_generation", sa.BigInteger(), nullable=False),
        sa.Column("activity_id", sa.String(), nullable=True),
        sa.Column("turn_id", sa.String(), nullable=True),
        sa.Column("turn_generation", sa.BigInteger(), nullable=True),
        sa.Column("attempt_id", sa.String(), nullable=True),
        sa.Column("audience_scope", sa.String(), nullable=False),
        sa.Column("content_ref", sa.String(), nullable=False),
        sa.Column("content_format", postgresql.JSONB(), nullable=True),
        sa.Column("valid_until", _TS, nullable=False),
        sa.Column("generated_text_ref", sa.String(), nullable=True),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("output_id", "sequence", name="pk_output_intents"),
        sa.UniqueConstraint("output_id", "sequence", "kind", name="uq_output_intents_output_sequence_kind"),
        sa.CheckConstraint(_in("kind", _OUTPUT_KINDS), name="ck_output_intents_kind"),
        sa.CheckConstraint(_in("origin", ("persona", "system")), name="ck_output_intents_origin"),
        sa.CheckConstraint(
            "(turn_id IS NOT NULL AND turn_generation IS NOT NULL AND attempt_id IS NOT NULL) "
            "OR activity_id IS NOT NULL",
            name="ck_output_intents_interactive_or_activity",
        ),
    )
    op.create_index("ix_output_intents_turn_id", "output_intents", ["turn_id"])
    op.execute(_INTENT_GUARD)
    op.execute(
        "CREATE TRIGGER output_intents_immutable BEFORE UPDATE OR DELETE ON output_intents "
        "FOR EACH ROW EXECUTE FUNCTION output_intents_immutable()"
    )

    op.create_table(
        "deliveries",
        sa.Column("delivery_id", sa.String(), nullable=False),
        sa.Column("output_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("dispatch_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("delivery_evidence", sa.String(), nullable=False),
        sa.Column("played_range", postgresql.JSONB(), nullable=True),
        sa.Column("cancel_position", postgresql.JSONB(), nullable=True),
        sa.Column("spoken_text_ref", sa.String(), nullable=True),
        sa.Column("spoken_text_hash", sa.String(), nullable=True),
        sa.Column("failure", postgresql.JSONB(), nullable=True),
        sa.Column("sent_at", _TS, nullable=True),
        sa.Column("reported_at", _TS, nullable=True),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("claimed_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("delivery_id", name="pk_deliveries"),
        sa.UniqueConstraint("output_id", "sequence", name="uq_deliveries_output_sequence"),
        sa.UniqueConstraint("dispatch_id", name="uq_deliveries_dispatch_id"),
        sa.ForeignKeyConstraint(
            ["output_id", "sequence", "kind"],
            ["output_intents.output_id", "output_intents.sequence", "output_intents.kind"],
            name="fk_deliveries_output_id_output_intents",
        ),
        sa.ForeignKeyConstraint(
            ["spoken_text_ref"], ["payload_blobs.payload_ref"],
            name="fk_deliveries_spoken_text_ref_payload_blobs",
        ),
        sa.CheckConstraint(_in("status", _DELIVERY_STATUSES), name="ck_deliveries_status"),
        sa.CheckConstraint(_in("delivery_evidence", _EVIDENCES), name="ck_deliveries_evidence"),
        # O-2：accepted／rendered_played 需強證據；sent 不得是 unknown 證據
        sa.CheckConstraint(
            "status NOT IN ('accepted', 'rendered_played') "
            "OR delivery_evidence IN ('local_playback', 'remote_render')",
            name="ck_deliveries_strong_evidence",
        ),
        sa.CheckConstraint(
            "status <> 'sent' OR delivery_evidence <> 'unknown'",
            name="ck_deliveries_sent_has_evidence",
        ),
        # failure 只收 {code, retryable}（M3），不夾帶文字
        sa.CheckConstraint(
            "failure IS NULL OR (failure - 'code' - 'retryable') = '{}'::jsonb",
            name="ck_deliveries_failure_keys",
        ),
        # synthesized 只屬於 audio_stream
        sa.CheckConstraint(
            "status <> 'synthesized' OR kind = 'audio_stream'",
            name="ck_deliveries_synthesized_audio_only",
        ),
    )
    op.execute(_DELIVERY_GUARD)
    op.execute(
        "CREATE TRIGGER deliveries_guard BEFORE UPDATE OR DELETE ON deliveries "
        "FOR EACH ROW EXECUTE FUNCTION deliveries_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER deliveries_guard ON deliveries")
    op.drop_table("deliveries")
    op.execute("DROP FUNCTION deliveries_guard()")
    op.execute("DROP TRIGGER output_intents_immutable ON output_intents")
    op.drop_index("ix_output_intents_turn_id", table_name="output_intents")
    op.drop_table("output_intents")
    op.execute("DROP FUNCTION output_intents_immutable()")
    op.execute("DROP TRIGGER context_snapshots_immutable ON context_snapshots")
    op.drop_index("ix_context_snapshots_run_id", table_name="context_snapshots")
    op.drop_table("context_snapshots")
    op.execute("DROP FUNCTION context_snapshots_immutable()")
