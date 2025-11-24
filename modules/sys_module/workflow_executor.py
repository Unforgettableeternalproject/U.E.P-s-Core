"""
modules/sys_module/workflow_executor.py

背景工作流執行器 - 用於 BACKGROUND 模式的工作流

與 MonitoringThreadPool 的區別：
- WorkflowExecutor: 執行有限步驟的背景工作流（會完成）
- MonitoringThreadPool: 持續運行的監控任務（無限循環）
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, Any, Optional
from enum import Enum

from utils.debug_helper import debug_log, info_log, error_log


class WorkflowStatus(str, Enum):
    """工作流狀態"""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BackgroundWorkflowExecutor:
    """
    背景工作流執行器
    
    用於在背景線程中執行 BACKGROUND 模式的工作流。
    只負責執行工作流步驟，不負責監控任務。
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
        """初始化執行器"""
        if self._initialized:
            return
        
        self.executor = ThreadPoolExecutor(
            max_workers=5,
            thread_name_prefix="WorkflowBG"
        )
        self.active_workflows: Dict[str, Dict[str, Any]] = {}  # task_id -> task_info
        self._initialized = True
        
        info_log("[WorkflowExecutor] 背景工作流執行器已初始化（max_workers=5）")
    
    def submit_workflow(
        self,
        workflow_engine,
        workflow_type: str,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        提交工作流到背景執行
        
        Args:
            workflow_engine: WorkflowEngine 實例
            workflow_type: 工作流類型
            session_id: 會話 ID（可選）
            metadata: 額外元數據
            
        Returns:
            task_id: 任務唯一識別碼
        """
        # 生成唯一任務 ID
        task_id = f"workflow_{workflow_type}_{uuid.uuid4().hex[:8]}"
        
        # 記錄任務信息
        task_info = {
            "task_id": task_id,
            "workflow_type": workflow_type,
            "session_id": session_id,
            "status": WorkflowStatus.QUEUED,
            "start_time": None,
            "end_time": None,
            "result": None,
            "error": None,
            "metadata": metadata or {}
        }
        
        self.active_workflows[task_id] = task_info
        
        # 定義執行函數
        def execute_workflow():
            """在背景線程中執行工作流"""
            try:
                # 更新狀態
                task_info["status"] = WorkflowStatus.RUNNING
                task_info["start_time"] = datetime.now()
                
                info_log(f"[WorkflowExecutor] 開始執行背景工作流: {task_id}")
                
                # 執行工作流引擎（自動推進模式）
                max_iterations = 100  # 防止無限循環
                iteration = 0
                final_result = None
                
                while iteration < max_iterations:
                    iteration += 1
                    
                    # 處理當前步驟（空輸入，自動模式）
                    step_result = workflow_engine.process_input("")
                    
                    # 檢查是否完成
                    if step_result.complete:
                        final_result = step_result
                        break
                    elif step_result.cancel:
                        raise Exception(f"工作流被取消: {step_result.message}")
                    elif not step_result.success:
                        raise Exception(f"工作流步驟失敗: {step_result.message}")
                    
                    # 檢查當前步驟是否需要用戶輸入
                    current_step = workflow_engine.get_current_step()
                    if current_step and current_step.step_type == current_step.STEP_TYPE_INTERACTIVE:
                        # 背景工作流不應該有互動步驟
                        raise Exception(f"背景工作流不能有互動步驟: {current_step.id}")
                
                if iteration >= max_iterations:
                    raise Exception("工作流超過最大迭代次數（可能是無限循環）")
                
                # 成功完成
                task_info["status"] = WorkflowStatus.COMPLETED
                task_info["end_time"] = datetime.now()
                task_info["result"] = final_result.data if final_result else {}
                
                # 🔧 提取已執行的步驟列表
                step_history = workflow_engine.session.get_data("step_history", [])
                completed_steps = [step["step_id"] for step in step_history if "step_id" in step]
                
                info_log(f"[WorkflowExecutor] 工作流完成: {task_id}（執行了 {iteration} 步）")
                info_log(f"[WorkflowExecutor] 完成的步驟: {completed_steps}")
                
                # 發布完成事件
                try:
                    from core.event_bus import event_bus, SystemEvent
                    if event_bus:
                        event_bus.publish(
                            SystemEvent.BACKGROUND_WORKFLOW_COMPLETED,
                            {
                                "task_id": task_id,
                                "workflow_type": workflow_type,
                                "session_id": session_id,
                                "result": task_info["result"],
                                "completed_steps": completed_steps  # ✅ 包含已完成步驟列表
                            },
                            source="sys"
                        )
                except Exception as e:
                    error_log(f"[WorkflowExecutor] 發布完成事件失敗: {e}")
                
            except Exception as e:
                # 執行失敗
                task_info["status"] = WorkflowStatus.FAILED
                task_info["end_time"] = datetime.now()
                task_info["error"] = str(e)
                
                error_log(f"[WorkflowExecutor] 工作流失敗: {task_id}, 錯誤: {e}")
                
                # 發布失敗事件
                try:
                    from core.event_bus import event_bus, SystemEvent
                    if event_bus:
                        event_bus.publish(
                            SystemEvent.BACKGROUND_WORKFLOW_FAILED,
                            {
                                "task_id": task_id,
                                "workflow_type": workflow_type,
                                "session_id": session_id,
                                "error": str(e)
                            },
                            source="sys"
                        )
                except Exception as event_error:
                    error_log(f"[WorkflowExecutor] 發布失敗事件失敗: {event_error}")
        
        # 提交到線程池
        self.executor.submit(execute_workflow)
        
        info_log(f"[WorkflowExecutor] 已提交背景工作流: {workflow_type} (task_id: {task_id})")
        return task_id
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        獲取任務狀態
        
        Args:
            task_id: 任務 ID
            
        Returns:
            任務信息字典，如果不存在則返回 None
        """
        return self.active_workflows.get(task_id)
    
    def cancel_task(self, task_id: str) -> bool:
        """
        取消任務（注意：無法強制停止正在運行的線程）
        
        Args:
            task_id: 任務 ID
            
        Returns:
            是否成功標記為取消
        """
        if task_id not in self.active_workflows:
            debug_log(2, f"[WorkflowExecutor] 任務不存在: {task_id}")
            return False
        
        task_info = self.active_workflows[task_id]
        
        # 只能取消 QUEUED 或 RUNNING 狀態的任務
        if task_info["status"] not in [WorkflowStatus.QUEUED, WorkflowStatus.RUNNING]:
            debug_log(2, f"[WorkflowExecutor] 任務狀態不允許取消: {task_info['status']}")
            return False
        
        # 更新狀態
        task_info["status"] = WorkflowStatus.CANCELLED
        task_info["end_time"] = datetime.now()
        
        info_log(f"[WorkflowExecutor] 已標記任務為取消: {task_id}")
        return True
    
    def cleanup_completed_tasks(self, max_history: int = 100):
        """
        清理已完成的任務
        
        Args:
            max_history: 保留的最大歷史記錄數量
        """
        # 獲取已完成的任務
        completed_tasks = [
            task for task in self.active_workflows.values()
            if task["status"] in [WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED]
        ]
        
        if len(completed_tasks) <= max_history:
            return
        
        # 按結束時間排序（最舊的在前）
        completed_tasks.sort(key=lambda t: t["end_time"] or datetime.min)
        
        # 移除最舊的任務
        tasks_to_remove = completed_tasks[:len(completed_tasks) - max_history]
        for task in tasks_to_remove:
            del self.active_workflows[task["task_id"]]
            debug_log(3, f"[WorkflowExecutor] 清理舊任務: {task['task_id']}")
        
        debug_log(2, f"[WorkflowExecutor] 清理了 {len(tasks_to_remove)} 個舊任務")


# 全局實例
_executor = None

def get_workflow_executor() -> BackgroundWorkflowExecutor:
    """
    獲取全局背景工作流執行器實例
    
    Returns:
        BackgroundWorkflowExecutor 實例
    """
    global _executor
    
    if _executor is None:
        _executor = BackgroundWorkflowExecutor()
    
    return _executor
