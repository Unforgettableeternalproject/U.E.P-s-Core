"""tasks／runs 暫停前狀態 paused_from（SA-7、D49）

Revision ID: 0017_paused_from
Revises: 0016_approval_revoked
Create Date: 2026-11-10

- ``paused_from``：轉入 PAUSED 前的狀態（PENDING／RUNNING／WAITING_INPUT／WAITING_APPROVAL），恢復後清除；Task 與 Run 各存各的。
- 全 nullable，既有 PAUSED 列為 NULL（恢復時回 RUNNING 並記 warning）。
- CHECK：值域限定，且只有 PAUSED 列可有值。
"""
import sqlalchemy as sa
from alembic import op

revision = "0017_paused_from"
down_revision = "0016_approval_revoked"
branch_labels = None
depends_on = None

_VALUES = "paused_from IS NULL OR paused_from IN ('PENDING', 'RUNNING', 'WAITING_INPUT', 'WAITING_APPROVAL')"


def upgrade() -> None:
    for table in ("tasks", "runs"):
        op.add_column(table, sa.Column("paused_from", sa.String(), nullable=True))
        op.create_check_constraint(op.f(f"ck_{table}_paused_from"), table, _VALUES)
        op.create_check_constraint(
            op.f(f"ck_{table}_paused_from_only_paused"), table, "status = 'PAUSED' OR paused_from IS NULL"
        )


def downgrade() -> None:
    for table in ("runs", "tasks"):
        op.drop_constraint(op.f(f"ck_{table}_paused_from_only_paused"), table, type_="check")
        op.drop_constraint(op.f(f"ck_{table}_paused_from"), table, type_="check")
        op.drop_column(table, "paused_from")
