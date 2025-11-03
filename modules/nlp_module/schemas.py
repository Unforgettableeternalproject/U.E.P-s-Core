# modules/nlp_module/schemas.py

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Literal, Union
from enum import Enum  # ✅ 保留用於 IdentityStatus
from datetime import datetime


# === 語者身份系統 ===

class IdentityStatus(str, Enum):
    """語者身份狀態"""
    UNKNOWN = "unknown"              # 未知語者
    ACCUMULATING = "accumulating"    # 樣本累積中
    CONFIRMED = "confirmed"          # 已確認身份
    GUEST = "guest"                  # 訪客模式
    TEMPORARY = "temporary"          # 臨時通用身份


class UserProfile(BaseModel):
    """使用者檔案"""
    identity_id: str = Field(..., description="身份識別ID")
    speaker_id: Optional[str] = Field(None, description="對應的語者ID")
    display_name: Optional[str] = Field(None, description="顯示名稱")
    status: IdentityStatus = Field(IdentityStatus.UNKNOWN, description="身份狀態")
    
    # 記憶令牌與憑證 (用於MEM模組存取記憶庫)
    memory_token: Optional[str] = Field(None, description="記憶庫存取令牌")
    
    # 偏好設定
    preferences: Dict[str, Any] = Field(default_factory=dict, description="使用者偏好")
    system_habits: Dict[str, Any] = Field(default_factory=dict, description="系統使用習慣")
    
    # 語音風格偏好 (用於TTS模組)
    voice_preferences: Dict[str, Any] = Field(default_factory=dict, description="語音風格偏好")
    
    # LLM互動偏好 (用於LLM模組)
    conversation_style: Dict[str, Any] = Field(default_factory=dict, description="對話風格偏好")
    
    # 統計資訊
    total_interactions: int = Field(0, description="總互動次數")
    last_interaction: Optional[datetime] = Field(None, description="最後互動時間")
    created_at: datetime = Field(default_factory=datetime.now, description="創建時間")


# === 意圖分析系統 ===

# ✅ 導入統一的 IntentType 定義（避免重複定義）
from .intent_types import IntentType


class IntentSegment(BaseModel):
    """意圖片段 - 支援分段標籤分析"""
    text: str = Field(..., description="片段文本")
    intent: IntentType = Field(..., description="片段意圖")
    confidence: float = Field(..., description="信心度 0-1")
    start_pos: int = Field(..., description="在原文中的起始位置")
    end_pos: int = Field(..., description="在原文中的結束位置")
    
    # 實體識別
    entities: List[Dict[str, Any]] = Field(default_factory=list, description="識別出的實體")
    
    # 上下文
    context_hints: List[str] = Field(default_factory=list, description="上下文提示")


class SystemStateTransition(BaseModel):
    """系統狀態轉換建議"""
    from_state: str = Field(..., description="源狀態")
    to_state: str = Field(..., description="目標狀態")
    reason: str = Field(..., description="轉換原因")
    confidence: float = Field(..., description="轉換信心度")


# === NLP 輸入輸出 ===

class NLPInput(BaseModel):
    """NLP模組輸入"""
    text: str = Field(..., description="要分析的文字內容")
    
    # 語者資訊 (來自STT)
    speaker_id: Optional[str] = Field(None, description="語者ID")
    speaker_confidence: Optional[float] = Field(None, description="語者識別信心度")
    speaker_status: Optional[str] = Field(None, description="語者狀態 (new/existing/unknown)")
    
    # 時間戳記
    timestamp: Optional[str] = Field(None, description="處理時間戳記")
    
    # 上下文資訊
    conversation_history: Optional[List[str]] = Field(None, description="對話歷史")
    current_system_state: Optional[str] = Field(None, description="當前系統狀態")
    
    # 元資料 (來自STT,用於特殊處理模式)
    metadata: Optional[Dict[str, Any]] = Field(None, description="額外元資料 (如文字輸入模式標記)")
    
    # 處理選項
    enable_segmentation: bool = Field(True, description="啟用分段分析")
    enable_entity_extraction: bool = Field(True, description="啟用實體抽取")
    enable_identity_processing: bool = Field(True, description="啟用身份處理")


class NLPOutput(BaseModel):
    """NLP模組輸出"""
    # 原始資料
    original_text: str = Field(..., description="原始輸入文字")
    
    # 時間戳記
    timestamp: Optional[float] = Field(None, description="處理時間戳記")
    
    # 語者身份處理結果
    identity: Optional[UserProfile] = Field(None, description="使用者身份資訊")
    identity_action: Optional[str] = Field(None, description="身份處理動作 (create/update/load)")
    
    # 意圖分析結果
    primary_intent: IntentType = Field(..., description="主要意圖")
    intent_segments: List[IntentSegment] = Field(..., description="意圖片段列表")
    overall_confidence: float = Field(..., description="整體分析信心度")
    
    # 系統控制
    state_transition: Optional[SystemStateTransition] = Field(None, description="建議的狀態轉換")
    
    # 後續處理指引
    next_modules: List[str] = Field(default_factory=list, description="建議的下一步模組")
    processing_notes: List[str] = Field(default_factory=list, description="處理註記")
    
    # 等待狀態 (用於 call 類型)
    awaiting_further_input: bool = Field(False, description="等待進一步輸入")
    timeout_seconds: Optional[int] = Field(None, description="等待超時時間")
    
    # 狀態佇列管理
    queue_states_added: Optional[List[str]] = Field(None, description="添加到狀態佇列的狀態")
    current_system_state: Optional[str] = Field(None, description="當前系統狀態")
    
    # Working Context更新記錄
    working_context_updates: List[Dict[str, Any]] = Field(default_factory=list, description="Working Context更新記錄")


# === 決策處理 ===

class IdentityDecision(BaseModel):
    """身份決策"""
    action: Literal["create", "update", "merge", "ignore"] = Field(..., description="決策動作")
    target_identity_id: Optional[str] = Field(None, description="目標身份ID")
    confidence_threshold: float = Field(0.8, description="信心度閾值")
    reasoning: str = Field(..., description="決策原因")


class NLPDecisionPackage(BaseModel):
    """NLP決策包 - 用於Working Context"""
    context_id: str = Field(..., description="上下文ID")
    speaker_samples: List[Any] = Field(..., description="語者樣本數據")
    accumulated_interactions: List[str] = Field(..., description="累積的互動記錄")
    decision_options: List[IdentityDecision] = Field(..., description="可選決策")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="額外元數據")
