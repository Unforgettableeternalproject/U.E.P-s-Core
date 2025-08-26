# modules/mov_module/mov_module.py
"""
MOV 模組 - 行為和移動控制器

負責：
- 自主行為邏輯 (漫遊、互動)
- 拖拽和物理互動處理
- 檔案拖放功能
- 使用者輸入響應
- 位置和移動計算
"""

import os
import sys
import time
import math
import random
import threading
from typing import Dict, Any, Optional, List, Tuple, Callable
from enum import Enum, auto
from dataclasses import dataclass

try:
    from PyQt5.QtCore import QTimer, pyqtSignal, QPoint
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    # 定義替代類別
    class QTimer: pass
    def pyqtSignal(*args): return None
    class QPoint:
        def __init__(self, x=0, y=0):
            self.x = x
            self.y = y

from core.frontend_base import BaseFrontendModule, FrontendModuleType, UIEventType
from core.working_context import ContextType
from core.state_manager import UEPState
from utils.debug_helper import debug_log, info_log, error_log


class MovementMode(Enum):
    """移動模式"""
    GROUND = "ground"       # 地面行走
    FLOAT = "float"         # 浮動
    DRAGGING = "dragging"   # 被拖拽
    THROWN = "thrown"       # 被拋擲
    IDLE = "idle"          # 靜止


class BehaviorType(Enum):
    """行為類型"""
    WANDER = "wander"           # 漫遊
    FOLLOW_CURSOR = "follow"    # 跟隨游標
    WATCH_CURSOR = "watch"      # 觀察游標


class BehaviorState(Enum):
    """新的行為狀態架構 45-35-15-5"""
    NORMAL_MOVE = "normal_move"     # 45% - 正常移動到目標點
    IDLE = "idle"                   # 35% - 靜止不動
    SPECIAL_MOVE = "special_move"   # 15% - 特殊移動(變速)
    TRANSITION = "transition"       # 5%  - 轉場(浮空<->落地)
    INTERACT = "interact"       # 互動
    SLEEP = "sleep"            # 休眠


@dataclass
class Position:
    """位置資料結構"""
    x: float
    y: float
    
    def to_qpoint(self) -> QPoint:
        return QPoint(int(self.x), int(self.y))
    
    def distance_to(self, other: 'Position') -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass
class Velocity:
    """速度資料結構"""
    x: float
    y: float
    
    def magnitude(self) -> float:
        return math.hypot(self.x, self.y)
    
    def normalize(self) -> 'Velocity':
        mag = self.magnitude()
        if mag > 0:
            return Velocity(self.x / mag, self.y / mag)
        return Velocity(0, 0)


class PhysicsEngine:
    """簡單的物理引擎"""
    
    def __init__(self):
        self.gravity = 0.8
        self.damping = 0.978
        self.ground_friction = 0.95
        self.air_resistance = 0.99
    
    def apply_gravity(self, velocity: Velocity, is_grounded: bool) -> Velocity:
        """應用重力"""
        if not is_grounded:
            velocity.y += self.gravity
        return velocity
    
    def apply_damping(self, velocity: Velocity, damping_factor: float = None) -> Velocity:
        """應用阻尼"""
        factor = damping_factor or self.damping
        velocity.x *= factor
        velocity.y *= factor
        return velocity
    
    def apply_friction(self, velocity: Velocity, is_grounded: bool) -> Velocity:
        """應用摩擦力"""
        if is_grounded:
            velocity.x *= self.ground_friction
        else:
            velocity.x *= self.air_resistance
            velocity.y *= self.air_resistance
        return velocity


