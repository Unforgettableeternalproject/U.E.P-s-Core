# core/states/sleep_manager.py
"""
SLEEP 狀態管理器

負責管理系統休眠狀態，包括：
- 資源釋放（暫停非關鍵服務、清理快取等）
- 喚醒機制（使用者輸入、定時喚醒等）
- 狀態持久化（保存休眠前的上下文）
"""

import time
import threading
from typing import Dict, Any, Optional, List, Callable
from pathlib import Path
import json
from dataclasses import dataclass, asdict, field

from utils.debug_helper import debug_log, info_log, error_log


@dataclass
class SleepContext:
    """休眠上下文 - 儲存休眠前的狀態"""
    sleep_start_time: float
    previous_state: str
    reason: str
    boredom_level: float
    inactive_duration: float
    # 保存未完成的任務（未來擴展）
    pending_tasks: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SleepContext':
        return cls(**data)


class SleepManager:
    """SLEEP 狀態管理器"""
    
    def __init__(self):
        self._is_sleeping = False
        self._sleep_context: Optional[SleepContext] = None
        self._wake_callbacks: List[Callable] = []
        self._monitoring_thread: Optional[threading.Thread] = None
        self._stop_monitoring = threading.Event()
        
        # 休眠配置
        self.config = {
            "min_sleep_duration": 60,  # 最短休眠時間（秒）
            "max_sleep_duration": 3600,  # 最長休眠時間（秒）
            "auto_wake_enabled": False,  # 是否啟用自動喚醒
            "save_context": True,  # 是否保存休眠上下文
        }
        
        # 休眠狀態儲存路徑
        self.storage_path = Path("memory/sleep_context.json")
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        info_log("[SleepManager] SLEEP 狀態管理器已初始化")
    
    def enter_sleep(self, context: Dict[str, Any]) -> bool:
        """
        進入休眠狀態
        
        Args:
            context: 觸發休眠的上下文資訊
            
        Returns:
            bool: 是否成功進入休眠
        """
        if self._is_sleeping:
            debug_log(2, "[SleepManager] 系統已在休眠狀態")
            return False
        
        try:
            info_log("[SleepManager] 🌙 系統準備進入休眠狀態...")
            
            # 創建休眠上下文
            self._sleep_context = SleepContext(
                sleep_start_time=time.time(),
                previous_state=context.get("previous_state", "idle"),
                reason=context.get("trigger_reason", "unknown"),
                boredom_level=context.get("boredom_level", 0.0),
                inactive_duration=context.get("inactive_duration", 0.0)
            )
            
            # 執行資源釋放
            self._release_resources()
            
            # 降低系統活動度
            self._reduce_system_activity()
            
            # 保存休眠上下文
            if self.config["save_context"]:
                self._save_sleep_context()
            
            # 啟動喚醒監控（如果啟用）
            if self.config["auto_wake_enabled"]:
                self._start_wake_monitoring()
            
            self._is_sleeping = True
            
            info_log(f"[SleepManager] ✅ 系統已進入休眠狀態（原因: {self._sleep_context.reason}）")
            
            # 發布休眠事件
            self._publish_sleep_event("SLEEP_ENTERED")
            
            return True
            
        except Exception as e:
            error_log(f"[SleepManager] 進入休眠失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
            return False
    
    def wake_up(self, reason: str = "user_input") -> bool:
        """
        喚醒系統
        
        Args:
            reason: 喚醒原因
            
        Returns:
            bool: 是否成功喚醒
        """
        if not self._is_sleeping:
            debug_log(2, "[SleepManager] 系統未在休眠狀態")
            return False
        
        try:
            info_log(f"[SleepManager] ⏰ 系統喚醒中... (原因: {reason})")
            
            # 停止喚醒監控
            self._stop_wake_monitoring()
            
            # 計算休眠時長
            if self._sleep_context:
                sleep_duration = time.time() - self._sleep_context.sleep_start_time
                info_log(f"[SleepManager] 休眠時長: {sleep_duration:.1f} 秒")
            
            # 恢復系統活動
            self._restore_system_activity()
            
            # 恢復資源
            self._restore_resources()
            
            # 執行喚醒回調
            self._execute_wake_callbacks(reason)
            
            # 清理休眠上下文
            self._clear_sleep_context()
            
            self._is_sleeping = False
            
            info_log("[SleepManager] ✅ 系統已喚醒")
            
            # 發布喚醒事件
            self._publish_sleep_event("SLEEP_EXITED", {"wake_reason": reason})
            
            return True
            
        except Exception as e:
            error_log(f"[SleepManager] 喚醒失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
            return False
    
    def _release_resources(self):
        """釋放資源 - 發布事件通知 Framework 卸載模組
        
        注意：實際的模組卸載由 Framework 響應 SLEEP_ENTERED 事件處理
        這裡只負責發布事件和清理內部狀態
        """
        try:
            debug_log(2, "[SleepManager] 發布資源釋放事件...")
            
            # 發布事件，讓 Framework 處理模組卸載
            # Framework 會響應 SLEEP_ENTERED 事件並卸載非關鍵模組
            pass
            
            debug_log(2, "[SleepManager] ✓ 資源釋放事件已發布")
            
        except Exception as e:
            error_log(f"[SleepManager] 釋放資源失敗: {e}")
    
    def _restore_resources(self):
        """恢復資源 - 發布事件通知 Framework 重載模組
        
        注意：實際的模組重載由前端小工具觸發或系統啟動時檢測
        這裡只負責清理休眠狀態
        """
        try:
            debug_log(2, "[SleepManager] 恢復資源標記...")
            
            # 發布喚醒事件，前端/Framework 會響應並重載模組
            # 實際重載由使用者小工具的喚醒功能或系統重啟觸發
            
            debug_log(2, "[SleepManager] ✓ 資源恢復標記完成")
            
        except Exception as e:
            error_log(f"[SleepManager] 恢復資源失敗: {e}")
    

    

    

    
    def _reduce_system_activity(self):
        """降低系統活動度 - 發布事件通知"""
        try:
            debug_log(2, "[SleepManager] 標記系統進入休眠模式")
            # 實際的模組暫停由 Framework 響應 SLEEP_ENTERED 事件處理
            pass
            
        except Exception as e:
            debug_log(2, f"[SleepManager] 標記休眠模式失敗: {e}")
    
    def _restore_system_activity(self):
        """恢復系統活動度 - 發布事件通知"""
        try:
            debug_log(2, "[SleepManager] 標記系統恢復正常模式")
            # 實際的模組重載由前端小工具觸發或系統啟動時處理
            pass
            
        except Exception as e:
            debug_log(2, f"[SleepManager] 恢復正常模式失敗: {e}")
    
    def _start_wake_monitoring(self):
        """啟動喚醒監控線程"""
        if self._monitoring_thread and self._monitoring_thread.is_alive():
            return
        
        self._stop_monitoring.clear()
        self._monitoring_thread = threading.Thread(
            target=self._wake_monitoring_loop,
            daemon=True,
            name="SleepWakeMonitor"
        )
        self._monitoring_thread.start()
        debug_log(2, "[SleepManager] 喚醒監控已啟動")
    
    def _stop_wake_monitoring(self):
        """停止喚醒監控線程"""
        if not self._monitoring_thread:
            return
        
        self._stop_monitoring.set()
        if self._monitoring_thread.is_alive():
            self._monitoring_thread.join(timeout=2.0)
        debug_log(2, "[SleepManager] 喚醒監控已停止")
    
    def _wake_monitoring_loop(self):
        """喚醒監控循環"""
        debug_log(2, "[SleepManager] 喚醒監控循環開始")
        
        while not self._stop_monitoring.is_set():
            try:
                # 檢查自動喚醒條件
                if self._check_auto_wake_conditions():
                    info_log("[SleepManager] 自動喚醒條件滿足")
                    self.wake_up("auto_wake")
                    break
                
                # 每 5 秒檢查一次
                time.sleep(5.0)
                
            except Exception as e:
                error_log(f"[SleepManager] 喚醒監控錯誤: {e}")
        
        debug_log(2, "[SleepManager] 喚醒監控循環結束")
    
    def _check_auto_wake_conditions(self) -> bool:
        """檢查自動喚醒條件"""
        if not self._sleep_context:
            return False
        
        # 條件1: 超過最長休眠時間
        sleep_duration = time.time() - self._sleep_context.sleep_start_time
        if sleep_duration > self.config["max_sleep_duration"]:
            debug_log(2, f"[SleepManager] 超過最長休眠時間: {sleep_duration:.1f}s")
            return True
        
        # 條件2: 系統事件（TODO: 未來擴展）
        # 例如：有排程任務需要執行
        
        return False
    
    def register_wake_callback(self, callback: Callable[[str], None]):
        """註冊喚醒回調"""
        self._wake_callbacks.append(callback)
        debug_log(3, f"[SleepManager] 註冊喚醒回調: {callback.__name__}")
    
    def _execute_wake_callbacks(self, reason: str):
        """執行所有喚醒回調"""
        for callback in self._wake_callbacks:
            try:
                callback(reason)
            except Exception as e:
                error_log(f"[SleepManager] 執行喚醒回調失敗: {e}")
    
    def _save_sleep_context(self):
        """保存休眠上下文"""
        if not self._sleep_context:
            return
        
        try:
            with open(self.storage_path, 'w', encoding='utf-8') as f:
                json.dump(self._sleep_context.to_dict(), f, ensure_ascii=False, indent=2)
            debug_log(3, f"[SleepManager] 休眠上下文已保存: {self.storage_path}")
        except Exception as e:
            error_log(f"[SleepManager] 保存休眠上下文失敗: {e}")
    
    def _clear_sleep_context(self):
        """清理休眠上下文"""
        self._sleep_context = None
        
        # 刪除保存的文件
        try:
            if self.storage_path.exists():
                self.storage_path.unlink()
                debug_log(3, "[SleepManager] 休眠上下文已清理")
        except Exception as e:
            debug_log(2, f"[SleepManager] 清理休眠上下文失敗: {e}")
    
    def _publish_sleep_event(self, event_type: str, data: Optional[Dict[str, Any]] = None):
        """發布休眠相關事件"""
        try:
            from core.event_bus import event_bus, SystemEvent
            
            # 將字符串轉換為 SystemEvent 枚舉（如果存在）
            event_enum = None
            for evt in SystemEvent:
                if evt.value == event_type.lower():
                    event_enum = evt
                    break
            
            if not event_enum:
                debug_log(2, f"[SleepManager] 未找到事件類型: {event_type}")
                return
            
            event_data = data or {}
            if self._sleep_context:
                event_data["sleep_context"] = self._sleep_context.to_dict()
            
            event_bus.publish(
                event_enum,
                event_data,
                source="sleep_manager"
            )
            
        except Exception as e:
            debug_log(2, f"[SleepManager] 發布休眠事件失敗: {e}")
    
    def is_sleeping(self) -> bool:
        """是否在休眠狀態"""
        return self._is_sleeping
    
    def get_sleep_duration(self) -> Optional[float]:
        """獲取當前休眠時長（秒）"""
        if not self._is_sleeping or not self._sleep_context:
            return None
        return time.time() - self._sleep_context.sleep_start_time
    
    def get_sleep_info(self) -> Dict[str, Any]:
        """獲取休眠狀態資訊"""
        if not self._is_sleeping or not self._sleep_context:
            return {"is_sleeping": False}
        
        return {
            "is_sleeping": True,
            "sleep_duration": self.get_sleep_duration(),
            "reason": self._sleep_context.reason,
            "start_time": self._sleep_context.sleep_start_time,
            "previous_state": self._sleep_context.previous_state
        }


# 全局實例
sleep_manager = SleepManager()
