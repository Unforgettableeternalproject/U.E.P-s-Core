"""
動畫選擇策略基類

定義動畫選擇策略的接口
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..mov_module import MOVModule


class AnimationStrategy(ABC):
    """
    動畫選擇策略基類
    
    所有動畫選擇策略都應該繼承此類並實現 select_animation() 方法
    """
    
    def __init__(self, coordinator: 'MOVModule', config: Optional[Dict[str, Any]] = None):
        """
        初始化策略
        
        Args:
            coordinator: MOV 模組實例
            config: 策略配置
        """
        self.coordinator = coordinator
        self.config = config or {}
        self.enabled = True
        self.priority = 0  # 優先級，數字越大優先級越高
        
    @abstractmethod
    def select_animation(self, context: Dict[str, Any]) -> Optional[str]:
        """
        選擇動畫
        
        Args:
            context: 上下文信息，包含狀態、層級、情緒等
            
        Returns:
            Optional[str]: 動畫名稱，如果無法選擇則返回 None
        """
        pass
    
    def can_select(self, context: Dict[str, Any]) -> bool:
        """
        判斷此策略是否可以為當前上下文選擇動畫
        
        Args:
            context: 上下文信息
            
        Returns:
            bool: 如果可以選擇返回 True
        """
        return self.enabled
    
    def enable(self):
        """啟用此策略"""
        self.enabled = True
        
    def disable(self):
        """禁用此策略"""
        self.enabled = False


class StrategyManager:
    """
    策略管理器
    
    管理多個動畫選擇策略，按優先級順序嘗試選擇動畫
    """
    
    def __init__(self):
        self.strategies: list[AnimationStrategy] = []
        
    def add_strategy(self, strategy: AnimationStrategy):
        """
        添加策略
        
        Args:
            strategy: 策略實例
        """
        self.strategies.append(strategy)
        # 按優先級排序（降序）
        self.strategies.sort(key=lambda s: s.priority, reverse=True)
        
    def remove_strategy(self, strategy: AnimationStrategy):
        """移除策略"""
        if strategy in self.strategies:
            self.strategies.remove(strategy)
            
    def select_animation(self, context: Dict[str, Any]) -> Optional[str]:
        """
        選擇動畫
        
        按優先級順序嘗試所有策略，直到有一個成功選擇
        
        Args:
            context: 上下文信息
            
        Returns:
            Optional[str]: 動畫名稱，如果沒有策略能選擇則返回 None
        """
        for strategy in self.strategies:
            if not strategy.can_select(context):
                continue
                
            animation = strategy.select_animation(context)
            if animation:
                return animation
                
        return None
