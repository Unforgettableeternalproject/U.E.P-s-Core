"""決策日誌新增 sub_kind／trigger／namespace／snapshot_version 四欄

Revision ID: 0014_decision_log_columns
Revises: 0013_run_supervisor_flags
Create Date: 2026-11-06

- ``sub_kind``：決策子類（例如 loop.consult 的 sub_kind）；``trigger``：決策觸發（initial／reeval／event／tick）；
  ``namespace``：決策主體的 namespace ID；``snapshot_version``：決策依據的 policy 快照版本。
- 只加不改：四欄皆 nullable，既有列為 NULL，不改動任何既有欄位、索引與資料；重放工具讀到 NULL 行為不變。
- 同樣只存 ID 與列舉，不存原文。新增索引 ``ix_decision_log_kind_sub_kind`` 供依 sub_kind 篩選。
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_decision_log_columns"
down_revision = "0013_run_supervisor_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("decision_log", sa.Column("sub_kind", sa.String(), nullable=True))
    op.add_column("decision_log", sa.Column("trigger", sa.String(), nullable=True))
    op.add_column("decision_log", sa.Column("namespace", sa.String(), nullable=True))
    op.add_column("decision_log", sa.Column("snapshot_version", sa.Integer(), nullable=True))
    op.create_index("ix_decision_log_kind_sub_kind", "decision_log", ["kind", "sub_kind"])


def downgrade() -> None:
    op.drop_index("ix_decision_log_kind_sub_kind", table_name="decision_log")
    op.drop_column("decision_log", "snapshot_version")
    op.drop_column("decision_log", "namespace")
    op.drop_column("decision_log", "trigger")
    op.drop_column("decision_log", "sub_kind")
