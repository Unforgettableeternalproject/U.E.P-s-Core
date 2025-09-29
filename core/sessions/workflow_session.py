# core/workflow_session.py
"""
Workflow Session (WS) 重構版本

WS 專門負責處理任務型交互，特點包括：
1. 任務隔離：每個 WS 都有獨立的任務上下文
2. 系統整合：與 SYS 模組緊密整合，執行系統級任務
3. 狀態追蹤：詳細記錄任務執行過程和結果
4. 錯誤處理：完善的錯誤恢復和回滾機制

會話生命週期：
1. 初始化 -> 解析任務需求
2. 任務執行 -> 調用相關系統模組
3. 狀態更新 -> 追蹤執行進度
4. 結果輸出 -> 格式化執行結果
5. 結束 -> 清理資源、返回 GS
"""

from typing import Dict, Any, Optional, List, Callable
from datetime import datetime
from enum import Enum
import uuid
import traceback

from core.working_context import working_context_manager, ContextType
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


class TaskStep:
    """任務步驟記錄"""
    
    def __init__(self, step_id: str, step_name: str, step_type: str):
        self.step_id = step_id
        self.step_name = step_name
        self.step_type = step_type
        self.status = "pending"
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.retry_count = 0
        self.max_retries = 3
        
    def start_execution(self):
        """開始執行步驟"""
        self.status = "executing"
        self.started_at = datetime.now()
        
    def complete_execution(self, result: Dict[str, Any]):
        """完成執行"""
        self.status = "completed"
        self.completed_at = datetime.now()
        self.result = result
        
    def fail_execution(self, error: str):
        """執行失敗"""
        self.status = "failed"
        self.completed_at = datetime.now()
        self.error = error
        
    def can_retry(self) -> bool:
        """檢查是否可以重試"""
        return self.retry_count < self.max_retries
        
    def retry(self):
        """重試執行"""
        if self.can_retry():
            self.retry_count += 1
            self.status = "pending"
            self.error = None
            return True
        return False
        
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
            "retry_count": self.retry_count
        }


