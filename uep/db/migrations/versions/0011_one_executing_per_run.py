"""D62／TC-3：同一個 Run 至多一個 EXECUTING 的部分唯一索引

Revision ID: 0011_one_executing_per_run
Revises: 0010_decision_log
Create Date: 2026-11-03

首版只有 serial 動作，並行資格（切片 D／F）啟用前不放寬；啟用時此索引依 Agent 架構提案 §6.5 改為應用層資格檢查。
注意：升級前若已有同 Run 多個 EXECUTING 的列，索引建立會失敗，須先人工收尾（正常流程不會出現）。
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_one_executing_per_run"
down_revision = "0010_decision_log"  # 決策層 DL-3；0010 尚未合入 develop 時本檔無法單獨升級，見 PR 說明
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_action_intents_one_executing_per_run", "action_intents", ["run_id"], unique=True,
        postgresql_where=sa.text("status = 'EXECUTING'"),
    )


def downgrade() -> None:
    op.drop_index("uq_action_intents_one_executing_per_run", table_name="action_intents")
