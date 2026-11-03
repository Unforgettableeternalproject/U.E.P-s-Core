"""DL-3：decision_log（決策中樞的決策日誌）

Revision ID: 0010_decision_log
Revises: 0009_action_tool_version
Create Date: 2026-11-03

primary 與 shadow 決策同表（role 區分）；shadow 列以 primary_decision_id 指向 primary。
只存摘要，不存原文（utterance_refs 指回事件）。非熱路徑寫入（背景批次），表無外鍵，丟失不影響其他資料。
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_decision_log"
down_revision = "0009_action_tool_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_log",
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("primary_decision_id", sa.String(), nullable=True),
        sa.Column("engine", sa.String(), nullable=True),
        sa.Column("proposer_id", sa.String(), nullable=True),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("reason_code", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("agree", sa.Boolean(), nullable=True),
        sa.Column("fallback", sa.Boolean(), nullable=True),
        sa.Column("candidate_id", sa.String(), nullable=True),
        sa.Column("participant_count", sa.Integer(), nullable=True),
        sa.Column("addressed_hint", sa.Boolean(), nullable=True),
        sa.Column("utterance_refs", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("decision_id", name="pk_decision_log"),
        sa.CheckConstraint("role IN ('primary', 'shadow')", name="ck_decision_log_role"),
    )
    op.create_index("ix_decision_log_kind_created_at", "decision_log", ["kind", "created_at"])
    op.create_index("ix_decision_log_created_at", "decision_log", ["created_at"])
    op.create_index("ix_decision_log_request_id", "decision_log", ["request_id"])
    op.create_index("ix_decision_log_primary_decision_id", "decision_log", ["primary_decision_id"])


def downgrade() -> None:
    op.drop_table("decision_log")
