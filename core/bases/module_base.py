# core/module_base.py

from abc import ABC, abstractmethod
import time
import tracemalloc
from typing import Dict, Any


class BaseModule(ABC):
    """所有模組的基本接口"""
    
    def __init__(self):
        """初始化基本屬性"""
        self.is_initialized = False
        self._module_id = self.__class__.__name__  # 模組ID，子類可覆寫
        self._enable_auto_metrics = True  # 是否啟用自動性能追踪
        
    def set_module_id(self, module_id: str):
        """設置模組ID"""
        self._module_id = module_id

    @abstractmethod
    def initialize(self) -> bool:
        """初始化模組，如載入模型、參數等"""
        # 子類別需要在成功初始化後設置 self.is_initialized = True
        pass

    @abstractmethod
    def handle(self, data: dict) -> dict:
        """處理資料並回傳統一格式"""
        pass
    
    def handle_with_metrics(self, data: dict) -> dict:
        """
        包裝 handle 方法，自動追踪性能指標
        
        所有對 handle() 的調用應改為調用此方法，或者在子類中覆寫 handle() 並調用此方法
        """
        if not self._enable_auto_metrics:
            return self.handle(data)
        
        start_time = time.time()
        start_memory = 0
        memory_tracking = False
        
        # 嘗試追踪記憶體（可選功能，失敗不影響主流程）
        try:
            if not tracemalloc.is_tracing():
                tracemalloc.start()
            start_memory = tracemalloc.get_traced_memory()[0] / (1024 * 1024)  # MB
            memory_tracking = True
        except Exception:
            pass
        
        result = None
        success = False
        
        try:
            result = self.handle(data)
            success = True
        except Exception as e:
            success = False
            raise
        finally:
            # 計算性能指標
            processing_time = time.time() - start_time
            memory_usage = 0
            
            if memory_tracking:
                try:
                    current_memory = tracemalloc.get_traced_memory()[0] / (1024 * 1024)  # MB
                    memory_usage = current_memory - start_memory
                except Exception:
                    pass
            
            # 報告給 Framework
            self._report_performance({
                'processing_time': processing_time,
                'memory_usage': max(0, memory_usage),
                'request_result': 'success' if success else 'failure'
            })
        
        return result
    
    def _report_performance(self, metrics_data: Dict[str, Any]):
        """報告性能指標給 Framework"""
        try:
            from core.framework import core_framework
            core_framework.update_module_metrics(self._module_id, metrics_data)
        except Exception:
            # 靜默失敗，不影響主流程
            pass

    def shutdown(self):
        """釋放資源，可選實作"""
        self.is_initialized = False
        pass
