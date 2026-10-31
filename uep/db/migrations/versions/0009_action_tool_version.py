"""TG-1／D55：action_intents.tool_version

Revision ID: 0009_action_tool_version
Revises: 0008_receipt_worker_epoch
Create Date: 2026-10-31

action 建立時由工具註冊表寫入的工具版本，進 action hash 的域（params_hash = H({"tool","v","params"})）。
既有列回填 1（升級前沒有版本概念，視為 v1）；NOT NULL、server_default 1。
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_action_tool_version"
down_revision = "0008_receipt_worker_epoch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "action_intents", sa.Column("tool_version", sa.Integer(), nullable=False, server_default="1")
    )


def downgrade() -> None:
    op.drop_column("action_intents", "tool_version")
