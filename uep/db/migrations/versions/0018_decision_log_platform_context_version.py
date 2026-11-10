"""決策日誌新增 platform_context_version 欄

Revision ID: 0018_decision_log_platform_ctx
Revises: 0017_paused_from
Create Date: 2026-11-10

- ``platform_context_version``：決策當下的平台快照版本（``PlatformContext.version``，int），供自決事後稽核
  （平台宣告注入端契約草案 §11、SYS 契約增補 PD-18）。
- 只加不改：nullable、不加索引；既有列為 NULL，不改動任何既有欄位、索引與資料。
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_decision_log_platform_ctx"
down_revision = "0017_paused_from"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("decision_log", sa.Column("platform_context_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("decision_log", "platform_context_version")
