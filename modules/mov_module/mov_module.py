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
    
    def trigger_animation(self, animation_type: str, params: dict = None):
        """觸發動畫，替代 animation_trigger.emit 調用"""
        params = params or {}
        debug_log(2, f"[{self.module_id}] 觸發動畫: {animation_type}")
        
        # 呼叫所有註冊的回調函數
        for callback in self._animation_callbacks:
            try:
                callback(animation_type, params)
            except Exception as e:
                error_log(f"[{self.module_id}] 執行動畫回調時發生錯誤: {e}")
    
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
                self.movement_timer.start(16)  # ~60 FPS
                
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
            
            # 檢查是否需要切換行為
            if current_time - self.last_behavior_switch > self.behavior_switch_interval:
                self._switch_behavior()
                self.last_behavior_switch = current_time
                self.behavior_switch_interval = random.uniform(3.0, 6.0)
            
            # 執行當前行為
            self._execute_current_behavior()
            
        except Exception as e:
            error_log(f"[{self.module_id}] 行為更新異常: {e}")
    
    def _apply_physics(self):
        """應用物理效果"""
        # 漸進到目標速度
        self.velocity.x += (self.target_velocity.x - self.velocity.x) * 0.12
        self.velocity.y += (self.target_velocity.y - self.velocity.y) * 0.12
        
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
        if hasattr(self.signals, 'position_changed'):
            self.signals.position_changed.emit({
                "x": int(self.position.x),
                "y": int(self.position.y)
            })
    
    # ========== 事件處理器 ==========
    
    def _on_drag_start(self, event):
        """拖拽開始事件處理"""
        self.is_being_dragged = True
        self.movement_mode = MovementMode.DRAGGING
        self.drag_history.clear()
        
        start_pos = event.data.get('start_position', {})
        self.drag_start_position = Position(
            start_pos.get('x', self.position.x),
            start_pos.get('y', self.position.y)
        )
        
        debug_log(1, f"[{self.module_id}] 開始拖拽")
    
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
                self._start_walking()
            else:
                self.movement_mode = MovementMode.FLOAT
                self._start_floating()
        
        debug_log(1, f"[{self.module_id}] 結束拖拽，切換到: {self.movement_mode.value}")

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
