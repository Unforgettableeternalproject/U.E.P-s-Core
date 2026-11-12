"""決策日誌新增 chain_id／chain_seq 欄

Revision ID: 0020_decision_log_chain
Revises: 0019_decision_log_target
Create Date: 2026-11-12

- ``chain_id``：複合句 chain 的 ID（``chain_<ULID>``，含大寫），供 ``decision_log(chain_id)`` 反查段落 turn。
- ``chain_seq``：chain 內序號（0＝母句，1 起為段落），SMALLINT。
- 兩欄 nullable；既有列為 NULL，不回填。索引 ``(chain_id, chain_seq)``。
- downgrade：先移除索引再移除欄位（可逆；欄位內容會遺失）。
"""
import sqlalchemy as sa
from alembic import op

revision = "0020_decision_log_chain"
down_revision = "0019_decision_log_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("decision_log", sa.Column("chain_id", sa.String(), nullable=True))
    op.add_column("decision_log", sa.Column("chain_seq", sa.SmallInteger(), nullable=True))
    op.create_index("ix_decision_log_chain", "decision_log", ["chain_id", "chain_seq"])


def downgrade() -> None:
    op.drop_index("ix_decision_log_chain", table_name="decision_log")
    op.drop_column("decision_log", "chain_seq")
    op.drop_column("decision_log", "chain_id")
