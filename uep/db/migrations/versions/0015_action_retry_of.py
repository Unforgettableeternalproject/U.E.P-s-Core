"""action_intents.retry_of：重試來源（CC-6 §6.5、SA-11／D52）

Revision ID: 0015_action_retry_of
Revises: 0014_decision_log_columns
Create Date: 2026-11-10

- ``retry_of`` 指向被重試的 FAILED action，或被人工標記 retry（``resolve_unknown_manual``）後轉 FAILED 的 UNKNOWN。
- 只加不改：nullable，既有列為 NULL；FK 自參照 ``action_intents.action_id``。
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_action_retry_of"
down_revision = "0014_decision_log_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("action_intents", sa.Column("retry_of", sa.String(), nullable=True))
    op.create_foreign_key(
        op.f("fk_action_intents_retry_of_action_intents"), "action_intents", "action_intents",
        ["retry_of"], ["action_id"],
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_action_intents_retry_of_action_intents"), "action_intents", type_="foreignkey")
    op.drop_column("action_intents", "retry_of")
