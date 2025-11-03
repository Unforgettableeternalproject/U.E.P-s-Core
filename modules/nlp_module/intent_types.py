"""
意圖類型定義和意圖分段數據結構

用於 BIOS 標籤化的多意圖分段系統，支援：
- 意圖類型枚舉（CHAT/WORK/CALL/UNKNOWN/COMPOUND）
- 意圖分段數據結構（WORK 使用 work_mode metadata 區分 direct/background）
- 優先級計算
"""

from enum import Enum
from dataclasses import dataclass
from typing import Dict, Any, Optional


class IntentType(Enum):
    """
    意圖類型枚舉 (BIOS Tagger 輸出標籤)
    
    注意: 
    - COMPOUND 不是 BIOS 標籤，而是系統層級判斷（當輸入包含多個意圖分段時）
    - WORK 意圖使用 work_mode metadata 區分 direct/background 執行模式
    - work_mode 僅影響 CS 場景下的中斷行為和優先級，不影響意圖類型本身
    - RESPONSE 用於工作流場景中的用戶回應（根據 NLP狀態處理.md）
    """
    CHAT = "chat"          # 一般對話
    WORK = "work"          # 工作任務（使用 work_mode metadata 區分 direct/background）
    CALL = "call"          # 呼叫功能，不進佇列
    RESPONSE = "response"  # 工作流回應（WS 場景中的用戶輸入）
    UNKNOWN = "unknown"    # 未知意圖


# 意圖類型基礎優先級映射
# 注意：WORK 的實際優先級由 work_mode 決定 (direct=100, background=30)
INTENT_PRIORITY_MAP = {
    IntentType.CALL: 70,       # 呼叫系統，不進入狀態佇列
    IntentType.WORK: 50,       # 工作任務，基礎優先級（實際由 work_mode 覆蓋）
    IntentType.CHAT: 50,       # 普通對話，標準優先權
    IntentType.RESPONSE: 50,   # 工作流回應，標準優先權
    IntentType.UNKNOWN: 10     # 未知意圖，最低優先級
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
        """檢查是否為工作意圖"""
        return self.intent_type == IntentType.WORK
    
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


def should_interrupt_chat(intent_type: IntentType, work_mode: Optional[str] = None) -> bool:
    """
    判斷該意圖類型是否應該中斷當前聊天
    
    Args:
        intent_type: 意圖類型
        work_mode: 工作模式 ("direct" 或 "background")，僅當 intent_type 為 WORK 時需要
        
    Returns:
        bool: 是否應該中斷
    """
    if intent_type == IntentType.WORK:
        return work_mode == "direct"
    return False
