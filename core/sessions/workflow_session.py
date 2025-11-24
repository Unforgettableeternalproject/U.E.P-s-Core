# core/sessions/workflow_session.py
"""
Workflow Session (WS) 實現 - 重構版本

根據系統流程文檔，WS 的正確職責：
1. 追蹤工作流會話生命週期和狀態
2. 記錄任務步驟執行信息
3. 維護任務相關的元數據和配置
4. 提供工作流級別的上下文信息

WS 不應該做的事（由模組和 Router 處理）：
- ❌ 直接調用 SYS/LLM 模組
- ❌ 管理模組間數據傳遞
- ❌ 執行具體的任務邏輯
- ❌ 協調模組工作流

正確的流程應該是：
Router → 啟動 WS → WS 提供工作流上下文 → Router 調用 SYS/LLM → 
模組從 Working Context 獲取數據並執行 → Router 轉送結果 → WS 記錄執行結果
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
from enum import Enum
import uuid

from utils.debug_helper import debug_log, info_log, error_log


class WSStatus(Enum):
    """WS 狀態"""
    INITIALIZING = "initializing"
    READY = "ready"
    EXECUTING = "executing"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WSTaskType(Enum):
    """任務類型"""
    SYSTEM_COMMAND = "system_command"
    FILE_OPERATION = "file_operation"
    WORKFLOW_AUTOMATION = "workflow_automation"
    MODULE_INTEGRATION = "module_integration"
    CUSTOM_TASK = "custom_task"
    SYSTEM_NOTIFICATION = "system_notification"


class TaskStep:
    """任務步驟記錄 - 簡化版本，只記錄不執行"""
    
    def __init__(self, step_id: str, step_name: str, step_type: str):
        self.step_id = step_id
        self.step_name = step_name
        self.step_type = step_type
        self.status = "pending"
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.metadata: Dict[str, Any] = {}
        
    def start(self):
        """標記步驟開始"""
        self.status = "executing"
        self.started_at = datetime.now()
        
    def complete(self, result: Dict[str, Any]):
        """標記步驟完成"""
        self.status = "completed"
        self.completed_at = datetime.now()
        self.result = result
        
    def fail(self, error: str):
        """標記步驟失敗"""
        self.status = "failed"
        self.completed_at = datetime.now()
        self.error = error
        
    def add_metadata(self, key: str, value: Any):
        """添加元數據"""
        self.metadata[key] = value
        
    def get_duration(self) -> Optional[float]:
        """獲取執行時間"""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None
        
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式"""
        return {
            "step_id": self.step_id,
            "step_name": self.step_name,
            "step_type": self.step_type,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration": self.get_duration(),
            "result": self.result,
            "error": self.error,
            "metadata": self.metadata
        }


