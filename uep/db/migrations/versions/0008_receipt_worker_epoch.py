"""T-24：receipts.worker_epoch（契約 §6.5 列出的欄位）

Revision ID: 0008_receipt_worker_epoch
Revises: 0007_journal_truncate_guard
Create Date: 2026-10-27

記錄 receipt 是在哪個 worker_epoch 的呼叫下取得的，供 resolve_unknown／稽核判斷是否為舊 epoch 的證據。
可為空：既有列與無 epoch 的來源（resolve_unknown 查核到的 receipt）不填。
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_receipt_worker_epoch"
down_revision = "0007_journal_truncate_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("receipts", sa.Column("worker_epoch", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("receipts", "worker_epoch")
