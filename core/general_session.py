# core/general_session.py
"""
General Session (GS) 管理器

General Session 是 U.E.P 系統中最基礎的會話單位，負責管理從使用者輸入開始到程式輸出結束的完整生命週期。

核心概念：
- 每個 GS 包含完整的「輸入→處理→輸出」流程
- GS 結束時部分資訊會保留到下個 GS
- Working Context 在 GS 內保持一致性
- GS 內可能包含 CS (聊天會話) 或 WS (工作流會話)

會話階層：
General Session (GS)
├── 使用者輸入開始
├── Working Context 一致性保持
├── 可能啟動 CS 或 WS
├── 程式輸出結束
└── 下個 GS 開始時部分資訊保留
"""

import uuid
import time
from datetime import datetime
from typing import Dict, Any, Optional, List, Callable
from enum import Enum, auto
from dataclasses import dataclass, field

from utils.debug_helper import debug_log, info_log, error_log
from core.working_context import working_context_manager, ContextType

# 延遲導入避免循環依賴
def _get_status_manager():
    from core.status_manager import StatusManager
    return StatusManager()


class SessionRecordType(Enum):
    """會話記錄類型"""
    GS = "general_session"
    CS = "chatting_session"
    WS = "workflow_session"


class SessionRecordStatus(Enum):
    """會話記錄狀態"""
    TRIGGERED = "triggered"
    ACTIVE = "active"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class SessionRecord:
    """會話記錄"""
    record_id: str
    session_type: SessionRecordType
    session_id: str
    status: SessionRecordStatus
    
    # 觸發信息
    trigger_content: str
    context_content: str
    trigger_user: Optional[str]
    triggered_at: datetime
    
    # 狀態變更
    status_history: List[Dict[str, Any]] = field(default_factory=list)
    
    # 會話數據
    metadata: Dict[str, Any] = field(default_factory=dict)
    session_summary: Optional[Dict[str, Any]] = None
    
    def update_status(self, new_status: SessionRecordStatus, details: Optional[Dict[str, Any]] = None):
        """更新狀態"""
        self.status = new_status
        self.status_history.append({
            "status": new_status.value,
            "timestamp": datetime.now().isoformat(),
            "details": details or {}
        })
    
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
            "session_summary": self.session_summary
        }


class GSStatus(Enum):
    """General Session 狀態"""
    INACTIVE = auto()       # 未啟動
    INITIALIZING = auto()   # 初始化中
    ACTIVE = auto()         # 進行中
    PROCESSING = auto()     # 處理中 (可能有 CS/WS 在運行)
    FINALIZING = auto()     # 結束處理中
    COMPLETED = auto()      # 已完成
    ERROR = auto()          # 錯誤狀態


class GSType(Enum):
    """General Session 類型"""
    VOICE_INPUT = "voice_input"        # 語音輸入觸發
    TEXT_INPUT = "text_input"          # 文本輸入觸發
    SYSTEM_EVENT = "system_event"      # 系統事件觸發
    SCHEDULED = "scheduled"            # 排程觸發
    CONTINUATION = "continuation"      # 延續上個 GS


@dataclass
class GSPreservedData:
    """跨 GS 保留的資料"""
    user_context: Dict[str, Any] = field(default_factory=dict)      # 使用者上下文
    system_state: Dict[str, Any] = field(default_factory=dict)      # 系統狀態
    conversation_memory: Dict[str, Any] = field(default_factory=dict)  # 對話記憶
    active_identities: List[str] = field(default_factory=list)      # 活躍身份
    pending_tasks: List[Dict[str, Any]] = field(default_factory=list)  # 待處理任務
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_context": self.user_context,
            "system_state": self.system_state,
            "conversation_memory": self.conversation_memory,
            "active_identities": self.active_identities,
            "pending_tasks": self.pending_tasks
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GSPreservedData':
        return cls(
            user_context=data.get("user_context", {}),
            system_state=data.get("system_state", {}),
            conversation_memory=data.get("conversation_memory", {}),
            active_identities=data.get("active_identities", []),
            pending_tasks=data.get("pending_tasks", [])
        )


