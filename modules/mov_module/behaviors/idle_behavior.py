from __future__ import annotations
from typing import Optional

from .base_behavior import BaseBehavior, BehaviorContext
from ..core.state_machine import BehaviorState


class IdleBehavior(BaseBehavior):
    state = BehaviorState.IDLE

    def __init__(self):
        super().__init__()
        self._has_triggered_idle_anim = False

    def on_enter(self, ctx: BehaviorContext) -> None:
        # 停止移動
        ctx.velocity.x = 0.0
        ctx.velocity.y = 0.0
        ctx.target_velocity.x = 0.0
        ctx.target_velocity.y = 0.0
        
        self._has_triggered_idle_anim = False
        
        # 延遲觸發閒置動畫，讓轉向動畫有時間完成
        prev_state = getattr(ctx, 'previous_state', None)
        if prev_state != BehaviorState.NORMAL_MOVE:
            # 立即播放閒置動畫（非移動狀態轉換過來）
            self._trigger_idle_animation(ctx)
        # 如果是從移動狀態轉換過來，等待轉向動畫完成後再觸發
        
        # 標記 idle 起點
        ctx.sm.begin_idle(ctx.now)

    def on_tick(self, ctx: BehaviorContext):
        # 如果還沒觸發閒置動畫且從移動狀態切換過來，檢查是否可以觸發了
        prev_state = getattr(ctx, 'previous_state', None)
        if not self._has_triggered_idle_anim and prev_state == BehaviorState.NORMAL_MOVE:
            # 延遲一點時間確保轉向動畫有機會播放
            if hasattr(self, '_idle_start_time'):
                elapsed = ctx.now - self._idle_start_time
                if elapsed > 0.5:  # 等待 0.5 秒
                    self._trigger_idle_animation(ctx)
            else:
                self._idle_start_time = ctx.now
        
        if ctx.sm.should_exit_idle(ctx.now):
            # 用狀態機的權重決定下一步
            return ctx.sm.pick_next(ctx.movement_mode)
        return None

    def _trigger_idle_animation(self, ctx: BehaviorContext):
        """觸發閒置動畫"""
        if self._has_triggered_idle_anim:
            return
        
        self._has_triggered_idle_anim = True
        # 修復：確保 movement_mode 是枚舉類型，不是字符串
        if hasattr(ctx.movement_mode, 'value'):
            mode_value = ctx.movement_mode.value
        else:
            mode_value = str(ctx.movement_mode)
            
        idle_anim = "stand_idle_g" if mode_value == "ground" else "smile_idle_f"
        
        # 先停止當前動畫，然後播放閒置動畫
        ctx.trigger_anim(idle_anim, {
            "loop": True,
            "force_restart": True  # 強制重新開始動畫
        })