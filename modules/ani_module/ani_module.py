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
import time
import math
import random
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
    class QObject: pass

from core.frontend_base import BaseFrontendModule, FrontendModuleType, UIEventType
from core.working_context import ContextType
from core.state_manager import UEPState
from utils.debug_helper import debug_log, info_log, error_log


class AnimationType(Enum):
    """動畫類型對應resources/animations目錄"""
    # 浮空狀態動畫 (_f)
    ANGRY_IDLE_F = "angry_idle_f"           # 生氣待機(浮空)
    AWKWARD_F = "awkward_f"                 # 尷尬(浮空)
    CLICK_F = "click_f"                     # 點擊(浮空)
    CURIOUS_IDLE_F = "curious_idle_f"       # 好奇待機(浮空)
    DANCE_F = "dance_f"                     # 舞蹈(浮空)
    DATA_SEARCH_F = "data_search_f"         # 數據搜索(浮空)
    LAUGH_F = "laugh_f"                     # 笑(浮空)
    SMILE_IDLE_F = "smile_idle_f"           # 微笑待機(浮空)
    STRUGGLE_F = "struggle_f"               # 掙扎(浮空)
    TALK_ANI_F = "talk_ani_f"               # 說話動畫1(浮空)
    TALK_ANI2_F = "talk_ani2_f"             # 說話動畫2(浮空)
    TEASE_F = "tease_f"                     # 嘲弄1(浮空)
    TEASE2_F = "tease2_f"                   # 嘲弄2(浮空)
    
    # 落地狀態動畫 (_g)
    DANCE2_G = "dance2_g"                   # 舞蹈2(落地)
    STAND_IDLE_G = "stand_idle_g"           # 站立待機(落地)
    TURN_HEAD_G = "turn_head_g"             # 轉頭(落地)
    TURN_LEFT_G = "turn_left_g"             # 轉左(落地)
    TURN_RIGHT_G = "turn_right_g"           # 轉右(落地)
    TURN_WALK_G = "turn_walk_g"             # 轉身走(落地)
    WALK_LEFT_G = "walk_left_g"             # 向左走(落地)
    WALK_RIGHT_G = "walk_right_g"           # 向右走(落地)
    YAWN_G = "yawn_g"                       # 打哈欠(落地)
    
    # 休息狀態動畫 (_l)
    SLEEP_L = "sleep_l"                     # 睡覺(休息)
    
    # 轉場動畫
    F_TO_G = "f_to_g"                       # 浮空到落地
    G_TO_F = "g_to_f"                       # 落地到浮空
    G_TO_L = "g_to_l"                       # 落地到休息
    L_TO_G = "l_to_g"                       # 休息到落地
    
    # 舊版動畫 (向後兼容)
    PUSH_UP = "push_up"
    PUSH_DOWN = "push_down"
    PUSH_LEFT = "push_left"
    PUSH_RIGHT = "push_right"


class BehaviorMode(Enum):
    """行為模式"""
    FLOAT = "float"     # 浮空模式 (_f 動畫)
    GROUND = "ground"   # 落地模式 (_g 動畫)  
    REST = "rest"       # 休息模式 (_l 動畫)


