# modules/ani_module/ani_module.py
"""
ANI 模組 - 動畫控制器

負責：
- 管理動畫幀序列和狀態
- 根據系統狀態切換動畫
- 處理動畫過渡和插值
- 響應情感狀態變化
- 載入和管理動畫資源
"""

import os
import sys
import time
import threading
from typing import Dict, Any, Optional, List
from enum import Enum, auto

try:
    from PyQt5.QtCore import QTimer, pyqtSignal, QObject
    from PyQt5.QtGui import QPixmap
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    # 定義替代類別
    class QTimer: pass
    class QPixmap: pass
    def pyqtSignal(*args): return None

from core.frontend_base import BaseFrontendModule, FrontendModuleType, UIEventType
from core.working_context import ContextType
from core.state_manager import UEPState
from utils.debug_helper import debug_log, info_log, error_log


class AnimationType(Enum):
    """動畫類型對應resources/animations目錄"""
    # 基本狀態
    STAND_IDLE = "stand_idle"           # 站立待機
    SMILE_IDLE = "smile_idle"           # 微笑待機  
    CURIOUS_IDLE = "curious_idle"       # 好奇待機
    ANGRY_IDLE = "angry_idle"           # 生氣待機
    
    # 說話動畫
    TALK_ANI = "talk_ani"               # 說話動畫1
    TALK_ANI2 = "talk_ani2"             # 說話動畫2
    
    # 行走動畫
    WALK_LEFT = "walk_ani(left"         # 向左走
    WALK_RIGHT = "walk_ani(right"       # 向右走
    
    # 轉身動畫
    TURN_BODY_LEFT = "turn_body(left"   # 轉身向左
    TURN_BODY_RIGHT = "turn_body(right" # 轉身向右
    TURN_BODY_WALK = "turn_body+walk"   # 轉身行走
    TURN_HEAD = "turn_head"             # 轉頭
    
    # 表情動畫
    LAUGH = "laugh"                     # 笑
    TEASE = "tease"                     # 調皮1
    TEASE2 = "tease2"                   # 調皮2
    AWKWARD = "awkward"                 # 尷尬
    YAWN = "yawn"                       # 打哈欠
    
    # 動作動畫
    LIE_DOWN = "lie_down"               # 躺下
    PULL_UMBRELLA = "pull_umbrella"     # 拉雨傘
    DATA_SEARCH = "data_search"         # 資料搜尋
    
    # 推動動畫
    PUSH_UP = "push_up"                 # 向上推
    PUSH_DOWN = "push_down"             # 向下推
    PUSH_LEFT = "push_left"             # 向左推
    PUSH_RIGHT = "push_right"           # 向右推


class AnimationState(Enum):
    """動畫狀態"""
    STOPPED = auto()    # 停止
    PLAYING = auto()    # 播放中
    PAUSED = auto()     # 暫停
    FINISHED = auto()   # 完成


class AnimationFrame:
    """動畫幀資料結構"""
    def __init__(self, pixmap: QPixmap, duration: float = 0.1):
        self.pixmap = pixmap
        self.duration = duration  # 幀持續時間 (秒)


class Animation:
    """動畫序列"""
    def __init__(self, name: str, frames: List[AnimationFrame], loop: bool = True):
        self.name = name
        self.frames = frames
        self.loop = loop
        self.current_frame = 0
        self.last_frame_time = 0