class WorkflowSession:
    """工作流會話 (重構版本)"""
    
    def __init__(self, session_id: str, gs_session_id: str, 
                 task_type: WSTaskType, task_definition: Dict[str, Any]):
        self.session_id = session_id
        self.gs_session_id = gs_session_id
        self.task_type = task_type
        self.task_definition = task_definition
        
        self.status = WSStatus.INITIALIZING
        self.created_at = datetime.now()
        self.last_activity = self.created_at
        
        # 任務執行相關
        self.task_steps: List[TaskStep] = []
        self.current_step_index = 0
        self.execution_context: Dict[str, Any] = {}
        self.task_result: Optional[Dict[str, Any]] = None
        
        # 會話配置
        self.config = {
            "timeout_seconds": 300,  # 5 分鐘超時
            "auto_retry": True,
            "parallel_execution": False,
            "save_intermediate_results": True
        }
        
        # 執行器映射
        self.step_executors: Dict[str, Callable] = {}
        
        # 初始化會話
        self._initialize_session()
        
    def _initialize_session(self):
        """初始化會話"""
        try:
            # 1. 解析任務定義
            self._parse_task_definition()
            
            # 2. 設定執行器
            self._setup_step_executors()
            
            # 3. 設定 Working Context
            self._setup_working_context()
            
            self.status = WSStatus.READY
            info_log(f"[WorkflowSession] WS 初始化完成: {self.session_id}")
            
        except Exception as e:
            error_log(f"[WorkflowSession] WS 初始化失敗: {e}")
            self.status = WSStatus.FAILED
    
    def _parse_task_definition(self):
        """解析任務定義"""
        task_command = self.task_definition.get("command", "")
        task_params = self.task_definition.get("parameters", {})
        
        # 根據任務類型生成執行步驟
        if self.task_type == WSTaskType.SYSTEM_COMMAND:
            self._generate_system_command_steps(task_command, task_params)
        elif self.task_type == WSTaskType.FILE_OPERATION:
            self._generate_file_operation_steps(task_command, task_params)
        elif self.task_type == WSTaskType.WORKFLOW_AUTOMATION:
            self._generate_workflow_automation_steps(task_command, task_params)
        elif self.task_type == WSTaskType.MODULE_INTEGRATION:
            self._generate_module_integration_steps(task_command, task_params)
        else:
            self._generate_custom_task_steps(task_command, task_params)
        
        debug_log(2, f"[WorkflowSession] 生成任務步驟: {len(self.task_steps)} 步")
    
    def _generate_system_command_steps(self, command: str, params: Dict[str, Any]):
        """生成系統命令步驟"""
        steps = [
            TaskStep("validate_command", "驗證命令", "validation"),
            TaskStep("prepare_environment", "準備環境", "preparation"),
            TaskStep("execute_command", "執行命令", "execution"),
            TaskStep("process_result", "處理結果", "processing")
        ]
        self.task_steps.extend(steps)
    
    def _generate_file_operation_steps(self, command: str, params: Dict[str, Any]):
        """生成文件操作步驟"""
        steps = [
            TaskStep("validate_paths", "驗證路徑", "validation"),
            TaskStep("check_permissions", "檢查權限", "validation"),
            TaskStep("perform_operation", "執行操作", "execution"),
            TaskStep("verify_result", "驗證結果", "verification")
        ]
        self.task_steps.extend(steps)
    
    def _generate_workflow_automation_steps(self, command: str, params: Dict[str, Any]):
        """生成工作流自動化步驟"""
        steps = [
            TaskStep("analyze_workflow", "分析工作流", "analysis"),
            TaskStep("prepare_modules", "準備模組", "preparation"),
            TaskStep("execute_workflow", "執行工作流", "execution"),
            TaskStep("collect_results", "收集結果", "collection")
        ]
        self.task_steps.extend(steps)
    
    def _generate_module_integration_steps(self, command: str, params: Dict[str, Any]):
        """生成模組整合步驟"""
        steps = [
            TaskStep("identify_modules", "識別模組", "identification"),
            TaskStep("setup_integration", "設定整合", "setup"),
            TaskStep("perform_integration", "執行整合", "execution"),
            TaskStep("validate_integration", "驗證整合", "validation")
        ]
        self.task_steps.extend(steps)
    
    def _generate_custom_task_steps(self, command: str, params: Dict[str, Any]):
        """生成自定義任務步驟"""
        steps = [
            TaskStep("parse_custom_task", "解析自定義任務", "parsing"),
            TaskStep("execute_custom_logic", "執行自定義邏輯", "execution"),
            TaskStep("format_custom_result", "格式化自定義結果", "formatting")
        ]
        self.task_steps.extend(steps)
    
    def _setup_step_executors(self):
        """設定步驟執行器"""
        self.step_executors = {
            # 驗證類執行器
            "validate_command": self._execute_validate_command,
            "validate_paths": self._execute_validate_paths,
            "check_permissions": self._execute_check_permissions,
            
            # 準備類執行器
            "prepare_environment": self._execute_prepare_environment,
            "prepare_modules": self._execute_prepare_modules,
            "setup_integration": self._execute_setup_integration,
            
            # 執行類執行器
            "execute_command": self._execute_command,
            "perform_operation": self._execute_file_operation,
            "execute_workflow": self._execute_workflow,
            "perform_integration": self._execute_integration,
            
            # 處理類執行器
            "process_result": self._execute_process_result,
            "verify_result": self._execute_verify_result,
            "collect_results": self._execute_collect_results,
            
            # 其他執行器
            "analyze_workflow": self._execute_analyze_workflow,
            "identify_modules": self._execute_identify_modules,
            "validate_integration": self._execute_validate_integration,
            
            # 自定義執行器
            "parse_custom_task": self._execute_parse_custom_task,
            "execute_custom_logic": self._execute_custom_logic,
            "format_custom_result": self._execute_format_custom_result
        }
    
    def _setup_working_context(self):
        """設定 Working Context"""
        workflow_context = {
            "session_id": self.session_id,
            "gs_session_id": self.gs_session_id,
            "task_type": self.task_type.value,
            "task_definition": self.task_definition,
            "execution_mode": "workflow",
            "step_count": len(self.task_steps)
        }
        
        working_context_manager.set_data(
            ContextType.SYS_WORKFLOW,
            "workflow_session",
            workflow_context
        )
    
    def start_execution(self) -> bool:
        """開始執行任務"""
        if self.status != WSStatus.READY:
            error_log(f"[WorkflowSession] WS 狀態不正確，無法開始執行: {self.status}")
            return False
        
        try:
            self.status = WSStatus.EXECUTING
            self.last_activity = datetime.now()
            self.current_step_index = 0
            
            info_log(f"[WorkflowSession] 開始執行任務: {self.session_id}")
            return True
            
        except Exception as e:
            error_log(f"[WorkflowSession] 開始執行失敗: {e}")
            self.status = WSStatus.FAILED
            return False
    
    def execute_next_step(self) -> Dict[str, Any]:
        """執行下一步驟"""
        if self.status != WSStatus.EXECUTING:
            return {
                "success": False,
                "error": f"會話狀態不正確: {self.status}"
            }
        
        if self.current_step_index >= len(self.task_steps):
            # 所有步驟完成
            return self._complete_execution()
        
        try:
            current_step = self.task_steps[self.current_step_index]
            
            # 開始執行步驟
            current_step.start_execution()
            self.last_activity = datetime.now()
            
            # 獲取執行器
            executor = self.step_executors.get(current_step.step_id)
            if not executor:
                raise Exception(f"找不到步驟執行器: {current_step.step_id}")
            
            # 執行步驟
            result = executor(current_step)
            
            if result.get("success", False):
                # 步驟執行成功
                current_step.complete_execution(result)
                self.current_step_index += 1
                
                info_log(f"[WorkflowSession] 步驟執行成功: {current_step.step_name}")
                
                # 如果還有下一步，返回繼續信號
                if self.current_step_index < len(self.task_steps):
                    return {
                        "success": True,
                        "step_completed": current_step.to_dict(),
                        "has_next_step": True,
                        "next_step": self.task_steps[self.current_step_index].step_name
                    }
                else:
                    # 最後一步完成，結束執行
                    return self._complete_execution()
            else:
                # 步驟執行失敗
                error_msg = result.get("error", "未知錯誤")
                current_step.fail_execution(error_msg)
                
                # 檢查是否可以重試
                if self.config["auto_retry"] and current_step.can_retry():
                    current_step.retry()
                    error_log(f"[WorkflowSession] 步驟失敗，重試中: {current_step.step_name}")
                    return self.execute_next_step()  # 遞歸重試
                else:
                    # 無法重試，標記為失敗
                    self.status = WSStatus.FAILED
                    error_log(f"[WorkflowSession] 步驟執行失敗: {current_step.step_name}, 錯誤: {error_msg}")
                    return {
                        "success": False,
                        "error": error_msg,
                        "failed_step": current_step.to_dict()
                    }
                    
        except Exception as e:
            error_log(f"[WorkflowSession] 執行步驟時發生異常: {e}")
            traceback.print_exc()
            
            current_step = self.task_steps[self.current_step_index]
            current_step.fail_execution(str(e))
            self.status = WSStatus.FAILED
            
            return {
                "success": False,
                "error": str(e),
                "failed_step": current_step.to_dict()
            }
    
    def _complete_execution(self) -> Dict[str, Any]:
        """完成執行"""
        try:
            # 收集所有步驟結果
            step_results = [step.to_dict() for step in self.task_steps]
            
            # 生成任務結果
            self.task_result = {
                "session_id": self.session_id,
                "task_type": self.task_type.value,
                "execution_summary": {
                    "total_steps": len(self.task_steps),
                    "completed_steps": len([s for s in self.task_steps if s.status == "completed"]),
                    "failed_steps": len([s for s in self.task_steps if s.status == "failed"]),
                    "total_duration": (datetime.now() - self.created_at).total_seconds()
                },
                "step_results": step_results,
                "final_status": "completed"
            }
            
            self.status = WSStatus.COMPLETED
            self.last_activity = datetime.now()
            
            info_log(f"[WorkflowSession] 任務執行完成: {self.session_id}")
            
            return {
                "success": True,
                "execution_completed": True,
                "task_result": self.task_result
            }
            
        except Exception as e:
            error_log(f"[WorkflowSession] 完成執行時發生錯誤: {e}")
            self.status = WSStatus.FAILED
            return {
                "success": False,
                "error": str(e)
            }
    
    # 步驟執行器實現 (簡化版本，實際使用時需要完整實現)
    def _execute_validate_command(self, step: TaskStep) -> Dict[str, Any]:
        """驗證命令執行器"""
        command = self.task_definition.get("command", "")
        if command.strip():
            return {"success": True, "validated_command": command}
        else:
            return {"success": False, "error": "命令為空"}
    
    def _execute_validate_paths(self, step: TaskStep) -> Dict[str, Any]:
        """驗證路徑執行器"""
        # 模擬路徑驗證
        return {"success": True, "paths_valid": True}
    
    def _execute_check_permissions(self, step: TaskStep) -> Dict[str, Any]:
        """檢查權限執行器"""
        # 模擬權限檢查
        return {"success": True, "permissions_ok": True}
    
    def _execute_prepare_environment(self, step: TaskStep) -> Dict[str, Any]:
        """準備環境執行器"""
        # 模擬環境準備
        return {"success": True, "environment_ready": True}
    
    def _execute_prepare_modules(self, step: TaskStep) -> Dict[str, Any]:
        """準備模組執行器"""
        # 模擬模組準備
        return {"success": True, "modules_prepared": True}
    
    def _execute_setup_integration(self, step: TaskStep) -> Dict[str, Any]:
        """設定整合執行器"""
        # 模擬整合設定
        return {"success": True, "integration_setup": True}
    
    def _execute_command(self, step: TaskStep) -> Dict[str, Any]:
        """執行命令執行器"""
        command = self.task_definition.get("command", "")
        
        # 透過 Working Context 發送命令執行請求
        execution_request = {
            "action": "execute_system_command",
            "command": command,
            "session_id": self.session_id,
            "context": self.execution_context
        }
        
        working_context_manager.set_data(
            ContextType.SYS_COMMAND,
            "command_execution",
            execution_request
        )
        
        # 模擬命令執行結果
        return {
            "success": True,
            "command_output": f"模擬執行命令: {command}",
            "exit_code": 0
        }
    
    def _execute_file_operation(self, step: TaskStep) -> Dict[str, Any]:
        """執行文件操作執行器"""
        return {"success": True, "file_operation_completed": True}
    
    def _execute_workflow(self, step: TaskStep) -> Dict[str, Any]:
        """執行工作流執行器"""
        return {"success": True, "workflow_executed": True}
    
    def _execute_integration(self, step: TaskStep) -> Dict[str, Any]:
        """執行整合執行器"""
        return {"success": True, "integration_completed": True}
    
    def _execute_process_result(self, step: TaskStep) -> Dict[str, Any]:
        """處理結果執行器"""
        return {"success": True, "result_processed": True}
    
    def _execute_verify_result(self, step: TaskStep) -> Dict[str, Any]:
        """驗證結果執行器"""
        return {"success": True, "result_verified": True}
    
    def _execute_collect_results(self, step: TaskStep) -> Dict[str, Any]:
        """收集結果執行器"""
        return {"success": True, "results_collected": True}
    
    def _execute_analyze_workflow(self, step: TaskStep) -> Dict[str, Any]:
        """分析工作流執行器"""
        return {"success": True, "workflow_analyzed": True}
    
    def _execute_identify_modules(self, step: TaskStep) -> Dict[str, Any]:
        """識別模組執行器"""
        return {"success": True, "modules_identified": True}
    
    def _execute_validate_integration(self, step: TaskStep) -> Dict[str, Any]:
        """驗證整合執行器"""
        return {"success": True, "integration_validated": True}
    
    def _execute_parse_custom_task(self, step: TaskStep) -> Dict[str, Any]:
        """解析自定義任務執行器"""
        return {"success": True, "custom_task_parsed": True}
    
    def _execute_custom_logic(self, step: TaskStep) -> Dict[str, Any]:
        """執行自定義邏輯執行器"""
        return {"success": True, "custom_logic_executed": True}
    
    def _execute_format_custom_result(self, step: TaskStep) -> Dict[str, Any]:
        """格式化自定義結果執行器"""
        return {"success": True, "custom_result_formatted": True}
    
    def pause_execution(self):
        """暫停執行"""
        if self.status == WSStatus.EXECUTING:
            self.status = WSStatus.PAUSED
            info_log(f"[WorkflowSession] WS 已暫停: {self.session_id}")
    
    def resume_execution(self):
        """恢復執行"""
        if self.status == WSStatus.PAUSED:
            self.status = WSStatus.EXECUTING
            self.last_activity = datetime.now()
            info_log(f"[WorkflowSession] WS 已恢復: {self.session_id}")
    
    def cancel_execution(self):
        """取消執行"""
        if self.status in [WSStatus.EXECUTING, WSStatus.PAUSED, WSStatus.WAITING]:
            self.status = WSStatus.CANCELLED
            info_log(f"[WorkflowSession] WS 已取消: {self.session_id}")
    
    def get_progress(self) -> Dict[str, Any]:
        """獲取執行進度"""
        total_steps = len(self.task_steps)
        completed_steps = len([s for s in self.task_steps if s.status == "completed"])
        
        return {
            "session_id": self.session_id,
            "status": self.status.value,
            "progress": {
                "total_steps": total_steps,
                "completed_steps": completed_steps,
                "current_step": self.current_step_index,
                "progress_percentage": (completed_steps / total_steps * 100) if total_steps > 0 else 0
            },
            "current_step_info": self.task_steps[self.current_step_index].to_dict() if self.current_step_index < total_steps else None,
            "execution_time": (datetime.now() - self.created_at).total_seconds()
        }
    
    def get_session_info(self) -> Dict[str, Any]:
        """獲取會話信息"""
        return {
            "session_id": self.session_id,
            "gs_session_id": self.gs_session_id,
            "task_type": self.task_type.value,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "task_definition": self.task_definition,
            "progress": self.get_progress()["progress"],
            "config": self.config
        }


