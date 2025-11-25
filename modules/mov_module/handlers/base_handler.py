"""
處理器基類

定義所有事件/互動處理器的接口
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..mov_module import MOVModule


class BaseHandler(ABC):
    """
    處理器基類
    
    所有處理器都應該繼承此類並實現 can_handle() 和 handle() 方法
    支援責任鏈模式：如果當前處理器無法處理，返回 False 傳遞給下一個處理器
    """
    
    def __init__(self, coordinator: 'MOVModule'):
        """
        初始化處理器
        
        Args:
            coordinator: MOV 模組實例，用於訪問狀態和觸發動作
        """
        self.coordinator = coordinator
        self.enabled = True
        
    @abstractmethod
    def can_handle(self, event: Any) -> bool:
        """
        判斷此處理器是否可以處理該事件
        
        Args:
            event: 事件對象
            
        Returns:
            bool: 如果可以處理返回 True
        """
        pass
    
    @abstractmethod
    def handle(self, event: Any) -> bool:
        """
        處理事件
        
        Args:
            event: 事件對象
            
        Returns:
            bool: 如果成功處理返回 True，否則返回 False 讓下一個處理器嘗試
        """
        pass
    
    def enable(self):
        """啟用此處理器"""
        self.enabled = True
        
    def disable(self):
        """禁用此處理器"""
        self.enabled = False


class HandlerChain:
    """
    處理器鏈
    
    管理多個處理器，按順序嘗試處理事件
    """
    
    def __init__(self):
        self.handlers: list[tuple[int, BaseHandler]] = []
        
    def add_handler(self, handler: BaseHandler, priority: int = 0):
        """
        添加處理器
        
        Args:
            handler: 處理器實例
            priority: 優先級（數字越大優先級越高）
        """
        self.handlers.append((priority, handler))
        # 按優先級排序（降序）
        self.handlers.sort(key=lambda x: x[0], reverse=True)
        
    def remove_handler(self, handler: BaseHandler):
        """移除處理器"""
        self.handlers = [(p, h) for p, h in self.handlers if h != handler]
        
    def handle(self, event: Any) -> bool:
        """
        處理事件
        
        按優先級順序嘗試所有處理器，直到有一個成功處理
        
        Args:
            event: 事件對象
            
        Returns:
            bool: 如果有處理器成功處理返回 True
        """
        for priority, handler in self.handlers:
            if not handler.enabled:
                continue
                
            if handler.can_handle(event):
                if handler.handle(event):
                    return True
                    
        return False
