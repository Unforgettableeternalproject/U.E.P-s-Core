"""
core/sessions/session_manager.py
統一會話管理器 - 重構版本

作為三種會話管理器的統一介面：
1. GeneralSessionManager (GS) - 基礎會話管理
2. ChattingSessionManager (CS) - 對話會話管理  
3. WorkflowSessionManager (WS) - 工作流會話管理

提供SessionRecord功能來追蹤歷史會話記錄
"""

import uuid
import time
from enum import Enum
from typing import Dict, Any, Optional, List, Union
from datetime import datetime
from dataclasses import dataclass, field

from utils.debug_helper import debug_log, info_log, error_log

# 延遲導入避免循環依賴
def _import_session_managers():
    from .general_session import general_session_manager, GeneralSession, GSType
    from .chatting_session import chatting_session_manager, ChattingSession
    from .workflow_session import workflow_session_manager, WorkflowSession, WSTaskType
    return {
        'gs_manager': general_session_manager,
        'cs_manager': chatting_session_manager, 
        'ws_manager': workflow_session_manager,
        'GSType': GSType,
        'WSTaskType': WSTaskType,
        'GeneralSession': GeneralSession,
        'ChattingSession': ChattingSession,
        'WorkflowSession': WorkflowSession
    }


class SessionType(Enum):
    """會話類型"""
    GENERAL = "general"           # General Session (GS)
    CHATTING = "chatting"         # Chatting Session (CS)
    WORKFLOW = "workflow"         # Workflow Session (WS)


class SessionRecordStatus(Enum):
    """會話記錄狀態"""
    TRIGGERED = "triggered"       # 已觸發
    ACTIVE = "active"            # 進行中
    COMPLETED = "completed"      # 已完成
    FAILED = "failed"           # 失敗
    CANCELLED = "cancelled"     # 已取消
    EXPIRED = "expired"         # 已過期


