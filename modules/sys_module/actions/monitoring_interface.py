"""
modules/sys_module/actions/monitoring_interface.py
前端監控接口層

提供統一的 API 供前端查詢待辦事項和行事曆的快照數據。
前端可以在視窗開啟時獲取完整快照，然後通過事件系統接收增量更新。
"""

from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import sqlite3

from modules.sys_module.actions.automation_helper import _DB, local_todo, local_calendar
from modules.sys_module.actions.monitoring_events import (
    get_event_bus,
    MonitoringEventData,
    MonitoringEventType,
    publish_system_startup
)
from utils.debug_helper import info_log, error_log, debug_log


class MonitoringInterface:
    """
    監控接口（單例）
    
    提供前端查詢和管理待辦事項與行事曆的統一接口。
    """
    
    _instance = None
    
    def __new__(cls):
        """單例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """初始化接口"""
        if self._initialized:
            return
        
        self.event_bus = get_event_bus()
        self._initialized = True
        info_log("[MonitoringInterface] 監控接口已初始化")
    
    # ==================== 待辦事項接口 ====================
    
    def get_all_todos(self, include_completed: bool = False) -> List[Dict[str, Any]]:
        """
        獲取所有待辦事項快照
        
        Args:
            include_completed: 是否包含已完成的任務
            
        Returns:
            待辦事項列表，按優先級和截止時間排序
        """
        try:
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            if include_completed:
                query = """
                    SELECT id, task_name, task_description, priority, status, 
                           deadline, created_at, updated_at, completed_at
                    FROM todos
                    ORDER BY 
                        CASE status
                            WHEN 'pending' THEN 1
                            WHEN 'completed' THEN 2
                            ELSE 3
                        END,
                        CASE priority
                            WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2
                            WHEN 'low' THEN 3
                            ELSE 4
                        END,
                        deadline ASC,
                        created_at ASC
                """
            else:
                query = """
                    SELECT id, task_name, task_description, priority, status, 
                           deadline, created_at, updated_at, completed_at
                    FROM todos
                    WHERE status != 'completed'
                    ORDER BY 
                        CASE priority
                            WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2
                            WHEN 'low' THEN 3
                            ELSE 4
                        END,
                        deadline ASC,
                        created_at ASC
                """
            
            c.execute(query)
            
            todos = []
            for row in c.fetchall():
                todos.append({
                    "id": row[0],
                    "task_name": row[1],
                    "task_description": row[2],
                    "priority": row[3],
                    "status": row[4],
                    "deadline": row[5],
                    "created_at": row[6],
                    "updated_at": row[7],
                    "completed_at": row[8]
                })
            
            conn.close()
            debug_log(2, f"[MonitoringInterface] 查詢到 {len(todos)} 個待辦事項")
            return todos
            
        except Exception as e:
            error_log(f"[MonitoringInterface] 查詢待辦事項失敗：{e}")
            return []
    
    def get_todos_by_priority(self, priority: str) -> List[Dict[str, Any]]:
        """
        按優先級獲取待辦事項
        
        Args:
            priority: 優先級（"high", "medium", "low", "none"）
            
        Returns:
            符合條件的待辦事項列表
        """
        try:
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            c.execute("""
                SELECT id, task_name, task_description, priority, status, 
                       deadline, created_at, updated_at
                FROM todos
                WHERE priority = ? AND status != 'completed'
                ORDER BY deadline ASC, created_at ASC
            """, (priority,))
            
            todos = []
            for row in c.fetchall():
                todos.append({
                    "id": row[0],
                    "task_name": row[1],
                    "task_description": row[2],
                    "priority": row[3],
                    "status": row[4],
                    "deadline": row[5],
                    "created_at": row[6],
                    "updated_at": row[7]
                })
            
            conn.close()
            return todos
            
        except Exception as e:
            error_log(f"[MonitoringInterface] 按優先級查詢失敗：{e}")
            return []
    
    def get_expired_todos(self) -> List[Dict[str, Any]]:
        """
        獲取已過期的待辦事項
        
        Returns:
            過期的待辦事項列表
        """
        try:
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            now = datetime.now().isoformat()
            
            c.execute("""
                SELECT id, task_name, task_description, priority, status, 
                       deadline, created_at, updated_at
                FROM todos
                WHERE deadline < ? AND status != 'completed'
                ORDER BY deadline ASC
            """, (now,))
            
            todos = []
            for row in c.fetchall():
                todos.append({
                    "id": row[0],
                    "task_name": row[1],
                    "task_description": row[2],
                    "priority": row[3],
                    "status": row[4],
                    "deadline": row[5],
                    "created_at": row[6],
                    "updated_at": row[7],
                    "is_expired": True
                })
            
            conn.close()
            return todos
            
        except Exception as e:
            error_log(f"[MonitoringInterface] 查詢過期任務失敗：{e}")
            return []
    
    # ==================== 行事曆接口 ====================
    
    def get_all_calendar_events(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        獲取行事曆事件快照
        
        Args:
            start_time: 起始時間（ISO 格式），預設為當前時間
            end_time: 結束時間（ISO 格式），預設為 30 天後
            
        Returns:
            行事曆事件列表，按開始時間排序
        """
        try:
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            # 設定預設時間範圍
            if not start_time:
                start_time = datetime.now().isoformat()
            if not end_time:
                end_dt = datetime.now() + timedelta(days=30)
                end_time = end_dt.isoformat()
            
            c.execute("""
                SELECT id, summary, description, start_time, end_time, location,
                       created_at, updated_at, last_notified_at
                FROM calendar_events
                WHERE start_time >= ? AND start_time <= ?
                ORDER BY start_time ASC
            """, (start_time, end_time))
            
            events = []
            for row in c.fetchall():
                events.append({
                    "id": row[0],
                    "summary": row[1],
                    "description": row[2],
                    "start_time": row[3],
                    "end_time": row[4],
                    "location": row[5],
                    "created_at": row[6],
                    "updated_at": row[7],
                    "last_notified_at": row[8]
                })
            
            conn.close()
            debug_log(2, f"[MonitoringInterface] 查詢到 {len(events)} 個行事曆事件")
            return events
            
        except Exception as e:
            error_log(f"[MonitoringInterface] 查詢行事曆失敗：{e}")
            return []
    
    def get_upcoming_events(self, hours: int = 24) -> List[Dict[str, Any]]:
        """
        獲取即將到來的事件
        
        Args:
            hours: 未來幾小時內的事件
            
        Returns:
            即將到來的事件列表
        """
        try:
            now = datetime.now()
            end_time = (now + timedelta(hours=hours)).isoformat()
            
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            c.execute("""
                SELECT id, summary, description, start_time, end_time, location
                FROM calendar_events
                WHERE start_time >= ? AND start_time <= ?
                ORDER BY start_time ASC
            """, (now.isoformat(), end_time))
            
            events = []
            for row in c.fetchall():
                # 計算距離現在的時間差
                start_dt = datetime.fromisoformat(row[3])
                time_until = (start_dt - now).total_seconds() / 60  # 轉換為分鐘
                
                events.append({
                    "id": row[0],
                    "summary": row[1],
                    "description": row[2],
                    "start_time": row[3],
                    "end_time": row[4],
                    "location": row[5],
                    "minutes_until": int(time_until)
                })
            
            conn.close()
            return events
            
        except Exception as e:
            error_log(f"[MonitoringInterface] 查詢即將到來的事件失敗：{e}")
            return []
    
    # ==================== 統一快照接口 ====================
    
    def get_monitoring_snapshot(self) -> Dict[str, Any]:
        """
        獲取完整的監控快照（用於視窗開啟時）
        
        Returns:
            包含待辦事項和行事曆的完整快照：
            {
                "todos": {
                    "all": [...],
                    "by_priority": {"high": [...], "medium": [...], "low": [...], "none": [...]},
                    "expired": [...]
                },
                "calendar": {
                    "upcoming_24h": [...],
                    "all": [...]
                },
                "timestamp": "2025-12-01T12:00:00"
            }
        """
        try:
            snapshot = {
                "todos": {
                    "all": self.get_all_todos(include_completed=False),
                    "by_priority": {
                        "high": self.get_todos_by_priority("high"),
                        "medium": self.get_todos_by_priority("medium"),
                        "low": self.get_todos_by_priority("low"),
                        "none": self.get_todos_by_priority("none")
                    },
                    "expired": self.get_expired_todos()
                },
                "calendar": {
                    "upcoming_24h": self.get_upcoming_events(hours=24),
                    "all": self.get_all_calendar_events()
                },
                "timestamp": datetime.now().isoformat()
            }
            
            info_log(
                f"[MonitoringInterface] 生成監控快照："
                f"{len(snapshot['todos']['all'])} 個待辦，"
                f"{len(snapshot['calendar']['all'])} 個事件"
            )
            
            return snapshot
            
        except Exception as e:
            error_log(f"[MonitoringInterface] 生成快照失敗：{e}")
            return {
                "todos": {"all": [], "by_priority": {}, "expired": []},
                "calendar": {"upcoming_24h": [], "all": []},
                "timestamp": datetime.now().isoformat(),
                "error": str(e)
            }
    
    # ==================== 事件訂閱便捷方法 ====================
    
    def subscribe_todo_events(self, callback):
        """訂閱待辦事項事件"""
        self.event_bus.subscribe("todo", callback)
    
    def subscribe_calendar_events(self, callback):
        """訂閱行事曆事件"""
        self.event_bus.subscribe("calendar", callback)
    
    def subscribe_all_events(self, callback):
        """訂閱所有監控事件"""
        self.event_bus.subscribe("all", callback)
    
    def unsubscribe_events(self, category: str, callback):
        """取消訂閱事件"""
        self.event_bus.unsubscribe(category, callback)


# ==================== 全域接口實例 ====================

_monitoring_interface = None


def get_monitoring_interface() -> MonitoringInterface:
    """獲取監控接口實例（單例）"""
    global _monitoring_interface
    if _monitoring_interface is None:
        _monitoring_interface = MonitoringInterface()
    return _monitoring_interface


# ==================== 系統啟動時初始化 ====================

def initialize_monitoring_system():
    """
    初始化監控系統（在系統啟動時調用）
    
    - 初始化事件總線
    - 初始化監控接口
    - 發布系統啟動事件
    """
    try:
        # 初始化接口
        interface = get_monitoring_interface()
        
        # 發布系統啟動事件（觸發前端載入快照）
        publish_system_startup()
        
        info_log("[MonitoringInterface] 監控系統初始化完成")
        return True
        
    except Exception as e:
        error_log(f"[MonitoringInterface] 監控系統初始化失敗：{e}")
        return False
