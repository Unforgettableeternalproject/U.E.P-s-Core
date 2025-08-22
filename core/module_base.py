# core/module_base.py

from abc import ABC, abstractmethod

class BaseModule(ABC):
    """所有模組的基本接口"""
    
    def __init__(self):
        """初始化基本屬性"""
        self.is_initialized = False

    @abstractmethod
    def initialize(self):
        """初始化模組，如載入模型、參數等"""
        # 子類別需要在成功初始化後設置 self.is_initialized = True
        pass

    @abstractmethod
    def handle(self, data: dict) -> dict:
        """處理資料並回傳統一格式"""
        pass

    def shutdown(self):
        """釋放資源，可選實作"""
        self.is_initialized = False
        pass
