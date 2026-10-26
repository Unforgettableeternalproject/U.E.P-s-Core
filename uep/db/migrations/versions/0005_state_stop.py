"""CC-7：state_projection、state_requests；全域停止旗標 global_stop_flags

Revision ID: 0005_state_stop
Revises: 0004_presence
Create Date: 2026-10-26
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_state_stop"
down_revision = "0004_presence"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)
_AXES = ("Runtime", "Presence", "Interaction", "Task", "Behavior", "Health")
_DECISIONS = (
    "ACCEPTED", "REJECTED_ILLEGAL", "REJECTED_VERSION", "REJECTED_UNAUTHORIZED", "REJECTED_EXPIRED", "DEFERRED",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


# 投影版本只增不減、不可刪除（6A.4 S-3：不得靜默覆寫）；請求紀錄為追加式
_PROJECTION_GUARD = """
CREATE FUNCTION state_projection_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'state_projection 不可刪除';
    END IF;
    IF NEW.persona_id IS DISTINCT FROM OLD.persona_id OR NEW.axis IS DISTINCT FROM OLD.axis
       OR NEW.subject IS DISTINCT FROM OLD.subject THEN
        RAISE EXCEPTION 'state_projection 主鍵不可變更';
    END IF;
    IF NEW.version < OLD.version THEN
        RAISE EXCEPTION 'state_projection.version 不得減少';
    END IF;
    IF NEW.version = OLD.version AND NEW.state IS DISTINCT FROM OLD.state THEN
        RAISE EXCEPTION 'state_projection.state 變更必須同時遞增 version';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_REQUEST_GUARD = """
CREATE FUNCTION state_requests_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'state_requests 為追加式紀錄，不可修改或刪除';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "state_projection",
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("axis", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), server_default="", nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("source_version", sa.BigInteger(), nullable=True),
        sa.Column("cause_event_id", sa.String(), nullable=True),
        sa.Column("updated_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("persona_id", "axis", "subject", name="pk_state_projection"),
        sa.CheckConstraint(_in("axis", _AXES), name="ck_state_projection_axis"),
    )
    op.create_table(
        "state_requests",
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("axis", sa.String(), nullable=False),
        sa.Column("proposer", sa.String(), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(), nullable=False),
        sa.Column("to_state", sa.String(), nullable=False),
        sa.Column("reason", postgresql.JSONB(), nullable=False),
        sa.Column("scope_ref", sa.String(), nullable=False),
        sa.Column("expires_at", _TS, nullable=True),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=True),
        sa.Column("new_version", sa.Integer(), nullable=True),
        sa.Column("decided_at", _TS, nullable=False),
        sa.Column("cause_event_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("request_id", name="pk_state_requests"),
        sa.CheckConstraint(_in("decision", _DECISIONS), name="ck_state_requests_decision"),
    )
    op.create_index("ix_state_requests_persona_axis", "state_requests", ["persona_id", "axis"])
    op.create_table(
        "global_stop_flags",
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("engaged", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("engaged_by", sa.String(), nullable=True),
        sa.Column("engaged_at", _TS, nullable=True),
        sa.Column("released_by", sa.String(), nullable=True),
        sa.Column("released_at", _TS, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("persona_id", name="pk_global_stop_flags"),
    )
    op.execute(_PROJECTION_GUARD)
    op.execute(
        "CREATE TRIGGER state_projection_guard BEFORE UPDATE OR DELETE ON state_projection "
        "FOR EACH ROW EXECUTE FUNCTION state_projection_guard()"
    )
    op.execute(_REQUEST_GUARD)
    op.execute(
        "CREATE TRIGGER state_requests_guard BEFORE UPDATE OR DELETE ON state_requests "
        "FOR EACH ROW EXECUTE FUNCTION state_requests_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER state_requests_guard ON state_requests")
    op.execute("DROP TRIGGER state_projection_guard ON state_projection")
    op.execute("DROP FUNCTION state_requests_guard()")
    op.execute("DROP FUNCTION state_projection_guard()")
    op.drop_table("global_stop_flags")
    op.drop_index("ix_state_requests_persona_axis", table_name="state_requests")
    op.drop_table("state_requests")
    op.drop_table("state_projection")