class AnimationState(Enum):
    """動畫狀態"""
    IDLE = "idle"           # 待機
    PLAYING = "playing"     # 播放中
    TRANSITIONING = "transitioning"  # 轉場中
    PAUSED = "paused"       # 暫停
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
        self.is_initialized = False
        
        # 動畫資源
        self.animations = {}  # 動畫序列字典
        self.current_animation = None
        self.animation_state = AnimationState.STOPPED
        
        # 行為模式狀態
        self.current_behavior_mode = BehaviorMode.FLOAT
        
        # 動畫控制
        self.frame_timer = None
        self.frame_interval = self.config.get('frame_interval', 16)  # 60 FPS (1000ms / 60 = 16.67ms)
        
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
                self.frame_timer = QTimer()
                self.frame_timer.timeout.connect(self._update_animation)
            
            # 載入動畫資源
            if not self._load_animation_resources():
                error_log(f"[{self.module_id}] 載入動畫資源失敗")
                return False
            
            # 註冊事件處理器
            self._register_animation_handlers()

            self.is_initialized = True
            
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
            
            # 載入新版本動畫 (支援行為模式)
            self._load_float_animations()      # 浮空動畫 (_f)
            self._load_ground_animations()     # 落地動畫 (_g)
            self._load_rest_animations()       # 休息動畫 (_l)
            self._load_transition_animations() # 轉場動畫
            
            # 載入舊版動畫 (向後兼容)
            self._load_legacy_animations()
            
            info_log(f"[{self.module_id}] 共載入 {len(self.animations)} 個動畫")
            return len(self.animations) > 0
            
        except Exception as e:
            error_log(f"[{self.module_id}] 載入動畫資源失敗: {e}")
            return False
    
    def _load_float_animations(self):
        """載入浮空狀態動畫 (_f)"""
        float_animations = [
            "angry_idle_f", "awkward_f", "click_f", "curious_idle_f",
            "dance_f", "data_search_f", "laugh_f", "smile_idle_f", 
            "struggle_f", "talk_ani_f", "talk_ani2_f", "tease_f", "tease2_f"
        ]
        
        for anim_name in float_animations:
            anim_path = os.path.join(self.animation_base_path, anim_name)
            if os.path.exists(anim_path):
                frames = self._load_frames_from_directory(anim_path, frame_duration=0.1)
                if frames:
                    # 浮空動畫大多是循環的
                    loop = anim_name in ["angry_idle_f", "curious_idle_f", "smile_idle_f", "dance_f"]
                    self.animations[anim_name] = Animation(anim_name, frames, loop=loop)
                    debug_log(2, f"[{self.module_id}] 載入 {anim_name} 動畫: {len(frames)} 幀")

    def _load_ground_animations(self):
        """載入落地狀態動畫 (_g)"""
        ground_animations = [
            "dance2_g", "stand_idle_g", "turn_head_g", "turn_left_g",
            "turn_right_g", "turn_walk_g", "walk_left_g", "walk_right_g", "yawn_g"
        ]
        
        for anim_name in ground_animations:
            anim_path = os.path.join(self.animation_base_path, anim_name)
            if os.path.exists(anim_path):
                frames = self._load_frames_from_directory(anim_path, frame_duration=0.1)
                if frames:
                    # 落地動畫中站立待機和舞蹈可循環
                    loop = anim_name in ["stand_idle_g", "dance2_g", "walk_left_g", "walk_right_g"]
                    self.animations[anim_name] = Animation(anim_name, frames, loop=loop)
                    debug_log(2, f"[{self.module_id}] 載入 {anim_name} 動畫: {len(frames)} 幀")

    def _load_rest_animations(self):
        """載入休息狀態動畫 (_l)"""
        rest_animations = ["sleep_l"]
        
        for anim_name in rest_animations:
            anim_path = os.path.join(self.animation_base_path, anim_name)
            if os.path.exists(anim_path):
                frames = self._load_frames_from_directory(anim_path, frame_duration=0.15)  # 休息動畫稍慢
                if frames:
                    self.animations[anim_name] = Animation(anim_name, frames, loop=True)  # 休息動畫循環
                    debug_log(2, f"[{self.module_id}] 載入 {anim_name} 動畫: {len(frames)} 幀")

    def _load_transition_animations(self):
        """載入轉場動畫"""
        transition_animations = ["f_to_g", "g_to_f", "g_to_l", "l_to_g"]
        
        for anim_name in transition_animations:
            anim_path = os.path.join(self.animation_base_path, anim_name)
            if os.path.exists(anim_path):
                frames = self._load_frames_from_directory(anim_path, frame_duration=0.08)  # 轉場動畫較快
                if frames:
                    self.animations[anim_name] = Animation(anim_name, frames, loop=False)  # 轉場動畫不循環
                    debug_log(2, f"[{self.module_id}] 載入 {anim_name} 轉場動畫: {len(frames)} 幀")

    def _load_legacy_animations(self):
        """載入舊版動畫 (向後兼容)"""
        legacy_animations = ["push_up", "push_down", "push_left", "push_right"]
        
        for anim_name in legacy_animations:
            anim_path = os.path.join(self.animation_base_path, anim_name)
            if os.path.exists(anim_path):
                frames = self._load_frames_from_directory(anim_path, frame_duration=0.1)
                if frames:
                    self.animations[anim_name] = Animation(anim_name, frames, loop=False)
                    debug_log(2, f"[{self.module_id}] 載入 {anim_name} 舊版動畫: {len(frames)} 幀")

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
    
    # ========== 行為模式控制方法 ==========
    
    def set_behavior_mode(self, mode: BehaviorMode) -> bool:
        """設置行為模式"""
        try:
            if mode == self.current_behavior_mode:
                return True
                
            # 執行轉場動畫
            transition_anim = self._get_transition_animation(self.current_behavior_mode, mode)
            if transition_anim:
                self._play_transition_animation(transition_anim, mode)
            else:
                # 直接切換
                self.current_behavior_mode = mode
                self._set_default_animation_for_mode(mode)
            
            info_log(f"[{self.module_id}] 切換行為模式: {self.current_behavior_mode.value} -> {mode.value}")
            return True
            
        except Exception as e:
            error_log(f"[{self.module_id}] 設置行為模式失敗: {e}")
            return False
    
    def _get_transition_animation(self, from_mode: BehaviorMode, to_mode: BehaviorMode) -> str:
        """獲取轉場動畫名稱"""
        transition_map = {
            (BehaviorMode.FLOAT, BehaviorMode.GROUND): "f_to_g",
            (BehaviorMode.GROUND, BehaviorMode.REST): "g_to_l",
            (BehaviorMode.REST, BehaviorMode.GROUND): "l_to_g"
        }
        return transition_map.get((from_mode, to_mode))
    
    def _play_transition_animation(self, transition_anim: str, target_mode: BehaviorMode):
        """播放轉場動畫"""
        if transition_anim in self.animations:
            # 播放轉場動畫
            self._play_animation({"animation_type": transition_anim})
            
            # 設置轉場完成後的回調
            def on_transition_complete():
                self.current_behavior_mode = target_mode
                self._set_default_animation_for_mode(target_mode)
            
            # 這裡可以使用 QTimer 來延遲執行
            QTimer.singleShot(len(self.animations[transition_anim].frames) * 100, on_transition_complete)
    
    def _set_default_animation_for_mode(self, mode: BehaviorMode):
        """為行為模式設置默認動畫"""
        default_animations = {
            BehaviorMode.FLOAT: "smile_idle_f",
            BehaviorMode.GROUND: "stand_idle_g", 
            BehaviorMode.REST: "sleep_l"
        }
        
        default_anim = default_animations.get(mode)
        if default_anim and default_anim in self.animations:
            self._play_animation({"animation_type": default_anim})
    
    def get_animations_for_mode(self, mode: BehaviorMode) -> List[str]:
        """獲取指定行為模式的所有動畫"""
        mode_suffix = {
            BehaviorMode.FLOAT: "_f",
            BehaviorMode.GROUND: "_g",
            BehaviorMode.REST: "_l"
        }
        
        suffix = mode_suffix.get(mode, "")
        return [name for name in self.animations.keys() if name.endswith(suffix)]
    
    def play_behavior_animation(self, mode: BehaviorMode, animation_name: str) -> bool:
        """播放指定行為模式的動畫"""
        if mode != self.current_behavior_mode:
            error_log(f"[{self.module_id}] 行為模式不匹配: 當前{self.current_behavior_mode.value}, 要求{mode.value}")
            return False
        
        available_animations = self.get_animations_for_mode(mode)
        if animation_name not in available_animations:
            error_log(f"[{self.module_id}] 動畫不適用於當前模式: {animation_name}")
            return False
        
        result = self._play_animation({"animation_type": animation_name})
        return result.get("success", False)
    
    # ==========
    
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
            elif command == 'set_behavior_animation':
                return self._set_behavior_animation(data)
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
    
    def _set_behavior_animation(self, data: dict) -> dict:
        """根據行為設置相應的動畫"""
        try:
            behavior = data.get('behavior')
            if not behavior:
                return {"error": "未指定行為類型"}
            
            # 行為與動畫的映射
            behavior_animation_mapping = {
                "idle": "stand_idle",
                "walking": "walk_ani(left",  # 可以根據方向選擇左或右
                "following": "walk_ani(left",
                "wandering": "walk_ani(left",
                "talking": "talk_ani",
                "happy": "smile_idle",
                "excited": "laugh",
                "curious": "curious_idle",
                "angry": "angry_idle"
            }
            
            # 獲取對應的動畫
            animation_type = behavior_animation_mapping.get(behavior)
            if not animation_type:
                # 如果沒有映射，使用默認動畫
                animation_type = "stand_idle"
            
            # 檢查動畫是否存在
            if animation_type not in self.animations:
                # 如果指定的動畫不存在，嘗試使用備選動畫
                fallback_animations = ["stand_idle", "smile_idle"]
                for fallback in fallback_animations:
                    if fallback in self.animations:
                        animation_type = fallback
                        break
                else:
                    return {"error": f"無法找到適合行為 '{behavior}' 的動畫"}
            
            # 播放動畫
            play_result = self._play_animation({
                "animation_type": animation_type,
                "loop": True  # 行為動畫通常需要循環
            })
            
            if play_result.get("success"):
                info_log(f"[{self.module_id}] 根據行為 '{behavior}' 設置動畫: {animation_type}")
                return {
                    "success": True,
                    "behavior": behavior,
                    "animation": animation_type
                }
            else:
                return play_result
                
        except Exception as e:
            error_log(f"[{self.module_id}] 設置行為動畫失敗: {e}")
            return {"error": str(e)}
    
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
                        if self.frame_timer:
                            self.frame_timer.stop()
                        info_log(f"[{self.module_id}] 動畫完成: {self.current_animation.name}")
                        return
                
                # 發送新幀
                self._emit_current_frame()
                
        except Exception as e:
            error_log(f"[{self.module_id}] 更新動畫異常: {e}")
    
    def get_current_frame(self) -> Optional[QPixmap]:
        """獲取當前動畫幀的 QPixmap"""
        try:
            if self.current_animation and self.current_animation.frames:
                current_frame_obj = self.current_animation.frames[self.current_animation.current_frame]
                return current_frame_obj.pixmap
            return None
        except Exception as e:
            debug_log(3, f"[{self.module_id}] 獲取當前動畫幀失敗: {e}")
            return None
    
    def get_current_animation_status(self) -> dict:
        """獲取當前動畫狀態資訊"""
        try:
            if self.current_animation:
                return {
                    "name": self.current_animation.name,
                    "current_frame": self.current_animation.current_frame,
                    "total_frames": len(self.current_animation.frames),
                    "state": self.animation_state.name if hasattr(self.animation_state, 'name') else str(self.animation_state),
                    "loop": self.current_animation.loop,
                    "is_playing": self.frame_timer.isActive() if self.frame_timer else False
                }
            return {"name": None, "current_frame": 0, "total_frames": 0, "state": "STOPPED", "loop": False, "is_playing": False}
        except Exception as e:
            debug_log(3, f"[{self.module_id}] 獲取動畫狀態失敗: {e}")
            return {"name": None, "current_frame": 0, "total_frames": 0, "state": "ERROR", "loop": False, "is_playing": False}
    
    def _emit_current_frame(self):
        """發送當前動畫幀"""
        if self.current_animation and self.current_animation.frames:
            current_frame_obj = self.current_animation.frames[self.current_animation.current_frame]
            # 直接更新當前幀，由桌面寵物的計時器來獲取
            debug_log(3, f"[{self.module_id}] 更新動畫幀: {self.current_animation.name} 第 {self.current_animation.current_frame} 幀")
    
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
