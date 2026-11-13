"""presence_current 的 location CHECK 加入 console（測試 Console 的獨立平台）

Revision ID: 0022_console_location
Revises: 0021_presence_plan
Create Date: 2026-11-13

- ``presence_current.location`` 的 CHECK 允許值加入 ``console``（測試模式的 Console 持有 Lease、在場時使用）。
- ``presence_plan_items`` / ``presence_plan_overrides`` 不動：console 不是行程可指派的位置，只存在於測試模式。
- ``presence_leases.location`` 沒有 CHECK，不需變更。
- 約束名稱沿用既有慣例（0004 建立）：傳入 ``ck_presence_current_location``，由命名慣例展開為
  ``ck_presence_current_ck_presence_current_location``。
- downgrade：若 ``presence_current`` 仍有 ``console`` 資料列，**拒絕降版**（RuntimeError），
  不自動刪除或改寫資料；請先手動處理這些列再降版。
"""
import sqlalchemy as sa
from alembic import context, op

revision = "0022_console_location"
down_revision = "0021_presence_plan"
branch_labels = None
depends_on = None

_TABLE = "presence_current"
_NAME = "ck_presence_current_location"
_OLD = ("desktop", "discord", "website")
_NEW = (*_OLD, "console")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _actual_name() -> str:
    """online：從資料庫找出實際的 location CHECK 名稱（不依賴命名慣例展開結果）；offline 退回寫死的雙重前綴。"""
    if context.is_offline_mode():
        return f"ck_{_TABLE}_{_NAME}"
    found = [
        c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(_TABLE)
        if c.get("name") and "location" in c["sqltext"] and "desktop" in c["sqltext"]
    ]
    if len(found) != 1:
        raise RuntimeError(f"{_TABLE} 找不到唯一的 location CHECK 約束（符合者：{found}），無法安全替換")
    return found[0]


def _replace(values: tuple[str, ...]) -> None:
    name = _actual_name()
    op.drop_constraint(op.f(name), _TABLE, type_="check")
    op.create_check_constraint(op.f(name), _TABLE, "location IS NULL OR " + _in("location", values))


def upgrade() -> None:
    _replace(_NEW)


def downgrade() -> None:
    if not context.is_offline_mode():  # 離線產 SQL 時沒有連線可查
        bind = op.get_bind()
        count = bind.execute(sa.text(f"SELECT count(*) FROM {_TABLE} WHERE location = 'console'")).scalar_one()
        if count:
            raise RuntimeError(
                f"{_TABLE} 仍有 {count} 筆 location='console' 的資料，拒絕降版；請先手動處理這些列"
            )
    _replace(_OLD)
