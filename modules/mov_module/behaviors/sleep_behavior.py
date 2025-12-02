from __future__ import annotations
import time
from typing import Optional

from .base_behavior import BaseBehavior, BehaviorContext
from ..core.state_machine import BehaviorState, MovementMode
from ..core.animation_priority import AnimationPriority

class SleepBehavior(BaseBehavior):
    """睡眠行為：負責驅動 g_to_l → sleep_l 循環，並在喚醒時讓協調器切回 IDLE。
    
    這個行為的 tick 幾乎不做移動，只維持睡眠動畫。
    """

    def on_enter(self, ctx: BehaviorContext) -> Optional[BehaviorState]:
        # 進入睡眠：確保在地面，播放 g_to_l，再切 sleep_l
        is_ground = (ctx.movement_mode == MovementMode.GROUND)
        if not is_ground:
            # 如果不是地面，交由協調器處理轉換（已在 MOV 中做）
            pass
        # 直接請求 g_to_l，之後由 MOV 轉到 sleep_l
        ctx.trigger_anim(
            "g_to_l",
            {
                "loop": False,
                "force_restart": True,
                "priority": AnimationPriority.SYSTEM_CYCLE  # 以系統級優先度壓過一般動畫
            }
        )
        return None

    def on_tick(self, ctx: BehaviorContext) -> Optional[BehaviorState]:
        # 睡眠時不移動，維持 sleep_l，如果尚未切換則再次嘗試觸發
        now = ctx.now
        # 略過移動更新，僅保障動畫
        if ctx.current_layer:
            # 層級動畫期間不改動睡眠姿勢
            return None
        # 如果沒有在播放 sleep_l，要求播放
        ctx.trigger_anim(
            "sleep_l",
            {
                "loop": True,
                "priority": AnimationPriority.SYSTEM_CYCLE,
            }
        )
        return BehaviorState.SLEEPING  # 維持睡眠狀態