class ANIModule(BaseFrontendModule):
    """ANI 模組 - 動畫控制器"""
    
    def __init__(self, config: dict = None):
        super().__init__(FrontendModuleType.ANI)
        
        self.config = config or {}
        
        # 動畫資源
        self.animations = {}  # 動畫序列字典
        self.current_animation = None
        self.animation_state = AnimationState.STOPPED
        
        # 動畫控制
        self.frame_timer = None
        self.frame_interval = self.config.get('frame_interval', 100)  # 毫秒
        
        # 動畫資源路徑
        self.animation_base_path = None
        
        info_log(f"[{self.module_id}] ANI 模組初始化")
    
    def initialize_frontend(self) -> bool:
        """初始化動畫前端功能"""
        try:
            if not PYQT5_AVAILABLE:
                error_log(f"[{self.module_id}] PyQt5 不可用，無法初始化動畫")
                return False
            
            # 初始化計時器
            if PYQT5_AVAILABLE:
                # 設置計時器回調
                self.signals.add_timer_callback("frame_update", self._update_animation)
                
                self.frame_timer = QTimer()
                self.frame_timer.timeout.connect(lambda: self.signals.timer_timeout("frame_update"))
            
            # 載入動畫資源
            if not self._load_animation_resources():
                error_log(f"[{self.module_id}] 載入動畫資源失敗")
                return False
            
            # 註冊事件處理器
            self._register_animation_handlers()
            
            info_log(f"[{self.module_id}] ANI 前端初始化成功")
            return True
            
        except Exception as e:
            error_log(f"[{self.module_id}] ANI 前端初始化失敗: {e}")
            return False
    
    def _load_animation_resources(self) -> bool:
        """載入動畫資源"""
        try:
            # 設置動畫資源基礎路徑
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            self.animation_base_path = os.path.join(project_root, "resources", "animations")
            
            if not os.path.exists(self.animation_base_path):
                error_log(f"[{self.module_id}] 動畫資源目錄不存在: {self.animation_base_path}")
                return False
            
            info_log(f"[{self.module_id}] 載入動畫資源從: {self.animation_base_path}")
            
            # 載入各種動畫
            self._load_idle_animations()
            self._load_talking_animations()
            self._load_walking_animations()
            self._load_expression_animations()
            self._load_action_animations()
            
            info_log(f"[{self.module_id}] 共載入 {len(self.animations)} 個動畫")
            return len(self.animations) > 0
            
        except Exception as e:
            error_log(f"[{self.module_id}] 載入動畫資源失敗: {e}")
            return False
    
    def _load_idle_animations(self):
        """載入待機動畫"""
        idle_types = ["stand_idle", "smile_idle", "curious_idle", "angry_idle"]
        
        for idle_type in idle_types:
            idle_path = os.path.join(self.animation_base_path, idle_type)
            if os.path.exists(idle_path):
                frames = self._load_frames_from_directory(idle_path, frame_duration=0.1)
                if frames:
                    self.animations[idle_type] = Animation(idle_type, frames, loop=True)
                    debug_log(2, f"[{self.module_id}] 載入 {idle_type} 動畫: {len(frames)} 幀")
    
    def _load_talking_animations(self):
        """載入說話動畫"""
        talk_types = ["talk_ani", "talk_ani2"]
        
        for talk_type in talk_types:
            talk_path = os.path.join(self.animation_base_path, talk_type)
            if os.path.exists(talk_path):
                frames = self._load_frames_from_directory(talk_path, frame_duration=0.05)
                if frames:
                    self.animations[talk_type] = Animation(talk_type, frames, loop=True)
                    debug_log(2, f"[{self.module_id}] 載入 {talk_type} 動畫: {len(frames)} 幀")
    
    def _load_walking_animations(self):
        """載入行走動畫"""
        walk_types = ["walk_ani(left", "walk_ani(right"]
        
        for walk_type in walk_types:
            walk_path = os.path.join(self.animation_base_path, walk_type)
            if os.path.exists(walk_path):
                frames = self._load_frames_from_directory(walk_path, frame_duration=0.08)
                if frames:
                    self.animations[walk_type] = Animation(walk_type, frames, loop=True)
                    debug_log(2, f"[{self.module_id}] 載入 {walk_type} 動畫: {len(frames)} 幀")
    
    def _load_expression_animations(self):
        """載入表情動畫"""
        expression_types = ["laugh", "tease", "tease2", "awkward", "yawn"]
        
        for expr_type in expression_types:
            expr_path = os.path.join(self.animation_base_path, expr_type)
            if os.path.exists(expr_path):
                frames = self._load_frames_from_directory(expr_path, frame_duration=0.1)
                if frames:
                    self.animations[expr_type] = Animation(expr_type, frames, loop=False)
                    debug_log(2, f"[{self.module_id}] 載入 {expr_type} 動畫: {len(frames)} 幀")

    def _load_action_animations(self):
        """載入動作動畫"""
        action_types = [
            "lie_down", "pull_umbrella", "data_search",
            "push_up", "push_down", "push_left", "push_right",
            "turn_body(left", "turn_body(right", "turn_body+walk", "turn_head"
        ]
        
        for action_type in action_types:
            action_path = os.path.join(self.animation_base_path, action_type)
            if os.path.exists(action_path):
                frames = self._load_frames_from_directory(action_path, frame_duration=0.1)
                if frames:
                    loop = action_type in ["turn_head"]  # 轉頭動畫可循環
                    self.animations[action_type] = Animation(action_type, frames, loop=loop)
                    debug_log(2, f"[{self.module_id}] 載入 {action_type} 動畫: {len(frames)} 幀")

    def _load_frames_from_directory(self, directory_path: str, frame_duration: float = 0.1) -> List[AnimationFrame]:
        """從目錄載入動畫幀"""
        frames = []
        try:
            if not os.path.exists(directory_path):
                return frames
            
            # 獲取所有圖片文件並排序
            image_files = []
            for file in os.listdir(directory_path):
                if file.lower().endswith(('.png', '.jpg', '.jpeg')):
                    image_files.append(file)
            
            image_files.sort()  # 確保幀順序正確
            
            for image_file in image_files:
                image_path = os.path.join(directory_path, image_file)
                pixmap = QPixmap(image_path)
                if not pixmap.isNull():
                    frames.append(AnimationFrame(pixmap, frame_duration))
            
        except Exception as e:
            error_log(f"[{self.module_id}] 載入目錄 {directory_path} 的幀失敗: {e}")
        
        return frames
    
    def _register_animation_handlers(self):
        """註冊動畫相關事件處理器"""
        self.register_event_handler(UIEventType.STATE_CHANGE, self._on_state_change)
        self.register_event_handler(UIEventType.MOUSE_HOVER, self._on_mouse_hover)
    
    def handle_frontend_request(self, data: dict) -> dict:
        """處理前端動畫請求"""
        try:
            command = data.get('command')
            
            if command == 'play_animation':
                return self._play_animation(data)
            elif command == 'stop_animation':
                return self._stop_animation()
            elif command == 'pause_animation':
                return self._pause_animation()
            elif command == 'resume_animation':
                return self._resume_animation()
            elif command == 'get_current_animation':
                return self._get_current_animation_info()
            elif command == 'list_animations':
                return self._list_available_animations()
            else:
                return {"error": f"未知動畫命令: {command}"}
                
        except Exception as e:
            error_log(f"[{self.module_id}] 處理動畫請求異常: {e}")
            return {"error": str(e)}
    
    def _play_animation(self, data: dict) -> dict:
        """播放動畫"""
        try:
            animation_type = data.get('animation_type')
            if not animation_type:
                return {"error": "未指定動畫類型"}
            
            if animation_type not in self.animations:
                return {"error": f"動畫不存在: {animation_type}"}
            
            self.current_animation = self.animations[animation_type]
            self.current_animation.current_frame = 0
            self.current_animation.last_frame_time = time.time()
            
            self.animation_state = AnimationState.PLAYING
            
            # 啟動計時器
            if not self.frame_timer.isActive():
                self.frame_timer.start(self.frame_interval)
            
            # 立即發送第一幀
            self._emit_current_frame()
            
            info_log(f"[{self.module_id}] 開始播放動畫: {animation_type}")
            return {"success": True, "animation": animation_type}
            
        except Exception as e:
            error_log(f"[{self.module_id}] 播放動畫異常: {e}")
            return {"error": str(e)}
    
    def _stop_animation(self) -> dict:
        """停止動畫"""
        try:
            if self.frame_timer and self.frame_timer.isActive():
                self.frame_timer.stop()
            
            self.animation_state = AnimationState.STOPPED
            self.current_animation = None
            
            # 回到靜態動畫
            if AnimationType.STATIC.value in self.animations:
                static_frame = self.animations[AnimationType.STATIC.value].frames[0]
                if hasattr(self.signals, 'animation_ready'):
                    self.signals.animation_ready.emit(static_frame.pixmap)
            
            info_log(f"[{self.module_id}] 停止動畫")
            return {"success": True}
            
        except Exception as e:
            return {"error": str(e)}
    
    def _pause_animation(self) -> dict:
        """暫停動畫"""
        if self.frame_timer and self.frame_timer.isActive():
            self.frame_timer.stop()
            self.animation_state = AnimationState.PAUSED
            return {"success": True}
        return {"error": "沒有正在播放的動畫"}
    
    def _resume_animation(self) -> dict:
        """恢復動畫"""
        if self.animation_state == AnimationState.PAUSED and self.current_animation:
            self.frame_timer.start(self.frame_interval)
            self.animation_state = AnimationState.PLAYING
            return {"success": True}
        return {"error": "沒有暫停的動畫"}
    
    def _get_current_animation_info(self) -> dict:
        """獲取當前動畫資訊"""
        if self.current_animation:
            return {
                "name": self.current_animation.name,
                "current_frame": self.current_animation.current_frame,
                "total_frames": len(self.current_animation.frames),
                "state": self.animation_state.name,
                "loop": self.current_animation.loop
            }
        return {"error": "沒有正在播放的動畫"}
    
    def _list_available_animations(self) -> dict:
        """列出可用動畫"""
        return {
            "animations": list(self.animations.keys()),
            "count": len(self.animations)
        }
    
    def _update_animation(self):
        """更新動畫幀"""
        if not self.current_animation or self.animation_state != AnimationState.PLAYING:
            return
        
        try:
            current_time = time.time()
            current_frame_obj = self.current_animation.frames[self.current_animation.current_frame]
            
            # 檢查是否需要切換到下一幀
            if current_time - self.current_animation.last_frame_time >= current_frame_obj.duration:
                self.current_animation.current_frame += 1
                self.current_animation.last_frame_time = current_time
                
                # 檢查是否到達動畫結尾
                if self.current_animation.current_frame >= len(self.current_animation.frames):
                    if self.current_animation.loop:
                        self.current_animation.current_frame = 0  # 重新開始
                    else:
                        # 動畫完成
                        self.animation_state = AnimationState.FINISHED
                        self.frame_timer.stop()
                        self.animation_finished.emit(self.current_animation.name)
                        return
                
                # 發送新幀
                self._emit_current_frame()
                
        except Exception as e:
            error_log(f"[{self.module_id}] 更新動畫異常: {e}")
    
    def _emit_current_frame(self):
        """發送當前動畫幀"""
        if self.current_animation and self.current_animation.frames:
            current_frame_obj = self.current_animation.frames[self.current_animation.current_frame]
            if hasattr(self.signals, 'animation_ready'):
                self.signals.animation_ready.emit(current_frame_obj.pixmap)
    
    def handle_animation_request(self, animation_type: str, params: dict):
        """處理來自其他模組的動畫請求"""
        self._play_animation({
            'animation_type': animation_type,
            **params
        })
    
    # ========== 事件處理器 ==========
    
    def _on_state_change(self, event):
        """狀態變更事件處理"""
        state_data = event.data
        debug_log(3, f"[{self.module_id}] 響應狀態變更: {state_data}")
    
    def _on_mouse_hover(self, event):
        """滑鼠懸停事件處理"""
        hover_type = event.data.get('type')
        if hover_type == 'enter':
            # 滑鼠進入，可能需要特殊動畫
            debug_log(3, f"[{self.module_id}] 滑鼠進入，考慮切換動畫")
        elif hover_type == 'leave':
            # 滑鼠離開，回到正常動畫
            debug_log(3, f"[{self.module_id}] 滑鼠離開，回到正常動畫")

    # ========== 系統狀態回調 ==========
    
    def on_system_state_changed(self, old_state: UEPState, new_state: UEPState):
        """系統狀態變更回調 - 自動切換動畫"""
        debug_log(3, f"[{self.module_id}] 系統狀態變更，切換動畫: {old_state} -> {new_state}")
        
        # 根據系統狀態自動切換動畫
        if new_state == UEPState.LISTENING:
            self._play_animation({'animation_type': AnimationType.TALKING.value})
        elif new_state == UEPState.PROCESSING:
            self._play_animation({'animation_type': AnimationType.THINKING.value})
        elif new_state == UEPState.RESPONDING:
            self._play_animation({'animation_type': AnimationType.TALKING.value})
        elif new_state == UEPState.IDLE:
            self._play_animation({'animation_type': AnimationType.STATIC.value})
        
        # 更新上下文
        self.update_context(ContextType.CROSS_MODULE_DATA, {
            'module': 'ani',
            'event': 'state_change',
            'old_state': old_state.name if old_state else None,
            'new_state': new_state.name if new_state else None,
            'timestamp': time.time()
        })
    
    def shutdown(self):
        """關閉 ANI 模組"""
        if self.frame_timer:
            self.frame_timer.stop()
        
        self.animations.clear()
        self.current_animation = None
        
        super().shutdown()
        info_log(f"[{self.module_id}] ANI 模組已關閉")
