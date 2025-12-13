# core/module_base.py
"""
BaseModule - 所有模組的基礎類別

提供標準化的效能監控機制：
1. 自動追蹤基本指標（請求數、處理時間、成功率等）
2. 支持自定義指標（模組特定的數據）
3. 提供統一的數據查詢接口

使用示例：
    class STTModule(BaseModule):
        def handle(self, data):
            # 處理邏輯
            result = self.recognize_speech(data)
            
            # 更新自定義指標
            self.update_custom_metric('audio_duration', data['duration'])
            self.update_custom_metric('recognition_confidence', result['confidence'])
            
            return result
        
        def get_performance_window(self):
            # 可選：覆寫此方法以添加更多模組特定數據
            window = super().get_performance_window()
            window['total_audio_processed'] = self.total_audio_time
            return window
"""

from abc import ABC, abstractmethod
import time
import tracemalloc
from typing import Dict, Any


class BaseModule(ABC):
    """
    所有模組的基本接口
    
    提供：
    - 標準化的 initialize/handle/shutdown 接口
    - 自動效能追蹤（透過 handle_with_metrics）
    - 本地效能數據收集和查詢
    - 自定義指標支持
    """
    
    def __init__(self):
        """初始化基本屬性"""
        self.is_initialized = False
        self._module_id = self.__class__.__name__  # 模組ID，子類可覆寫
        self._enable_auto_metrics = True  # 是否啟用自動性能追踪
        
        # 本地性能數據追蹤（供快速查詢）
        self._local_metrics = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'total_processing_time': 0.0,
            'min_processing_time': float('inf'),
            'max_processing_time': 0.0,
            'last_request_time': None,
            'last_processing_time': None
        }
        
        # 自定義指標（子類可擴展）
        self._custom_metrics = {}
        
    def set_module_id(self, module_id: str):
        """設置模組ID"""
        self._module_id = module_id
    
    def update_custom_metric(self, metric_name: str, value: Any):
        """
        更新自定義指標
        
        子類可以使用此方法記錄模組特定的指標，例如：
        - STT: 識別準確率、平均音頻長度
        - LLM: token數量、上下文使用率
        - TTS: 音頻生成時長、音質評分
        
        Args:
            metric_name: 指標名稱
            value: 指標值
        """
        self._custom_metrics[metric_name] = value
    
    def get_custom_metrics(self) -> Dict[str, Any]:
        """獲取所有自定義指標"""
        return self._custom_metrics.copy()

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
        """報告性能指標給 Framework 並更新本地快取"""
        try:
            # 更新本地指標
            processing_time = metrics_data.get('processing_time', 0)
            is_success = metrics_data.get('request_result') == 'success'
            
            self._local_metrics['total_requests'] += 1
            if is_success:
                self._local_metrics['successful_requests'] += 1
            else:
                self._local_metrics['failed_requests'] += 1
            
            if processing_time > 0:
                self._local_metrics['total_processing_time'] += processing_time
                self._local_metrics['min_processing_time'] = min(
                    self._local_metrics['min_processing_time'], processing_time
                )
                self._local_metrics['max_processing_time'] = max(
                    self._local_metrics['max_processing_time'], processing_time
                )
                self._local_metrics['last_processing_time'] = processing_time
            
            self._local_metrics['last_request_time'] = time.time()
            
            # 報告給 Framework
            from core.framework import core_framework
            core_framework.update_module_metrics(self._module_id, metrics_data)
        except Exception:
            # 靜默失敗，不影響主流程
            pass
    
    def get_performance_window(self) -> Dict[str, Any]:
        """
        獲取模組的效能數據窗口
        
        Returns:
            包含模組效能統計的字典，包括：
            - total_requests: 總請求數
            - successful_requests: 成功請求數
            - failed_requests: 失敗請求數
            - success_rate: 成功率
            - avg_processing_time: 平均處理時間
            - min_processing_time: 最小處理時間
            - max_processing_time: 最大處理時間
            - last_processing_time: 最後一次處理時間
            - last_request_time: 最後一次請求時間
            - module_id: 模組ID
            - custom_metrics: 自定義指標（如果有）
        """
        metrics = self._local_metrics.copy()
        
        # 計算平均處理時間
        if metrics['total_requests'] > 0:
            metrics['avg_processing_time'] = (
                metrics['total_processing_time'] / metrics['total_requests']
            )
            metrics['success_rate'] = (
                metrics['successful_requests'] / metrics['total_requests']
            )
        else:
            metrics['avg_processing_time'] = 0.0
            metrics['success_rate'] = 0.0
        
        # 處理無限值
        if metrics['min_processing_time'] == float('inf'):
            metrics['min_processing_time'] = 0.0
        
        metrics['module_id'] = self._module_id
        
        # 包含自定義指標
        if self._custom_metrics:
            metrics['custom_metrics'] = self._custom_metrics.copy()
        
        return metrics
    
    def reset_performance_metrics(self):
        """重置本地效能指標（用於測試或系統重啟）"""
        self._local_metrics = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'total_processing_time': 0.0,
            'min_processing_time': float('inf'),
            'max_processing_time': 0.0,
            'last_request_time': None,
            'last_processing_time': None
        }
        self._custom_metrics = {}

    def shutdown(self):
        """釋放資源，可選實作"""
        self.is_initialized = False
        pass
