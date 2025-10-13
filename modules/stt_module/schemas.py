from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from enum import Enum
from core.schemas import STTModuleData

class ActivationMode(Enum):
    """STT 啟動模式"""
    MANUAL = "manual"           # 手動錄音 (按需錄音，不做智能判斷)
    CONTINUOUS = "continuous"   # 持續背景監聽 (持續背景監聽並實時傳送結果給NLP)

class SpeakerInfo(BaseModel):
    """說話人信息"""
    speaker_id: str             # 說話人ID
    confidence: float           # 識別信心度
    is_new_speaker: bool        # 是否為新說話人
    voice_features: Optional[Dict[str, Any]] = None  # 語音特徵
    
class STTInput(BaseModel):
    """STT 模組輸入 (模組內部使用)"""
    mode: ActivationMode = ActivationMode.CONTINUOUS  # 啟動模式，默認為持續監聽
    duration: Optional[float] = None               # 錄音時長限制
    language: str = "en-US"                       # 語言設定 (預設英文)
    enable_speaker_id: bool = True                # 是否啟用說話人識別
    context: Optional[str] = None                 # 上下文信息
    
class STTOutput(BaseModel):
    """STT 模組輸出 (模組內部使用)"""
    text: str                                     # 辨識出的文字內容
    confidence: float                             # 識別信心度
    speaker_info: Optional[SpeakerInfo] = None    # 說話人信息
    activation_reason: Optional[str] = None       # 啟動原因
    processing_time: Optional[float] = None       # 處理時間
    alternatives: Optional[List[str]] = None      # 備選結果
    error: Optional[str] = None                   # 錯誤訊息
    should_activate: bool = True                  # 是否應該啟動（智能判斷結果）
    metadata: Dict[str, Any] = {}                 # 額外元資料 (如文字輸入模式標記)
    
    def to_unified_format(self) -> STTModuleData:
        """轉換為統一數據格式，用於模組間通訊"""
        has_valid_text = self.text and len(self.text.strip()) > 0
        
        # 合併 metadata,確保特殊標記被保留
        combined_metadata = {
            "processing_time": self.processing_time,
            "alternatives": self.alternatives,
            "should_activate": self.should_activate,
            **self.metadata  # 包含文字輸入模式等特殊標記
        }
        
        return STTModuleData(
            text=self.text,
            confidence=self.confidence,
            speaker_info=self.speaker_info.model_dump() if self.speaker_info else None,
            activation_reason=self.activation_reason,
            status="success" if has_valid_text and not self.error else "error",
            error=self.error,
            source_module="stt",
            metadata=combined_metadata
        )

class VoiceActivityEvent(BaseModel):
    """語音活動事件"""
    event_type: str            # 事件類型: "speech_start", "speech_end", "silence"
    timestamp: float           # 時間戳
    confidence: float          # 信心度
    energy_level: float        # 能量級別
    
class SpeakerModel(BaseModel):
    """說話人模型"""
    speaker_id: str            # 說話人ID
    feature_vectors: List[List[float]]  # 特徵向量列表
    sample_count: int          # 樣本數量
    created_at: float          # 創建時間
    last_updated: float        # 最後更新時間
    metadata: Dict[str, Any] = {}  # 額外元數據
