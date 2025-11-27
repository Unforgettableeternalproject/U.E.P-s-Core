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
        
        # 投擲後行為
        self._post_throw_tease = False
        self._post_throw_time = 0.0
        self._post_throw_delay = float(config.get("throw_post_tease_delay", 3.0))
        
        # 🔧 投擲動畫超時保護
        self._throw_anim_name: Optional[str] = None
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
        
        # 計算速度和距離
        vx, vy, speed = drag_tracker.calculate_velocity()
        
        drag_distance = 0
        if drag_start_pos:
            drag_distance = math.hypot(
                self.coordinator.position.x - drag_start_pos.x,
                self.coordinator.position.y - drag_start_pos.y
            )
        
        debug_log(2, f"[{self.__class__.__name__}] 拖曳結束: 速度={speed:.1f} px/s, 距離={drag_distance:.1f} px")
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
        
        # 限制最大投擲速度
        if speed > self.max_throw_speed:
            scale = self.max_throw_speed / speed
            vx *= scale
            vy *= scale
            speed = self.max_throw_speed
        
        # 設置投擲速度
        if hasattr(self.coordinator, 'velocity'):
            self.coordinator.velocity.x = vx
            self.coordinator.velocity.y = vy
        
        # **投擲動畫處理**
        # 目前沒有專用 throw 動畫，使用 struggle 作為 fallback
        # 關鍵：設置 loop=False，讓 struggle 播放一次後停止（避免卡住）
        # 使用 immediate_interrupt 強制結束拖曳時的 loop=True struggle
        if hasattr(self.coordinator, '_trigger_anim'):
            # 檢查是否有 throw 動畫（未來擴展）
            throw_anim = "throw" if self._has_animation("throw") else "struggle"
            self.coordinator._trigger_anim(
                throw_anim, 
                {"loop": False, "immediate_interrupt": True, "force_restart": True}, 
                source="throw_handler"
            )
            if throw_anim == "struggle":
                debug_log(2, f"[{self.__class__.__name__}] 使用 struggle 作為投擲動畫 (loop=False)")
            
            # 🔧 設置手動超時保護（防止動畫卡住）
            # 投擲動畫應該在 2 秒內完成，超時後強制清除優先度
            import time
            self._throw_anim_name = throw_anim
            self._throw_anim_deadline = time.time() + 2.0
            debug_log(2, f"[{self.__class__.__name__}] 投擲動畫超時保護已啟動 (2.0s)")
        
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
        
        Note:
            暂時禁用 tease 動畫，等待未來 throw 專用動畫實現
        """
        # 暂時禁用 tease 動畫
        # now = time.time()
        # self._post_throw_tease = True
        # self._post_throw_time = now + self._post_throw_delay
        
        debug_log(1, f"[{self.__class__.__name__}] 投擲落地（tease 動畫已禁用）")
    
    def update(self, now: float):
        """
        每幀更新，檢查是否需要執行投擲後行為
        
        Args:
            now: 當前時間
        """
        # 🔧 檢查投擲動畫超時（防止卡住）
        if self._throw_anim_name and self._throw_anim_deadline > 0:
            if now > self._throw_anim_deadline:
                debug_log(1, f"[{self.__class__.__name__}] ⚠️ 投擲動畫 {self._throw_anim_name} 超時，強制清除優先度")
                # 手動清除優先度管理器中的動畫請求
                if hasattr(self.coordinator, '_animation_priority'):
                    self.coordinator._animation_priority.on_animation_finished(self._throw_anim_name)
                    debug_log(2, f"[{self.__class__.__name__}] 已手動清除動畫優先度: {self._throw_anim_name}")
                # 清除超時狀態
                self._throw_anim_name = None
                self._throw_anim_deadline = 0.0
        
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
        
        if hasattr(self.coordinator, '_switch_behavior') and BehaviorState:
            self.coordinator._switch_behavior(BehaviorState.TRANSITION)
        
        # 標記需要在進入 NORMAL_MOVE 後播放 tease2_f
        if hasattr(self.coordinator, '_post_throw_tease_pending'):
            self.coordinator._post_throw_tease_pending = True
    
    @property
    def is_waiting_for_tease(self) -> bool:
        """是否正在等待播放調皮動畫"""
        return self._post_throw_tease
    
    def cancel_tease(self):
        """取消投擲後的調皮行為（例如被拖曳打斷）"""
        if self._post_throw_tease:
            debug_log(1, f"[{self.__class__.__name__}] 取消投擲後調皮行為")
            self._post_throw_tease = False
