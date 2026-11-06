"""D62／TC-3 修訂（切片 D1b）：action_intents.concurrency，移除「一 Run 至多一個 EXECUTING」部分唯一索引

Revision ID: 0012_action_concurrency
Revises: 0011_one_executing_per_run
Create Date: 2026-11-06

依 Agent 架構提案 §6.5：同一 Run 的 EXECUTING 集合要麼恰好一個 serial，要麼全部是 parallel_safe。
該不變量改由 TaskManager 在持有 Run 鎖（task -> run FOR UPDATE）的同一交易內檢查並轉 EXECUTING。
concurrency 於 action 建立時由工具註冊表快照寫入，之後不可改；既有列回填 serial（fail-closed）。
注意：downgrade 會重建部分唯一索引，若同一 Run 已有多個 EXECUTING 的列，重建索引會失敗，須先人工收尾。
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_action_concurrency"
down_revision = "0011_one_executing_per_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_action_intents_one_executing_per_run", table_name="action_intents")
    op.add_column(
        "action_intents", sa.Column("concurrency", sa.String(), nullable=False, server_default="serial")
    )
    op.create_check_constraint(
        op.f("ck_action_intents_concurrency"), "action_intents", "concurrency IN ('parallel_safe', 'parallel_one_shot', 'serial')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_action_intents_concurrency"), "action_intents", type_="check")
    op.drop_column("action_intents", "concurrency")
    op.create_index(
        "uq_action_intents_one_executing_per_run", "action_intents", ["run_id"], unique=True,
        postgresql_where=sa.text("status = 'EXECUTING'"),
    )
