# core/module_base.py

from abc import ABC, abstractmethod

class BaseModule(ABC):
    """所有模組的基本接口"""

    @abstractmethod
    def initialize(self):
        """初始化模組，如載入模型、參數等"""
        pass

    @abstractmethod
    def handle(self, data: dict) -> dict:
        """處理資料並回傳統一格式"""
        pass

    def shutdown(self):
        """釋放資源，可選實作"""
        pass