class WorkflowSession:
    """
    工作流會話 - 重構版本
    
    職責：
    - 會話生命週期管理
    - 任務步驟記錄
    - 工作流元數據維護
    - 提供工作流上下文信息
    
    不負責：
    - 模組調用（由 Router 處理）
    - 任務執行（由 SYS/LLM 模組處理）
    - 工作流協調（由 Router 和 Working Context 處理）
    """
    
    def __init__(self, session_id: str, gs_session_id: str, 
                 task_type: WSTaskType, task_definition: Dict[str, Any]):
        self.session_id = session_id
        self.gs_session_id = gs_session_id
        self.task_type = task_type
        self.task_definition = task_definition
        
        self.status = WSStatus.INITIALIZING
        self.created_at = datetime.now()
        self.last_activity = self.created_at
        self.ended_at: Optional[datetime] = None
        
        # 待結束標記 - 符合會話生命週期架構
        # 設置後會在循環完成邊界時終止會話
        self.pending_end = False
        self.pending_end_reason: Optional[str] = None
        
        # 任務步驟記錄
        self.task_steps: List[TaskStep] = []
        self.current_step_index = 0
        
        # 工作流數據存儲（用於在步驟間傳遞數據）
        self.workflow_data: Dict[str, Any] = {}
        
        # 工作流元數據
        self.workflow_token = self._generate_workflow_token()
        self.session_metadata: Dict[str, Any] = {
            "task_type": task_type.value,
            "task_name": task_definition.get("name", "unnamed_task"),
            "task_priority": task_definition.get("priority", "normal"),
            "expected_steps": task_definition.get("steps_count", 0)
        }
        
        # 會話配置
        self.config = {
            "timeout_seconds": 300,  # 5 分鐘超時
            "max_steps": 50,  # 最大步驟數
            "allow_parallel": False,  # 是否允許並行執行（供模組參考）
        }
        
        # 會話統計
        self.stats = {
            "total_steps": 0,
            "completed_steps": 0,
            "failed_steps": 0,
            "total_processing_time": 0.0,
            "avg_step_time": 0.0
        }
        
        self._initialize()
        
    def _generate_workflow_token(self) -> str:
        """生成工作流標識符"""
        return f"ws_{self.session_id}_{int(self.created_at.timestamp())}"
    
    def _initialize(self):
        """初始化會話"""
        try:
            self.status = WSStatus.READY
            info_log(f"[WorkflowSession] WS 初始化完成: {self.session_id}")
            debug_log(2, f"  └─ GS: {self.gs_session_id}")
            debug_log(2, f"  └─ 工作流標識: {self.workflow_token}")
            debug_log(2, f"  └─ 任務類型: {self.task_type.value}")
            
        except Exception as e:
            error_log(f"[WorkflowSession] WS 初始化失敗: {e}")
            self.status = WSStatus.FAILED
    
    def add_step(self, step_name: str, step_type: str) -> str:
        """
        添加新的任務步驟
        
        Args:
            step_name: 步驟名稱
            step_type: 步驟類型
            
        Returns:
            step_id: 步驟ID
        """
        try:
            step_id = f"step_{len(self.task_steps) + 1}"
            step = TaskStep(step_id, step_name, step_type)
            self.task_steps.append(step)
            
            self.last_activity = datetime.now()
            
            debug_log(2, f"[WorkflowSession] 添加步驟: {step_id} - {step_name}")
            
            return step_id
            
        except Exception as e:
            error_log(f"[WorkflowSession] 添加步驟失敗: {e}")
            return ""
    
    def start_step(self, step_id: str):
        """
        標記步驟開始執行
        
        Args:
            step_id: 步驟ID
        """
        try:
            step = self._get_step(step_id)
            if step:
                step.start()
                self.status = WSStatus.EXECUTING
                self.last_activity = datetime.now()
                debug_log(2, f"[WorkflowSession] 開始執行步驟: {step_id}")
            else:
                error_log(f"[WorkflowSession] 找不到步驟: {step_id}")
                
        except Exception as e:
            error_log(f"[WorkflowSession] 開始步驟失敗: {e}")
    
    def complete_step(self, step_id: str, result: Dict[str, Any]):
        """
        標記步驟完成
        
        Args:
            step_id: 步驟ID
            result: 執行結果
        """
        try:
            step = self._get_step(step_id)
            if step:
                step.complete(result)
                self.last_activity = datetime.now()
                
                # 更新統計
                self.stats["total_steps"] += 1
                self.stats["completed_steps"] += 1
                
                duration = step.get_duration()
                if duration:
                    self.stats["total_processing_time"] += duration
                    self.stats["avg_step_time"] = (
                        self.stats["total_processing_time"] / self.stats["total_steps"]
                    )
                
                debug_log(2, f"[WorkflowSession] 步驟完成: {step_id}")
                
                # 檢查是否所有步驟都完成
                if self._all_steps_completed():
                    self.status = WSStatus.COMPLETED
                else:
                    self.status = WSStatus.READY
            else:
                error_log(f"[WorkflowSession] 找不到步驟: {step_id}")
                
        except Exception as e:
            error_log(f"[WorkflowSession] 完成步驟失敗: {e}")
    
    def fail_step(self, step_id: str, error: str):
        """
        標記步驟失敗
        
        Args:
            step_id: 步驟ID
            error: 錯誤信息
        """
        try:
            step = self._get_step(step_id)
            if step:
                step.fail(error)
                self.last_activity = datetime.now()
                
                # 更新統計
                self.stats["total_steps"] += 1
                self.stats["failed_steps"] += 1
                
                error_log(f"[WorkflowSession] 步驟失敗: {step_id} - {error}")
                
                # 步驟失敗可能導致整個工作流失敗
                self.status = WSStatus.FAILED
            else:
                error_log(f"[WorkflowSession] 找不到步驟: {step_id}")
                
        except Exception as e:
            error_log(f"[WorkflowSession] 標記步驟失敗時出錯: {e}")
    
    def get_session_context(self) -> Dict[str, Any]:
        """
        獲取工作流會話上下文（供模組使用）
        
        Returns:
            會話上下文數據
        """
        return {
            "session_id": self.session_id,
            "gs_session_id": self.gs_session_id,
            "workflow_token": self.workflow_token,
            "task_type": self.task_type.value,
            "task_definition": self.task_definition,
            "session_metadata": self.session_metadata,
            "current_step_index": self.current_step_index,
            "total_steps": len(self.task_steps),
            "steps": [step.to_dict() for step in self.task_steps],
            "config": self.config.copy(),
            "stats": self.stats.copy(),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat()
        }
    
    def get_step(self, step_id: str) -> Optional[Dict[str, Any]]:
        """
        獲取特定步驟數據
        
        Args:
            step_id: 步驟ID
            
        Returns:
            步驟數據（字典格式）
        """
        step = self._get_step(step_id)
        return step.to_dict() if step else None
    
    def get_current_step(self) -> Optional[Dict[str, Any]]:
        """獲取當前步驟"""
        if 0 <= self.current_step_index < len(self.task_steps):
            return self.task_steps[self.current_step_index].to_dict()
        return None
    
    def get_pending_steps(self) -> List[Dict[str, Any]]:
        """獲取待執行的步驟"""
        pending = [step for step in self.task_steps if step.status == "pending"]
        return [step.to_dict() for step in pending]
    
    def update_metadata(self, key: str, value: Any):
        """
        更新會話元數據
        
        Args:
            key: 元數據鍵
            value: 元數據值
        """
        self.session_metadata[key] = value
        debug_log(3, f"[WorkflowSession] 更新元數據: {key} = {value}")
    
    def add_data(self, key: str, value: Any):
        """
        添加工作流數據（用於在步驟間傳遞數據）
        
        Args:
            key: 數據鍵
            value: 數據值
        """
        if not hasattr(self, 'workflow_data'):
            self.workflow_data = {}
        self.workflow_data[key] = value
        debug_log(3, f"[WorkflowSession] 添加數據: {key}")
    
    def get_data(self, key: str, default: Any = None) -> Any:
        """
        獲取工作流數據
        
        Args:
            key: 數據鍵
            default: 默認值（如果鍵不存在）
            
        Returns:
            數據值或默認值
        """
        if not hasattr(self, 'workflow_data'):
            self.workflow_data = {}
        return self.workflow_data.get(key, default)
    
    def pause(self):
        """暫停工作流"""
        if self.status in [WSStatus.READY, WSStatus.EXECUTING, WSStatus.WAITING]:
            self.status = WSStatus.PAUSED
            info_log(f"[WorkflowSession] WS 已暫停: {self.session_id}")
    
    def resume(self):
        """恢復工作流"""
        if self.status == WSStatus.PAUSED:
            self.status = WSStatus.READY
            self.last_activity = datetime.now()
            info_log(f"[WorkflowSession] WS 已恢復: {self.session_id}")
    
    def mark_for_end(self, reason: str = "workflow_complete"):
        """
        標記會話待結束 - 符合會話生命週期架構
        會話將在循環完成邊界時真正終止
        
        Args:
            reason: 標記原因
        """
        self.pending_end = True
        self.pending_end_reason = reason
        debug_log(2, f"[WorkflowSession] WS 已標記待結束: {self.session_id} - {reason}")
    
    def cancel(self, reason: str = "user_cancelled"):
        """
        取消工作流
        
        Args:
            reason: 取消原因
        """
        self.status = WSStatus.CANCELLED
        self.ended_at = datetime.now()
        info_log(f"[WorkflowSession] WS 已取消: {self.session_id} - {reason}")
    
    def end(self, reason: str = "normal") -> Dict[str, Any]:
        """
        結束工作流會話
        
        Args:
            reason: 結束原因
            
        Returns:
            工作流總結數據
        """
        try:
            if self.status not in [WSStatus.COMPLETED, WSStatus.FAILED, WSStatus.CANCELLED]:
                self.status = WSStatus.COMPLETED
            
            self.ended_at = datetime.now()
            
            duration = (self.ended_at - self.created_at).total_seconds()
            
            summary = {
                "session_id": self.session_id,
                "gs_session_id": self.gs_session_id,
                "workflow_token": self.workflow_token,
                "task_type": self.task_type.value,
                "duration": duration,
                "total_steps": len(self.task_steps),
                "completed_steps": self.stats["completed_steps"],
                "failed_steps": self.stats["failed_steps"],
                "stats": self.stats.copy(),
                "end_reason": reason,
                "final_status": self.status.value,
                "created_at": self.created_at.isoformat(),
                "ended_at": self.ended_at.isoformat()
            }
            
            info_log(f"[WorkflowSession] WS 已結束: {self.session_id}")
            info_log(f"  └─ 持續時間: {duration:.1f}秒")
            info_log(f"  └─ 完成步驟: {self.stats['completed_steps']}/{len(self.task_steps)}")
            info_log(f"  └─ 平均步驟時間: {self.stats['avg_step_time']:.2f}秒")
            
            # 發布會話結束事件 - 通知 StateManager 處理狀態轉換
            try:
                from core.event_bus import event_bus, SystemEvent
                from core.working_context import working_context_manager
                
                # ✅ 讀取當前 cycle_index（會話在循環結束後才真正結束，值已正確更新）
                current_cycle = working_context_manager.global_context_data.get('current_cycle_index', 0)
                debug_log(1, f"[WorkflowSession] 📍 發布 SESSION_ENDED: session={self.session_id}, cycle={current_cycle}")
                
                event_bus.publish(
                    event_type=SystemEvent.SESSION_ENDED,
                    data={
                        'session_id': self.session_id,
                        'session_type': 'workflow',
                        'reason': reason,
                        'duration': duration,
                        'task_type': self.task_type.value,
                        'completed_steps': self.stats['completed_steps'],
                        'total_steps': len(self.task_steps),
                        'cycle_index': current_cycle  # ✅ 附帶當前循環索引
                    },
                    source='workflow_session'
                )
                debug_log(2, f"[WorkflowSession] 已發布 SESSION_ENDED 事件: {self.session_id}")
            except Exception as e:
                error_log(f"[WorkflowSession] 發布會話結束事件失敗: {e}")
            
            return summary
            
        except Exception as e:
            error_log(f"[WorkflowSession] 結束會話失敗: {e}")
            return {}
    
    def _get_step(self, step_id: str) -> Optional[TaskStep]:
        """獲取步驟對象"""
        for step in self.task_steps:
            if step.step_id == step_id:
                return step
        return None
    
    def _all_steps_completed(self) -> bool:
        """檢查是否所有步驟都已完成"""
        if not self.task_steps:
            return False
        
        return all(
            step.status in ["completed", "failed"] 
            for step in self.task_steps
        )
    
    def get_summary(self) -> Dict[str, Any]:
        """
        獲取工作流總結
        
        Returns:
            工作流總結數據
        """
        duration = (
            (self.ended_at or datetime.now()) - self.created_at
        ).total_seconds()
        
        # 獲取當前步驟名稱
        current_step_info = self.get_current_step()
        current_step_name = current_step_info.get("step_name") if current_step_info else None
        
        return {
            "session_id": self.session_id,
            "gs_session_id": self.gs_session_id,
            "workflow_token": self.workflow_token,
            "workflow_type": self.task_definition.get("workflow_type", "unknown"),  # 從 task_definition 提取
            "command": self.task_definition.get("command", ""),  # 從 task_definition 提取
            "current_step": current_step_name,  # 當前步驟名稱
            "current_step_index": self.current_step_index,  # 當前步驟索引
            "task_type": self.task_type.value,
            "task_definition": self.task_definition,
            "status": self.status.value,
            "duration": duration,
            "total_steps": len(self.task_steps),
            "stats": self.stats.copy(),
            "session_metadata": self.session_metadata,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None
        }
    
    def to_dict(self) -> Dict[str, Any]:
        """
        轉換為字典格式（與 get_summary 相同，為兼容性提供）
        
        Returns:
            工作流數據字典
        """
        return self.get_summary()


