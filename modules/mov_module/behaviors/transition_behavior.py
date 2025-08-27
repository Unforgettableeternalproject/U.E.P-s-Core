from __future__ import annotations
import random

from .base_behavior import BaseBehavior, BehaviorContext
from ..core.state_machine import BehaviorState, MovementMode


class TransitionBehavior(BaseBehavior):
    state = BehaviorState.TRANSITION

    def on_enter(self, ctx: BehaviorContext) -> None:
        target_mode = ctx.sm.decide_transition_target(ctx.movement_mode)
        if not target_mode:
            return
        ctx.transition_start_time = ctx.now
        ctx.movement_locked_until = ctx.now + ctx.sm.transition_duration
        # 觸發對應轉場動畫（非循環）
        ctx.trigger_anim("f_to_g" if target_mode == MovementMode.GROUND else "g_to_f", {"loop": False})
        # 存起來給 tick 用
        self._target_mode = target_mode
        self._start_y = ctx.position.y
        self._float_target_y = random.uniform(100, 300)

    def on_tick(self, ctx: BehaviorContext):
        if ctx.transition_start_time is None:
            return BehaviorState.IDLE
        prog = ctx.sm.transition_progress(ctx.transition_start_time, ctx.now)
        gy = ctx.ground_y()

        if self._target_mode == MovementMode.GROUND:
            eased = 1 - (1 - prog) ** 2
            ctx.position.y = self._start_y + (gy - self._start_y) * eased
        else:
            eased = prog * prog
            ctx.position.y = gy + (self._float_target_y - gy) * eased

        # 完成條件：時間到或超時
        if prog >= 1.0 or ctx.now >= ctx.movement_locked_until:
            ctx.movement_mode = self._target_mode
            ctx.transition_start_time = None
            ctx.movement_locked_until = 0.0
            # 轉場後交給狀態機決定下一步
            return ctx.sm.pick_next(ctx.movement_mode)
        return None