class WorkflowSessionManager:
    """WS 管理器 (重構版本)"""
    
    def __init__(self):
        self.active_sessions: Dict[str, WorkflowSession] = {}
        self.session_history: List[Dict[str, Any]] = []
        
        info_log("[WorkflowSessionManager] WS 管理器初始化完成")
    
    def create_session(self, gs_session_id: str, task_type: WSTaskType, 
                      task_definition: Dict[str, Any]) -> Optional[WorkflowSession]:
        """創建新的 WS"""
        try:
            session_id = f"ws_{gs_session_id}_{len(self.active_sessions) + 1}"
            
            new_session = WorkflowSession(session_id, gs_session_id, task_type, task_definition)
            
            if new_session.status == WSStatus.READY:
                self.active_sessions[session_id] = new_session
                info_log(f"[WorkflowSessionManager] 創建 WS: {session_id}")
                return new_session
            else:
                error_log(f"[WorkflowSessionManager] WS 創建失敗: {session_id}")
                return None
                
        except Exception as e:
            error_log(f"[WorkflowSessionManager] 創建 WS 時發生錯誤: {e}")
            return None
    
    def get_session(self, session_id: str) -> Optional[WorkflowSession]:
        """獲取 WS"""
        return self.active_sessions.get(session_id)
    
    def end_session(self, session_id: str) -> bool:
        """結束 WS"""
        session = self.active_sessions.get(session_id)
        if session:
            session_info = session.get_session_info()
            session_info["final_result"] = session.task_result
            
            self.session_history.append(session_info)
            del self.active_sessions[session_id]
            
            # 清理 Working Context
            working_context_manager.clear_data(ContextType.SYS_WORKFLOW, "workflow_session")
            
            info_log(f"[WorkflowSessionManager] 結束 WS: {session_id}")
            return True
        return False
    
    def get_active_sessions(self) -> List[WorkflowSession]:
        """獲取所有活躍 WS"""
        return list(self.active_sessions.values())
    
    def cleanup_completed_sessions(self):
        """清理已完成的會話"""
        completed_sessions = []
        
        for session_id, session in self.active_sessions.items():
            if session.status in [WSStatus.COMPLETED, WSStatus.FAILED, WSStatus.CANCELLED]:
                completed_sessions.append(session_id)
        
        for session_id in completed_sessions:
            self.end_session(session_id)
            info_log(f"[WorkflowSessionManager] 清理已完成 WS: {session_id}")


# 全域 WS 管理器實例
workflow_session_manager = WorkflowSessionManager()