"""Behavior 合理化（純函式，PP-0；契約草案 §8）：行程建議 → 主機合理化 → 行為決定。

同樣輸入恆得同樣輸出（BH-4）。本模組不寫任何狀態；driver（PP-3）才負責提 CC-7 請求。
``BEHAVIOR_TRANSITIONS`` 為 §8.4 擴充後的預期轉換表（含 REST）；coordinator 尚未加入 REST，PP-3 對齊。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from uep.contracts.schedule import (
    MODE_CHAT,
    MODE_MISCHIEF,
    MODE_REST,
    MODE_SLEEP,
    MODE_WORK,
    PlanSlot,
)

REST_FAMILY = frozenset({MODE_REST, MODE_SLEEP})
PRESENCE_FAMILY = frozenset({MODE_CHAT, MODE_WORK, MODE_MISCHIEF})

# §8.4 轉換表（順序即 BFS 的確定性優先序）
BEHAVIOR_TRANSITIONS: dict[str, tuple[str, ...]] = {
    MODE_REST: (MODE_SLEEP, MODE_CHAT),
    MODE_SLEEP: (MODE_REST, MODE_CHAT),
    MODE_CHAT: (MODE_REST, MODE_SLEEP, MODE_WORK, MODE_MISCHIEF),
    MODE_WORK: (MODE_CHAT, MODE_REST, MODE_SLEEP, MODE_MISCHIEF),
    MODE_MISCHIEF: (MODE_CHAT, MODE_WORK, MODE_REST, MODE_SLEEP),
}


@dataclass(frozen=True)
class BehaviorProfile:
    """B-4：消費者讀屬性而非比對模式字串。muted 與 dream_policy 分開（BH-6）。"""

    mode: str
    family: Literal["rest", "presence"]
    muted: bool
    dream_policy: Literal["normal", "rest_relaxed"]
    wake_policy: Literal["immediate", "finish_dream_round", "safe_point", "none"]
    enterable: bool = True


BEHAVIOR_PROFILES: dict[str, BehaviorProfile] = {
    MODE_REST: BehaviorProfile(MODE_REST, "rest", True, "normal", "immediate"),
    MODE_SLEEP: BehaviorProfile(MODE_SLEEP, "rest", True, "rest_relaxed", "finish_dream_round"),
    MODE_CHAT: BehaviorProfile(MODE_CHAT, "presence", False, "normal", "none"),
    MODE_WORK: BehaviorProfile(MODE_WORK, "presence", False, "normal", "safe_point"),
    MODE_MISCHIEF: BehaviorProfile(MODE_MISCHIEF, "presence", False, "normal", "none", enterable=False),  # B-6
}


@dataclass(frozen=True)
class RationalizeInput:
    slot: PlanSlot
    presence_status: Literal["NONE", "ACTIVE", "RECOVERING"]
    current_behavior: str
    now: datetime
    behavior_entered_at: datetime | None = None
    interaction_idle: bool = True
    has_uninterruptible_work: bool = False
    dream_in_flight: bool = False
    global_stop: bool = False
    runtime_ready: bool = True
    autonomy_enabled: bool = False  # v1 恆 False


@dataclass(frozen=True)
class RationalizePolicy:
    min_dwell_s: int = 60  # 防抖最短停留；建議值，暫定
    recheck_s: int = 5  # HOLD 的下次檢查間隔（對應建議 tick）；暫定


@dataclass(frozen=True)
class BehaviorDecision:
    outcome: Literal["ACCEPT_SUGGESTION", "SUBSTITUTE", "HOLD", "NOOP", "REJECT_ALL"]
    target: str | None  # 本步要提請的模式（HOLD＝想去但暫不提請；REJECT_ALL＝None）
    reason_code: str
    path: tuple[str, ...] = ()  # 到最終目標的完整路徑（含 target 在首位）
    suggestion_rank: int | None = None
    recheck_at: datetime | None = None


def plan_path(current: str, goal: str) -> tuple[str, ...]:
    """轉換表上的最短路徑（不含 current）；如 SLEEP→WORK ＝ (CHAT, WORK)。無路徑時直達。"""
    if current == goal:
        return ()
    queue = deque([(current, ())])
    seen = {current}
    while queue:
        node, path = queue.popleft()
        for nxt in BEHAVIOR_TRANSITIONS.get(node, ()):
            if nxt in seen:
                continue
            if nxt == goal:
                return (*path, nxt)
            seen.add(nxt)
            queue.append((nxt, (*path, nxt)))
    return (goal,)


def rationalize(inp: RationalizeInput, policy: RationalizePolicy | None = None) -> BehaviorDecision:
    policy = policy or RationalizePolicy()
    now = inp.now
    recheck = now + timedelta(seconds=policy.recheck_s)
    rest_side = inp.presence_status in ("NONE", "RECOVERING")  # RECOVERING 視同 NONE
    family = REST_FAMILY if rest_side else PRESENCE_FAMILY
    fallback_mode = MODE_REST if rest_side else MODE_CHAT

    # 1. 候選池（行程不可用 → 空）
    ordered = sorted(inp.slot.suggestions, key=lambda s: s.rank) if inp.slot.availability == "available" else []
    # 2. 位置一致性（BH-1，硬約束）
    pool = [s for s in ordered if s.mode in family]
    chosen_rank: int | None
    if not inp.runtime_ready:  # Runtime 非 READY/DEGRADED → 暫緩
        return BehaviorDecision("HOLD", None, "runtime_not_ready", recheck_at=recheck)
    if not pool:
        pool_mode, reason, chosen_rank, substituted = fallback_mode, "presence_fallback", None, True
    else:
        # 3. 硬禁止
        removed: list[str] = []
        kept = []
        for s in pool:
            if s.mode == MODE_MISCHIEF and not inp.autonomy_enabled:
                removed.append("autonomy_not_enabled")
            elif inp.global_stop and s.mode in (MODE_WORK, MODE_MISCHIEF):
                removed.append("substituted_stop")
            else:
                kept.append(s)
        if kept:
            first = kept[0]
            substituted = first.rank != pool[0].rank
            pool_mode, chosen_rank = first.mode, first.rank
            reason = (removed[0] if substituted and removed else first.reason_code)
        elif "substituted_stop" in removed:
            pool_mode, reason, chosen_rank, substituted = fallback_mode, "substituted_stop", None, True
        else:
            return BehaviorDecision("REJECT_ALL", None, "autonomy_not_enabled")

    # 4. 與當前比較、規劃路徑
    current = inp.current_behavior
    if current == pool_mode:
        return BehaviorDecision("NOOP", pool_mode, "already_in_target", suggestion_rank=chosen_rank)
    path = plan_path(current, pool_mode)
    step = path[0]
    outcome = "SUBSTITUTE" if substituted else "ACCEPT_SUGGESTION"

    # 5. 當下事實保持
    if current in (MODE_CHAT, MODE_WORK) and not inp.interaction_idle:
        return BehaviorDecision("HOLD", step, "interaction_active", path, chosen_rank, recheck)
    if current == MODE_SLEEP and inp.dream_in_flight:
        return BehaviorDecision("HOLD", step, "dream_in_flight", path, chosen_rank, recheck)
    if current == MODE_WORK and inp.has_uninterruptible_work:
        return BehaviorDecision("HOLD", step, "uninterruptible_work", path, chosen_rank, recheck)
    hard = (current not in family) or (inp.global_stop and current in (MODE_WORK, MODE_MISCHIEF))
    if not hard and inp.behavior_entered_at is not None:
        stay_until = inp.behavior_entered_at + timedelta(seconds=policy.min_dwell_s)
        if now < stay_until:
            return BehaviorDecision("HOLD", step, "min_dwell", path, chosen_rank, stay_until)

    # 6. 輸出（driver 以 core.policy 經 CC-7 提請 step）
    return BehaviorDecision(outcome, step, reason, path, chosen_rank)
