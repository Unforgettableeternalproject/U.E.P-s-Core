"""CC-4：presence_current、presence_transfers、presence_leases

Revision ID: 0004_presence
Revises: 0003_context_output
Create Date: 2026-10-26
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_presence"
down_revision = "0003_context_output"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)
_STATUSES = ("NONE", "ACTIVE", "RECOVERING")
_LOCATIONS = ("desktop", "discord", "website")
_PHASES = ("PREPARING", "QUIESCING", "CHECKPOINTED", "ACTIVATING", "DONE", "ABORTED")
_LEASE_STATES = ("ISSUED", "ACTIVE", "EXPIRED", "RELEASED", "REVOKED", "SUPERSEDED")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


# generation 只增不減、persona_id 不可改（P-3）；Lease 識別欄位不可變，歷史不可改寫
_CURRENT_GUARD = """
CREATE FUNCTION presence_current_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.persona_id IS DISTINCT FROM OLD.persona_id THEN
        RAISE EXCEPTION 'presence_current.persona_id 不可變更';
    END IF;
    IF NEW.generation < OLD.generation THEN
        RAISE EXCEPTION 'presence_current.generation 不得減少';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_LEASE_GUARD = """
CREATE FUNCTION presence_leases_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'presence_leases 不可刪除';
    END IF;
    IF NEW.lease_id IS DISTINCT FROM OLD.lease_id OR NEW.persona_id IS DISTINCT FROM OLD.persona_id
       OR NEW.endpoint_id IS DISTINCT FROM OLD.endpoint_id OR NEW.location IS DISTINCT FROM OLD.location
       OR NEW.generation IS DISTINCT FROM OLD.generation OR NEW.core_epoch IS DISTINCT FROM OLD.core_epoch
       OR NEW.fencing_epoch IS DISTINCT FROM OLD.fencing_epoch OR NEW.issued_at IS DISTINCT FROM OLD.issued_at THEN
        RAISE EXCEPTION 'presence_leases 識別欄位不可變更';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "presence_current",
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("endpoint_id", sa.String(), nullable=True),
        sa.Column("owner_activity_id", sa.String(), nullable=True),
        sa.Column("lease_id", sa.String(), nullable=True),
        sa.Column("core_epoch", sa.BigInteger(), nullable=False),
        sa.Column("fencing_epoch", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", _TS, nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("transfer_id", sa.String(), nullable=True),
        sa.Column("unquiesced_endpoint_id", sa.String(), nullable=True),
        sa.Column("recovery_reason", sa.String(), nullable=True),
        sa.Column("updated_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("persona_id", name="pk_presence_current"),
        sa.CheckConstraint(_in("status", _STATUSES), name="ck_presence_current_status"),
        sa.CheckConstraint(
            "location IS NULL OR " + _in("location", _LOCATIONS), name="ck_presence_current_location"
        ),
        sa.CheckConstraint(
            "status <> 'ACTIVE' OR (lease_id IS NOT NULL AND endpoint_id IS NOT NULL "
            "AND location IS NOT NULL AND expires_at IS NOT NULL)",
            name="ck_presence_current_active_complete",
        ),
        sa.CheckConstraint(
            "status <> 'RECOVERING' OR recovery_reason IS NOT NULL",
            name="ck_presence_current_recovering_has_reason",
        ),
    )
    op.create_table(
        "presence_transfers",
        sa.Column("transfer_id", sa.String(), nullable=False),
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("from_endpoint_id", sa.String(), nullable=True),
        sa.Column("to_endpoint_id", sa.String(), nullable=False),
        sa.Column("reason", postgresql.JSONB(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("lease_id", sa.String(), nullable=True),
        sa.Column("checkpoint_ref", sa.String(), nullable=True),
        sa.Column("stop_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("deadline", _TS, nullable=False),
        sa.Column("recovery_reason", sa.String(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("transfer_id", name="pk_presence_transfers"),
        sa.CheckConstraint(_in("phase", _PHASES), name="ck_presence_transfers_phase"),
    )
    op.create_index(
        "uq_presence_transfers_open", "presence_transfers", ["persona_id"], unique=True,
        postgresql_where=sa.text("phase NOT IN ('DONE', 'ABORTED')"),
    )
    op.create_table(
        "presence_leases",
        sa.Column("lease_id", sa.String(), nullable=False),
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("endpoint_id", sa.String(), nullable=False),
        sa.Column("location", sa.String(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("core_epoch", sa.BigInteger(), nullable=False),
        sa.Column("fencing_epoch", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("issued_at", _TS, nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("ended_at", _TS, nullable=True),
        sa.Column("end_reason", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("lease_id", name="pk_presence_leases"),
        sa.UniqueConstraint("persona_id", "generation", name="uq_presence_leases_persona_generation"),
        sa.CheckConstraint(_in("state", _LEASE_STATES), name="ck_presence_leases_state"),
    )
    op.execute(_CURRENT_GUARD)
    op.execute(
        "CREATE TRIGGER presence_current_guard BEFORE UPDATE ON presence_current "
        "FOR EACH ROW EXECUTE FUNCTION presence_current_guard()"
    )
    op.execute(_LEASE_GUARD)
    op.execute(
        "CREATE TRIGGER presence_leases_guard BEFORE UPDATE OR DELETE ON presence_leases "
        "FOR EACH ROW EXECUTE FUNCTION presence_leases_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER presence_leases_guard ON presence_leases")
    op.execute("DROP TRIGGER presence_current_guard ON presence_current")
    op.execute("DROP FUNCTION presence_leases_guard()")
    op.execute("DROP FUNCTION presence_current_guard()")
    op.drop_table("presence_leases")
    op.drop_index("uq_presence_transfers_open", table_name="presence_transfers")
    op.drop_table("presence_transfers")
    op.drop_table("presence_current")
