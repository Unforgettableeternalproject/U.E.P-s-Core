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
from typing import Dict, Any, Optional, List, Callable

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
        
        # 移除工作線程
        if task_id in self.workers:
            del self.workers[task_id]
        
        # 轉發信號
        if self.signals and self.signals.error:
            self.signals.error.emit(task_id, error_msg)


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