class MOVModule(BaseFrontendModule):
    """MOV 模組 - 行為和移動控制器"""

    DRAG_PAUSE_REASON = "拖拽中"

    def __init__(self, config: dict = None):
        super().__init__(FrontendModuleType.MOV)

        self.config = config or {}
        self.is_initialized = False
        
        # 物理狀態
        self.position = Position(100, 100)
        self.velocity = Velocity(0, 0)
        self.target_velocity = Velocity(0, 0)
        
        # 替代PyQt信號的事件回調系統
        self._animation_callbacks = []
        self._position_callbacks = []
        
        # 移動參數
        self.SIZE = self.config.get('window_size', 250)
        self.GROUND_SPEED = self.config.get('ground_speed', 2.2)
        self.FLOAT_MIN_SPEED = self.config.get('float_min_speed', 1.0)
        self.FLOAT_MAX_SPEED = self.config.get('float_max_speed', 3.5)
        self.THROW_SPEED_THRESHOLD = self.config.get('throw_speed_threshold', 800)
        self.GROUND_OFFSET = self.config.get('ground_offset', 48)
        
        # 行為狀態
        self.movement_mode = MovementMode.GROUND
        self.behavior_type = BehaviorType.WANDER
        self.facing_direction = 1  # 1 為右，-1 為左
        
        # 計時器和控制
        self.movement_timer = None
        self.behavior_timer = None
        self.behavior_switch_interval = random.uniform(3.0, 6.0)
        self.last_behavior_switch = time.time()
        
        # 螢幕邊界
        self.screen_width = 1920
        self.screen_height = 1080
        
        # 拖拽狀態
        self.is_being_dragged = False
        self.drag_history = []
        self.drag_start_position = None
        
        # 物理引擎
        self.physics = PhysicsEngine()
        
        # 行為設置
        self.behaviors_enabled = {
            'walking': self.config.get('walking_enabled', True),
            'floating': self.config.get('floating_enabled', True),
            'interaction': self.config.get('interaction_enabled', True)
        }
        
        # 狀態轉換機制
        self.state_transition_chance = 0.05  # 每次tick有5%機率轉換狀態
        self.state_stay_chance = 0.95  # 95%機率保持當前狀態
        self.transition_duration = 2.0  # 轉場持續時間（秒）
        self.transition_start_time = None
        self.transition_from_state = None
        self.transition_to_state = None
        self.is_transitioning = False
        
        # 新的45-35-15-5行為架構
        self.current_behavior_state = BehaviorState.NORMAL_MOVE  # 當前行為狀態
        self.behavior_probabilities = {
            BehaviorState.NORMAL_MOVE: 0.45,   # 45%
            BehaviorState.IDLE: 0.35,          # 35%
            BehaviorState.SPECIAL_MOVE: 0.15,  # 15%
            BehaviorState.TRANSITION: 0.05     # 5%
        }
        
        # 靜止狀態相關
        self.idle_start_time = None
        self.idle_tick_chance = 0.07  # 靜止時每個tick有7%機率結束靜止
        
        # 移動目標管理
        self.movement_target = None  # 當前移動目標位置
        self.target_reached = True  # 是否已到達目標
        self.target_reach_threshold = 10  # 到達目標的距離閾值
        
        # 移動暫停控制
        self.movement_paused = False  # 移動邏輯是否暫停
        self.pause_reason = ""  # 暫停原因（合併顯示）
        self.pause_reasons = set()  # 所有暫停原因
        self._transition_pause_reason = None  # 狀態轉換暫停識別
        
        info_log(f"[{self.module_id}] MOV 模組初始化")
    
    # ==== 信號替代方案 ====
    
    def add_animation_callback(self, callback: Callable[[str, dict], None]):
        """註冊動畫回調函數，替代 animation_trigger 信號"""
        if callback not in self._animation_callbacks:
            self._animation_callbacks.append(callback)
            debug_log(1, f"[{self.module_id}] 新增動畫回調函數: {callback.__qualname__}")
    
    def remove_animation_callback(self, callback: Callable[[str, dict], None]):
        """移除動畫回調函數"""
        if callback in self._animation_callbacks:
            self._animation_callbacks.remove(callback)
            debug_log(1, f"[{self.module_id}] 移除動畫回調函數: {callback.__qualname__}")
    
    def add_position_callback(self, callback: Callable[[int, int], None]):
        """註冊位置更新回調函數，替代 position_changed 信號"""
        if callback not in self._position_callbacks:
            self._position_callbacks.append(callback)
            debug_log(1, f"[{self.module_id}] 新增位置回調函數: {callback.__qualname__}")
    
    def remove_position_callback(self, callback: Callable[[int, int], None]):
        """移除位置更新回調函數"""
        if callback in self._position_callbacks:
            self._position_callbacks.remove(callback)
            debug_log(1, f"[{self.module_id}] 移除位置回調函數: {callback.__qualname__}")
    
    def trigger_animation(self, animation_type: str, params: dict = None):
        """觸發動畫，替代 animation_trigger.emit 調用"""
        params = params or {}
        debug_log(2, f"[{self.module_id}] 觸發動畫: {animation_type}")
        
        # 呼叫所有註冊的回調函數
        for callback in self._animation_callbacks:
            try:
                callback(animation_type, params)
            except Exception as e:
                error_log(f"[{self.module_id}] 動畫回調執行失敗: {e}")
    
    def trigger_position_update(self, x: int, y: int):
        """觸發位置更新，替代 position_changed.emit 調用"""
        debug_log(3, f"[{self.module_id}] 觸發位置更新: ({x}, {y})")
        
        # 如果正在被拖拽或移動暫停，直接更新內部位置
        if self.is_being_dragged or self.movement_paused:
            self.position.x = x
            self.position.y = y
            debug_log(3, f"[{self.module_id}] 直接更新位置 (暫停狀態)")
        
        # 呼叫所有註冊的回調函數
        for callback in self._position_callbacks:
            try:
                callback(x, y)
            except Exception as e:
                error_log(f"[{self.module_id}] 位置回調執行失敗: {e}")
    
    def initialize_frontend(self) -> bool:
        """初始化移動前端功能"""
        try:
            if not PYQT5_AVAILABLE:
                error_log(f"[{self.module_id}] PyQt5 不可用，使用基本移動功能")
            
            # 初始化計時器
            if PYQT5_AVAILABLE:
                # 設置計時器回調
                self.signals.add_timer_callback("movement", self._update_movement)
                self.signals.add_timer_callback("behavior", self._update_behavior)
                
                self.movement_timer = QTimer()
                self.movement_timer.timeout.connect(lambda: self.signals.timer_timeout("movement"))
                self.movement_timer.start(16)  # ~60 FPS for smooth movement
                
                self.behavior_timer = QTimer()
                self.behavior_timer.timeout.connect(lambda: self.signals.timer_timeout("behavior"))
                self.behavior_timer.start(100)  # 10 FPS for behavior logic
            
            # 註冊事件處理器
            self._register_movement_handlers()
            
            # 初始化位置
            self._initialize_position()
            self.is_initialized = True
            info_log(f"[{self.module_id}] MOV 前端初始化成功")
            return True
            
        except Exception as e:
            error_log(f"[{self.module_id}] MOV 前端初始化失敗: {e}")
            return False
    
    def _register_movement_handlers(self):
        """註冊移動相關事件處理器"""
        self.register_event_handler(UIEventType.DRAG_START, self._on_drag_start)
        self.register_event_handler(UIEventType.DRAG_END, self._on_drag_end)
        self.register_event_handler(UIEventType.MOUSE_HOVER, self._on_mouse_hover)
        self.register_event_handler(UIEventType.FILE_DROP, self._on_file_drop)
    
    def _initialize_position(self):
        """初始化位置"""
        # 放置在螢幕底部
        ground_y = self.screen_height - self.SIZE + self.GROUND_OFFSET
        self.position = Position(
            random.randint(0, self.screen_width - self.SIZE),
            ground_y
        )
        self._emit_position_change()
    
    def handle_frontend_request(self, data: dict) -> dict:
        """處理前端移動請求"""
        try:
            command = data.get('command')
            
            if command == 'set_position':
                return self._set_position(data)
            elif command == 'set_velocity':
                return self._set_velocity(data)
            elif command == 'set_movement_mode':
                return self._set_movement_mode(data)
            elif command == 'set_behavior':
                return self._set_behavior(data)
            elif command == 'enable_behavior':
                return self._enable_behavior(data)
            elif command == 'get_status':
                return self._get_movement_status()
            elif command == 'stop_movement':
                return self._stop_movement()
            else:
                return {"error": f"未知移動命令: {command}"}
                
        except Exception as e:
            error_log(f"[{self.module_id}] 處理移動請求異常: {e}")
            return {"error": str(e)}
    
    def _set_position(self, data: dict) -> dict:
        """設定位置"""
        try:
            x = data.get('x', self.position.x)
            y = data.get('y', self.position.y)
            
            self.position = Position(x, y)
            self._emit_position_change()
            
            return {"success": True, "position": {"x": x, "y": y}}
        except Exception as e:
            return {"error": str(e)}
    
    def _set_velocity(self, data: dict) -> dict:
        """設定速度"""
        try:
            vx = data.get('vx', self.velocity.x)
            vy = data.get('vy', self.velocity.y)
            
            self.velocity = Velocity(vx, vy)
            if hasattr(self.signals, 'velocity_changed'):
                self.signals.velocity_changed.emit({"vx": vx, "vy": vy})
            
            return {"success": True, "velocity": {"vx": vx, "vy": vy}}
        except Exception as e:
            return {"error": str(e)}
    
    def _set_movement_mode(self, data: dict) -> dict:
        """設定移動模式"""
        try:
            mode = data.get('mode')
            if mode in [m.value for m in MovementMode]:
                self.movement_mode = MovementMode(mode)
                debug_log(3, f"[{self.module_id}] 移動模式變更: {mode}")
                return {"success": True, "mode": mode}
            return {"error": f"無效移動模式: {mode}"}
        except Exception as e:
            return {"error": str(e)}
    
    def _set_behavior(self, data: dict) -> dict:
        """設定行為類型"""
        try:
            behavior = data.get('behavior')
            if behavior in [b.value for b in BehaviorType]:
                self.behavior_type = BehaviorType(behavior)
                if hasattr(self.signals, 'behavior_changed'):
                    self.signals.behavior_changed.emit(behavior)
                debug_log(3, f"[{self.module_id}] 行為變更: {behavior}")
                return {"success": True, "behavior": behavior}
            return {"error": f"無效行為類型: {behavior}"}
        except Exception as e:
            return {"error": str(e)}
    
    def _enable_behavior(self, data: dict) -> dict:
        """啟用/停用行為"""
        try:
            behavior_name = data.get('behavior_name')
            enabled = data.get('enabled', True)
            
            if behavior_name in self.behaviors_enabled:
                self.behaviors_enabled[behavior_name] = enabled
                return {"success": True, "behavior": behavior_name, "enabled": enabled}
            return {"error": f"未知行為: {behavior_name}"}
        except Exception as e:
            return {"error": str(e)}
    
    def _get_movement_status(self) -> dict:
        """獲取移動狀態"""
        return {
            "position": {"x": self.position.x, "y": self.position.y},
            "velocity": {"x": self.velocity.x, "y": self.velocity.y},
            "movement_mode": self.movement_mode.value,
            "behavior_type": self.behavior_type.value,
            "facing_direction": self.facing_direction,
            "is_being_dragged": self.is_being_dragged,
            "behaviors_enabled": self.behaviors_enabled
        }
    
    def _stop_movement(self) -> dict:
        """停止移動"""
        self.velocity = Velocity(0, 0)
        self.target_velocity = Velocity(0, 0)
        return {"success": True}
    
    def _update_movement(self):
        """更新移動邏輯 (高頻率調用)"""
        try:
            # 檢查移動是否暫停
            if self.movement_paused:
                debug_log(3, f"[{self.module_id}] 移動已暫停: {self.pause_reason}")
                return
                
            if self.is_being_dragged:
                return  # 被拖拽時不更新自主移動
            
            # 物理更新
            self._apply_physics()
            
            # 邊界檢查
            self._check_boundaries()
            
            # 更新位置
            self.position.x += self.velocity.x
            self.position.y += self.velocity.y
            
            # 發送位置變更
            self._emit_position_change()
            
        except Exception as e:
            error_log(f"[{self.module_id}] 移動更新異常: {e}")
    
    def _update_behavior(self):
        """更新行為邏輯 (低頻率調用)"""
        try:
            current_time = time.time()

            # 狀態轉換應在暫停期間繼續進行
            if self.is_transitioning:
                self._handle_state_transition(current_time)
                return

            # 檢查移動是否暫停
            if self.movement_paused:
                debug_log(3, f"[{self.module_id}] 行為更新已暫停: {self.pause_reason}")
                return

            # 檢查是否到達移動目標
            self._check_target_reached()

            # 新的行為狀態處理
            self._handle_behavior_state(current_time)

        except Exception as e:
            error_log(f"[{self.module_id}] 行為更新異常: {e}")
    
    def _handle_behavior_state(self, current_time):
        """處理新的行為狀態邏輯"""
        if self.current_behavior_state == BehaviorState.NORMAL_MOVE:
            self._handle_normal_move()
        elif self.current_behavior_state == BehaviorState.IDLE:
            self._handle_idle_state(current_time)
        elif self.current_behavior_state == BehaviorState.SPECIAL_MOVE:
            self._handle_special_move()
        elif self.current_behavior_state == BehaviorState.TRANSITION:
            self._handle_transition_behavior()
    
    def _handle_normal_move(self):
        """處理正常移動行為 (45%)"""
        if self.target_reached:
            # 到達目標，決定下一個行為狀態
            self._choose_next_behavior_state()
    
    def _handle_idle_state(self, current_time):
        """處理靜止狀態 (35%)"""
        if self.idle_start_time is None:
            self.idle_start_time = current_time
            # 停止所有移動
            self.velocity = Velocity(0, 0)
            self.target_velocity = Velocity(0, 0)
            debug_log(1, f"[{self.module_id}] 開始靜止狀態")
        
        # 每個tick有機率結束靜止
        if random.random() < self.idle_tick_chance:
            self.idle_start_time = None
            self._choose_next_behavior_state()
            debug_log(1, f"[{self.module_id}] 結束靜止狀態")
    
    def _handle_special_move(self):
        """處理特殊移動行為 (15%) - 變速移動"""
        if self.target_reached:
            # 到達目標，決定下一個行為狀態
            self._choose_next_behavior_state()
    
    def _handle_transition_behavior(self):
        """處理轉場行為 (5%) - 浮空<->落地轉換"""
        # 直接開始狀態轉換
        if not self.is_transitioning:
            self._start_state_transition()
    
    def _choose_next_behavior_state(self):
        """根據機率選擇下一個行為狀態"""
        rand = random.random()
        cumulative = 0
        
        for state, probability in self.behavior_probabilities.items():
            cumulative += probability
            if rand <= cumulative:
                old_state = self.current_behavior_state
                self.current_behavior_state = state
                debug_log(1, f"[{self.module_id}] 行為狀態轉換: {old_state.value} -> {state.value}")
                
                # 根據新狀態開始相應行為
                self._start_behavior_state(state)
                break
    
    def _start_behavior_state(self, state):
        """開始新的行為狀態"""
        if state == BehaviorState.NORMAL_MOVE:
            self._start_normal_movement()
        elif state == BehaviorState.IDLE:
            # 靜止狀態在_handle_idle_state中處理
            pass
        elif state == BehaviorState.SPECIAL_MOVE:
            self._start_special_movement()
        elif state == BehaviorState.TRANSITION:
            # 轉場在_handle_transition_behavior中處理
            pass
    
    def _start_normal_movement(self):
        """開始正常移動"""
        if self.movement_mode == MovementMode.GROUND:
            self._start_walking()
        elif self.movement_mode == MovementMode.FLOAT:
            self._start_floating()
    
    def _start_special_movement(self):
        """開始特殊移動 - 速度變化"""
        speed_multiplier = random.choice([0.5, 1.5, 2.0])  # 慢、快、超快
        
        if self.movement_mode == MovementMode.GROUND:
            self._start_walking_with_speed(speed_multiplier)
        elif self.movement_mode == MovementMode.FLOAT:
            self._start_floating_with_speed(speed_multiplier)
        
        debug_log(1, f"[{self.module_id}] 開始特殊移動，速度倍率: {speed_multiplier}")
    
    def _start_walking_with_speed(self, speed_multiplier):
        """開始變速走路"""
        ground_y = self.screen_height - self.SIZE + self.GROUND_OFFSET
        self.position.y = ground_y
        self.target_velocity.y = 0
        self.target_velocity.x = self.GROUND_SPEED * speed_multiplier * random.choice([-1, 1])
        self.facing_direction = 1 if self.target_velocity.x >= 0 else -1
        
        # 設置隨機的走路目標點
        if self.facing_direction == 1:
            target_x = random.uniform(self.position.x + 100, min(self.screen_width - 50, self.position.x + 400))
        else:
            target_x = random.uniform(max(50, self.position.x - 400), self.position.x - 100)
        self._set_movement_target(target_x, ground_y)
        
        # 觸發行走動畫
        animation_type = "walk_right" if self.facing_direction == 1 else "walk_left"
        self.trigger_animation(animation_type, {})
    
    def _start_floating_with_speed(self, speed_multiplier):
        """開始變速浮動"""
        # 隨機浮動方向
        angle = random.uniform(-math.pi, math.pi)
        while abs(math.cos(angle)) <= 0.1:  # 避免垂直移動
            angle = random.uniform(-math.pi, math.pi)
        
        base_speed = random.uniform(self.FLOAT_MIN_SPEED, self.FLOAT_MAX_SPEED)
        speed = base_speed * speed_multiplier
        self.target_velocity.x = speed * math.cos(angle)
        self.target_velocity.y = speed * math.sin(angle)
        
        # 設置隨機的浮動目標點
        target_x = random.uniform(50, self.screen_width - 50)
        target_y = random.uniform(50, 400)
        self._set_movement_target(target_x, target_y)
        
        # 觸發浮動動畫
        self.trigger_animation("static", {})
    
    def _check_target_reached(self):
        """檢查是否到達移動目標"""
        if self.movement_target is None:
            self.target_reached = True
            return
        
        # 計算到目標的距離
        distance = math.hypot(
            self.position.x - self.movement_target.x,
            self.position.y - self.movement_target.y
        )
        
        # 檢查是否在閾值範圍內
        if distance <= self.target_reach_threshold:
            if not self.target_reached:  # 剛到達目標
                self.target_reached = True
                debug_log(1, f"[{self.module_id}] 到達移動目標")
        else:
            self.target_reached = False
    
    def _set_movement_target(self, x, y):
        """設置新的移動目標"""
        self.movement_target = Position(x, y)
        self.target_reached = False
        debug_log(1, f"[{self.module_id}] 設置新移動目標: ({x}, {y})")
    
    def pause_movement(self, reason="未指定"):
        """暫停移動邏輯"""
        self.pause_reasons.add(reason)
        self.movement_paused = True
        self.pause_reason = ", ".join(self.pause_reasons)
        # 停止所有速度
        self.velocity = Velocity(0, 0)
        self.target_velocity = Velocity(0, 0)
        debug_log(1, f"[{self.module_id}] 移動已暫停: {reason}")

    def resume_movement(self, reason=None):
        """恢復移動邏輯"""
        if reason:
            self.pause_reasons.discard(reason)
        else:
            self.pause_reasons.clear()

        if self.pause_reasons:
            self.movement_paused = True
            self.pause_reason = ", ".join(self.pause_reasons)
            debug_log(1, f"[{self.module_id}] 移動暫停未解除，剩餘原因: {self.pause_reason}")
            return

        was_paused = self.movement_paused
        self.movement_paused = False
        self.pause_reason = ""

        if was_paused:
            # 根據當前狀態恢復適當的移動
            if self.movement_mode == MovementMode.GROUND:
                self._start_walking()
            elif self.movement_mode == MovementMode.FLOAT:
                self._start_floating()
            debug_log(1, f"[{self.module_id}] 移動已恢復")
    
    def _notify_ui_state_change(self, event_type, data):
        """通知UI狀態變更"""
        try:
            # 通過位置回調系統通知UI（使用特殊標記）
            for callback in self._position_callbacks:
                try:
                    # 如果回調有處理狀態變更的能力，調用它
                    if hasattr(callback, '__self__') and hasattr(callback.__self__, 'handle_mov_state_change'):
                        callback.__self__.handle_mov_state_change(event_type, data)
                except Exception as e:
                    debug_log(3, f"[{self.module_id}] 狀態變更通知失敗: {e}")
        except Exception as e:
            debug_log(3, f"[{self.module_id}] UI狀態變更通知異常: {e}")
    
    def _start_state_transition(self):
        """開始狀態轉換 - 只在TRANSITION行為狀態時調用"""
        if self.is_being_dragged or self.movement_mode == MovementMode.DRAGGING:
            return  # 拖拽時不進行狀態轉換
        
        # 決定轉換目標狀態
        current_state = self.movement_mode
        available_states = []
        
        if current_state == MovementMode.FLOAT:
            available_states = [MovementMode.GROUND]
        elif current_state == MovementMode.GROUND:
            available_states = [MovementMode.FLOAT]
        
        if not available_states:
            # 如果無法轉換，改為正常移動
            self.current_behavior_state = BehaviorState.NORMAL_MOVE
            self._start_normal_movement()
            return
        
        target_state = random.choice(available_states)
        
        # 暫停移動邏輯並記錄暫停原因
        self._transition_pause_reason = f"狀態轉換: {current_state.value} -> {target_state.value}"
        self.pause_movement(self._transition_pause_reason)
        
        # 開始轉換
        self.is_transitioning = True
        self.transition_from_state = current_state
        self.transition_to_state = target_state
        self.transition_start_time = time.time()
        
        # 通知UI暫停渲染（如果有回調）
        self._notify_ui_state_change("transition_start", {
            "from": current_state.value,
            "to": target_state.value
        })
        
        debug_log(1, f"[{self.module_id}] 開始狀態轉換: {current_state.value} -> {target_state.value}")
    
    def _handle_state_transition(self, current_time):
        """處理狀態轉換過程"""
        if not self.is_transitioning:
            return
        
        elapsed = current_time - self.transition_start_time
        progress = min(elapsed / self.transition_duration, 1.0)

        # 根據轉換類型執行不同的轉換邏輯
        if self.transition_from_state == MovementMode.FLOAT and self.transition_to_state == MovementMode.GROUND:
            self._transition_float_to_ground(progress)
        elif self.transition_from_state == MovementMode.GROUND and self.transition_to_state == MovementMode.FLOAT:
            self._transition_ground_to_float(progress)

        # 轉換過程中持續更新位置
        self._emit_position_change()

        # 轉換完成
        if progress >= 1.0:
            self.movement_mode = self.transition_to_state
            self.is_transitioning = False
            self.transition_from_state = None
            self.transition_to_state = None
            self.transition_start_time = None
            
            # 通知UI恢復渲染
            self._notify_ui_state_change("transition_complete", {
                "current_state": self.movement_mode.value
            })

            # 恢復移動邏輯
            self.resume_movement(self._transition_pause_reason)
            self._transition_pause_reason = None

            # 轉換完成後，選擇新的行為狀態（排除轉場）
            self._choose_post_transition_behavior()

            debug_log(1, f"[{self.module_id}] 狀態轉換完成: {self.movement_mode.value}")
    
    def _choose_post_transition_behavior(self):
        """轉換完成後選擇新的行為狀態（排除轉場）"""
        # 重新計算機率，排除轉場
        adjusted_probabilities = {
            BehaviorState.NORMAL_MOVE: 0.45 / 0.95,   # 約47.4%
            BehaviorState.IDLE: 0.35 / 0.95,          # 約36.8%
            BehaviorState.SPECIAL_MOVE: 0.15 / 0.95,  # 約15.8%
        }
        
        rand = random.random()
        cumulative = 0
        
        for state, probability in adjusted_probabilities.items():
            cumulative += probability
            if rand <= cumulative:
                self.current_behavior_state = state
                debug_log(1, f"[{self.module_id}] 轉換後選擇行為狀態: {state.value}")
                self._start_behavior_state(state)
                break
    
    def _transition_float_to_ground(self, progress):
        """浮空到落地的轉換（慢慢降落）"""
        ground_y = self.screen_height - self.SIZE + self.GROUND_OFFSET
        
        if not hasattr(self, '_transition_start_y'):
            self._transition_start_y = self.position.y
        
        start_y = self._transition_start_y
        
        # 使用緩動函數讓降落更自然
        eased_progress = 1 - (1 - progress) ** 2  # 二次緩出
        target_y = start_y + (ground_y - start_y) * eased_progress
        
        # 設置目標位置
        self.position.y = target_y
        
        # 減緩水平移動
        self.target_velocity.x *= (1 - progress * 0.3)
        
        # 轉換完成後清理臨時變數
        if progress >= 1.0:
            delattr(self, '_transition_start_y')
    
    def _transition_ground_to_float(self, progress):
        """落地到浮空的轉換（輕輕上升）"""
        ground_y = self.screen_height - self.SIZE + self.GROUND_OFFSET
        float_y = random.uniform(100, 300)  # 隨機浮空高度
        
        if not hasattr(self, '_transition_target_y'):
            self._transition_target_y = float_y
        
        # 使用緩動函數讓上升更自然
        eased_progress = progress * progress  # 二次緩入
        target_y = ground_y + (self._transition_target_y - ground_y) * eased_progress
        
        # 設置目標位置
        self.position.y = target_y
        
        # 轉換完成後清理臨時變數
        if progress >= 1.0:
            delattr(self, '_transition_target_y')
    
    def _apply_physics(self):
        """應用物理效果"""
        # 如果正在狀態轉換中，不應用常規物理效果
        if self.is_transitioning:
            return
        
        # 恢復到60FPS的平滑係數
        smoothing_factor = 0.12
        
        # 漸進到目標速度
        self.velocity.x += (self.target_velocity.x - self.velocity.x) * smoothing_factor
        self.velocity.y += (self.target_velocity.y - self.velocity.y) * smoothing_factor
        
        # 檢查是否在地面
        ground_y = self.screen_height - self.SIZE + self.GROUND_OFFSET
        is_grounded = abs(self.position.y - ground_y) < 5
        
        # 應用物理效果
        if self.movement_mode == MovementMode.THROWN:
            self.velocity = self.physics.apply_gravity(self.velocity, is_grounded)
            self.velocity = self.physics.apply_damping(self.velocity)
            
            # 地面碰撞
            if self.position.y >= ground_y:
                self.position.y = ground_y
                if abs(self.velocity.y) > 2:
                    self.velocity.y = -self.velocity.y * 0.4  # 反彈
                else:
                    self.velocity.y = 0
                    self.movement_mode = MovementMode.GROUND
        
        elif self.movement_mode == MovementMode.GROUND:
            self.position.y = ground_y  # 鎖定在地面
            self.velocity = self.physics.apply_friction(self.velocity, True)
    
    def _check_boundaries(self):
        """檢查邊界碰撞"""
        # 左右邊界
        if self.position.x <= 0 and self.velocity.x < 0:
            self.velocity.x = abs(self.velocity.x)  # 反轉速度
            self.target_velocity.x = abs(self.target_velocity.x)
            self.facing_direction = 1
        elif self.position.x >= self.screen_width - self.SIZE and self.velocity.x > 0:
            self.velocity.x = -abs(self.velocity.x)
            self.target_velocity.x = -abs(self.target_velocity.x)
            self.facing_direction = -1
        
        # 浮動模式的上下邊界
        if self.movement_mode == MovementMode.FLOAT:
            if self.position.y <= 0 and self.velocity.y < 0:
                self.velocity.y = abs(self.velocity.y)
                self.target_velocity.y = abs(self.target_velocity.y)
            elif self.position.y >= self.screen_height - self.SIZE and self.velocity.y > 0:
                self.velocity.y = -abs(self.velocity.y)
                self.target_velocity.y = -abs(self.target_velocity.y)
    
    def _switch_behavior(self):
        """切換行為模式"""
        if not self.behaviors_enabled.get('walking', True) and not self.behaviors_enabled.get('floating', True):
            self.target_velocity = Velocity(0, 0)
            return
        
        # 隨機選擇新的移動模式
        if random.random() < 0.25:  # 25% 機率切換模式
            if self.behaviors_enabled.get('walking', True) and self.behaviors_enabled.get('floating', True):
                if self.movement_mode == MovementMode.GROUND:
                    self.movement_mode = MovementMode.FLOAT
                    self._start_floating()
                else:
                    self.movement_mode = MovementMode.GROUND
                    self._start_walking()
            elif self.behaviors_enabled.get('walking', True):
                self.movement_mode = MovementMode.GROUND
                self._start_walking()
            elif self.behaviors_enabled.get('floating', True):
                self.movement_mode = MovementMode.FLOAT
                self._start_floating()
    
    def _start_walking(self):
        """開始地面行走"""
        ground_y = self.screen_height - self.SIZE + self.GROUND_OFFSET
        self.position.y = ground_y
        self.target_velocity.y = 0
        self.target_velocity.x = self.GROUND_SPEED * random.choice([-1, 1])
        self.facing_direction = 1 if self.target_velocity.x >= 0 else -1
        
        # 設置隨機的走路目標點
        if self.facing_direction == 1:
            target_x = random.uniform(self.position.x + 100, min(self.screen_width - 50, self.position.x + 400))
        else:
            target_x = random.uniform(max(50, self.position.x - 400), self.position.x - 100)
        self._set_movement_target(target_x, ground_y)
        
        # 觸發行走動畫
        animation_type = "walk_right" if self.facing_direction == 1 else "walk_left"
        self.trigger_animation(animation_type, {})
    
    def _start_floating(self):
        """開始浮動"""
        # 隨機浮動方向和速度
        angle = random.uniform(-math.pi, math.pi)
        while abs(math.cos(angle)) <= 0.1:  # 避免垂直移動
            angle = random.uniform(-math.pi, math.pi)
        
        speed = random.uniform(self.FLOAT_MIN_SPEED, self.FLOAT_MAX_SPEED)
        self.target_velocity.x = speed * math.cos(angle)
        self.target_velocity.y = speed * math.sin(angle)
        
        # 設置隨機的浮動目標點
        target_x = random.uniform(50, self.screen_width - 50)
        target_y = random.uniform(50, 400)
        self._set_movement_target(target_x, target_y)
        
        # 觸發浮動動畫
        self.trigger_animation("static", {})
    
    def _execute_current_behavior(self):
        """執行當前行為"""
        if self.behavior_type == BehaviorType.WANDER:
            # 漫遊行為已在 _switch_behavior 中處理
            pass
        elif self.behavior_type == BehaviorType.FOLLOW_CURSOR:
            self._follow_cursor_behavior()
        elif self.behavior_type == BehaviorType.WATCH_CURSOR:
            self._watch_cursor_behavior()
    
    def _follow_cursor_behavior(self):
        """跟隨游標行為"""
        # 這裡需要獲取游標位置，實際實作時需要與 UI 模組協調
        debug_log(2, f"[{self.module_id}] 執行跟隨游標行為")

    def _watch_cursor_behavior(self):
        """觀察游標行為"""
        # 觸發轉頭動畫指向游標
        debug_log(2, f"[{self.module_id}] 執行觀察游標行為")

    def handle_movement_request(self, movement_type: str, params: dict):
        """處理來自其他模組的移動請求"""
        if movement_type == "drag_move":
            self._handle_drag_move(params)
        elif movement_type == "throw":
            self._handle_throw(params)
        elif movement_type == "set_position":
            self._set_position(params)
    
    def _handle_drag_move(self, params: dict):
        """處理拖拽移動"""
        if self.is_being_dragged:
            position = params.get('position', {})
            new_x = position.get('x', self.position.x) - self.SIZE // 2
            new_y = position.get('y', self.position.y) - self.SIZE // 2
            
            self.position = Position(new_x, new_y)
            self._emit_position_change()
            
            # 記錄拖拽歷史用於拋擲計算
            current_time = time.time()
            self.drag_history.append((current_time, new_x, new_y))
            if len(self.drag_history) > 5:
                self.drag_history.pop(0)
    
    def _handle_throw(self, params: dict):
        """處理拋擲"""
        if len(self.drag_history) >= 2:
            t0, x0, y0 = self.drag_history[0]
            t1, x1, y1 = self.drag_history[-1]
            dt = t1 - t0
            
            if dt > 0:
                vx = (x1 - x0) / dt
                vy = (y1 - y0) / dt
                speed = math.hypot(vx, vy)
                
                if speed > self.THROW_SPEED_THRESHOLD:
                    self.movement_mode = MovementMode.THROWN
                    fps = 60
                    max_speed = 1600 / fps
                    
                    self.velocity.x = max(min(vx / fps, max_speed), -max_speed)
                    self.velocity.y = max(min(vy / fps, max_speed), -max_speed)
                    
                    info_log(f"[{self.module_id}] 拋擲開始，速度: {speed}")
    
    def _emit_position_change(self):
        """發送位置變更信號"""
        x = int(self.position.x)
        y = int(self.position.y)
        
        # 使用回調系統替代PyQt信號
        self.trigger_position_update(x, y)
        
        # 保留原有的信號系統（如果可用）
        if hasattr(self, 'signals') and hasattr(self.signals, 'position_changed'):
            self.signals.position_changed.emit({
                "x": x,
                "y": y
            })
    
    # ========== 事件處理器 ==========
    
    def _on_drag_start(self, event):
        """拖拽開始事件處理"""
        self.is_being_dragged = True
        
        # 暫停移動邏輯
        self.pause_movement(self.DRAG_PAUSE_REASON)
        
        # 設置拖拽模式
        self.movement_mode = MovementMode.DRAGGING
        self.drag_history.clear()
        
        start_pos = event.data.get('start_position', {})
        self.drag_start_position = Position(
            start_pos.get('x', self.position.x),
            start_pos.get('y', self.position.y)
        )
        
        debug_log(1, f"[{self.module_id}] 開始拖拽，暫停移動邏輯")
    
    def _on_drag_end(self, event):
        """拖拽結束事件處理"""
        # 檢查是否需要拋擲
        self._handle_throw({})
        
        self.is_being_dragged = False
        if self.movement_mode == MovementMode.DRAGGING:
            # 根據結束位置決定移動模式
            ground_y = self.screen_height - self.SIZE + self.GROUND_OFFSET
            if self.position.y >= ground_y - 50:  # 接近地面
                self.movement_mode = MovementMode.GROUND
            else:
                self.movement_mode = MovementMode.FLOAT
        
        # 恢復移動邏輯
        self.resume_movement(self.DRAG_PAUSE_REASON)
        
        debug_log(1, f"[{self.module_id}] 結束拖拽，恢復移動邏輯，當前狀態: {self.movement_mode.value}")

    def _start_behavior_state(self, state: BehaviorState):
        """開始新的行為狀態"""
        if state == BehaviorState.NORMAL_MOVE:
            # 開始正常移動
            if self.movement_mode == MovementMode.GROUND:
                self._start_walking()
            elif self.movement_mode == MovementMode.FLOAT:
                self._start_floating()
        elif state == BehaviorState.IDLE:
            # 開始閒置狀態
            self.target_velocity = Velocity(0, 0)
            self.idle_start_time = time.time()
            self.idle_duration = random.uniform(2.0, 5.0)  # 2-5秒的閒置時間
        elif state == BehaviorState.SPECIAL_MOVE:
            # 開始特殊移動
            speed_multiplier = random.choice([0.5, 1.5, 2.0])
            self.special_speed_multiplier = speed_multiplier
            debug_log(1, f"[{self.module_id}] 特殊移動速度倍數: {speed_multiplier}x")
            
            if self.movement_mode == MovementMode.GROUND:
                self._start_walking()
            elif self.movement_mode == MovementMode.FLOAT:
                self._start_floating()
        elif state == BehaviorState.TRANSITION:
            # 轉場狀態不需要特殊初始化
            pass
    
    def _on_mouse_hover(self, event):
        """滑鼠懸停事件處理"""
        hover_type = event.data.get('type')
        if hover_type == 'enter' and self.behaviors_enabled.get('interaction', True):
            # 暫停移動
            self.target_velocity = Velocity(0, 0)
        elif hover_type == 'leave':
            # 恢復移動
            if self.movement_mode == MovementMode.GROUND:
                self._start_walking()
            elif self.movement_mode == MovementMode.FLOAT:
                self._start_floating()
    
    def _on_file_drop(self, event):
        """檔案拖放事件處理"""
        files = event.data.get('files', [])
        info_log(f"[{self.module_id}] 處理檔案拖放: {len(files)} 個檔案")
        
        # 觸發特殊動畫或行為
        self.trigger_animation("turn_head", {})
        
        # 更新上下文
        self.update_context(ContextType.CROSS_MODULE_DATA, {
            'module': 'mov',
            'event': 'file_drop',
            'file_count': len(files),
            'timestamp': time.time()
        })
    
    # ========== 系統狀態回調 ==========
    
    def on_system_state_changed(self, old_state: UEPState, new_state: UEPState):
        """系統狀態變更回調"""
        debug_log(3, f"[{self.module_id}] 系統狀態變更: {old_state} -> {new_state}")
        
        # 根據系統狀態調整行為
        if new_state == UEPState.LISTENING:
            # 聆聽時可能需要更安靜的行為
            self.target_velocity = Velocity(self.target_velocity.x * 0.5, self.target_velocity.y * 0.5)
        elif new_state == UEPState.PROCESSING:
            # 處理時保持相對靜止
            self.target_velocity = Velocity(0, 0)
        elif new_state == UEPState.RESPONDING:
            # 回應時可能有特殊動作
            pass
        elif new_state == UEPState.IDLE:
            # 閒置時恢復正常行為
            if self.movement_mode == MovementMode.GROUND and not self.is_being_dragged:
                self._start_walking()
    
    def shutdown(self):
        """關閉 MOV 模組"""
        if self.movement_timer:
            self.movement_timer.stop()
        if self.behavior_timer:
            self.behavior_timer.stop()
        
        super().shutdown()
        info_log(f"[{self.module_id}] MOV 模組已關閉")
