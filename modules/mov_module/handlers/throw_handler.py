"""
投擲處理器

處理投擲檢測、投擲物理模擬和投擲後行為
"""

import time
import math
from typing import Any, Optional
from utils.debug_helper import debug_log, info_log, error_log

from .base_handler import BaseHandler

try:
    from ..core.position import Position, Velocity
    from ..core.state_machine import MovementMode, BehaviorState
    from ..core.drag_tracker import DragTracker
except ImportError:
    Position = None  # type: ignore
    Velocity = None  # type: ignore
    MovementMode = None  # type: ignore
    BehaviorState = None  # type: ignore
    DragTracker = None  # type: ignore


class ThrowHandler(BaseHandler):
    """
    投擲處理器
    
    職責：
    1. 從 DragTracker 檢測投擲動作
    2. 切換到 THROWN 模式並設置初始速度
    3. 管理投擲後的調皮行為（3秒延遲）
    4. 提供可配置的投擲參數
    """
    
    def __init__(self, coordinator):
        super().__init__(coordinator)
        
        # 從配置讀取投擲參數
        config = getattr(coordinator, 'config', {})
        
        # 投擲檢測參數
        self.throw_threshold_speed = float(config.get("throw_threshold_speed", 800.0))
        self.throw_threshold_dist = float(config.get("throw_threshold_dist", 30.0))
        self.max_throw_speed = float(config.get("max_throw_speed", 80.0))
        self.horizontal_threshold = float(config.get("horizontal_throw_threshold", 15.0))  # 水平速度門檻
        
        # 投擲後行為
        self._post_throw_tease = False
        self._post_throw_time = 0.0
        self._post_throw_delay = float(config.get("throw_post_tease_delay", 3.0))
        
        # 🔧 投擲動畫追蹤
        self._throw_direction: Optional[str] = None  # 'left', 'right', 'vertical'
        self._is_in_throw_animation = False  # 是否正在播放投擲動畫
        self._throw_anim_deadline: float = 0.0
        
        info_log(f"[{self.__class__.__name__}] 初始化: 速度門檻={self.throw_threshold_speed}, "
                f"距離門檻={self.throw_threshold_dist}, 最大速度={self.max_throw_speed}")
    
    def can_handle(self, event: Any) -> bool:
        """此 handler 不處理外部事件，由 coordinator 主動調用"""
        return False
    
    def handle(self, event: Any) -> bool:
        """此 handler 不處理外部事件"""
        return False
    
    def check_throw(self, drag_tracker: 'DragTracker', drag_start_pos: Optional['Position']) -> bool:
        """
        檢測是否為投擲動作
        
        Args:
            drag_tracker: 拖曳追蹤器（包含速度數據）
            drag_start_pos: 拖曳開始位置
            
        Returns:
            是否觸發投擲
        """
        if not drag_tracker or not hasattr(self.coordinator, 'position'):
            return False
        
        # 計算速度和距離（只使用最近 0.15 秒的拖曳點）
        vx, vy, speed = drag_tracker.calculate_velocity(time_window=0.15)
        
        drag_distance = 0
        if drag_start_pos:
            drag_distance = math.hypot(
                self.coordinator.position.x - drag_start_pos.x,
                self.coordinator.position.y - drag_start_pos.y
            )
        
        debug_log(2, f"[{self.__class__.__name__}] 拖曳結束: 速度={speed:.1f} px/s (最近0.15s), 距離={drag_distance:.1f} px")
        debug_log(2, f"[{self.__class__.__name__}]   速度分量: vx={vx:.1f}, vy={vy:.1f}")
        debug_log(2, f"[{self.__class__.__name__}]   投擲門檻: 速度>{self.throw_threshold_speed} 且 距離>{self.throw_threshold_dist}")
        
        # 判斷是否觸發投擲
        is_throw = (speed > self.throw_threshold_speed and drag_distance > self.throw_threshold_dist)
        
        debug_log(1, f"[{self.__class__.__name__}]   投擲判斷: {'YES' if is_throw else 'NO'}")
        
        if is_throw:
            self._execute_throw(vx, vy, speed)
        
        return is_throw
    
    def _execute_throw(self, vx: float, vy: float, speed: float):
        """
        執行投擲動作
        
        Args:
            vx: 水平速度
            vy: 垂直速度
            speed: 總速度
        """
        if not MovementMode or not Velocity:
            return
        
        # 切換到投擲模式
        if hasattr(self.coordinator, 'movement_mode'):
            self.coordinator.movement_mode = MovementMode.THROWN
        
        # 分別限制水平和垂直速度（避免垂直投擲過快）
        max_vx = 50.0  # 水平最大速度
        max_vy = 30.0  # 垂直最大速度（向上為負）
        
        # 限制水平速度
        if abs(vx) > max_vx:
            vx = max_vx if vx > 0 else -max_vx
            debug_log(2, f"[{self.__class__.__name__}] 水平速度限制到 ±{max_vx}")
        
        # 限制垂直速度（向上為負值）
        if vy < -max_vy:  # 向上投擲
            vy = -max_vy
            debug_log(2, f"[{self.__class__.__name__}] 向上速度限制到 -{max_vy}")
        elif vy > max_vy:  # 向下投擲（不太可能）
            vy = max_vy
        
        # 重新計算總速度
        speed = math.hypot(vx, vy)
        
        # 設置投擲速度
        if hasattr(self.coordinator, 'velocity'):
            self.coordinator.velocity.x = vx
            self.coordinator.velocity.y = vy
        
        # **判斷投擲方向**
        abs_vx = abs(vx)
        debug_log(1, f"[{self.__class__.__name__}] 投擲方向判斷: abs_vx={abs_vx:.1f}, threshold={self.horizontal_threshold}")
        
        if abs_vx > self.horizontal_threshold:
            # 水平投擲：使用 swoop 動畫
            self._throw_direction = 'left' if vx < 0 else 'right'
            throw_anim = f"swoop_{self._throw_direction}"
            debug_log(1, f"[{self.__class__.__name__}] 水平投擲 → {throw_anim}")
        else:
            # 垂直投擲：使用 struggle 動畫
            self._throw_direction = 'vertical'
            throw_anim = "struggle"
            debug_log(1, f"[{self.__class__.__name__}] 垂直投擲 → {throw_anim}")
        
        # **播放投擲動畫**
        if hasattr(self.coordinator, '_trigger_anim'):
            from ..core.animation_priority import AnimationPriority
            
            # 檢查動畫是否存在，否則 fallback 到 struggle
            if not self._has_animation(throw_anim):
                debug_log(1, f"[{self.__class__.__name__}] ⚠️ 動畫 {throw_anim} 不存在，使用 struggle")
                throw_anim = "struggle"
                self._throw_direction = 'vertical'
            
            self._is_in_throw_animation = True
            info_log(f"[{self.__class__.__name__}] 🎬 觸發投擲動畫: {throw_anim} (方向={self._throw_direction})")
            info_log(f"[{self.__class__.__name__}]   速度: vx={vx:.1f}, vy={vy:.1f}, 總速度={speed:.1f}")
            
            self.coordinator._trigger_anim(
                throw_anim, 
                {
                    "loop": False,  # 只播放一次,自動停在最後一幀
                    "force_restart": True,
                }, 
                source="throw_handler",
                priority=AnimationPriority.USER_INTERACTION
            )
        
        info_log(f"[{self.__class__.__name__}] 觸發投擲！速度={speed:.1f} (vx={vx:.1f}, vy={vy:.1f})")
    
    def _has_animation(self, anim_name: str) -> bool:
        """檢查動畫是否存在"""
        if not hasattr(self.coordinator, 'ani_module'):
            return False
        ani = self.coordinator.ani_module
        if not hasattr(ani, 'manager') or not hasattr(ani.manager, 'clips'):
            return False
        return anim_name in ani.manager.clips
    
    def handle_throw_landing(self):
        """
        處理投擲落地
        
        應該在 coordinator 檢測到投擲結束時調用
        播放對應的落地動畫 (swoop_*_end)
        """
        if not self._is_in_throw_animation:
            return
        
        # 播放落地動畫
        if hasattr(self.coordinator, '_trigger_anim'):
            from ..core.animation_priority import AnimationPriority
            
            # 根據投擲方向選擇落地動畫
            if self._throw_direction in ['left', 'right']:
                land_anim = f"swoop_{self._throw_direction}_end"
                
                # 檢查動畫是否存在
                if self._has_animation(land_anim):
                    self.coordinator._trigger_anim(
                        land_anim,
                        {
                            "loop": False,
                            "force_restart": True,
                            "await_finish": True,  # 等待落地動畫完成
                            "max_wait": 1.0,  # 最多等待1秒
                        },
                        source="throw_handler",
                        priority=AnimationPriority.USER_INTERACTION
                    )
                    info_log(f"[{self.__class__.__name__}] 🎬 播放落地動畫: {land_anim}，zoom 保持 1.5")
                else:
                    debug_log(1, f"[{self.__class__.__name__}] 落地動畫 {land_anim} 不存在，等待動畫完成回調")
            # 不在這裡重置標記，等待 on_throw_animation_complete() 回調
        
        # 不重置 _throw_direction，讓它保持到動畫完成
    
    def update(self, now: float):
        """
        每幀更新，檢查是否需要執行投擲後行為
        
        Args:
            now: 當前時間
        """
        # 檢查投擲後調皮時間
        if self._post_throw_tease and now >= self._post_throw_time:
            self._execute_post_throw_tease()
    
    def _execute_post_throw_tease(self):
        """執行投擲後的調皮行為"""
        debug_log(1, f"[{self.__class__.__name__}] 投擲後延遲已到，開始調皮行為")
        self._post_throw_tease = False
        
        if not hasattr(self.coordinator, '_ground_y'):
            return
        
        # 移動回螢幕中間並播放 tease2_f
        gy = self.coordinator._ground_y()
        
        v_left = getattr(self.coordinator, 'v_left', 0)
        v_right = getattr(self.coordinator, 'v_right', 1920)
        screen_center_x = (v_left + v_right) / 2
        
        # 設置目標
        if hasattr(self.coordinator, '_set_target'):
            self.coordinator._set_target(screen_center_x, gy)
        
        # 播放轉場動畫然後切換到漂浮模式移動
        if hasattr(self.coordinator, '_trigger_anim'):
            self.coordinator._trigger_anim("g_to_f", {"loop": False}, source="throw_handler")
        
        # 投擲時不切換行為狀態（避免 TransitionBehavior 觸發 idle 動畫）
        # 只改變 MovementMode，保持當前行為狀態
        debug_log(1, f"[{self.__class__.__name__}] 投擲期間不改變行為狀態，保持當前狀態")
        
        # 標記需要在進入 NORMAL_MOVE 後播放 tease2_f
        if hasattr(self.coordinator, '_post_throw_tease_pending'):
            self.coordinator._post_throw_tease_pending = True
    
    @property
    def is_waiting_for_tease(self) -> bool:
        """是否正在等待播放調皮動畫"""
        return self._post_throw_tease
    
    @property
    def is_in_throw_animation(self) -> bool:
        """是否正在播放投擲動畫序列"""
        return self._is_in_throw_animation
    
    def on_throw_animation_complete(self):
        """投擲動畫序列完成（落地動畫播完）"""
        info_log(f"[{self.__class__.__name__}] ✅ 投擲動畫序列完全結束，現在可以重置 zoom")
        self._is_in_throw_animation = False
        self._throw_direction = None
    
    def cancel_throw(self):
        """取消投擲動畫（例如被拖曳打斷）"""
        if self._is_in_throw_animation:
            debug_log(1, f"[{self.__class__.__name__}] 取消投擲動畫")
            self._is_in_throw_animation = False
            self._throw_direction = None
            
            # 重置 movement_mode，避免卡在 THROWN 狀態
            if hasattr(self.coordinator, 'movement_mode') and MovementMode:
                if self.coordinator.movement_mode == MovementMode.THROWN:
                    debug_log(1, f"[{self.__class__.__name__}] 重置 movement_mode: THROWN → FLOAT")
                    self.coordinator.movement_mode = MovementMode.FLOAT
    
    def cancel_tease(self):
        """取消投擲後的調皮行為（例如被拖曳打斷）"""
        if self._post_throw_tease:
            debug_log(1, f"[{self.__class__.__name__}] 取消投擲後調皮行為")
            self._post_throw_tease = False
