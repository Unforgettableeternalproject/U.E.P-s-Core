"""approvals 撤銷欄位（E2：人類否決時作廢可消耗的核准）

Revision ID: 0016_approval_revoked
Revises: 0015_action_retry_of
Create Date: 2026-11-10

- ``revoked_at``／``revoked_by``／``revoked_reason``／``revoked_by_action_id``：誰在何時因哪個 action 的否決撤銷了這筆核准。
- 只加不改：全 nullable，既有列為 NULL（未撤銷）。
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_approval_revoked"
down_revision = "0015_action_retry_of"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("approvals", sa.Column("revoked_by", sa.String(), nullable=True))
    op.add_column("approvals", sa.Column("revoked_reason", sa.String(), nullable=True))
    op.add_column("approvals", sa.Column("revoked_by_action_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("approvals", "revoked_by_action_id")
    op.drop_column("approvals", "revoked_reason")
    op.drop_column("approvals", "revoked_by")
    op.drop_column("approvals", "revoked_at")
