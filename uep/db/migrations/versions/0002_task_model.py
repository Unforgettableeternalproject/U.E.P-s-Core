"""CC-6：activities、tasks、runs、steps、approvals、action_intents、receipts

Revision ID: 0002_task_model
Revises: 0001_events_journal
Create Date: 2026-10-26
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_task_model"
down_revision = "0001_events_journal"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)

_RUN_STATUSES = (
    "PENDING", "RUNNING", "WAITING_INPUT", "WAITING_APPROVAL", "PAUSED",
    "SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN",
)
_PAUSE_REASONS = ("user", "cooldown", "resource_missing", "presence_left", "core_shutdown")
_INTERRUPT = ("interruptible", "checkpoint_only", "non_interruptible")
_ACTIVITY_KINDS = ("interaction_episode", "task", "maintenance")
_ACTIVITY_STATUSES = ("ACTIVE", "ENDED")
_STEP_STATUSES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED")
_ACTION_STATUSES = ("INTENDED", "APPROVED", "EXECUTING", "SUCCEEDED", "FAILED", "UNKNOWN")


def _in(column: str, items: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in items)})"


def _check(table: str, name: str, expr: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expr, name=op.f(f"ck_{table}_{name}"))


def _version() -> sa.Column:
    return sa.Column("version", sa.Integer(), server_default="1", nullable=False)


def _created() -> sa.Column:
    return sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "activities",
        sa.Column("activity_id", sa.String(), nullable=False),
        sa.Column("persona_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("presence_bound", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("origin_ref", postgresql.JSONB(), nullable=True),
        sa.Column("legacy_ref", postgresql.JSONB(), nullable=True),
        _version(),
        _created(),
        sa.Column("ended_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("activity_id", name="pk_activities"),
        _check("activities", "kind", _in("kind", _ACTIVITY_KINDS)),
        _check("activities", "status", _in("status", _ACTIVITY_STATUSES)),
    )

    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("activity_id", sa.String(), nullable=False),
        sa.Column("parent_task_id", sa.String(), nullable=True),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("owner_actor_id", sa.String(), nullable=False),
        sa.Column("scope_ref", sa.String(), nullable=False),
        sa.Column("presence_bound", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pause_reason", sa.String(), nullable=True),
        sa.Column("deadline", _TS, nullable=True),
        sa.Column("interrupt_policy", sa.String(), nullable=False),
        sa.Column("origin_ref", postgresql.JSONB(), nullable=True),
        _version(),
        sa.Column("policy_ref", sa.String(), nullable=False),
        _created(),
        sa.Column("updated_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("task_id", name="pk_tasks"),
        sa.UniqueConstraint("activity_id", name="uq_tasks_activity_id"),
        sa.ForeignKeyConstraint(
            ["activity_id"], ["activities.activity_id"], name="fk_tasks_activity_id_activities"
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"], ["tasks.task_id"], name="fk_tasks_parent_task_id_tasks"
        ),
        _check("tasks", "status", _in("status", _RUN_STATUSES)),
        _check("tasks", "interrupt_policy", _in("interrupt_policy", _INTERRUPT)),
        _check("tasks", "pause_reason", "pause_reason IS NULL OR " + _in("pause_reason", _PAUSE_REASONS)),
        _check("tasks", "pause_reason_iff_paused", "(status = 'PAUSED') = (pause_reason IS NOT NULL)"),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])

    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_epoch", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pause_reason", sa.String(), nullable=True),
        sa.Column("run_deadline", _TS, nullable=True),
        sa.Column("step_timeout", sa.Interval(), nullable=True),
        sa.Column("approval_expiry", _TS, nullable=True),
        sa.Column("worker_heartbeat_at", _TS, nullable=True),
        sa.Column("paused_at", _TS, nullable=True),
        sa.Column("checkpoint_ref", sa.String(), nullable=True),
        sa.Column("checkpoint", postgresql.JSONB(), nullable=True),
        sa.Column("persona_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        _version(),
        _created(),
        sa.Column("updated_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", name="pk_runs"),
        sa.UniqueConstraint("task_id", "attempt", name="uq_runs_task_attempt"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], name="fk_runs_task_id_tasks"),
        _check("runs", "status", _in("status", _RUN_STATUSES)),
        _check("runs", "pause_reason", "pause_reason IS NULL OR " + _in("pause_reason", _PAUSE_REASONS)),
        _check("runs", "pause_reason_iff_paused", "(status = 'PAUSED') = (pause_reason IS NOT NULL)"),
    )
    op.create_index("ix_runs_task_id", "runs", ["task_id"])
    op.create_index(
        "uq_runs_one_active_per_task", "runs", ["task_id"], unique=True,
        postgresql_where=sa.text("status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')"),
    )

    op.create_table(
        "steps",
        sa.Column("step_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("result_ref", sa.String(), nullable=True),
        _version(),
        _created(),
        sa.Column("ended_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("step_id", name="pk_steps"),
        sa.UniqueConstraint("run_id", "seq", name="uq_steps_run_seq"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], name="fk_steps_run_id_runs"),
        _check("steps", "status", _in("status", _STEP_STATUSES)),
    )

    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("action_hash", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("expiry", _TS, nullable=False),
        sa.Column("consumed_at", _TS, nullable=True),
        sa.Column("consumed_by_action_id", sa.String(), nullable=True),
        _created(),
        sa.PrimaryKeyConstraint("approval_id", name="pk_approvals"),
    )

    op.create_table(
        "action_intents",
        sa.Column("action_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("step_id", sa.String(), nullable=False),
        sa.Column("tool", sa.String(), nullable=False),
        sa.Column("executor", sa.String(), nullable=False),
        sa.Column("params_hash", sa.String(), nullable=False),
        sa.Column("side_effect_class", sa.String(), nullable=False),
        sa.Column("idempotency", sa.String(), nullable=False),
        sa.Column("approval_ref", sa.String(), nullable=True),
        sa.Column("presence_bound", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        _version(),
        _created(),
        sa.Column("updated_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("action_id", name="pk_action_intents"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], name="fk_action_intents_run_id_runs"),
        sa.ForeignKeyConstraint(["step_id"], ["steps.step_id"], name="fk_action_intents_step_id_steps"),
        sa.ForeignKeyConstraint(
            ["approval_ref"], ["approvals.approval_id"], name="fk_action_intents_approval_ref_approvals"
        ),
        _check("action_intents", "status", _in("status", _ACTION_STATUSES)),
        _check(
            "action_intents", "side_effect_class",
            "side_effect_class IN ('internal_read', 'reversible_local', 'external_effect', 'high_impact')",
        ),
        _check("action_intents", "idempotency", "idempotency IN ('native', 'via_action_id', 'none')"),
    )
    op.create_index("ix_action_intents_run_id", "action_intents", ["run_id"])
    op.create_index("ix_action_intents_step_id", "action_intents", ["step_id"])

    op.create_table(
        "receipts",
        sa.Column("receipt_id", sa.String(), nullable=False),
        sa.Column("action_id", sa.String(), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("result_ref", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("source_verified", sa.Boolean(), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.Column("recorded_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("receipt_id", name="pk_receipts"),
        sa.UniqueConstraint("action_id", name="uq_receipts_action_id"),
        sa.ForeignKeyConstraint(
            ["action_id"], ["action_intents.action_id"], name="fk_receipts_action_id_action_intents"
        ),
    )


def downgrade() -> None:
    op.drop_table("receipts")
    op.drop_index("ix_action_intents_step_id", table_name="action_intents")
    op.drop_index("ix_action_intents_run_id", table_name="action_intents")
    op.drop_table("action_intents")
    op.drop_table("approvals")
    op.drop_table("steps")
    op.drop_index("uq_runs_one_active_per_task", table_name="runs")
    op.drop_index("ix_runs_task_id", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("activities")