@dataclass
class GSContext:
    """General Session 上下文"""
    session_id: str
    gs_type: GSType
    trigger_event: Dict[str, Any]
    created_at: datetime
    working_contexts: Dict[str, str] = field(default_factory=dict)  # context_type -> context_id
    sub_sessions: List[str] = field(default_factory=list)          # CS/WS session IDs
    processing_pipeline: List[str] = field(default_factory=list)   # 處理流程記錄
    outputs: List[Dict[str, Any]] = field(default_factory=list)    # 輸出記錄


class GeneralSession:
    """General Session 實例"""
    
    def __init__(self, gs_type: GSType, trigger_event: Dict[str, Any], 
                 preserved_data: Optional[GSPreservedData] = None):
        self.session_id = f"gs_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        self.gs_type = gs_type  # 直接存儲 gs_type 屬性
        self.status = GSStatus.INACTIVE
        self.context = GSContext(
            session_id=self.session_id,
            gs_type=gs_type,
            trigger_event=trigger_event,
            created_at=datetime.now()
        )
        
        # 繼承上個 GS 的保留資料
        self.preserved_data = preserved_data or GSPreservedData()
        
        # 生命週期時間戳
        self.started_at: Optional[datetime] = None
        self.ended_at: Optional[datetime] = None
        
        # 事件處理器
        self.lifecycle_handlers: Dict[GSStatus, List[Callable]] = {
            status: [] for status in GSStatus
        }
        
        # Working Context 整合
        self.working_context_ids: Dict[ContextType, str] = {}
        
        info_log(f"[GeneralSession] 創建新的 GS: {self.session_id} (類型: {gs_type.value})")
    
    def start(self) -> bool:
        """啟動 General Session"""
        if self.status != GSStatus.INACTIVE:
            error_log(f"[GeneralSession] GS {self.session_id} 已啟動或無法啟動 (狀態: {self.status.name})")
            return False
        
        try:
            self.status = GSStatus.INITIALIZING
            self.started_at = datetime.now()
            
            # 觸發初始化處理器
            self._trigger_lifecycle_handlers(GSStatus.INITIALIZING)
            
            # 初始化 Working Context
            self._initialize_working_contexts()
            
            # 處理觸發事件
            self._process_trigger_event()
            
            # 轉換到活躍狀態
            self.status = GSStatus.ACTIVE
            self._trigger_lifecycle_handlers(GSStatus.ACTIVE)
            
            info_log(f"[GeneralSession] GS {self.session_id} 已啟動")
            return True
            
        except Exception as e:
            self.status = GSStatus.ERROR
            error_log(f"[GeneralSession] GS {self.session_id} 啟動失敗: {e}")
            return False
    
    def _initialize_working_contexts(self):
        """初始化 Working Context"""
        
        # 設定 General Session 上下文資訊
        working_context_manager.set_data(
            ContextType.GENERAL_SESSION, 
            "current_session", 
            {
                "session_id": self.session_id,
                "gs_type": self.context.gs_type.value,
                "status": self.status.name,
                "created_at": self.context.created_at.isoformat(),
                "trigger_event": self.context.trigger_event
            }
        )
        
        # 根據 GS 類型創建合適的 Working Context
        if self.context.gs_type == GSType.VOICE_INPUT:
            context_id = working_context_manager.create_context(
                context_type=ContextType.CROSS_MODULE_DATA,
                threshold=1
            )
            self.working_context_ids[ContextType.CROSS_MODULE_DATA] = context_id
            self.context.working_contexts["cross_module_data"] = context_id
            
        elif self.context.gs_type == GSType.TEXT_INPUT:
            context_id = working_context_manager.create_context(
                context_type=ContextType.CONVERSATION,
                threshold=1
            )
            self.working_context_ids[ContextType.CONVERSATION] = context_id
            self.context.working_contexts["conversation"] = context_id
    
    def _process_trigger_event(self):
        """處理觸發事件"""
        trigger_data = self.context.trigger_event
        event_type = trigger_data.get("type", "unknown")
        
        # 記錄處理流程
        self.context.processing_pipeline.append(f"trigger_processed: {event_type}")
        
        # 根據事件類型進行不同處理
        if event_type == "voice_input":
            self._handle_voice_input(trigger_data)
        elif event_type == "text_input":
            self._handle_text_input(trigger_data)
        elif event_type == "system_event":
            self._handle_system_event(trigger_data)
    
    def _handle_voice_input(self, data: Dict[str, Any]):
        """處理語音輸入"""
        debug_log(2, f"[GeneralSession] 處理語音輸入: {data}")
        # 將語音資料存入 Working Context
        if ContextType.CROSS_MODULE_DATA in self.working_context_ids:
            working_context_manager.set_context_data("gs_voice_input", data)
    
    def _handle_text_input(self, data: Dict[str, Any]):
        """處理文本輸入"""
        debug_log(2, f"[GeneralSession] 處理文本輸入: {data}")
        # 將文本資料存入 Working Context
        if ContextType.CONVERSATION in self.working_context_ids:
            working_context_manager.set_context_data("gs_text_input", data)
    
    def _handle_system_event(self, data: Dict[str, Any]):
        """處理系統事件"""
        debug_log(2, f"[GeneralSession] 處理系統事件: {data}")
    
    def register_sub_session(self, sub_session_id: str, session_type: str):
        """註冊子會話 (CS/WS)"""
        self.context.sub_sessions.append(sub_session_id)
        info_log(f"[GeneralSession] 註冊子會話: {sub_session_id} (類型: {session_type})")
    
    def add_output(self, output_data: Dict[str, Any]):
        """添加輸出記錄"""
        output_record = {
            "timestamp": datetime.now().isoformat(),
            "data": output_data,
            "output_id": f"out_{len(self.context.outputs) + 1}"
        }
        self.context.outputs.append(output_record)
        debug_log(3, f"[GeneralSession] 添加輸出: {output_record['output_id']}")
    
    def transition_to_processing(self):
        """轉換到處理狀態 (有 CS/WS 運行時)"""
        if self.status == GSStatus.ACTIVE:
            self.status = GSStatus.PROCESSING
            self._trigger_lifecycle_handlers(GSStatus.PROCESSING)
    
    def transition_to_active(self):
        """轉換回活躍狀態 (CS/WS 結束後)"""
        if self.status == GSStatus.PROCESSING:
            self.status = GSStatus.ACTIVE
            self._trigger_lifecycle_handlers(GSStatus.ACTIVE)
    
    def finalize(self, final_output: Optional[Dict[str, Any]] = None) -> GSPreservedData:
        """結束 GS 並準備保留資料"""
        if self.status in [GSStatus.COMPLETED, GSStatus.ERROR]:
            return self.preserved_data
        
        try:
            self.status = GSStatus.FINALIZING
            self._trigger_lifecycle_handlers(GSStatus.FINALIZING)
            
            # 添加最終輸出
            if final_output:
                self.add_output(final_output)
            
            # 準備保留資料
            preserved_data = self._prepare_preserved_data()
            
            # 清理 Working Context
            self._cleanup_working_contexts()
            
            # 完成 GS
            self.status = GSStatus.COMPLETED
            self.ended_at = datetime.now()
            self._trigger_lifecycle_handlers(GSStatus.COMPLETED)
            
            duration = (self.ended_at - self.started_at).total_seconds() if self.started_at else 0
            info_log(f"[GeneralSession] GS {self.session_id} 已完成 (持續時間: {duration:.2f}s)")
            
            return preserved_data
            
        except Exception as e:
            self.status = GSStatus.ERROR
            error_log(f"[GeneralSession] GS {self.session_id} 結束時發生錯誤: {e}")
            return self.preserved_data
    
    def _prepare_preserved_data(self) -> GSPreservedData:
        """準備跨 GS 保留的資料"""
        new_preserved = GSPreservedData()
        
        # 保留重要的使用者上下文
        if self.context.outputs:
            new_preserved.user_context["last_interaction"] = self.context.outputs[-1]
        
        # 保留系統狀態
        new_preserved.system_state["last_session_id"] = self.session_id
        new_preserved.system_state["session_count"] = self.preserved_data.system_state.get("session_count", 0) + 1
        
        # 保留活躍身份
        current_identity = working_context_manager.get_current_identity()
        if current_identity:
            identity_id = current_identity.get("identity_id")
            if identity_id and identity_id not in new_preserved.active_identities:
                new_preserved.active_identities.append(identity_id)
        
        # 繼承之前的保留資料
        new_preserved.conversation_memory.update(self.preserved_data.conversation_memory)
        new_preserved.pending_tasks.extend(self.preserved_data.pending_tasks)
        
        return new_preserved
    
    def _cleanup_working_contexts(self):
        """清理 Working Context"""
        # 清理過期的上下文
        working_context_manager.cleanup_expired_contexts()
    
    def register_lifecycle_handler(self, status: GSStatus, handler: Callable):
        """註冊生命週期處理器"""
        self.lifecycle_handlers[status].append(handler)
    
    def _trigger_lifecycle_handlers(self, status: GSStatus):
        """觸發生命週期處理器"""
        for handler in self.lifecycle_handlers[status]:
            try:
                handler(self)
            except Exception as e:
                error_log(f"[GeneralSession] 生命週期處理器錯誤 ({status.name}): {e}")
    
    def get_status_info(self) -> Dict[str, Any]:
        """獲取狀態資訊"""
        return {
            "session_id": self.session_id,
            "status": self.status.name,
            "gs_type": self.context.gs_type.value,
            "created_at": self.context.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "sub_sessions": self.context.sub_sessions,
            "output_count": len(self.context.outputs),
            "processing_pipeline": self.context.processing_pipeline
        }


