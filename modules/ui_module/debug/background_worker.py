# debug/background_worker.py
"""
Background Worker

背景工作線程管理器
用於處理費時操作，避免UI主線程阻塞
"""

import os
import sys
import threading
import queue
import time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List, Callable
from enum import Enum
from dataclasses import dataclass, field

try:
    from PyQt5.QtCore import QObject, pyqtSignal, QThread
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    QObject = object
    pyqtSignal = None

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, info_log, error_log


class TaskStatus(str, Enum):
    """Background task status enumeration"""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BackgroundWorkflowTask:
    """
    Background workflow task data structure
    
    Tracks workflow execution in background thread with status, progress, and metadata.
    """
    task_id: str
    workflow_type: str
    session_id: Optional[str] = None
    status: TaskStatus = TaskStatus.QUEUED
    progress: float = 0.0  # 0.0 - 1.0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    error_message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "task_id": self.task_id,
            "workflow_type": self.workflow_type,
            "session_id": self.session_id,
            "status": self.status.value,
            "progress": self.progress,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "error_message": self.error_message,
            "result": self.result,
            "metadata": self.metadata
        }


class WorkerSignals(QObject):
    """
    定義工作線程的信號
    """
    started = pyqtSignal(str) if pyqtSignal else None
    finished = pyqtSignal(str, object) if pyqtSignal else None
    error = pyqtSignal(str, str) if pyqtSignal else None
    progress = pyqtSignal(str, int, str) if pyqtSignal else None


class BackgroundWorker(QThread if PYQT5_AVAILABLE else object):
    """
    背景工作線程類
    """
    
    def __init__(self, task_id: str, task_func: Callable, *args, **kwargs):
        """
        初始化工作線程
        
        Args:
            task_id: 任務ID
            task_func: 要執行的任務函數
            args, kwargs: 傳遞給任務函數的參數
        """
        if PYQT5_AVAILABLE:
            super(QThread, self).__init__()
        else:
            super().__init__()
            
        self.task_id = task_id
        self.task_func = task_func
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals() if PYQT5_AVAILABLE else None
        self._is_stopped = False
        
        debug_log(1, f"[BackgroundWorker] 初始化工作線程 {task_id}")
    
    def _ensure_signals_valid(self):
        """確保signals對象有效，如果無效則重新創建"""
        if not PYQT5_AVAILABLE:
            return False
            
        try:
            # 嘗試檢查signals是否仍然有效
            if self.signals is None:
                self.signals = WorkerSignals()
                debug_log(1, f"[BackgroundWorker] 重新創建signals對象")
                return True
            
            # 檢查signals對象是否仍然可用
            # 如果對象已被刪除，訪問其屬性會引發RuntimeError
            _ = self.signals.started
            return True
        except (RuntimeError, AttributeError) as e:
            # signals對象已被刪除或無效，重新創建
            try:
                self.signals = WorkerSignals()
                debug_log(1, f"[BackgroundWorker] 重新創建無效的signals對象: {e}")
                return True
            except Exception as create_error:
                error_log(f"[BackgroundWorker] 無法重新創建signals對象: {create_error}")
                self.signals = None
                return False
    
    def run(self):
        """執行工作線程"""
        if not PYQT5_AVAILABLE:
            error_log("[BackgroundWorker] PyQt5 未安裝，無法執行背景工作")
            return
            
        debug_log(1, f"[BackgroundWorker] 啟動工作線程 {self.task_id}")
        
        # 確保signals對象有效
        if not self._ensure_signals_valid():
            error_log("[BackgroundWorker] 無法確保signals對象有效，無法執行工作")
            return
        
        # 發送開始信號
        try:
            if self.signals and self.signals.started:
                self.signals.started.emit(self.task_id)
        except Exception as e:
            error_log(f"[BackgroundWorker] 發送開始信號失敗: {e}")
        
        result = None
        try:
            # 執行任務函數
            result = self.task_func(*self.args, **self.kwargs)
            
            # 發送完成信號
            if self.signals and self.signals.finished and not self._is_stopped:
                self.signals.finished.emit(self.task_id, result)
                
        except Exception as e:
            error_log(f"[BackgroundWorker] 工作線程 {self.task_id} 執行異常: {e}")
            
            # 發送錯誤信號
            if self.signals and self.signals.error and not self._is_stopped:
                self.signals.error.emit(self.task_id, str(e))
    
    def stop(self):
        """停止工作線程"""
        self._is_stopped = True
        if PYQT5_AVAILABLE and hasattr(self, 'quit'):
            self.quit()
            self.wait()
        debug_log(1, f"[BackgroundWorker] 停止工作線程 {self.task_id}")


