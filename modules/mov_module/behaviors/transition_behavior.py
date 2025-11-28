from __future__ import annotations
import random

from .base_behavior import BaseBehavior, BehaviorContext
from ..core.state_machine import BehaviorState, MovementMode


class TransitionBehavior(BaseBehavior):
    state = BehaviorState.TRANSITION

    def __init__(self):
        super().__init__()
        self._target_mode = None
        self._start_position = None
        self._target_x = 0.0
        self._target_y = 0.0

    def on_enter(self, ctx: BehaviorContext) -> None:
        print(f"🚀 TransitionBehavior.on_enter 被調用！當前模式: {ctx.movement_mode}")
        
        target_mode = ctx.sm.decide_transition_target(ctx.movement_mode)
        if not target_mode:
            print("⚠️ 轉換目標為空，退出轉換")
            return
            
        print(f"🎯 轉換目標模式: {ctx.movement_mode.value} -> {target_mode.value}")
        
        ctx.transition_start_time = ctx.now
        # 不再鎖定移動，讓轉場行為自己控制位置
        # ctx.movement_locked_until = ctx.now + ctx.sm.transition_duration
        
        # 存起來給 tick 用
        self._target_mode = target_mode
        self._start_position = ctx.position.copy()  # 保存開始位置
        
        if target_mode == MovementMode.FLOAT:
            # 從落地轉浮空：計算垂直飛行的目標位置
            import random
            # 目標 Y 位置在較高的位置
            self._target_y = random.uniform(80, 250)
            # 目標 X 位置可以稍微調整，但主要是垂直移動
            x_offset = random.uniform(-100, 100)  # 小幅度水平偏移
            left = ctx.v_left + 50
            right = ctx.v_right - ctx.SIZE - 50
            self._target_x = max(left, min(right, ctx.position.x + x_offset))
            
            print(f"🚁 地面->浮空轉換: 目標位置 ({self._target_x:.1f}, {self._target_y:.1f})")
            
            # 設定目標點用於動畫配合
            ctx.set_target(self._target_x, self._target_y)
            ctx.trigger_anim("g_to_f", {"loop": False})
        else:
            # 從浮空轉落地：直接下降到地面
            gy = ctx.ground_y()
            self._target_y = gy
            self._target_x = ctx.position.x  # 保持 X 位置不變
            
            print(f"🛬 浮空->地面轉換: 目標位置 ({self._target_x:.1f}, {self._target_y:.1f})")
            
            ctx.set_target(self._target_x, self._target_y)
            ctx.trigger_anim("f_to_g", {"loop": False})

    def on_tick(self, ctx: BehaviorContext):
        if ctx.transition_start_time is None:
            return BehaviorState.IDLE
        
        prog = ctx.sm.transition_progress(ctx.transition_start_time, ctx.now)
        
        # 透過設置速度來實現平滑移動，而不是直接設置位置
        if self._target_mode == MovementMode.GROUND:
            # 從浮空轉落地：設置向下和朝目標的速度
            target_vel_x = (self._target_x - ctx.position.x) * 0.1
            target_vel_y = max(2.0, (self._target_y - ctx.position.y) * 0.1)  # 確保有向下速度
            ctx.target_velocity.x = target_vel_x
            ctx.target_velocity.y = target_vel_y
        else:
            # 從落地轉浮空：設置向上和朝目標的速度
            target_vel_x = (self._target_x - ctx.position.x) * 0.08
            target_vel_y = min(-1.5, (self._target_y - ctx.position.y) * 0.08)  # 確保有向上速度
            ctx.target_velocity.x = target_vel_x
            ctx.target_velocity.y = target_vel_y

        # 完成條件：時間到或到達目標位置
        distance_to_target = ((ctx.position.x - self._target_x) ** 2 + (ctx.position.y - self._target_y) ** 2) ** 0.5
        time_up = prog >= 1.0 or ctx.now >= (ctx.transition_start_time + ctx.sm.transition_duration)
        close_enough = distance_to_target < 30.0
        
        if time_up or close_enough:
            ctx.movement_mode = self._target_mode
            # 確保最終位置
            if close_enough:
                ctx.position.x = self._target_x
                ctx.position.y = self._target_y
            ctx.transition_start_time = None
            # 停止轉場速度
            ctx.target_velocity.x = 0.0
            ctx.target_velocity.y = 0.0
            
            print(f"✅ 轉場完成: {self._target_mode.value}")
            
            # 轉場後強制觸發正確的 idle 動畫（根據新模式）
            is_ground = (self._target_mode == MovementMode.GROUND)
            idle_anim = "stand_idle_g" if is_ground else "smile_idle_f"
            ctx.trigger_anim(idle_anim, {"loop": True, "force_restart": True})
            print(f"🎬 轉場後觸發 idle 動畫: {idle_anim}")
            
            return BehaviorState.IDLE
        return None