@dataclass
class SessionRecord:
    """會話記錄 - 用於追蹤歷史會話"""
    record_id: str
    session_type: SessionType
    session_id: str
    status: SessionRecordStatus
    
    # 觸發信息
    trigger_content: str
    context_content: str
    trigger_user: Optional[str]
    triggered_at: datetime
    
    # 狀態變更歷史
    status_history: List[Dict[str, Any]] = field(default_factory=list)
    
    # 會話數據
    metadata: Dict[str, Any] = field(default_factory=dict)
    session_summary: Optional[Dict[str, Any]] = None
    
    def update_status(self, new_status: SessionRecordStatus, details: Optional[Dict[str, Any]] = None):
        """更新會話記錄狀態"""
        old_status = self.status
        self.status = new_status
        
        status_change = {
            "timestamp": datetime.now().isoformat(),
            "from_status": old_status.value,
            "to_status": new_status.value,
            "details": details or {}
        }
        self.status_history.append(status_change)
        
        debug_log(3, f"[SessionRecord] 狀態變更: {self.record_id} {old_status.value} -> {new_status.value}")
    
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式"""
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
            "session_summary": self.session_summary
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionRecord':
        """從字典創建會話記錄"""
        return cls(
            record_id=data["record_id"],
            session_type=SessionType(data["session_type"]),
            session_id=data["session_id"],
            status=SessionRecordStatus(data["status"]),
            trigger_content=data["trigger_content"],
            context_content=data["context_content"],
            trigger_user=data.get("trigger_user"),
            triggered_at=datetime.fromisoformat(data["triggered_at"]),
            status_history=data.get("status_history", []),
            metadata=data.get("metadata", {}),
            session_summary=data.get("session_summary")
        )

class UnifiedSessionManager:
    """
    統一會話管理器 - 作為三種會話管理器的統一介面
    
    管理功能：
    1. General Session (GS) - 基礎會話生命週期
    2. Chatting Session (CS) - 對話會話管理
    3. Workflow Session (WS) - 工作流會話管理
    4. SessionRecord - 歷史會話記錄追蹤
    """
    
    def __init__(self):
        # 延遲初始化會話管理器
        self._managers = None
        
        # 會話記錄管理
        self.session_records: List[SessionRecord] = []
        self.max_records = 1000
        
        # 配置
        self.config = {
            "auto_cleanup_interval": 3600,  # 1小時清理一次
            "max_session_age": 86400,       # 24小時過期
            "enable_session_recording": True
        }
        
        info_log("[UnifiedSessionManager] 統一會話管理器初始化完成")
    
    @property
    def managers(self):
        """延遲載入會話管理器"""
        if self._managers is None:
            self._managers = _import_session_managers()
        return self._managers
    
    # ==================== General Session 管理 ====================
    
    def start_general_session(self, gs_type: str, trigger_event: Dict[str, Any]) -> Optional[str]:
        """啟動 General Session"""
        try:
            GSType = self.managers['GSType']
            gs_manager = self.managers['gs_manager']
            
            # 轉換字串類型為枚舉
            if isinstance(gs_type, str):
                gs_type_enum = GSType(gs_type)
            else:
                gs_type_enum = gs_type
            
            session_id = gs_manager.start_session(gs_type_enum, trigger_event)
            if session_id:
                self._create_session_record(
                    session_id, 
                    SessionType.GENERAL,
                    trigger_event.get("content", ""),
                    str(trigger_event)
                )
                info_log(f"[UnifiedSessionManager] 已啟動 GS: {session_id}")
                return session_id
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 啟動 GS 失敗: {e}")
        return None
    
    def end_general_session(self, final_output: Optional[Dict[str, Any]] = None) -> bool:
        """結束當前 General Session"""
        try:
            gs_manager = self.managers['gs_manager']
            current_session = gs_manager.get_current_session()
            
            if current_session:
                session_id = current_session.session_id
                result = gs_manager.end_current_session(final_output)
                
                if result:
                    self._update_session_record(session_id, SessionRecordStatus.COMPLETED, final_output)
                    info_log(f"[UnifiedSessionManager] 已結束 GS: {session_id}")
                return result
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 結束 GS 失敗: {e}")
        return False
    
    def get_current_general_session(self) -> Optional[Any]:
        """獲取當前 General Session"""
        try:
            return self.managers['gs_manager'].get_current_session()
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 獲取當前 GS 失敗: {e}")
        return None
    
    # ==================== Chatting Session 管理 ====================
    
    def create_chatting_session(self, gs_session_id: str, 
                               identity_context: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """創建 Chatting Session - 必須依附於現有的 GS"""
        try:
            # 架構檢查：確保 GS 存在
            current_gs = self.get_current_general_session()
            if not current_gs:
                error_msg = "[UnifiedSessionManager] 架構錯誤：嘗試創建 CS 但沒有活躍的 GS！"
                error_log(error_msg)
                raise RuntimeError("會話架構錯誤：CS 必須依附於現有的 GS")
            
            # 確保提供的 gs_session_id 與當前 GS 匹配
            if current_gs.session_id != gs_session_id:
                error_msg = f"[UnifiedSessionManager] GS ID 不匹配：當前 {current_gs.session_id}，要求 {gs_session_id}"
                error_log(error_msg)
                raise ValueError("提供的 GS ID 與當前活躍的 GS 不匹配")
            
            cs_manager = self.managers['cs_manager']
            session_id = cs_manager.create_session(gs_session_id, identity_context)
            
            if session_id:
                self._create_session_record(
                    session_id,
                    SessionType.CHATTING,
                    f"對話會話 (GS: {gs_session_id})",
                    str(identity_context)
                )
                info_log(f"[UnifiedSessionManager] 已創建 CS: {session_id}")
                return session_id
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 創建 CS 失敗: {e}")
            raise  # 重新拋出錯誤，因為這是架構問題
        return None
    
    def end_chatting_session(self, session_id: str, save_memory: bool = True) -> bool:
        """結束 Chatting Session"""
        try:
            cs_manager = self.managers['cs_manager']
            result = cs_manager.end_session(session_id, save_memory)
            
            if result:
                self._update_session_record(session_id, SessionRecordStatus.COMPLETED, 
                                          {"save_memory": save_memory})
                info_log(f"[UnifiedSessionManager] 已結束 CS: {session_id}")
            return result
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 結束 CS 失敗: {e}")
        return False
    
    def get_chatting_session(self, session_id: str) -> Optional[Any]:
        """獲取 Chatting Session"""
        try:
            return self.managers['cs_manager'].get_session(session_id)
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 獲取 CS 失敗: {e}")
        return None
    
    def get_active_chatting_sessions(self) -> List[Any]:
        """獲取所有活躍的 Chatting Session"""
        try:
            return self.managers['cs_manager'].get_active_sessions()
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 獲取活躍 CS 失敗: {e}")
        return []
    
    # ==================== Workflow Session 管理 ====================
    
    def create_workflow_session(self, gs_session_id: str, task_type: str, 
                               task_definition: Dict[str, Any]) -> Optional[str]:
        """創建 Workflow Session - 必須依附於現有的 GS"""
        try:
            # 架構檢查：確保 GS 存在
            current_gs = self.get_current_general_session()
            if not current_gs:
                error_msg = "[UnifiedSessionManager] 架構錯誤：嘗試創建 WS 但沒有活躍的 GS！"
                error_log(error_msg)
                raise RuntimeError("會話架構錯誤：WS 必須依附於現有的 GS")
            
            # 確保提供的 gs_session_id 與當前 GS 匹配
            if current_gs.session_id != gs_session_id:
                error_msg = f"[UnifiedSessionManager] GS ID 不匹配：當前 {current_gs.session_id}，要求 {gs_session_id}"
                error_log(error_msg)
                raise ValueError("提供的 GS ID 與當前活躍的 GS 不匹配")
            
            WSTaskType = self.managers['WSTaskType']
            ws_manager = self.managers['ws_manager']
            
            # 轉換字串類型為枚舉
            if isinstance(task_type, str):
                task_type_enum = WSTaskType(task_type)
            else:
                task_type_enum = task_type
            
            session_id = ws_manager.create_session(gs_session_id, task_type_enum, task_definition)
            
            if session_id:
                self._create_session_record(
                    session_id,
                    SessionType.WORKFLOW,
                    f"工作流會話: {task_definition.get('command', 'unknown')}",
                    str(task_definition)
                )
                info_log(f"[UnifiedSessionManager] 已創建 WS: {session_id}")
                return session_id
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 創建 WS 失敗: {e}")
            raise  # 重新拋出錯誤，因為這是架構問題
        return None
    
    def end_workflow_session(self, session_id: str) -> bool:
        """結束 Workflow Session"""
        try:
            ws_manager = self.managers['ws_manager']
            result = ws_manager.end_session(session_id)
            
            if result:
                self._update_session_record(session_id, SessionRecordStatus.COMPLETED)
                info_log(f"[UnifiedSessionManager] 已結束 WS: {session_id}")
            return result
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 結束 WS 失敗: {e}")
        return False
    
    def get_workflow_session(self, session_id: str) -> Optional[Any]:
        """獲取 Workflow Session"""
        try:
            return self.managers['ws_manager'].get_session(session_id)
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 獲取 WS 失敗: {e}")
        return None
    
    def get_active_workflow_sessions(self) -> List[Any]:
        """獲取所有活躍的 Workflow Session"""
        try:
            return self.managers['ws_manager'].get_active_sessions()
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 獲取活躍 WS 失敗: {e}")
        return []
    
    # ==================== 通用會話管理 ====================
    
    def get_current_general_session_id(self) -> Optional[str]:
        """獲取當前 General Session 的 ID"""
        try:
            current_gs = self.get_current_general_session()
            if current_gs and hasattr(current_gs, 'session_id'):
                return current_gs.session_id
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 獲取當前 GS ID 失敗: {e}")
        return None
    
    def get_active_chatting_session_ids(self) -> List[str]:
        """獲取所有活躍 Chatting Session 的 ID 列表"""
        try:
            active_sessions = self.get_active_chatting_sessions()
            return [session.session_id for session in active_sessions if hasattr(session, 'session_id')]
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 獲取活躍 CS ID 失敗: {e}")
        return []
    
    def get_active_workflow_session_ids(self) -> List[str]:
        """獲取所有活躍 Workflow Session 的 ID 列表"""
        try:
            active_sessions = self.get_active_workflow_sessions()
            return [session.session_id for session in active_sessions if hasattr(session, 'session_id')]
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 獲取活躍 WS ID 失敗: {e}")
        return []
    
    def get_all_active_session_ids(self) -> Dict[str, Any]:
        """獲取所有類型活躍會話的 ID"""
        return {
            "general_session_id": self.get_current_general_session_id(),
            "chatting_session_ids": self.get_active_chatting_session_ids(),
            "workflow_session_ids": self.get_active_workflow_session_ids(),
            "total_active_sessions": (
                (1 if self.get_current_general_session_id() else 0) +
                len(self.get_active_chatting_session_ids()) +
                len(self.get_active_workflow_session_ids())
            )
        }
    
    def get_all_active_sessions(self) -> Dict[str, List[Any]]:
        """獲取所有類型的活躍會話"""
        return {
            "general": [self.get_current_general_session()] if self.get_current_general_session() else [],
            "chatting": self.get_active_chatting_sessions(),
            "workflow": self.get_active_workflow_sessions()
        }
    
    def get_primary_session_id(self) -> Optional[str]:
        """
        獲取主要的當前 session ID
        優先順序：General Session -> 最新的 Chatting Session -> 最新的 Workflow Session
        """
        # 優先返回 General Session ID
        gs_id = self.get_current_general_session_id()
        if gs_id:
            return gs_id
        
        # 如果沒有 GS，嘗試獲取最新的 CS
        cs_ids = self.get_active_chatting_session_ids()
        if cs_ids:
            return cs_ids[-1]  # 返回最新的
        
        # 最後嘗試 WS
        ws_ids = self.get_active_workflow_session_ids()
        if ws_ids:
            return ws_ids[-1]  # 返回最新的
        
        return None

    def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """獲取任意類型會話的資訊"""
        # 嘗試從各個管理器中查找
        
        # 檢查 General Session
        current_gs = self.get_current_general_session()
        if current_gs and current_gs.session_id == session_id:
            return current_gs.get_status_info()
        
        # 檢查 Chatting Session
        cs = self.get_chatting_session(session_id)
        if cs:
            return cs.get_session_info()
        
        # 檢查 Workflow Session
        ws = self.get_workflow_session(session_id)
        if ws:
            return ws.get_session_info()
        
        return None
    
    def terminate_session(self, session_id: str, reason: str = "手動終止") -> bool:
        """終止任意類型的會話"""
        try:
            # 嘗試從各個管理器中終止
            if self.end_chatting_session(session_id):
                self._update_session_record(session_id, SessionRecordStatus.CANCELLED, {"reason": reason})
                return True
            
            if self.end_workflow_session(session_id):
                self._update_session_record(session_id, SessionRecordStatus.CANCELLED, {"reason": reason})
                return True
            
            # General Session 需要特殊處理
            current_gs = self.get_current_general_session()
            if current_gs and current_gs.session_id == session_id:
                result = self.end_general_session({"termination_reason": reason})
                if result:
                    self._update_session_record(session_id, SessionRecordStatus.CANCELLED, {"reason": reason})
                return result
                
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 終止會話失敗: {e}")
        return False
    
    # ==================== SessionRecord 管理 ====================
    
    def _create_session_record(self, session_id: str, session_type: SessionType, 
                              trigger_content: str, context_content: str) -> Optional[SessionRecord]:
        """創建會話記錄"""
        if not self.config["enable_session_recording"]:
            return None
            
        record = SessionRecord(
            record_id=f"sr_{int(time.time())}_{uuid.uuid4().hex[:8]}",
            session_type=session_type,
            session_id=session_id,
            status=SessionRecordStatus.TRIGGERED,
            trigger_content=trigger_content,
            context_content=context_content,
            trigger_user="system",  # 可以從 Working Context 獲取
            triggered_at=datetime.now()
        )
        
        self.session_records.append(record)
        
        # 限制記錄數量
        if len(self.session_records) > self.max_records:
            self.session_records = self.session_records[-self.max_records:]
        
        debug_log(3, f"[UnifiedSessionManager] 創建會話記錄: {record.record_id}")
        return record
    
    def _update_session_record(self, session_id: str, status: SessionRecordStatus, 
                              details: Optional[Dict[str, Any]] = None):
        """更新會話記錄狀態"""
        for record in self.session_records:
            if record.session_id == session_id:
                record.update_status(status, details)
                break
    
    def get_session_records(self, session_type: Optional[SessionType] = None, 
                           status: Optional[SessionRecordStatus] = None,
                           limit: int = 100) -> List[Dict[str, Any]]:
        """獲取會話記錄"""
        filtered_records = self.session_records
        
        if session_type:
            filtered_records = [r for r in filtered_records if r.session_type == session_type]
        
        if status:
            filtered_records = [r for r in filtered_records if r.status == status]
        
        # 按時間倒序排列，返回最近的記錄
        filtered_records.sort(key=lambda x: x.triggered_at, reverse=True)
        
        return [record.to_dict() for record in filtered_records[:limit]]
    
    def get_session_record_by_id(self, record_id: str) -> Optional[Dict[str, Any]]:
        """根據記錄ID獲取會話記錄"""
        for record in self.session_records:
            if record.record_id == record_id:
                return record.to_dict()
        return None
    
    # ==================== 系統維護 ====================
    
    def cleanup_expired_sessions(self):
        """清理過期會話"""
        try:
            # 清理各個管理器的過期會話
            cs_manager = self.managers['cs_manager']
            ws_manager = self.managers['ws_manager']
            
            # 清理 CS 舊會話
            cs_manager.cleanup_old_sessions(keep_recent=10)
            
            # 清理 WS 舊會話
            ws_manager.cleanup_old_sessions(keep_recent=10)
            
            # 清理舊的會話記錄
            self._cleanup_old_records()
            
            debug_log(2, "[UnifiedSessionManager] 過期會話清理完成")
            
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 清理過期會話失敗: {e}")
    
    def _cleanup_old_records(self):
        """清理舊的會話記錄"""
        cutoff_time = datetime.now().timestamp() - self.config["max_session_age"]
        
        original_count = len(self.session_records)
        self.session_records = [
            record for record in self.session_records 
            if record.triggered_at.timestamp() > cutoff_time
        ]
        
        cleaned_count = original_count - len(self.session_records)
        if cleaned_count > 0:
            debug_log(2, f"[UnifiedSessionManager] 清理了 {cleaned_count} 條舊會話記錄")
    
    def get_system_status(self) -> Dict[str, Any]:
        """獲取系統狀態摘要"""
        try:
            active_sessions = self.get_all_active_sessions()
            
            return {
                "active_sessions": {
                    "general_count": len(active_sessions["general"]),
                    "chatting_count": len(active_sessions["chatting"]),
                    "workflow_count": len(active_sessions["workflow"]),
                    "total_count": sum(len(sessions) for sessions in active_sessions.values())
                },
                "session_records": {
                    "total_records": len(self.session_records),
                    "recent_records": len([r for r in self.session_records 
                                         if (datetime.now() - r.triggered_at).total_seconds() < 3600])
                },
                "system_info": {
                    "recording_enabled": self.config["enable_session_recording"],
                    "max_records": self.max_records,
                    "auto_cleanup_interval": self.config["auto_cleanup_interval"]
                }
            }
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 獲取系統狀態失敗: {e}")
            return {"error": str(e)}

    # ==================== 會話超時檢查機制 ====================
    
    def check_session_timeouts(self) -> List[Dict[str, Any]]:
        """檢查並處理會話超時 - 系統層面的超時處理"""
        timeout_sessions = []
        current_time = time.time()
        
        try:
            # 檢查 Chatting Sessions 超時
            active_cs = self.get_active_chatting_sessions()
            for cs in active_cs:
                if self._is_session_timeout(cs, current_time):
                    reason = f"會話超時：超過 {self.config['max_session_age']} 秒無活動"
                    if self.end_chatting_session(cs.session_id, save_memory=True):
                        timeout_sessions.append({
                            "session_id": cs.session_id,
                            "session_type": "chatting",
                            "reason": reason,
                            "last_activity": getattr(cs, 'last_activity', None)
                        })
                        debug_log(1, f"[SessionManager] 結束超時的聊天會話: {cs.session_id}")
            
            # 檢查 Workflow Sessions 超時
            active_ws = self.get_active_workflow_sessions()
            for ws in active_ws:
                if self._is_session_timeout(ws, current_time):
                    reason = f"工作流超時：超過 {self.config['max_session_age']} 秒無活動"
                    if self.end_workflow_session(ws.session_id):
                        timeout_sessions.append({
                            "session_id": ws.session_id,
                            "session_type": "workflow", 
                            "reason": reason,
                            "last_activity": getattr(ws, 'last_activity', None)
                        })
                        debug_log(1, f"[SessionManager] 結束超時的工作流會話: {ws.session_id}")
            
            # 檢查 General Session 超時
            current_gs = self.get_current_general_session()
            if current_gs and self._is_session_timeout(current_gs, current_time):
                reason = f"通用會話超時：超過 {self.config['max_session_age']} 秒無活動"
                if self.end_general_session({"reason": reason}):
                    timeout_sessions.append({
                        "session_id": current_gs.session_id,
                        "session_type": "general",
                        "reason": reason,
                        "last_activity": getattr(current_gs, 'last_activity', None)
                    })
                    debug_log(1, f"[SessionManager] 結束超時的通用會話: {current_gs.session_id}")
            
            if timeout_sessions:
                info_log(f"[SessionManager] 處理了 {len(timeout_sessions)} 個超時會話")
            
            return timeout_sessions
            
        except Exception as e:
            error_log(f"[SessionManager] 檢查會話超時失敗: {e}")
            return []
    
    def _is_session_timeout(self, session, current_time: float) -> bool:
        """判斷會話是否超時"""
        try:
            # 獲取最後活動時間
            last_activity = getattr(session, 'last_activity', None)
            if not last_activity:
                # 如果沒有最後活動時間，使用創建時間
                last_activity = getattr(session, 'created_at', current_time)
            
            # 轉換為時間戳（如果是 datetime 對象）
            if isinstance(last_activity, datetime):
                last_activity = last_activity.timestamp()
            
            # 計算是否超時
            inactive_duration = current_time - last_activity
            return inactive_duration > self.config['max_session_age']
            
        except Exception as e:
            error_log(f"[SessionManager] 判斷會話超時失敗: {e}")
            return False
    
    def start_timeout_monitor(self, check_interval: int = 300) -> None:
        """啟動超時監控（每5分鐘檢查一次）"""
        import threading
        
        def timeout_checker():
            while True:
                try:
                    self.check_session_timeouts()
                    time.sleep(check_interval)
                except Exception as e:
                    error_log(f"[SessionManager] 超時監控執行失敗: {e}")
                    time.sleep(check_interval)
        
        timeout_thread = threading.Thread(target=timeout_checker, daemon=True)
        timeout_thread.start()
        info_log(f"[SessionManager] 啟動會話超時監控，檢查間隔：{check_interval}秒")

    # ==================== 向後兼容方法 ====================
    
    def create_session(self, workflow_type: str = None, command: str = None,  # type: ignore
                      initial_data: Dict[str, Any] = None, **kwargs) -> Optional[Any]: # type: ignore
        """
        向後兼容的會話創建方法
        根據參數自動判斷要創建的會話類型
        """
        try:
            # 如果指定了 workflow_type，創建 WorkflowSession
            if workflow_type is not None:
                # 確保有 GS 會話存在 - 使用正確的 GSType 枚舉值
                current_gs = self.get_current_general_session()
                if not current_gs:
                    self.start_general_session("system_event", {"trigger": "create_workflow"})
                    current_gs = self.get_current_general_session()
                
                # 使用當前 GS 的 session_id，而不是生成新的
                gs_id = kwargs.get('gs_session_id') or (current_gs.session_id if current_gs else f"gs_{int(time.time())}")
                
                # 將 workflow_type 映射到正確的 WSTaskType（使用枚舉的 value）
                task_type_mapping = {
                    'single_command': 'system_command',
                    'file_operation': 'file_operation', 
                    'workflow_automation': 'workflow_automation',
                    'module_integration': 'module_integration',
                    'custom_task': 'custom_task'
                }
                mapped_task_type = task_type_mapping.get(workflow_type, 'custom_task')
                
                return self.create_workflow_session(
                    gs_session_id=gs_id,
                    task_type=mapped_task_type,
                    task_definition={
                        "command": command or "unknown",
                        "initial_data": initial_data or {}
                    }
                )
            
            # 預設創建 GeneralSession
            return self.start_general_session("text_input", {"trigger": "create_session"})
            
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 向後兼容創建會話失敗: {e}")
            return None
    
    def get_session(self, session_id: str) -> Optional[Any]:
        """向後兼容的會話獲取方法"""
        try:
            # 依次嘗試不同類型的會話
            # 由於沒有直接的 get_general_session，嘗試通過當前會話或其他方式
            current_gs = self.get_current_general_session()
            if current_gs and hasattr(current_gs, 'session_id') and current_gs.session_id == session_id:
                return current_gs
                
            session = self.get_chatting_session(session_id)
            if session:
                return session
                
            session = self.get_workflow_session(session_id)
            if session:
                return session
                
            return None
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 向後兼容獲取會話失敗: {e}")
            return None
    
    def end_session(self, session_id: str, reason: str = "manual") -> bool:
        """向後兼容的會話結束方法"""
        try:
            # 依次嘗試結束不同類型的會話
            # 對於 General Session，檢查是否是當前會話
            current_gs = self.get_current_general_session()
            if current_gs and hasattr(current_gs, 'session_id') and current_gs.session_id == session_id:
                return self.end_general_session({"reason": reason})
            
            # 嘗試結束 Chatting Session
            if self.end_chatting_session(session_id):
                return True
                
            # 嘗試結束 Workflow Session  
            if self.end_workflow_session(session_id):
                return True
                
            return False
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 向後兼容結束會話失敗: {e}")
            return False
    
    def get_active_sessions(self) -> List[Any]:
        """向後兼容的活躍會話獲取方法"""
        try:
            all_sessions = []
            # 使用現有的方法
            current_gs = self.get_current_general_session()
            if current_gs:
                all_sessions.append(current_gs)
            all_sessions.extend(self.get_active_chatting_sessions())
            all_sessions.extend(self.get_active_workflow_sessions())
            return all_sessions
        except Exception as e:
            error_log(f"[UnifiedSessionManager] 向後兼容獲取活躍會話失敗: {e}")
            return []


# 全域統一會話管理器實例
session_manager = UnifiedSessionManager()

# 向後兼容的別名
unified_session_manager = session_manager

# 導出必要的類和枚舉 - 明確導入以支持類型檢查器
try:
    from .general_session import GeneralSession, GSType, GSStatus
    from .chatting_session import ChattingSession  
    from .workflow_session import WorkflowSession, WSTaskType, WSStatus
    
    # 設定兼容別名
    SessionStatus = WSStatus
except ImportError as e:
    error_log(f"導出會話類時失敗: {e}")
    # 如果導入失敗，設定 None 值以避免未定義錯誤
    GeneralSession = None  # type: ignore
    ChattingSession = None  # type: ignore
    WorkflowSession = None  # type: ignore
    GSType = None  # type: ignore
    GSStatus = None  # type: ignore
    WSTaskType = None  # type: ignore
    WSStatus = None  # type: ignore
    SessionStatus = SessionRecordStatus

__all__ = [
    'session_manager', 'unified_session_manager',
    'SessionType', 'SessionRecordStatus', 'SessionRecord',
    'UnifiedSessionManager', 'SessionStatus',
    'GeneralSession', 'ChattingSession', 'WorkflowSession',
    'GSType', 'GSStatus', 'WSTaskType', 'WSStatus'
]
