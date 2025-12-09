"""
MISCHIEF 狀態專用行為基類與註冊系統

此模組定義 MISCHIEF 狀態下可執行的行為基類，以及行為註冊機制。
每個行為都有情境限制（mood_range）和優先級設定。
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Tuple
from enum import Enum

import sys
from pathlib import Path

# 確保可以導入 utils
sys.path.insert(0, str(Path(__file__).parents[3]))

from utils.debug_helper import info_log, debug_log, error_log


class MoodContext(Enum):
    """情境類型：基於當前系統情緒判定可用行為"""
    POSITIVE = "positive"  # mood > 0.3
    NEGATIVE = "negative"  # mood < -0.3
    NEUTRAL = "neutral"    # -0.3 <= mood <= 0.3
    ANY = "any"            # 任何情境皆可


class MischiefAction(ABC):
    """
    MISCHIEF 行為基類
    
    所有搗蛋行為都必須繼承此類並實現 execute() 方法。
    """
    
    def __init__(self):
        self.action_id: str = self.__class__.__name__
        self.display_name: str = "未命名行為"
        self.description: str = ""
        self.mood_context: MoodContext = MoodContext.ANY
        self.animation_name: Optional[str] = None  # 執行時播放的動畫
        self.requires_params: List[str] = []       # 需要的參數列表
        
    @abstractmethod
    def execute(self, params: Dict[str, Any]) -> Tuple[bool, str]:
        """
        執行行為
        
        Args:
            params: LLM 提供的參數字典
            
        Returns:
            (success, message): 成功與否及訊息
        """
        pass
    
    def validate_params(self, params: Dict[str, Any]) -> Tuple[bool, str]:
        """
        驗證參數是否完整
        
        Returns:
            (valid, error_message)
        """
        missing = [p for p in self.requires_params if p not in params]
        if missing:
            return False, f"缺少必要參數: {', '.join(missing)}"
        return True, ""
    
    def get_info(self) -> Dict[str, Any]:
        """返回行為資訊供 LLM 參考"""
        return {
            "action_id": self.action_id,
            "name": self.display_name,
            "description": self.description,
            "mood_context": self.mood_context.value,
            "required_params": self.requires_params,
            "animation": self.animation_name
        }


class MischiefActionRegistry:
    """
    MISCHIEF 行為註冊器
    
    負責註冊、查詢和過濾可用行為。
    """
    
    def __init__(self):
        self._actions: Dict[str, MischiefAction] = {}
        info_log("[MischiefRegistry] 行為註冊器初始化")
    
    def register(self, action: MischiefAction):
        """註冊一個行為"""
        self._actions[action.action_id] = action
        debug_log(2, f"[MischiefRegistry] 註冊行為: {action.action_id} ({action.display_name})")
    
    def get_action(self, action_id: str) -> Optional[MischiefAction]:
        """根據 ID 獲取行為"""
        return self._actions.get(action_id)
    
    def get_available_actions(self, mood: float) -> List[Dict[str, Any]]:
        """
        根據當前情緒獲取可用行為列表
        
        Args:
            mood: 當前 mood 值 (-1.0 ~ 1.0)
            
        Returns:
            行為資訊列表（供 LLM 參考）
        """
        # 判定當前情境
        if mood > 0.3:
            current_context = MoodContext.POSITIVE
        elif mood < -0.3:
            current_context = MoodContext.NEGATIVE
        else:
            current_context = MoodContext.NEUTRAL
        
        available = []
        for action in self._actions.values():
            # 檢查行為是否符合當前情境
            if action.mood_context == MoodContext.ANY or action.mood_context == current_context:
                available.append(action.get_info())
        
        debug_log(2, f"[MischiefRegistry] 當前情境 {current_context.value}，"
                     f"可用行為 {len(available)} 個")
        return available
    
    def list_all_actions(self) -> List[str]:
        """列出所有已註冊行為的 ID"""
        return list(self._actions.keys())


# 全局註冊器實例
mischief_registry = MischiefActionRegistry()


def register_mischief_action(action_class):
    """
    裝飾器：自動註冊行為類別
    
    使用方式:
        @register_mischief_action
        class MyAction(MischiefAction):
            ...
    """
    instance = action_class()
    mischief_registry.register(instance)
    return action_class
