#!/usr/bin/env python3
"""
core/session_record.py
會話記錄管理系統 - 追蹤所有 GS/CS/WS 的觸發歷史和狀態
"""

import json
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from utils.debug_helper import debug_log, info_log, error_log

class SessionType(Enum):
    """會話類型"""
    GS = "general_session"      # General Session
    CS = "chatting_session"     # Chatting Session
    WS = "workflow_session"     # Workflow Session

class SessionStatus(Enum):
    """會話狀態"""
    TRIGGERED = "triggered"     # 會話已觸發
    ACTIVE = "active"          # 會話進行中
    PAUSED = "paused"          # 會話暫停
    COMPLETED = "completed"    # 會話完成
    TERMINATED = "terminated"  # 會話終止
    ERROR = "error"           # 會話錯誤

@dataclass
class SessionRecord:
    """會話記錄"""
    record_id: str
    session_type: SessionType
    session_id: str
    status: SessionStatus
    
    # 觸發信息
    trigger_content: str
    context_content: str
    trigger_user: Optional[str]
    triggered_at: datetime
    
    # 狀態變更
    status_history: List[Dict[str, Any]]
    
    # 會話數據
    metadata: Dict[str, Any]
    session_summary: Optional[Dict[str, Any]]
    
    # 時間戳
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]
    
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典"""
        return {
            "record_id": self.record_id,
            "session_type": self.session_type.value,
            "session_id": self.session_id,
            "status": self.status.value,
            "trigger_content": self.trigger_content,
            "context_content": self.context_content,
            "trigger_user": self.trigger_user,
            "triggered_at": self.triggered_at.isoformat(),
            "status_history": self.status_history,
            "metadata": self.metadata,
            "session_summary": self.session_summary,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionRecord':
        """從字典創建"""
        return cls(
            record_id=data["record_id"],
            session_type=SessionType(data["session_type"]),
            session_id=data["session_id"],
            status=SessionStatus(data["status"]),
            trigger_content=data["trigger_content"],
            context_content=data["context_content"],
            trigger_user=data.get("trigger_user"),
            triggered_at=datetime.fromisoformat(data["triggered_at"]),
            status_history=data.get("status_history", []),
            metadata=data.get("metadata", {}),
            session_summary=data.get("session_summary"),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None
        )
    
    def update_status(self, new_status: SessionStatus, details: Optional[Dict[str, Any]] = None):
        """更新會話狀態"""
        old_status = self.status
        self.status = new_status
        self.updated_at = datetime.now()
        
        # 記錄狀態變更歷史
        status_change = {
            "from_status": old_status.value,
            "to_status": new_status.value,
            "timestamp": self.updated_at.isoformat(),
            "details": details or {}
        }
        self.status_history.append(status_change)
        
        # 如果會話完成，設置完成時間
        if new_status in [SessionStatus.COMPLETED, SessionStatus.TERMINATED, SessionStatus.ERROR]:
            self.completed_at = self.updated_at

class SessionRecordManager:
    """會話記錄管理器"""
    
    def __init__(self, storage_path: Optional[Path] = None):
        """初始化會話記錄管理器"""
        self.storage_path = storage_path or Path("memory/session_records.json")
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 會話記錄 - 按記錄ID索引
        self.records: Dict[str, SessionRecord] = {}
        
        # 會話索引 - 按會話ID快速查找
        self.session_index: Dict[str, str] = {}  # session_id -> record_id
        
        # 類型索引 - 按會話類型分組
        self.type_index: Dict[SessionType, List[str]] = {
            SessionType.GS: [],
            SessionType.CS: [],
            SessionType.WS: []
        }
        
        # 載入現有記錄
        self._load_records()
        
        info_log("[SessionRecord] 會話記錄管理器初始化完成")
    
    def record_session_trigger(self, 
                             session_type: str,
                             session_id: str,
                             trigger_content: str,
                             context_content: str,
                             trigger_user: Optional[str] = None,
                             metadata: Optional[Dict[str, Any]] = None) -> SessionRecord:
        """記錄會話觸發"""
        
        # 轉換會話類型
        if isinstance(session_type, str):
            # 標準化會話類型名稱
            type_mapping = {
                "CS": SessionType.CS,
                "cs": SessionType.CS,
                "chatting_session": SessionType.CS,
                "WS": SessionType.WS, 
                "ws": SessionType.WS,
                "workflow_session": SessionType.WS,
                "GS": SessionType.GS,
                "gs": SessionType.GS,
                "general_session": SessionType.GS
            }
            session_type_enum = type_mapping.get(session_type)
            if not session_type_enum:
                raise ValueError(f"未知的會話類型: {session_type}")
        else:
            session_type_enum = session_type
        
        # 生成記錄ID
        record_id = f"sr_{session_type_enum.value[:2]}_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:8]}"
        
        # 創建會話記錄
        now = datetime.now()
        record = SessionRecord(
            record_id=record_id,
            session_type=session_type_enum,
            session_id=session_id,
            status=SessionStatus.TRIGGERED,
            trigger_content=trigger_content,
            context_content=context_content,
            trigger_user=trigger_user,
            triggered_at=now,
            status_history=[{
                "from_status": None,
                "to_status": SessionStatus.TRIGGERED.value,
                "timestamp": now.isoformat(),
                "details": {"initial_trigger": True}
            }],
            metadata=metadata or {},
            session_summary=None,
            created_at=now,
            updated_at=now,
            completed_at=None
        )
        
        # 存儲記錄
        self.records[record_id] = record
        self.session_index[session_id] = record_id
        self.type_index[session_type_enum].append(record_id)
        
        # 保存到檔案
        self._save_records()
        
        info_log(f"[SessionRecord] 記錄會話觸發: {session_type_enum.value} - {session_id}")
        debug_log(4, f"[SessionRecord] 觸發內容: {trigger_content[:50]}...")
        
        return record
    
    def update_session_status(self, 
                            session_id: str, 
                            new_status: SessionStatus,
                            details: Optional[Dict[str, Any]] = None) -> bool:
        """更新會話狀態"""
        record_id = self.session_index.get(session_id)
        if not record_id:
            error_log(f"[SessionRecord] 找不到會話記錄: {session_id}")
            return False
        
        record = self.records.get(record_id)
        if not record:
            error_log(f"[SessionRecord] 記錄不存在: {record_id}")
            return False
        
        old_status = record.status
        record.update_status(new_status, details)
        
        # 保存到檔案
        self._save_records()
        
        info_log(f"[SessionRecord] 更新會話狀態: {session_id} - {old_status.value} -> {new_status.value}")
        if details:
            debug_log(4, f"[SessionRecord] 狀態詳情: {details}")
        
        return True
    
    def record_session_completion(self,
                                session_id: str,
                                session_summary: Dict[str, Any],
                                success: bool = True) -> bool:
        """記錄會話完成"""
        
        # 確定完成狀態
        completion_status = SessionStatus.COMPLETED if success else SessionStatus.ERROR
        
        # 更新狀態
        record_id = self.session_index.get(session_id)
        if not record_id:
            error_log(f"[SessionRecord] 找不到會話記錄用於完成: {session_id}")
            return False
        
        record = self.records.get(record_id)
        if not record:
            error_log(f"[SessionRecord] 記錄不存在用於完成: {record_id}")
            return False
        
        # 更新會話摘要和狀態
        record.session_summary = session_summary
        record.update_status(completion_status, {
            "completion_success": success,
            "session_summary_keys": list(session_summary.keys()) if session_summary else []
        })
        
        # 保存到檔案
        self._save_records()
        
        info_log(f"[SessionRecord] 記錄會話完成: {session_id} - {'成功' if success else '失敗'}")
        debug_log(4, f"[SessionRecord] 會話摘要: {session_summary}")
        
        return True
    
    def get_session_record(self, session_id: str) -> Optional[SessionRecord]:
        """獲取會話記錄"""
        record_id = self.session_index.get(session_id)
        if record_id:
            return self.records.get(record_id)
        return None
    
    def get_records_by_type(self, session_type: SessionType) -> List[SessionRecord]:
        """按類型獲取會話記錄"""
        record_ids = self.type_index.get(session_type, [])
        return [self.records[record_id] for record_id in record_ids if record_id in self.records]
    
    def get_active_sessions(self) -> List[SessionRecord]:
        """獲取所有活動會話"""
        active_statuses = {SessionStatus.TRIGGERED, SessionStatus.ACTIVE, SessionStatus.PAUSED}
        return [record for record in self.records.values() if record.status in active_statuses]
    
    def get_recent_records(self, limit: int = 50) -> List[SessionRecord]:
        """獲取最近的會話記錄"""
        sorted_records = sorted(self.records.values(), key=lambda r: r.created_at, reverse=True)
        return sorted_records[:limit]
    
    def cleanup_old_records(self, keep_days: int = 30):
        """清理舊記錄"""
        cutoff_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - \
                     datetime.timedelta(days=keep_days)
        
        records_to_remove = []
        for record_id, record in self.records.items():
            if record.created_at < cutoff_date and record.status in [SessionStatus.COMPLETED, SessionStatus.ERROR, SessionStatus.TERMINATED]:
                records_to_remove.append(record_id)
        
        # 移除舊記錄
        for record_id in records_to_remove:
            record = self.records[record_id]
            
            # 從索引中移除
            if record.session_id in self.session_index:
                del self.session_index[record.session_id]
            
            if record_id in self.type_index[record.session_type]:
                self.type_index[record.session_type].remove(record_id)
            
            # 從主記錄中移除
            del self.records[record_id]
        
        if records_to_remove:
            self._save_records()
            info_log(f"[SessionRecord] 清理 {len(records_to_remove)} 個舊記錄")
        
        return len(records_to_remove)
    
    def get_statistics(self) -> Dict[str, Any]:
        """獲取統計信息"""
        total_records = len(self.records)
        
        # 按類型統計
        type_stats = {}
        for session_type in SessionType:
            type_records = self.get_records_by_type(session_type)
            type_stats[session_type.value] = {
                "total": len(type_records),
                "active": len([r for r in type_records if r.status in [SessionStatus.TRIGGERED, SessionStatus.ACTIVE, SessionStatus.PAUSED]]),
                "completed": len([r for r in type_records if r.status == SessionStatus.COMPLETED]),
                "error": len([r for r in type_records if r.status == SessionStatus.ERROR])
            }
        
        # 按狀態統計
        status_stats = {}
        for status in SessionStatus:
            status_stats[status.value] = len([r for r in self.records.values() if r.status == status])
        
        # 時間統計
        now = datetime.now()
        today_records = [r for r in self.records.values() if r.created_at.date() == now.date()]
        week_records = [r for r in self.records.values() if (now - r.created_at).days <= 7]
        
        return {
            "total_records": total_records,
            "type_statistics": type_stats,
            "status_statistics": status_stats,
            "time_statistics": {
                "today": len(today_records),
                "this_week": len(week_records),
                "active_sessions": len(self.get_active_sessions())
            },
            "last_updated": datetime.now().isoformat()
        }
    
    def _save_records(self):
        """保存記錄到檔案"""
        try:
            data = {
                "records": {record_id: record.to_dict() for record_id, record in self.records.items()},
                "session_index": self.session_index,
                "type_index": {session_type.value: record_ids for session_type, record_ids in self.type_index.items()},
                "metadata": {
                    "total_records": len(self.records),
                    "last_saved": datetime.now().isoformat(),
                    "version": "1.0"
                }
            }
            
            with open(self.storage_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                
            debug_log(4, f"[SessionRecord] 保存 {len(self.records)} 個會話記錄")
                
        except Exception as e:
            error_log(f"[SessionRecord] 保存記錄失敗: {e}")
    
    def _load_records(self):
        """從檔案載入記錄"""
        try:
            if self.storage_path.exists():
                with open(self.storage_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 載入記錄
                records_data = data.get("records", {})
                for record_id, record_data in records_data.items():
                    try:
                        self.records[record_id] = SessionRecord.from_dict(record_data)
                    except Exception as e:
                        error_log(f"[SessionRecord] 載入記錄失敗 {record_id}: {e}")
                
                # 載入索引
                self.session_index = data.get("session_index", {})
                
                # 載入類型索引
                type_index_data = data.get("type_index", {})
                for type_name, record_ids in type_index_data.items():
                    try:
                        session_type = SessionType(type_name)
                        self.type_index[session_type] = record_ids
                    except ValueError:
                        debug_log(2, f"[SessionRecord] 未知會話類型: {type_name}")
                
                info_log(f"[SessionRecord] 載入 {len(self.records)} 個會話記錄")
                
        except Exception as e:
            error_log(f"[SessionRecord] 載入記錄失敗: {e}")
            # 使用空記錄開始
            self.records = {}
            self.session_index = {}
            self.type_index = {session_type: [] for session_type in SessionType}


# 全域會話記錄管理器實例
_session_record_manager = None

def get_session_record_manager() -> SessionRecordManager:
    """獲取全域會話記錄管理器實例"""
    global _session_record_manager
    if _session_record_manager is None:
        _session_record_manager = SessionRecordManager()
    return _session_record_manager