from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Optional
import math

from ..core.position import Position, Velocity
from ..core.state_machine import MovementMode, BehaviorState, MovementStateMachine
from ..core.physics import PhysicsEngine


@dataclass
class BehaviorContext:
    # 核心狀態（協調器注入引用，行為直接讀寫）
    position: Position
    velocity: Velocity
    target_velocity: Velocity

    screen_width: int
    screen_height: int
    SIZE: int
    GROUND_OFFSET: int

    v_left: int
    v_top: int
    v_right: int
    v_bottom: int

    movement_mode: MovementMode
    facing_direction: int

    movement_target: Optional[Position]
    target_reach_threshold: float
    target_reached: bool

    # 參數
    ground_speed: float
    float_min_speed: float
    float_max_speed: float

    # 核心元件
    physics: PhysicsEngine
    sm: MovementStateMachine

    # 事件/輸出接口（由協調器提供）
    trigger_anim: Callable[[str, dict], None]
    set_target: Callable[[float, float], None]
    get_cursor_pos: Callable[[], tuple[float, float]]  # 返回 (x, y)

    # 時序
    now: float

    # 選填的核心元件（有預設值）
    anim_query: Optional[object] = None  # AnimationQuery instance
    
    # 轉場臨時（供 TransitionBehavior 使用）
    transition_start_time: Optional[float] = None
    movement_locked_until: float = 0.0
    
    # 狀態追蹤
    previous_state: Optional[BehaviorState] = None
    
    # 系統循環相關（供 SystemCycleBehavior 使用）
    current_layer: Optional[str] = None
    layer_strategy: Optional[object] = None  # LayerAnimationStrategy instance
    
    # Tease 系統相關（供 IdleBehavior 使用）
    tease_tracker: Optional[object] = None  # TeaseTracker instance
    trigger_tease_callback: Optional[Callable[[], None]] = None

    def ground_y(self) -> float:
        # 用 v_bottom，避免原點偏移造成地面高度錯誤
        return self.v_bottom - self.SIZE + self.GROUND_OFFSET


class BaseBehavior:
    state: BehaviorState

    def __init__(self):
        pass

    def on_enter(self, ctx: BehaviorContext) -> None:
        pass

    def on_tick(self, ctx: BehaviorContext) -> Optional[BehaviorState]:
        """回傳下一個狀態（若需要切換），否則 None。"""
        return None

    def on_exit(self, ctx: BehaviorContext) -> None:
        pass


class BehaviorFactory:
    @staticmethod
    def create(state: BehaviorState) -> BaseBehavior:
        if state == BehaviorState.IDLE:
            from .idle_behavior import IdleBehavior
            return IdleBehavior()
        if state == BehaviorState.SLEEPING:
            from .sleep_behavior import SleepBehavior
            return SleepBehavior()
        if state == BehaviorState.SYSTEM_CYCLE:
            # SYSTEM_CYCLE 期間暫停移動，使用專門的系統循環行為
            from .system_cycle_behavior import SystemCycleBehavior
            return SystemCycleBehavior()
        if state == BehaviorState.NORMAL_MOVE:
            from .movement_behavior import MovementBehavior
            return MovementBehavior()
        if state == BehaviorState.SPECIAL_MOVE:
            from .special_move_behavior import SpecialMoveBehavior
            return SpecialMoveBehavior()
        if state == BehaviorState.TRANSITION:
            from .transition_behavior import TransitionBehavior
            return TransitionBehavior()
        # 預設回傳 idle
        from .idle_behavior import IdleBehavior
        return IdleBehavior()