class WorkflowSessionManager:
    """
    Workflow Session 管理器
    
    負責創建、追蹤和管理 WS 實例
    """
    
    def __init__(self):
        self.sessions: Dict[str, WorkflowSession] = {}
        self.active_session_id: Optional[str] = None
        
        info_log("[WorkflowSessionManager] WS 管理器已初始化")
    
    def create_session(self, gs_session_id: str, 
                      task_type: WSTaskType,
                      task_definition: Dict[str, Any]) -> str:
        """
        創建新的 WS
        
        Args:
            gs_session_id: 所屬的 GS ID
            task_type: 任務類型
            task_definition: 任務定義
            
        Returns:
            session_id: WS ID
        """
        session_id = f"ws_{uuid.uuid4().hex[:8]}"
        
        session = WorkflowSession(session_id, gs_session_id, task_type, task_definition)
        self.sessions[session_id] = session
        self.active_session_id = session_id
        
        info_log(f"[WorkflowSessionManager] 創建 WS: {session_id}")
        
        return session_id
    
    def get_session(self, session_id: str) -> Optional[WorkflowSession]:
        """獲取 WS 實例"""
        return self.sessions.get(session_id)
    
    def get_active_session(self) -> Optional[WorkflowSession]:
        """獲取當前活躍的 WS"""
        if self.active_session_id:
            return self.sessions.get(self.active_session_id)
        return None
    
    def get_active_sessions(self) -> List[WorkflowSession]:
        """獲取所有活躍的 WS（狀態為 EXECUTING 或 READY）"""
        return [
            session for session in self.sessions.values()
            if session.status in [WSStatus.EXECUTING, WSStatus.READY]
        ]
    
    def end_session(self, session_id: str, reason: str = "normal") -> Dict[str, Any]:
        """結束 WS"""
        session = self.sessions.get(session_id)
        if session:
            summary = session.end(reason)
            
            if self.active_session_id == session_id:
                self.active_session_id = None
            
            return summary
        
        return {}
    
    def cancel_session(self, session_id: str, reason: str = "user_cancelled"):
        """取消 WS"""
        session = self.sessions.get(session_id)
        if session:
            session.cancel(reason)
            
            if self.active_session_id == session_id:
                self.active_session_id = None
    
    def cleanup_old_sessions(self, keep_recent: int = 10):
        """清理舊的已完成會話"""
        completed_sessions = [
            (sid, s) for sid, s in self.sessions.items()
            if s.status in [WSStatus.COMPLETED, WSStatus.FAILED, WSStatus.CANCELLED]
        ]
        
        # 按結束時間排序
        completed_sessions.sort(key=lambda x: x[1].ended_at or datetime.min)
        
        # 保留最近的會話，刪除其餘的
        if len(completed_sessions) > keep_recent:
            to_remove = completed_sessions[:-keep_recent]
            for session_id, _ in to_remove:
                del self.sessions[session_id]
                debug_log(2, f"[WorkflowSessionManager] 清理舊會話: {session_id}")


# 全局 WS 管理器實例
workflow_session_manager = WorkflowSessionManager()
