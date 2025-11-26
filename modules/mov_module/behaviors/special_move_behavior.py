"""
Special Move Behavior - 多種特殊移動變體

支援多種有趣的移動模式：
- SPEED_BURST: 加速衝刺
- APPROACH_CURSOR: 靠近滑鼠游標
- FLEE_CURSOR: 遠離滑鼠游標  
- VISIT_WINDOW: 移動到非全螢幕視窗上方
- ZIGZAG: Z字型移動
"""

from __future__ import annotations
import math
import random
from typing import Optional

from .base_behavior import BaseBehavior, BehaviorContext
from ..core.state_machine import BehaviorState, MovementMode, SpecialMoveVariant
from ..core.position import Position


class SpecialMoveBehavior(BaseBehavior):
    state = BehaviorState.SPECIAL_MOVE

    def __init__(self):
        super().__init__()
        self._variant: Optional[SpecialMoveVariant] = None
        self._movement_start_time = 0.0
        self._min_duration = 2.0  # 最小持續時間
        self._max_duration = 5.0  # 最大持續時間
        self._target_duration = 3.0
        
        # Zigzag 專用
        self._zigzag_waypoints = []
        self._current_waypoint_idx = 0

    def on_enter(self, ctx: BehaviorContext) -> None:
        """進入特殊移動行為"""
        self._movement_start_time = ctx.now
        self._target_duration = random.uniform(self._min_duration, self._max_duration)
        
        # 從狀態機獲取變體類型
        self._variant = ctx.sm.pick_special_move_variant(ctx.movement_mode)
        
        # 根據變體初始化
        if self._variant == SpecialMoveVariant.SPEED_BURST:
            self._enter_speed_burst(ctx)
        elif self._variant == SpecialMoveVariant.APPROACH_CURSOR:
            self._enter_approach_cursor(ctx)
        elif self._variant == SpecialMoveVariant.FLEE_CURSOR:
            self._enter_flee_cursor(ctx)
        elif self._variant == SpecialMoveVariant.VISIT_WINDOW:
            self._enter_visit_window(ctx)
        elif self._variant == SpecialMoveVariant.ZIGZAG:
            self._enter_zigzag(ctx)
        else:
            # 預設為加速衝刺
            self._enter_speed_burst(ctx)

    def on_tick(self, ctx: BehaviorContext):
        """更新特殊移動"""
        elapsed = ctx.now - self._movement_start_time
        
        # 持續時間超過目標時間，切換回 IDLE
        if elapsed >= self._target_duration:
            return BehaviorState.IDLE
        
        # 根據變體更新行為
        if self._variant == SpecialMoveVariant.APPROACH_CURSOR:
            self._update_approach_cursor(ctx)
        elif self._variant == SpecialMoveVariant.FLEE_CURSOR:
            self._update_flee_cursor(ctx)
        elif self._variant == SpecialMoveVariant.ZIGZAG:
            self._update_zigzag(ctx)
        
        # 到達目標點時切換回 IDLE
        if ctx.target_reached and self._variant in [
            SpecialMoveVariant.SPEED_BURST,
            SpecialMoveVariant.VISIT_WINDOW
        ]:
            return BehaviorState.IDLE
        
        return None

    # ========= 變體實現 =========
    
    def _enter_speed_burst(self, ctx: BehaviorContext):
        """加速衝刺 - 原有的特殊移動"""
        if ctx.movement_mode == MovementMode.GROUND:
            gy = ctx.ground_y()
            ctx.position.y = gy
            
            # 隨機方向，加速移動
            desired_dir = random.choice([-1, 1])
            left = getattr(ctx, "v_left", 0) + 50
            right = getattr(ctx, "v_right", ctx.screen_width) - ctx.SIZE - 50
            
            if desired_dir > 0:
                target_x = random.uniform(max(left, ctx.position.x + 300),
                                        min(right, ctx.position.x + 800))
                speed_multiplier = 2.0  # 加速2倍
            else:
                target_x = random.uniform(max(left, ctx.position.x - 800),
                                        min(right, ctx.position.x - 300))
                speed_multiplier = 2.0
            
            ctx.set_target(target_x, gy)
            ctx.target_velocity.x = ctx.ground_speed * speed_multiplier * desired_dir
            ctx.facing_direction = desired_dir
            
            # 播放走路動畫（速度加快）
            anim = "walk_right_g" if desired_dir > 0 else "walk_left_g"
            ctx.trigger_anim(anim, {"loop": True})
        else:
            # 浮空模式加速
            angle = random.uniform(-math.pi, math.pi)
            speed = ctx.float_max_speed * 1.5  # 加速1.5倍
            ctx.target_velocity.x = speed * math.cos(angle)
            ctx.target_velocity.y = speed * math.sin(angle)
            
            left = ctx.v_left + 50
            right = ctx.v_right - ctx.SIZE - 50
            top = ctx.v_top + 80
            bot = ctx.v_bottom - ctx.SIZE - 50
            tx = random.uniform(left, right)
            ty = random.uniform(top, min(bot, ctx.v_top + 350))
            ctx.set_target(tx, ty)
            ctx.trigger_anim("smile_idle_f", {"loop": True})

    def _enter_approach_cursor(self, ctx: BehaviorContext):
        """靠近滑鼠游標"""
        cursor_x, cursor_y = ctx.get_cursor_pos()
        
        if ctx.movement_mode == MovementMode.GROUND:
            gy = ctx.ground_y()
            ctx.position.y = gy
            
            # 移動到游標附近（不完全到達，保留50px距離）
            offset = 50 if cursor_x > ctx.position.x else -50
            target_x = cursor_x + offset
            
            # 限制在邊界內
            left = getattr(ctx, "v_left", 0) + 50
            right = getattr(ctx, "v_right", ctx.screen_width) - ctx.SIZE - 50
            target_x = max(left, min(right, target_x))
            
            ctx.set_target(target_x, gy)
            ctx.facing_direction = 1 if cursor_x > ctx.position.x else -1
            ctx.target_velocity.x = ctx.ground_speed * 1.3 * ctx.facing_direction
            
            anim = "walk_right_g" if ctx.facing_direction > 0 else "walk_left_g"
            ctx.trigger_anim(anim, {"loop": True})
        else:
            # 浮空模式靠近游標
            target_x = cursor_x
            target_y = cursor_y + 100  # 停在游標下方100px
            ctx.set_target(target_x, target_y)
            
            dx = target_x - ctx.position.x
            dy = target_y - ctx.position.y
            dist = math.sqrt(dx**2 + dy**2)
            if dist > 0:
                speed = ctx.float_max_speed * 1.2
                ctx.target_velocity.x = (dx / dist) * speed
                ctx.target_velocity.y = (dy / dist) * speed
            
            ctx.trigger_anim("curious_idle_f", {"loop": True})

    def _update_approach_cursor(self, ctx: BehaviorContext):
        """持續更新靠近游標的目標"""
        cursor_x, cursor_y = ctx.get_cursor_pos()
        
        if ctx.movement_mode == MovementMode.GROUND:
            offset = 50 if cursor_x > ctx.position.x else -50
            target_x = cursor_x + offset
            left = getattr(ctx, "v_left", 0) + 50
            right = getattr(ctx, "v_right", ctx.screen_width) - ctx.SIZE - 50
            target_x = max(left, min(right, target_x))
            
            gy = ctx.ground_y()
            ctx.set_target(target_x, gy)
            
            new_dir = 1 if cursor_x > ctx.position.x else -1
            if new_dir != ctx.facing_direction:
                ctx.facing_direction = new_dir
                ctx.target_velocity.x = ctx.ground_speed * 1.3 * new_dir
        else:
            target_x = cursor_x
            target_y = cursor_y + 100
            ctx.set_target(target_x, target_y)
            
            dx = target_x - ctx.position.x
            dy = target_y - ctx.position.y
            dist = math.sqrt(dx**2 + dy**2)
            if dist > 0:
                speed = ctx.float_max_speed * 1.2
                ctx.target_velocity.x = (dx / dist) * speed
                ctx.target_velocity.y = (dy / dist) * speed

    def _enter_flee_cursor(self, ctx: BehaviorContext):
        """遠離滑鼠游標"""
        cursor_x, cursor_y = ctx.get_cursor_pos()
        
        if ctx.movement_mode == MovementMode.GROUND:
            gy = ctx.ground_y()
            ctx.position.y = gy
            
            # 遠離游標方向
            flee_dir = -1 if cursor_x > ctx.position.x else 1
            left = getattr(ctx, "v_left", 0) + 50
            right = getattr(ctx, "v_right", ctx.screen_width) - ctx.SIZE - 50
            
            if flee_dir > 0:
                target_x = random.uniform(max(left, ctx.position.x + 200), right)
            else:
                target_x = random.uniform(left, min(right, ctx.position.x - 200))
            
            ctx.set_target(target_x, gy)
            ctx.facing_direction = flee_dir
            ctx.target_velocity.x = ctx.ground_speed * 1.5 * flee_dir
            
            anim = "walk_right_g" if flee_dir > 0 else "walk_left_g"
            ctx.trigger_anim(anim, {"loop": True})
        else:
            # 浮空模式遠離游標
            dx = ctx.position.x - cursor_x
            dy = ctx.position.y - cursor_y
            dist = math.sqrt(dx**2 + dy**2)
            
            if dist < 10:
                # 太近了，隨機方向逃離
                angle = random.uniform(0, 2 * math.pi)
                dx = math.cos(angle)
                dy = math.sin(angle)
                dist = 1.0
            
            # 設定遠離方向的目標點
            flee_distance = 300
            target_x = ctx.position.x + (dx / dist) * flee_distance
            target_y = ctx.position.y + (dy / dist) * flee_distance
            
            # 限制在邊界內
            left = ctx.v_left + 50
            right = ctx.v_right - ctx.SIZE - 50
            top = ctx.v_top + 80
            bot = ctx.v_bottom - ctx.SIZE - 50
            target_x = max(left, min(right, target_x))
            target_y = max(top, min(bot, target_y))
            
            ctx.set_target(target_x, target_y)
            
            speed = ctx.float_max_speed * 1.5
            ctx.target_velocity.x = (dx / dist) * speed
            ctx.target_velocity.y = (dy / dist) * speed
            
            ctx.trigger_anim("awkward_f", {"loop": True})

    def _update_flee_cursor(self, ctx: BehaviorContext):
        """持續更新遠離游標"""
        # 重新計算遠離方向（游標可能移動）
        cursor_x, cursor_y = ctx.get_cursor_pos()
        
        dx = ctx.position.x - cursor_x
        dy = ctx.position.y - cursor_y
        dist = math.sqrt(dx**2 + dy**2)
        
        if dist < 10:
            return  # 避免除以零
        
        if ctx.movement_mode == MovementMode.GROUND:
            flee_dir = -1 if cursor_x > ctx.position.x else 1
            if flee_dir != ctx.facing_direction:
                ctx.facing_direction = flee_dir
                ctx.target_velocity.x = ctx.ground_speed * 1.5 * flee_dir
        else:
            # 浮空模式持續更新遠離方向
            speed = ctx.float_max_speed * 1.5
            ctx.target_velocity.x = (dx / dist) * speed
            ctx.target_velocity.y = (dy / dist) * speed

    def _enter_visit_window(self, ctx: BehaviorContext):
        """移動到非全螢幕視窗上方"""
        # TODO: 需要從系統獲取視窗列表
        # 目前先隨機選擇一個位置模擬
        if ctx.movement_mode == MovementMode.GROUND:
            gy = ctx.ground_y()
            ctx.position.y = gy
            
            left = getattr(ctx, "v_left", 0) + 50
            right = getattr(ctx, "v_right", ctx.screen_width) - ctx.SIZE - 50
            target_x = random.uniform(left, right)
            
            ctx.set_target(target_x, gy)
            ctx.facing_direction = 1 if target_x > ctx.position.x else -1
            ctx.target_velocity.x = ctx.ground_speed * ctx.facing_direction
            
            anim = "walk_right_g" if ctx.facing_direction > 0 else "walk_left_g"
            ctx.trigger_anim(anim, {"loop": True})

    def _enter_zigzag(self, ctx: BehaviorContext):
        """Z字型移動"""
        if ctx.movement_mode == MovementMode.GROUND:
            gy = ctx.ground_y()
            ctx.position.y = gy
            
            # 生成3-5個Z字路徑點
            num_waypoints = random.randint(3, 5)
            self._zigzag_waypoints = []
            self._current_waypoint_idx = 0
            
            left = getattr(ctx, "v_left", 0) + 50
            right = getattr(ctx, "v_right", ctx.screen_width) - ctx.SIZE - 50
            
            current_x = ctx.position.x
            direction = 1 if random.random() < 0.5 else -1
            
            for i in range(num_waypoints):
                # Z字形：左右交替
                if direction > 0:
                    target_x = min(right, current_x + random.uniform(150, 250))
                else:
                    target_x = max(left, current_x - random.uniform(150, 250))
                
                self._zigzag_waypoints.append(target_x)
                current_x = target_x
                direction *= -1  # 切換方向
            
            # 設定第一個目標點
            if self._zigzag_waypoints:
                first_target = self._zigzag_waypoints[0]
                ctx.set_target(first_target, gy)
                ctx.facing_direction = 1 if first_target > ctx.position.x else -1
                ctx.target_velocity.x = ctx.ground_speed * 1.2 * ctx.facing_direction
                
                anim = "walk_right_g" if ctx.facing_direction > 0 else "walk_left_g"
                ctx.trigger_anim(anim, {"loop": True})

    def _update_zigzag(self, ctx: BehaviorContext):
        """更新Z字型移動"""
        if ctx.target_reached and self._current_waypoint_idx < len(self._zigzag_waypoints) - 1:
            # 前往下一個路徑點
            self._current_waypoint_idx += 1
            next_target = self._zigzag_waypoints[self._current_waypoint_idx]
            
            gy = ctx.ground_y()
            ctx.set_target(next_target, gy)
            
            new_dir = 1 if next_target > ctx.position.x else -1
            if new_dir != ctx.facing_direction:
                ctx.facing_direction = new_dir
                ctx.target_velocity.x = ctx.ground_speed * 1.2 * new_dir
                
                # 切換動畫
                anim = "walk_right_g" if new_dir > 0 else "walk_left_g"
                ctx.trigger_anim(anim, {"loop": True})
