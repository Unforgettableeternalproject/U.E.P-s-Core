"""決策日誌新增 target 欄

Revision ID: 0019_decision_log_target
Revises: 0018_decision_log_platform_ctx
Create Date: 2026-11-12

- ``target``：決策指向的目標位置（``DecisionPlan.placement.target_platform``，場域列舉字串，如 desktop／discord），
  原本 ``presence.absent_intent`` 把它拼進 ``reason_code``（``presence_request.discord``）；改為獨立欄位，
  ``reason_code`` 只留純代碼。
- nullable、不加索引；既有列預設 NULL。
- 回填：僅針對 ``kind='presence.absent_intent'`` 且 ``reason_code`` 恰為 ``presence_request.<desktop|discord|website|host>``
  的列，把後綴搬進 ``target`` 並還原 ``reason_code='presence_request'``；其他列一律不動。
- downgrade：把上述列的 ``target`` 併回 ``reason_code`` 再移除欄位（可逆）。
"""
import sqlalchemy as sa
from alembic import op

revision = "0019_decision_log_target"
down_revision = "0018_decision_log_platform_ctx"
branch_labels = None
depends_on = None

_KIND = "presence.absent_intent"
_LOCATIONS = ("desktop", "discord", "website", "host")


def upgrade() -> None:
    op.add_column("decision_log", sa.Column("target", sa.String(), nullable=True))
    for loc in _LOCATIONS:
        op.execute(
            sa.text(
                "UPDATE decision_log SET target = :loc, reason_code = 'presence_request' "
                "WHERE kind = :kind AND reason_code = :old"
            ).bindparams(loc=loc, kind=_KIND, old=f"presence_request.{loc}")
        )


def downgrade() -> None:
    for loc in _LOCATIONS:
        op.execute(
            sa.text(
                "UPDATE decision_log SET reason_code = :old "
                "WHERE kind = :kind AND reason_code = 'presence_request' AND target = :loc"
            ).bindparams(loc=loc, kind=_KIND, old=f"presence_request.{loc}")
        )
    op.drop_column("decision_log", "target")