class GeneralSessionManager:
    """General Session 管理器"""
    
    def __init__(self):
        self.current_session: Optional[GeneralSession] = None
        self.session_history: List[GeneralSession] = []
        self.preserved_data: Optional[GSPreservedData] = None
        
        # 會話記錄管理
        self.session_records: List[SessionRecord] = []
        self.max_records_size = 100
        
        # 配置
        self.max_history_size = 10
        
        info_log("[GeneralSessionManager] General Session 管理器初始化完成")
    
    def start_session(self, gs_type: GSType, trigger_event: Dict[str, Any]) -> Optional[GeneralSession]:
        """啟動新的 General Session"""
        # 結束當前會話
        if self.current_session:
            self.end_current_session()
        
        # 應用系統狀態 penalty（每次創建 GS 時的自動微調）
        try:
            status_manager = _get_status_manager()
            penalties = status_manager.apply_session_penalties(gs_type.value)
            if penalties:
                debug_log(2, f"[GeneralSessionManager] 會話啟動時應用 penalty: {penalties}")
        except Exception as e:
            error_log(f"[GeneralSessionManager] 應用 status penalty 失敗: {e}")
        
        # 創建新會話
        new_session = GeneralSession(gs_type, trigger_event, self.preserved_data)
        
        if new_session.start():
            self.current_session = new_session
            
            # 創建會話記錄
            self._create_session_record(new_session, trigger_event)
            
            return new_session
        else:
            return None
    
    def end_current_session(self, final_output: Optional[Dict[str, Any]] = None) -> bool:
        """結束當前 General Session"""
        if not self.current_session:
            return False
        
        # 更新會話記錄
        self._update_session_record(self.current_session.session_id, SessionRecordStatus.COMPLETED, final_output)
        
        # 結束會話並獲取保留資料
        self.preserved_data = self.current_session.finalize(final_output)
        
        # 移到歷史記錄
        self.session_history.append(self.current_session)
        
        # 限制歷史記錄大小
        if len(self.session_history) > self.max_history_size:
            self.session_history.pop(0)
        
        self.current_session = None
        return True
    
    def get_current_session(self) -> Optional[GeneralSession]:
        """獲取當前會話"""
        return self.current_session
    
    def register_sub_session(self, sub_session_id: str, session_type: str) -> bool:
        """在當前 GS 中註冊子會話"""
        if self.current_session:
            self.current_session.register_sub_session(sub_session_id, session_type)
            self.current_session.transition_to_processing()
            return True
        return False
    
    def end_sub_session(self, sub_session_id: str) -> bool:
        """結束子會話"""
        if self.current_session and sub_session_id in self.current_session.context.sub_sessions:
            self.current_session.transition_to_active()
            return True
        return False
    
    def add_output_to_current(self, output_data: Dict[str, Any]) -> bool:
        """向當前會話添加輸出"""
        if self.current_session:
            self.current_session.add_output(output_data)
            return True
        return False
    
    def get_preserved_data(self) -> Optional[GSPreservedData]:
        """獲取保留資料"""
        return self.preserved_data
    
    def get_session_history(self) -> List[Dict[str, Any]]:
        """獲取會話歷史摘要"""
        return [session.get_status_info() for session in self.session_history]
    
    def get_system_status(self) -> Dict[str, Any]:
        """獲取系統狀態"""
        current_info = None
        if self.current_session:
            current_info = self.current_session.get_status_info()
        
        preserved_summary = None
        if self.preserved_data:
            preserved_summary = {
                "session_count": self.preserved_data.system_state.get("session_count", 0),
                "active_identities_count": len(self.preserved_data.active_identities),
                "pending_tasks_count": len(self.preserved_data.pending_tasks)
            }
        
        return {
            "current_session": current_info,
            "preserved_data_summary": preserved_summary,
            "history_count": len(self.session_history),
            "records_count": len(self.session_records),
            "manager_status": "active" if self.current_session else "idle"
        }
    
    def cleanup_completed_sessions(self):
        """清理已完成的會話"""
        # 如果當前會話已完成，清理它
        if self.current_session and self.current_session.status in [GSStatus.COMPLETED, GSStatus.ERROR]:
            self.session_history.append(self.current_session)
            self.current_session = None
            info_log("[GeneralSessionManager] 清理已完成的會話")
    
    def _create_session_record(self, session: GeneralSession, trigger_event: Dict[str, Any]):
        """創建會話記錄"""
        try:
            # 提取觸發內容
            trigger_content = trigger_event.get("data", {}).get("text", str(trigger_event))
            context_content = trigger_event.get("type", "unknown")
            trigger_user = trigger_event.get("user_id", "unknown")
            
            # 創建記錄
            record = SessionRecord(
                record_id=f"rec_{session.session_id}_{int(time.time())}",
                session_type=SessionRecordType.GS,
                session_id=session.session_id,
                status=SessionRecordStatus.ACTIVE,
                trigger_content=trigger_content,
                context_content=context_content,
                trigger_user=trigger_user,
                triggered_at=session.context.created_at,
                metadata={
                    "gs_type": session.context.gs_type.value,
                    "working_contexts": list(session.context.working_contexts.keys())
                }
            )
            
            # 添加初始狀態歷史
            record.update_status(SessionRecordStatus.ACTIVE, {"action": "session_started"})
            
            # 保存記錄
            self.session_records.append(record)
            
            # 限制記錄數量
            if len(self.session_records) > self.max_records_size:
                self.session_records.pop(0)
            
            debug_log(2, f"[GeneralSessionManager] 創建會話記錄: {record.record_id}")
            
        except Exception as e:
            error_log(f"[GeneralSessionManager] 創建會話記錄失敗: {e}")
    
    def _update_session_record(self, session_id: str, status: SessionRecordStatus, 
                              details: Optional[Dict[str, Any]] = None):
        """更新會話記錄"""
        try:
            # 查找對應記錄
            for record in self.session_records:
                if record.session_id == session_id:
                    record.update_status(status, details)
                    
                    # 如果是完成狀態，添加會話摘要
                    if status == SessionRecordStatus.COMPLETED and details:
                        record.session_summary = {
                            "final_output": details,
                            "completed_at": datetime.now().isoformat(),
                            "total_duration": (datetime.now() - record.triggered_at).total_seconds()
                        }
                    
                    debug_log(2, f"[GeneralSessionManager] 更新會話記錄: {record.record_id} -> {status.value}")
                    break
                    
        except Exception as e:
            error_log(f"[GeneralSessionManager] 更新會話記錄失敗: {e}")
    
    def get_session_records(self, session_type: Optional[SessionRecordType] = None, 
                           status: Optional[SessionRecordStatus] = None) -> List[Dict[str, Any]]:
        """獲取會話記錄"""
        records = []
        for record in self.session_records:
            if session_type and record.session_type != session_type:
                continue
            if status and record.status != status:
                continue
            records.append(record.to_dict())
        return records
    
    def get_session_record_by_id(self, session_id: str) -> Optional[Dict[str, Any]]:
        """根據會話ID獲取記錄"""
        for record in self.session_records:
            if record.session_id == session_id:
                return record.to_dict()
        return None


# 全域 General Session 管理器實例
general_session_manager = GeneralSessionManager()