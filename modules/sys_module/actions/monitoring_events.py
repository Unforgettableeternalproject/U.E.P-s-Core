"""
modules/sys_module/actions/monitoring_events.py
監控任務事件系統

提供事件驅動的接口，讓前端可以訂閱背景任務的關鍵事件：
- 系統啟動時載入快照
- 新增項目
- 編輯項目
- 刪除項目
- 項目自動移除（過期、完成等）

前端只需要在關鍵事件時更新，平時可以自行模擬時間追蹤。
"""

from typing import Dict, List, Any, Callable, Optional
from datetime import datetime
import threading
from enum import Enum

from utils.debug_helper import info_log, debug_log, error_log


class MonitoringEventType(Enum):
    """監控事件類型"""
    SYSTEM_STARTUP = "system_startup"  # 系統啟動，載入所有快照
    ITEM_ADDED = "item_added"  # 新增項目
    ITEM_UPDATED = "item_updated"  # 更新項目
    ITEM_DELETED = "item_deleted"  # 刪除項目
    ITEM_COMPLETED = "item_completed"  # 項目完成（待辦事項）
    ITEM_EXPIRED = "item_expired"  # 項目過期（自動移除）
    ITEM_NOTIFIED = "item_notified"  # 項目已通知（行事曆提醒）


class MonitoringEventData:
    """監控事件數據"""
    
    def __init__(
        self,
        event_type: MonitoringEventType,
        category: str,  # "todo" 或 "calendar"
        item_id: int,
        item_data: Optional[Dict[str, Any]] = None,
        timestamp: Optional[datetime] = None
    ):
        self.event_type = event_type
        self.category = category
        self.item_id = item_id
        self.item_data = item_data or {}
        self.timestamp = timestamp or datetime.now()
    
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式（供前端使用）"""
        return {
            "event_type": self.event_type.value,
            "category": self.category,
            "item_id": self.item_id,
            "item_data": self.item_data,
            "timestamp": self.timestamp.isoformat()
        }


class MonitoringEventBus:
    """
    監控事件總線（單例）
    
    負責管理事件訂閱和分發，前端通過註冊回調函數來接收事件。
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """單例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """初始化事件總線"""
        if self._initialized:
            return
        
        # 事件訂閱者：category -> list of callbacks
        self.subscribers: Dict[str, List[Callable]] = {
            "todo": [],
            "calendar": [],
            "all": []  # 訂閱所有事件
        }
        
        self._initialized = True
        info_log("[MonitoringEventBus] 事件總線已初始化")
    
    def subscribe(
        self,
        category: str,
        callback: Callable[[MonitoringEventData], None]
    ) -> None:
        """
        訂閱事件
        
        Args:
            category: 分類（"todo", "calendar", "all"）
            callback: 回調函數，接收 MonitoringEventData 參數
        """
        if category not in self.subscribers:
            self.subscribers[category] = []
        
        if callback not in self.subscribers[category]:
            self.subscribers[category].append(callback)
            debug_log(2, f"[MonitoringEventBus] 已訂閱 {category} 事件")
    
    def unsubscribe(
        self,
        category: str,
        callback: Callable[[MonitoringEventData], None]
    ) -> None:
        """取消訂閱事件"""
        if category in self.subscribers and callback in self.subscribers[category]:
            self.subscribers[category].remove(callback)
            debug_log(2, f"[MonitoringEventBus] 已取消訂閱 {category} 事件")
    
    def publish(self, event: MonitoringEventData) -> None:
        """
        發布事件
        
        Args:
            event: 事件數據
        """
        try:
            # 發送給特定分類的訂閱者
            category_subscribers = self.subscribers.get(event.category, [])
            for callback in category_subscribers:
                try:
                    callback(event)
                except Exception as e:
                    error_log(f"[MonitoringEventBus] 回調執行失敗: {e}")
            
            # 發送給 "all" 訂閱者
            all_subscribers = self.subscribers.get("all", [])
            for callback in all_subscribers:
                try:
                    callback(event)
                except Exception as e:
                    error_log(f"[MonitoringEventBus] 回調執行失敗: {e}")
            
            debug_log(
                2,
                f"[MonitoringEventBus] 已發布事件: {event.event_type.value} "
                f"({event.category}, ID={event.item_id})"
            )
            
        except Exception as e:
            error_log(f"[MonitoringEventBus] 發布事件失敗: {e}")
    
    def clear_subscribers(self, category: Optional[str] = None) -> None:
        """清除訂閱者"""
        if category:
            self.subscribers[category] = []
        else:
            for key in self.subscribers:
                self.subscribers[key] = []
        
        debug_log(2, f"[MonitoringEventBus] 已清除訂閱者: {category or 'all'}")


# 全域事件總線實例
_event_bus = None


def get_event_bus() -> MonitoringEventBus:
    """獲取全域事件總線實例（單例）"""
    global _event_bus
    if _event_bus is None:
        _event_bus = MonitoringEventBus()
    return _event_bus


# ==================== 便捷函數 ====================

def publish_todo_event(
    event_type: MonitoringEventType,
    item_id: int,
    item_data: Optional[Dict[str, Any]] = None
) -> None:
    """發布待辦事項事件"""
    event = MonitoringEventData(
        event_type=event_type,
        category="todo",
        item_id=item_id,
        item_data=item_data
    )
    get_event_bus().publish(event)


def publish_calendar_event(
    event_type: MonitoringEventType,
    item_id: int,
    item_data: Optional[Dict[str, Any]] = None
) -> None:
    """發布行事曆事件"""
    event = MonitoringEventData(
        event_type=event_type,
        category="calendar",
        item_id=item_id,
        item_data=item_data
    )
    get_event_bus().publish(event)


def publish_system_startup() -> None:
    """發布系統啟動事件（觸發快照載入）"""
    # 發送待辦事項啟動事件
    todo_event = MonitoringEventData(
        event_type=MonitoringEventType.SYSTEM_STARTUP,
        category="todo",
        item_id=0,  # 啟動事件沒有特定 ID
        item_data={}
    )
    get_event_bus().publish(todo_event)
    
    # 發送行事曆啟動事件
    calendar_event = MonitoringEventData(
        event_type=MonitoringEventType.SYSTEM_STARTUP,
        category="calendar",
        item_id=0,
        item_data={}
    )
    get_event_bus().publish(calendar_event)
    
    info_log("[MonitoringEventBus] 已發布系統啟動事件")
