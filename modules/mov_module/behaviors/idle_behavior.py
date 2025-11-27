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
        self._idle_start_time = ctx.now
        
        # 🎯 檢查是否有待觸發的 tease 動畫
        if hasattr(ctx, 'tease_tracker') and ctx.tease_tracker.has_pending():
            ctx.tease_tracker.clear_pending()
            # 觸發 tease 動畫（通過回調到主模組）
            if hasattr(ctx, 'trigger_tease_callback'):
                ctx.trigger_tease_callback()
                return  # 不播放 idle 動畫，等 tease 完成
        
        # 立即觸發閒置動畫（移除不必要的延遲）
        # 動畫切換緩衝已在 _trigger_anim 中處理
        self._trigger_idle_animation(ctx)
        
        # 標記 idle 起點
        ctx.sm.begin_idle(ctx.now)

    def on_tick(self, ctx: BehaviorContext):
        # 檢查是否應該退出 IDLE 狀態
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