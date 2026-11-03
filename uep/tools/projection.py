"""投影脈絡（Agent 架構提案 §5.6、§6.1，切片 B）。

``ProjectionContext`` 是 ``ToolRegistry.project`` 的場域輸入：全部來自 Core 的政策快照與 Presence，
**不取自模型輸入**。決策層只負責產生已收窄的 ``capability_tags``（I-1），脈絡由工具層的呼叫端提供。
"""
from __future__ import annotations

from dataclasses import dataclass

# 投影條件 audience ≥ audience_min 與 SYS 增補 PD-13／VC-PD-7（audience 收窄 ⇒ denied）一致。
# audience 的寬度排序（與 uep.events.envelope 的 scope 層級同序：private < group < public）
AUDIENCE_RANK: dict[str, int] = {"private": 0, "group": 1, "public": 2}


def audience_rank(level: str) -> int:
    """``private``／``group``／``public`` 或 ``scope_<level>_<id>`` 序列化字串 -> 排序值；不合法拋 ValueError。"""
    if level in AUDIENCE_RANK:
        return AUDIENCE_RANK[level]
    # 延後 import：envelope 較重，且只有序列化字串才需要
    from uep.events.envelope import scope_rank

    return scope_rank(level)


@dataclass(frozen=True)
class ProjectionContext:
    """``platform``：當下 Presence 所在場域（``desktop``／``discord``／``website``…），None 表示沒有場域。
    ``presence_active_platforms``：Presence 目前 ACTIVE 的場域集合（``presence=="bound"`` 的工具需要
    ``platform`` 在其中）。``available_context``：投影所需脈絡（例如 ``guild_roster``）。
    ``audience``：當下 audience 層級。"""

    platform: str | None = None
    presence_active_platforms: frozenset[str] = frozenset()
    available_context: frozenset[str] = frozenset()
    audience: str = "private"

    def __post_init__(self) -> None:
        audience_rank(self.audience)  # 不合法的 audience 當場失敗，不留到投影時靜默通過

    def presence_active(self) -> bool:
        return self.platform is not None and self.platform in self.presence_active_platforms
