from __future__ import annotations
import math
import random
from typing import Optional

from .base_behavior import BaseBehavior, BehaviorContext
from ..core.state_machine import BehaviorState, MovementMode
from ..core.position import Position


class MovementBehavior(BaseBehavior):
    state = BehaviorState.NORMAL_MOVE

    def on_enter(self, ctx: BehaviorContext) -> None:
        # 進入時若沒有目標就設定一個
        if ctx.movement_mode == MovementMode.GROUND:
            self._enter_ground(ctx)
        else:
            self._enter_float(ctx)

    def on_tick(self, ctx: BehaviorContext):
        # 行為本身只負責『到了沒』的判斷；到了就播放轉向動畫然後建議切回 Idle
        if ctx.target_reached:
            # 到達目標時播放轉向動畫，然後切換到閒置
            if ctx.movement_mode == MovementMode.GROUND:
                # 根據當前面向決定轉向動畫
                if ctx.facing_direction > 0:
                    turn_anim = "turn_right_g"
                else:
                    turn_anim = "turn_left_g"
                
                # 播放轉向動畫，然後自動切換到閒置動畫
                ctx.trigger_anim(turn_anim, {
                    "loop": False,
                    "await_finish": True,
                    "max_wait": 1.0,
                    "next_anim": "stand_idle_g",
                    "next_params": {"loop": True}
                })
            else:
                # 浮空模式直接切換到浮空閒置
                ctx.trigger_anim("smile_idle_f", {"loop": True})
            
            return BehaviorState.IDLE
        return None

    # —— helpers ——
    def _enter_ground(self, ctx: BehaviorContext):
        gy = ctx.ground_y()
        ctx.position.y = gy
        ctx.target_velocity.y = 0.0

        # 70% 保持當前面向，否則隨機左右
        desired_dir = ctx.facing_direction if random.random() < 0.7 else random.choice([-1, 1])
        need_turn = (desired_dir != ctx.facing_direction)

        # 決定目標 X（使用虛擬桌面邊界，若無則退回 screen_*）
        left  = getattr(ctx, "v_left", 0) + 50
        right = getattr(ctx, "v_right", ctx.screen_width) - ctx.SIZE - 50

        if desired_dir > 0:
            # 增加移動距離，讓移動更明顯
            min_dist = 200  # 最小距離
            max_dist = 600  # 最大距離
            target_x = random.uniform(max(left, ctx.position.x + min_dist),
                                    min(right, ctx.position.x + max_dist))
            follow_anim = "walk_right_g"
            turn_anim = "turn_right_g"
        else:
            min_dist = 200  # 最小距離  
            max_dist = 600  # 最大距離
            target_x = random.uniform(max(left, ctx.position.x - max_dist),
                                    min(right, ctx.position.x - min_dist))
            follow_anim = "walk_left_g"
            turn_anim = "turn_left_g"

        # 設定目標點與期望速度（速度先就緒；等待轉向時 MOV 會鎖住物理）
        ctx.set_target(target_x, gy)
        ctx.target_velocity.x = ctx.ground_speed * (1 if desired_dir > 0 else -1)
        ctx.facing_direction = desired_dir  # 先更新面向（之後動畫也會對齊）

        if need_turn:
            # 先播轉向（非 loop），並要求「等完成後」自動接走路動畫
            ctx.trigger_anim(turn_anim, {
                "loop": False,
                "await_finish": True,
                "max_wait": 1.2,                 # 依素材調整；避免卡死
                "next_anim": follow_anim,        # 轉向結束後自動接走路
                "next_params": {}                # 需要的話可加自定參數
            })
        else:
            # 不必轉向 → 直接走
            ctx.trigger_anim(follow_anim, {})

    def _enter_float(self, ctx: BehaviorContext):
        import math, random
        angle = random.uniform(-math.pi, math.pi)
        while abs(math.cos(angle)) <= 0.1:
            angle = random.uniform(-math.pi, math.pi)
        base = random.uniform(ctx.float_min_speed, ctx.float_max_speed)
        ctx.target_velocity.x = base * math.cos(angle)
        ctx.target_velocity.y = base * math.sin(angle)

        # 用虛擬桌面四邊選點（避免選到畫面外）
        left  = ctx.v_left + 50
        right = ctx.v_right - ctx.SIZE - 50
        top   = ctx.v_top + 80
        bot   = ctx.v_bottom - ctx.SIZE - 50
        tx = random.uniform(left, right)
        ty = random.uniform(top,  min(bot, ctx.v_top + 350))
        ctx.set_target(tx, ty)
        ctx.trigger_anim("smile_idle_f", {})