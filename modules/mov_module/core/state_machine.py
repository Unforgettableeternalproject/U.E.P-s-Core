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
    SYSTEM_CYCLE = "system_cycle" # 系統循環運行中（暫停移動，播放層級動畫）
    SLEEPING = "sleeping"         # 睡眠狀態（播放 g_to_l → sleep_l 循環）
    ON_CALL = "on_call"           # 🎤 ON_CALL 狀態（暫停行為機和追蹤）

class SpecialMoveVariant(Enum):
    """特殊移動變體 - 多種趣味移動模式"""
    SPEED_BURST = "speed_burst"           # 加速衝刺（原有的）
    APPROACH_CURSOR = "approach_cursor"   # 靠近滑鼠游標
    FLEE_CURSOR = "flee_cursor"           # 遠離滑鼠游標
    VISIT_WINDOW = "visit_window"         # 移動到非全螢幕視窗上方
    ZIGZAG = "zigzag"                     # Z 字型移動

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

    def pick_next(self, mode: MovementMode, current_state: BehaviorState | None = None) -> BehaviorState:
        """
        選擇下一個行為狀態
        
        Args:
            mode: 當前移動模式
            current_state: 當前行為狀態（用於檢查 SYSTEM_CYCLE）
        """
        # 如果當前在 SYSTEM_CYCLE，維持不變，不受行為機影響
        if current_state == BehaviorState.SYSTEM_CYCLE:
            return BehaviorState.SYSTEM_CYCLE
        
        w = self.weights_float if mode == MovementMode.FLOAT else self.weights_ground
        try:
            states, weights = zip(*w.items())
            selected_state = random.choices(states, weights=weights, k=1)[0]
            
            # 移除 print 洗屏，改用 debug_log（需在外部處理）
            # TRANSITION 觸發已經在 mov_module 的 _switch_behavior 有日誌記錄
            
            return selected_state
        except Exception as e:
            # 錯誤處理：如果權重有問題，回到預設狀態
            # 使用 error_log 而非 print
            from devtools.debugger import error_log
            error_log(f"[StateMachine] 權重選擇錯誤: {e}, 權重字典: {w}")
            return BehaviorState.IDLE

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
    
    # —— Special Move 變體 ——
    def pick_special_move_variant(self, mode: MovementMode) -> SpecialMoveVariant:
        """隨機選擇一種特殊移動變體"""
        if mode == MovementMode.GROUND:
            # 地面模式：支援所有變體
            variants = [
                SpecialMoveVariant.SPEED_BURST,
                SpecialMoveVariant.APPROACH_CURSOR,
                SpecialMoveVariant.FLEE_CURSOR,
                SpecialMoveVariant.VISIT_WINDOW,
                SpecialMoveVariant.ZIGZAG
            ]
            weights = [0.30, 0.175, 0.175, 0.175, 0.175]
        else:
            # 漂浮模式：只支援部分變體
            variants = [
                SpecialMoveVariant.SPEED_BURST,
                SpecialMoveVariant.APPROACH_CURSOR,
                SpecialMoveVariant.FLEE_CURSOR
            ]
            weights = [0.4, 0.3, 0.3]
        
        return random.choices(variants, weights=weights, k=1)[0]