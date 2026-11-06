"""SV-2（切片 F-3）：runs 新增 Supervisor 欄位

Revision ID: 0013_run_supervisor_flags
Revises: 0012_action_concurrency
Create Date: 2026-11-06

- ``cancel_requested_at``／``cancel_reason``：Supervisor 對 Run 設定的取消旗標，worker 於下次 heartbeat 讀到；
  ``cancel_reason`` 限 run_deadline／task_deadline／step_timeout。
- ``approval_expired_at``：存「已通知的 approval_expiry 值」（不是通知時間），同一 expiry 只通知一次。
三欄皆 nullable，既有列為 NULL。
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_run_supervisor_flags"
down_revision = "0012_action_concurrency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("cancel_reason", sa.String(), nullable=True))
    op.add_column("runs", sa.Column("approval_expired_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_runs_cancel_reason"), "runs",
        "cancel_reason IS NULL OR cancel_reason IN ('run_deadline', 'task_deadline', 'step_timeout')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_runs_cancel_reason"), "runs", type_="check")
    op.drop_column("runs", "approval_expired_at")
    op.drop_column("runs", "cancel_reason")
    op.drop_column("runs", "cancel_requested_at")
