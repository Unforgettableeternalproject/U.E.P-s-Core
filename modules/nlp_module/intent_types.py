"""
意圖類型定義和意圖分段數據結構

用於 BIOS 標籤化的多意圖分段系統，支援：
- 意圖類型枚舉（CHAT/DIRECT_WORK/BACKGROUND_WORK/CALL/COMPOUND）
- 意圖分段數據結構
- 優先級計算
"""

from enum import Enum
from dataclasses import dataclass
from typing import Dict, Any, Optional


class IntentType(Enum):
    """
    意圖類型枚舉 (BIOS Tagger 輸出標籤)
    
    注意: COMPOUND 不是 BIOS 標籤，而是系統層級判斷。
    當一個輸入包含多個意圖分段時，系統自動識別為複合意圖。
    """
    CHAT = "chat"                      # 一般對話 (priority: 50)
    DIRECT_WORK = "direct_work"        # 直接工作，可中斷 (priority: 100)
    BACKGROUND_WORK = "background_work"  # 背景工作，排隊執行 (priority: 30)
    CALL = "call"                      # 呼叫功能，不進佇列 (priority: 70)
    UNKNOWN = "unknown"                # 未知意圖 (priority: 10)


# 意圖類型優先級映射 (用於 BIOS Tagger 輸出)
INTENT_PRIORITY_MAP = {
    IntentType.DIRECT_WORK: 100,      # 最高優先級，可中斷現有對話
    IntentType.CALL: 70,              # 呼叫系統，不進入狀態佇列
    IntentType.CHAT: 50,              # 普通對話，標準優先權
    IntentType.BACKGROUND_WORK: 30,   # 背景工作，可排隊等待
    IntentType.UNKNOWN: 10            # 未知意圖，最低優先級
}


@dataclass
class IntentSegment:
    """
    意圖分段數據結構
    
    表示使用者輸入中的單個意圖段落，包含：
    - 分段文本
    - 意圖類型
    - 置信度
    - 優先級
    - 元數據
    """
    segment_text: str                      # 分段文本
    intent_type: IntentType                # 意圖類型
    confidence: float = 1.0                # 置信度（0.0-1.0）
    priority: int = 0                      # 優先級（自動從 intent_type 計算）
    metadata: Optional[Dict[str, Any]] = None  # 額外元數據
    
    def __post_init__(self):
        """初始化後處理：自動計算優先級和初始化元數據"""
        if self.priority == 0:
            self.priority = INTENT_PRIORITY_MAP.get(self.intent_type, 10)
        
        if self.metadata is None:
            self.metadata = {}
    
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式"""
        return {
            "segment_text": self.segment_text,
            "intent_type": self.intent_type.value,
            "confidence": self.confidence,
            "priority": self.priority,
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'IntentSegment':
        """從字典創建實例"""
        return cls(
            segment_text=str(data["segment_text"]),
            intent_type=IntentType(data["intent_type"]),
            confidence=float(data.get("confidence", 1.0)),
            priority=int(data.get("priority") or 0),
            metadata=data.get("metadata")
        )
    
    def is_work_intent(self) -> bool:
        """檢查是否為工作意圖（直接或背景）"""
        return self.intent_type in [IntentType.DIRECT_WORK, IntentType.BACKGROUND_WORK]
    
    def is_chat_intent(self) -> bool:
        """檢查是否為對話意圖"""
        return self.intent_type == IntentType.CHAT
    
    def requires_immediate_attention(self) -> bool:
        """檢查是否需要立即處理（高優先級）"""
        return self.priority >= 70
    
    @staticmethod
    def is_compound_input(segments: list['IntentSegment']) -> bool:
        """
        判斷是否為複合意圖輸入 (系統層級判斷)
        
        Args:
            segments: 意圖分段列表
            
        Returns:
            True 如果包含多個意圖分段（複合意圖）
        """
        return len(segments) > 1
    
    @staticmethod
    def get_highest_priority_segment(segments: list['IntentSegment']) -> 'IntentSegment':
        """
        從複合意圖中獲取最高優先權的分段
        
        Args:
            segments: 意圖分段列表
            
        Returns:
            優先權最高的分段，如果列表為空則返回 UNKNOWN 分段
        """
        if not segments:
            return IntentSegment("", IntentType.UNKNOWN, 0.0)
        return max(segments, key=lambda s: s.priority)


def get_intent_priority(intent_type: IntentType) -> int:
    """
    獲取意圖類型的優先級
    
    Args:
        intent_type: 意圖類型
        
    Returns:
        int: 優先級值（數值越高優先級越高）
    """
    return INTENT_PRIORITY_MAP.get(intent_type, 10)


def should_interrupt_chat(intent_type: IntentType) -> bool:
    """
    判斷該意圖類型是否應該中斷當前聊天
    
    Args:
        intent_type: 意圖類型
        
    Returns:
        bool: 是否應該中斷
    """
    return intent_type == IntentType.DIRECT_WORK
