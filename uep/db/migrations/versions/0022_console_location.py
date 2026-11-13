"""Presence／行程的 location CHECK 加入 console（測試 Console 的獨立平台）

Revision ID: 0022_console_location
Revises: 0021_presence_plan
Create Date: 2026-11-13

- ``presence_current.location``、``presence_plan_items.location`` 的 CHECK 允許值加入 ``console``。
- ``presence_plan_overrides.location`` 不動：override 刻意不含 console（緊急 override 不能把她移到測試入口）。
- ``presence_leases.location`` 沒有 CHECK，不需變更。
- downgrade：若 ``presence_current`` 或 ``presence_plan_items`` 仍有 ``console`` 資料列，**拒絕降版**（RuntimeError），
  不自動刪除或改寫資料；請先手動處理這些列再降版。
"""
import sqlalchemy as sa
from alembic import op

revision = "0022_console_location"
down_revision = "0021_presence_plan"
branch_labels = None
depends_on = None

_OLD_PRESENCE = ("desktop", "discord", "website")
_OLD_PLAN = ("desktop", "discord", "website", "host")
_TARGETS = (
    ("presence_current", "ck_presence_current_location", _OLD_PRESENCE),
    ("presence_plan_items", "ck_presence_plan_items_location", _OLD_PLAN),
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _replace(table: str, name: str, values: tuple[str, ...]) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, "location IS NULL OR " + _in("location", values))


def upgrade() -> None:
    for table, name, old in _TARGETS:
        _replace(table, name, (*old[:3], "console", *old[3:]))


def downgrade() -> None:
    bind = op.get_bind()
    for table, _name, _old in _TARGETS:
        count = bind.execute(sa.text(f"SELECT count(*) FROM {table} WHERE location = 'console'")).scalar_one()
        if count:
            raise RuntimeError(
                f"{table} 仍有 {count} 筆 location='console' 的資料，拒絕降版；請先手動處理這些列"
            )
    for table, name, old in _TARGETS:
        _replace(table, name, old)
