from __future__ import annotations
import random
import time
from enum import Enum
from dataclasses import dataclass

# —— 枚舉放在這裡，供 mov_module 與外界共用 ——
class MovementMode(Enum):
    GROUND = "ground"
    FLOAT = "float"
    DRAGGING = "dragging"
    THROWN = "thrown"

class BehaviorState(Enum):
    NORMAL_MOVE = "normal_move"   # 45%（或調整）
    IDLE = "idle"                 # 35%
    SPECIAL_MOVE = "special_move" # 15%
    TRANSITION = "transition"     # 5%

@dataclass
class IdleConfig:
    min_duration: float = 1.0
    max_duration: float = 2.0
    tick_chance: float = 0.07  # 每 tick 結束 idle 的機率


class MovementStateMachine:
    """行為機主體：只產生『下一個行為狀態』與『轉場方向』的決策，不觸碰動畫。
    協調器（mov_module）負責真正執行與發指令。"""

    def __init__(self):
        # 預設權重（可被 mov_module 依模式覆寫）
        self.weights_ground = {
            BehaviorState.NORMAL_MOVE: 0.40,
            BehaviorState.IDLE: 0.45,
            BehaviorState.SPECIAL_MOVE: 0.12,
            BehaviorState.TRANSITION: 0.03,
        }
        self.weights_float = {
            BehaviorState.NORMAL_MOVE: 0.25,
            BehaviorState.IDLE: 0.60,
            BehaviorState.SPECIAL_MOVE: 0.10,
            BehaviorState.TRANSITION: 0.05,
        }
        self.idle_cfg = IdleConfig()
        # 轉場
        self.transition_duration: float = 2.0

        # runtime（協調器也會持有一份鏡像，這裡保留以便測試與單元驗證）
        self.current_state: BehaviorState = BehaviorState.NORMAL_MOVE
        self.idle_start: float | None = None

    # —— 公用 API ——
    def choose_initial_state(self) -> BehaviorState:
        pool = [BehaviorState.NORMAL_MOVE, BehaviorState.IDLE, BehaviorState.SPECIAL_MOVE]
        probs = [0.45/0.95, 0.35/0.95, 0.15/0.95]
        return random.choices(pool, weights=probs, k=1)[0]

    def pick_next(self, mode: MovementMode) -> BehaviorState:
        w = self.weights_float if mode == MovementMode.FLOAT else self.weights_ground
        states, weights = zip(*w.items())
        return random.choices(states, weights=weights, k=1)[0]

    # —— Idle 管理 ——
    def begin_idle(self, now: float):
        self.idle_start = now

    def should_exit_idle(self, now: float) -> bool:
        if self.idle_start is None:
            return True
        elapsed = now - self.idle_start
        if elapsed < self.idle_cfg.min_duration:
            return False
        if elapsed > self.idle_cfg.max_duration:
            return True
        return random.random() < self.idle_cfg.tick_chance

    # —— 轉場 ——
    def decide_transition_target(self, mode: MovementMode) -> MovementMode | None:
        if mode == MovementMode.GROUND:
            return MovementMode.FLOAT
        if mode == MovementMode.FLOAT:
            return MovementMode.GROUND
        return None

    def transition_progress(self, start_time: float, now: float) -> float:
        if start_time is None:
            return 1.0
        return min((now - start_time) / max(self.transition_duration, 1e-3), 1.0)