from __future__ import annotations
from typing import Optional

from .base_behavior import BaseBehavior, BehaviorContext
from ..core.state_machine import BehaviorState


class IdleBehavior(BaseBehavior):
    state = BehaviorState.IDLE

    def on_enter(self, ctx: BehaviorContext) -> None:
        # 停止移動
        ctx.velocity.x = 0.0
        ctx.velocity.y = 0.0
        ctx.target_velocity.x = 0.0
        ctx.target_velocity.y = 0.0
        
        # 只在不是從移動狀態切換過來時播放閒置動畫
        # （從移動切換過來時，轉向動畫會自動接閒置動畫）
        prev_state = getattr(ctx, 'previous_state', None)
        if prev_state != BehaviorState.NORMAL_MOVE:
            # 播對應模式的 idle 動畫
            idle_anim = "stand_idle_g" if ctx.movement_mode.value == "ground" else "smile_idle_f"
            ctx.trigger_anim(idle_anim, {"loop": True})
        
        # 標記 idle 起點
        ctx.sm.begin_idle(ctx.now)

    def on_tick(self, ctx: BehaviorContext):
        if ctx.sm.should_exit_idle(ctx.now):
            # 用狀態機的權重決定下一步（行為本身不直接下移動目標，交給協調器切行為才做）
            return ctx.sm.pick_next(ctx.movement_mode)
        return None