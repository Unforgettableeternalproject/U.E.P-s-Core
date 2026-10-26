"""D41：events.txid（寫入交易 id），供 journal tailing 的安全水位使用

Revision ID: 0006_journal_txid
Revises: 0005_state_stop
Create Date: 2026-10-27

既有列的 txid 補 0（視為早已提交完成）；新列由預設值 ``txid_current()`` 記錄。
先以常數預設加欄（不改寫資料表、不觸發 append-only trigger），再把預設換成 ``txid_current()``。
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_journal_txid"
down_revision = "0005_state_stop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("txid", sa.BigInteger(), server_default="0", nullable=False))
    op.alter_column("events", "txid", server_default=sa.text("txid_current()"))
    op.create_index("ix_events_txid", "events", ["txid"])


def downgrade() -> None:
    op.drop_index("ix_events_txid", table_name="events")
    op.drop_column("events", "txid")
