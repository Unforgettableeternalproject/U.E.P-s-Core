"""行程項 interrupt_policy 拆為 min_rank 與 interrupt_timing（權限契約 §3.6，PM-2 第一步）

Revision ID: 0023_plan_min_rank_timing
Revises: 0022_console_location
Create Date: 2026-11-13

- ``presence_plan_items`` 新增 ``min_rank``（可為 NULL；RequesterTier 值，只允許 50／75／100／200，
  只能抬高門檻）與 ``interrupt_timing``（可為 NULL；free／safe_point）。
- 回填舊值：free → timing=free；safe_point → timing=safe_point；
  owner_only → min_rank=200（OWNER）且 timing=free（刻意的行為變更：不再因進行中互動而等待）。
- **舊 ``interrupt_policy`` 欄位與其 CHECK 保留**（只讀相容）；讀取以新欄位優先，兩個新欄位都是 NULL 才回退舊欄。
  舊欄位於下一個 migration（PM-7）才刪，避免回滾困難。
- 約束名稱以 ``op.f`` 明寫完整名稱，與 ORM 模型的命名慣例展開結果一致。
- downgrade：先把仍為 NULL 的舊欄由新欄位回填（owner_only／free／safe_point；ADMIN 等無法對應的門檻會遺失，
  只保留時機），再移除約束與欄位。
"""
import sqlalchemy as sa
from alembic import op

revision = "0023_plan_min_rank_timing"
down_revision = "0022_console_location"
branch_labels = None
depends_on = None

_TABLE = "presence_plan_items"
_CK_RANK = "ck_presence_plan_items_min_rank"
_CK_TIMING = "ck_presence_plan_items_interrupt_timing"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("min_rank", sa.SmallInteger(), nullable=True))
    op.add_column(_TABLE, sa.Column("interrupt_timing", sa.String(), nullable=True))
    op.execute(
        f"UPDATE {_TABLE} SET "
        "min_rank = CASE WHEN interrupt_policy = 'owner_only' THEN 200 END, "
        "interrupt_timing = CASE interrupt_policy WHEN 'owner_only' THEN 'free' WHEN 'free' THEN 'free' "
        "WHEN 'safe_point' THEN 'safe_point' END "
        "WHERE interrupt_policy IS NOT NULL")
    op.create_check_constraint(op.f(_CK_RANK), _TABLE, "min_rank IS NULL OR min_rank IN (50, 75, 100, 200)")
    op.create_check_constraint(op.f(_CK_TIMING), _TABLE,
                               "interrupt_timing IS NULL OR interrupt_timing IN ('free', 'safe_point')")


def downgrade() -> None:
    op.execute(
        f"UPDATE {_TABLE} SET interrupt_policy = CASE "
        "WHEN min_rank = 200 THEN 'owner_only' "
        "WHEN interrupt_timing = 'free' THEN 'free' "
        "WHEN interrupt_timing = 'safe_point' THEN 'safe_point' END "
        "WHERE interrupt_policy IS NULL AND (min_rank IS NOT NULL OR interrupt_timing IS NOT NULL)")
    op.drop_constraint(op.f(_CK_TIMING), _TABLE, type_="check")
    op.drop_constraint(op.f(_CK_RANK), _TABLE, type_="check")
    op.drop_column(_TABLE, "interrupt_timing")
    op.drop_column(_TABLE, "min_rank")