class BackgroundWorkerManager:
    """
    背景工作線程管理器
    """
    
    def __init__(self):
        """初始化管理器"""
        self.workers = {}
        self.workflow_tasks: Dict[str, BackgroundWorkflowTask] = {}  # Track workflow tasks
        self.max_workers = 5
        self.signals = WorkerSignals() if PYQT5_AVAILABLE else None
        
        debug_log(1, "[BackgroundWorkerManager] 初始化工作線程管理器")
    
    def _ensure_signals_valid(self):
        """確保管理器的signals對象有效"""
        if not PYQT5_AVAILABLE:
            return False
            
        try:
            if self.signals is None:
                self.signals = WorkerSignals()
                debug_log(1, "[BackgroundWorkerManager] 重新創建signals對象")
                return True
            
            # 檢查signals對象是否仍然可用
            _ = self.signals.started
            return True
        except (RuntimeError, AttributeError) as e:
            try:
                self.signals = WorkerSignals()
                debug_log(1, f"[BackgroundWorkerManager] 重新創建無效的signals對象: {e}")
                return True
            except Exception as create_error:
                error_log(f"[BackgroundWorkerManager] 無法重新創建signals對象: {create_error}")
                self.signals = None
                return False
    
    def start_task(self, task_id: str, task_func: Callable, *args, **kwargs) -> bool:
        """
        啟動新的背景任務
        
        Args:
            task_id: 任務ID
            task_func: 要執行的任務函數
            args, kwargs: 傳遞給任務函數的參數
            
        Returns:
            是否成功啟動任務
        """
        if not PYQT5_AVAILABLE:
            error_log("[BackgroundWorkerManager] PyQt5 未安裝，無法啟動背景任務")
            return False
            
        # 檢查任務ID是否已存在
        if task_id in self.workers:
            error_log(f"[BackgroundWorkerManager] 任務ID {task_id} 已存在")
            return False
            
        # 檢查是否超過最大工作線程數
        if len(self.workers) >= self.max_workers:
            error_log("[BackgroundWorkerManager] 已達到最大工作線程數量")
            return False
        
        try:
            # 確保管理器的signals對象有效
            if not self._ensure_signals_valid():
                error_log("[BackgroundWorkerManager] 無法確保signals對象有效")
                return False
            
            # 建立並啟動工作線程
            worker = BackgroundWorker(task_id, task_func, *args, **kwargs)
            
            # 連接信號 - 添加異常處理
            if worker.signals:
                try:
                    if worker.signals.started:
                        worker.signals.started.connect(self._handle_task_started)
                    if worker.signals.finished:
                        worker.signals.finished.connect(self._handle_task_finished)
                    if worker.signals.error:
                        worker.signals.error.connect(self._handle_task_error)
                except Exception as e:
                    error_log(f"[BackgroundWorkerManager] 連接工作線程信號失敗: {e}")
                    # 繼續執行，因為信號連接失敗不應該阻止任務執行
                    
            # 儲存工作線程
            self.workers[task_id] = worker
            
            # 啟動工作線程
            worker.start()
            
            debug_log(1, f"[BackgroundWorkerManager] 已啟動任務 {task_id}")
            return True
            
        except Exception as e:
            error_log(f"[BackgroundWorkerManager] 啟動任務 {task_id} 失敗: {e}")
            return False
    
    def stop_task(self, task_id: str) -> bool:
        """
        停止指定的背景任務
        
        Args:
            task_id: 任務ID
            
        Returns:
            是否成功停止任務
        """
        if task_id not in self.workers:
            # 使用debug_log而非error_log，因為這是常見的情況
            debug_log(1, f"[BackgroundWorkerManager] 任務ID {task_id} 不存在，或已經完成")
            return False
            
        try:
            # 停止工作線程
            worker = self.workers[task_id]
            worker.stop()
            
            # 移除工作線程
            del self.workers[task_id]
            
            debug_log(1, f"[BackgroundWorkerManager] 已停止任務 {task_id}")
            return True
            
        except Exception as e:
            error_log(f"[BackgroundWorkerManager] 停止任務 {task_id} 失敗: {e}")
            return False
    
    def stop_all_tasks(self):
        """停止所有背景任務"""
        debug_log(1, "[BackgroundWorkerManager] 正在停止所有背景任務")
        
        # 複製任務ID列表，避免在迭代過程中修改字典
        task_ids = list(self.workers.keys())
        
        # 停止所有任務
        for task_id in task_ids:
            self.stop_task(task_id)
    
    def _handle_task_started(self, task_id: str):
        """處理任務開始信號"""
        debug_log(1, f"[BackgroundWorkerManager] 任務 {task_id} 已開始")
        
        # 轉發信號
        if self.signals and self.signals.started:
            self.signals.started.emit(task_id)
    
    def _handle_task_finished(self, task_id: str, result: Any):
        """處理任務完成信號"""
        debug_log(1, f"[BackgroundWorkerManager] 任務 {task_id} 已完成")
        
        # 移除工作線程
        if task_id in self.workers:
            del self.workers[task_id]
        
        # 轉發信號
        if self.signals and self.signals.finished:
            self.signals.finished.emit(task_id, result)
    
    def _handle_task_error(self, task_id: str, error_msg: str):
        """處理任務錯誤信號"""
        error_log(f"[BackgroundWorkerManager] 任務 {task_id} 發生錯誤: {error_msg}")
        
        # Update workflow task status if it exists
        if task_id in self.workflow_tasks:
            task = self.workflow_tasks[task_id]
            task.status = TaskStatus.FAILED
            task.error_message = error_msg
            task.end_time = datetime.now()
            debug_log(2, f"[BackgroundWorkerManager] Workflow task {task_id} marked as FAILED")
        
        # 移除工作線程
        if task_id in self.workers:
            del self.workers[task_id]
        
        # 轉發信號
        if self.signals and self.signals.error:
            self.signals.error.emit(task_id, error_msg)
    
    # ==================== Workflow-specific methods ====================
    
    def submit_workflow(self, workflow_engine, workflow_type: str, 
                       session_id: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Submit workflow to background execution
        
        Args:
            workflow_engine: WorkflowEngine instance to execute
            workflow_type: Type identifier for the workflow
            session_id: Associated session ID (optional)
            metadata: Additional metadata (optional)
            
        Returns:
            task_id: Unique identifier for the background task
        """
        # Generate unique task ID
        task_id = f"workflow_{workflow_type}_{uuid.uuid4().hex[:8]}"
        
        # Create workflow task record
        workflow_task = BackgroundWorkflowTask(
            task_id=task_id,
            workflow_type=workflow_type,
            session_id=session_id,
            status=TaskStatus.QUEUED,
            metadata=metadata or {}
        )
        
        self.workflow_tasks[task_id] = workflow_task
        
        # Create wrapper function for workflow execution
        def workflow_executor():
            """Execute workflow in background thread"""
            try:
                # Update status to RUNNING
                workflow_task.status = TaskStatus.RUNNING
                workflow_task.start_time = datetime.now()
                debug_log(2, f"[BackgroundWorkerManager] Starting workflow execution: {task_id}")
                
                # Execute workflow engine in automated mode
                # Background workflows must be non-interactive - process all steps automatically
                final_result = None
                max_iterations = 100  # Prevent infinite loops
                iteration = 0
                
                while iteration < max_iterations:
                    iteration += 1
                    
                    # Process current step with empty input (automated mode)
                    step_result = workflow_engine.process_input("")
                    
                    # Update progress
                    workflow_task.progress = min(0.1 + (iteration * 0.01), 0.9)
                    
                    # Check if workflow is complete
                    if step_result.complete:
                        final_result = step_result
                        break
                    elif step_result.cancel:
                        raise Exception(f"Workflow cancelled: {step_result.message}")
                    elif not step_result.success:
                        raise Exception(f"Workflow step failed: {step_result.message}")
                    
                    # Check if current step requires user input
                    current_step = workflow_engine.get_current_step()
                    if current_step and current_step.step_type == current_step.STEP_TYPE_INTERACTIVE:
                        # Background workflows should not have interactive steps
                        raise Exception(f"Background workflow cannot have interactive step: {current_step.id}")
                
                if iteration >= max_iterations:
                    raise Exception("Workflow exceeded maximum iterations (possible infinite loop)")
                
                # Update task with result
                workflow_task.status = TaskStatus.COMPLETED
                workflow_task.end_time = datetime.now()
                workflow_task.result = final_result.data if final_result else {}
                workflow_task.progress = 1.0
                
                info_log(f"[BackgroundWorkerManager] Workflow {task_id} completed successfully in {iteration} steps")
                
                # Publish event (will be handled by Controller)
                try:
                    from core.event_bus import event_bus, SystemEvent
                    event_bus.publish(SystemEvent.BACKGROUND_WORKFLOW_COMPLETED, {
                        "task_id": task_id,
                        "workflow_type": workflow_type,
                        "session_id": session_id,
                        "result": result
                    })
                except Exception as e:
                    error_log(f"[BackgroundWorkerManager] Failed to publish completion event: {e}")
                
                return result
                
            except Exception as e:
                # Update task with error
                workflow_task.status = TaskStatus.FAILED
                workflow_task.end_time = datetime.now()
                workflow_task.error_message = str(e)
                
                error_log(f"[BackgroundWorkerManager] Workflow {task_id} failed: {e}")
                
                # Publish failure event
                try:
                    from core.event_bus import event_bus, SystemEvent
                    event_bus.publish(SystemEvent.BACKGROUND_WORKFLOW_FAILED, {
                        "task_id": task_id,
                        "workflow_type": workflow_type,
                        "session_id": session_id,
                        "error": str(e)
                    })
                except Exception as event_error:
                    error_log(f"[BackgroundWorkerManager] Failed to publish failure event: {event_error}")
                
                raise
        
        # Start background task
        success = self.start_task(task_id, workflow_executor)
        
        if not success:
            # Failed to start, update status
            workflow_task.status = TaskStatus.FAILED
            workflow_task.error_message = "Failed to start background worker"
            error_log(f"[BackgroundWorkerManager] Failed to start workflow task: {task_id}")
            raise RuntimeError(f"Failed to start background workflow task: {task_id}")
        
        info_log(f"[BackgroundWorkerManager] Submitted workflow {workflow_type} as task {task_id}")
        return task_id
    
    def get_task_status(self, task_id: str) -> Optional[BackgroundWorkflowTask]:
        """
        Get status of background workflow task
        
        Args:
            task_id: Task identifier
            
        Returns:
            BackgroundWorkflowTask or None if not found
        """
        return self.workflow_tasks.get(task_id)
    
    def get_all_tasks(self) -> List[BackgroundWorkflowTask]:
        """
        Get all workflow tasks
        
        Returns:
            List of all BackgroundWorkflowTask objects
        """
        return list(self.workflow_tasks.values())
    
    def get_active_tasks(self) -> List[BackgroundWorkflowTask]:
        """
        Get currently active (QUEUED or RUNNING) workflow tasks
        
        Returns:
            List of active BackgroundWorkflowTask objects
        """
        return [
            task for task in self.workflow_tasks.values()
            if task.status in [TaskStatus.QUEUED, TaskStatus.RUNNING]
        ]
    
    def cancel_task(self, task_id: str) -> bool:
        """
        Cancel background workflow task
        
        Args:
            task_id: Task identifier
            
        Returns:
            True if cancelled successfully, False otherwise
        """
        # Check if task exists
        if task_id not in self.workflow_tasks:
            debug_log(2, f"[BackgroundWorkerManager] Task {task_id} not found in workflow tasks")
            return False
        
        task = self.workflow_tasks[task_id]
        
        # Can only cancel QUEUED or RUNNING tasks
        if task.status not in [TaskStatus.QUEUED, TaskStatus.RUNNING]:
            debug_log(2, f"[BackgroundWorkerManager] Task {task_id} is {task.status.value}, cannot cancel")
            return False
        
        # Update task status
        task.status = TaskStatus.CANCELLED
        task.end_time = datetime.now()
        
        # Stop the background worker
        success = self.stop_task(task_id)
        
        if success:
            info_log(f"[BackgroundWorkerManager] Cancelled workflow task: {task_id}")
        else:
            error_log(f"[BackgroundWorkerManager] Failed to stop worker for task: {task_id}")
        
        return success
    
    def cleanup_completed_tasks(self, max_history: int = 100):
        """
        Clean up old completed/failed/cancelled tasks
        
        Args:
            max_history: Maximum number of completed tasks to keep
        """
        # Get completed tasks sorted by end time
        completed_tasks = [
            task for task in self.workflow_tasks.values()
            if task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ]
        
        if len(completed_tasks) <= max_history:
            return
        
        # Sort by end time (oldest first)
        completed_tasks.sort(key=lambda t: t.end_time or datetime.min)
        
        # Remove oldest tasks
        tasks_to_remove = completed_tasks[:len(completed_tasks) - max_history]
        for task in tasks_to_remove:
            del self.workflow_tasks[task.task_id]
            debug_log(3, f"[BackgroundWorkerManager] Cleaned up old task: {task.task_id}")
        
        debug_log(2, f"[BackgroundWorkerManager] Cleaned up {len(tasks_to_remove)} old tasks")


# 全局工作線程管理器實例
_worker_manager = None

def get_worker_manager() -> BackgroundWorkerManager:
    """
    獲取全局工作線程管理器實例
    
    Returns:
        工作線程管理器實例
    """
    global _worker_manager
    
    if _worker_manager is None:
        _worker_manager = BackgroundWorkerManager()
        
    return _worker_manager
