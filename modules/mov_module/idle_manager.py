"""
modules/mov_module/idle_manager.py
Auto-sleep idle detection manager

管理使用者閒置檢測和自動睡眠功能：
- 追蹤使用者互動（拖曳、點擊、系統事件）
- 閒置計時器
- 自動觸發睡眠動畫
- 互動時自動喚醒
"""

import time
from typing import Optional, Callable
from configs.user_settings_manager import get_user_setting
from utils.debug_helper import debug_log, info_log


class IdleManager:
    """
    閒置管理器 - 追蹤使用者互動並觸發自動睡眠
    
    與 user_settings 整合，從 behavior.auto_sleep 讀取設定
    """
    
    def __init__(self):
        """初始化閒置管理器"""
        self._last_interaction_time: float = time.time()
        self._is_sleeping: bool = False
        self._sleep_callback: Optional[Callable[[str], None]] = None
        self._wake_callback: Optional[Callable[[], None]] = None
        
        # 從 user_settings 讀取設定
        self._update_settings()
        
        info_log("[IdleManager] 閒置管理器初始化完成")
    
    def _update_settings(self):
        """從 user_settings 更新設定"""
        self.enabled = get_user_setting("behavior.auto_sleep.enabled", True)
        self.max_idle_time = get_user_setting("behavior.auto_sleep.max_idle_time", 1800)  # 預設30分鐘
        self.sleep_animation = get_user_setting("behavior.auto_sleep.sleep_animation", "sleep_l")
        self.wake_on_interaction = get_user_setting("behavior.auto_sleep.wake_on_interaction", True)
        
        debug_log(3, f"[IdleManager] 設定已更新: enabled={self.enabled}, max_idle_time={self.max_idle_time}s")
    
    def set_sleep_callback(self, callback: Callable[[str], None]):
        """
        設置睡眠回調函數
        
        Args:
            callback: 睡眠函數，接收動畫名稱
        """
        self._sleep_callback = callback
        debug_log(2, "[IdleManager] 已設置睡眠回調")
    
    def set_wake_callback(self, callback: Callable[[], None]):
        """
        設置喚醒回調函數
        
        Args:
            callback: 喚醒函數
        """
        self._wake_callback = callback
        debug_log(2, "[IdleManager] 已設置喚醒回調")
    
    def record_interaction(self, interaction_type: str = "generic"):
        """
        記錄使用者互動
        
        Args:
            interaction_type: 互動類型（drag, click, system_event等）
        """
        self._last_interaction_time = time.time()
        
        # 如果正在睡眠且允許喚醒，則喚醒
        if self._is_sleeping and self.wake_on_interaction:
            self.wake_up()
        
        debug_log(4, f"[IdleManager] 記錄互動: {interaction_type}")
    
    def check_idle(self) -> bool:
        """
        檢查是否應該進入睡眠
        
        Returns:
            是否應該睡眠
        """
        # 如果未啟用或已經在睡眠，不檢查
        if not self.enabled or self._is_sleeping:
            return False
        
        idle_time = time.time() - self._last_interaction_time
        
        if idle_time >= self.max_idle_time:
            info_log(f"[IdleManager] 檢測到閒置 {idle_time:.0f}s，觸發自動睡眠")
            self.enter_sleep()
            return True
        
        return False
    
    def enter_sleep(self):
        """進入睡眠狀態"""
        if self._is_sleeping:
            return
        
        self._is_sleeping = True
        info_log(f"[IdleManager] 進入睡眠狀態，動畫: {self.sleep_animation}")
        
        # 調用睡眠回調
        if self._sleep_callback:
            self._sleep_callback(self.sleep_animation)
    
    def wake_up(self):
        """從睡眠狀態喚醒"""
        if not self._is_sleeping:
            return
        
        self._is_sleeping = False
        self._last_interaction_time = time.time()
        info_log("[IdleManager] 從睡眠狀態喚醒")
        
        # 調用喚醒回調
        if self._wake_callback:
            self._wake_callback()
    
    def is_sleeping(self) -> bool:
        """檢查是否正在睡眠"""
        return self._is_sleeping
    
    def get_idle_time(self) -> float:
        """獲取當前閒置時間（秒）"""
        return time.time() - self._last_interaction_time
    
    def reload_settings(self):
        """重新載入設定（用於熱重載）"""
        old_enabled = self.enabled
        old_max_idle = self.max_idle_time
        
        self._update_settings()
        
        info_log(f"[IdleManager] 設定已重載: enabled {old_enabled}->{self.enabled}, max_idle_time {old_max_idle}->{self.max_idle_time}")
        
        # 如果禁用了自動睡眠且正在睡眠，喚醒
        if not self.enabled and self._is_sleeping:
            self.wake_up()
