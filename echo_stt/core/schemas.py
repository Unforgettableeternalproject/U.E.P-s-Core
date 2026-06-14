"""
echo_stt 資料模型定義

從 U.E.P Core 遷出並解耦：
- 移除對 core.schemas.STTModuleData 的依賴（本地定義）
- 修復 Bug: duration=0.0 被 falsy 判定誤吃
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from enum import Enum


# ──────────────────────────────────────────────
#  STTModuleData — 本地定義（替代 core.schemas.STTModuleData）
# ──────────────────────────────────────────────

class STTModuleData(BaseModel):
    """STT 模組的統一數據格式，用於模組間通訊。

    原本定義於 U.E.P Core 的 core.schemas，
    現在本地定義以解除對主專案的依賴。
    """
    text: str = ""
    confidence: float = 0.0
    speaker_info: Optional[Dict[str, Any]] = None
    activation_reason: Optional[str] = None
    status: str = "success"
    error: Optional[str] = None
    source_module: str = "stt"
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ──────────────────────────────────────────────
#  核心 Enums 與 Models
# ──────────────────────────────────────────────

class ActivationMode(Enum):
    """STT 啟動模式"""
    MANUAL = "manual"           # 手動錄音 (按需錄音，不做智能判斷)
    CONTINUOUS = "continuous"   # 持續背景監聽


class SpeakerInfo(BaseModel):
    """說話人信息"""
    speaker_id: str
    confidence: float
    is_new_speaker: bool
    voice_features: Optional[Dict[str, Any]] = None


class STTInput(BaseModel):
    """STT 模組輸入 (模組內部使用)"""
    mode: ActivationMode = ActivationMode.CONTINUOUS
    duration: Optional[float] = None
    language: str = "en-US"
    enable_speaker_id: bool = True
    context: Optional[str] = None


class STTOutput(BaseModel):
    """STT 模組輸出 (模組內部使用)"""
    text: str
    confidence: float
    speaker_info: Optional[SpeakerInfo] = None
    activation_reason: Optional[str] = None
    processing_time: Optional[float] = None
    alternatives: Optional[List[str]] = None
    error: Optional[str] = None
    should_activate: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def to_unified_format(self) -> STTModuleData:
        """轉換為統一數據格式。"""
        has_valid_text = self.text and len(self.text.strip()) > 0

        combined_metadata = {
            "processing_time": self.processing_time,
            "alternatives": self.alternatives,
            "should_activate": self.should_activate,
            **self.metadata
        }

        is_listening_completed = self.activation_reason in [
            "continuous_listening_completed",
            "text_input_empty",
        ]
        status = (
            "success"
            if (has_valid_text or is_listening_completed) and not self.error
            else "error"
        )

        return STTModuleData(
            text=self.text,
            confidence=self.confidence,
            speaker_info=self.speaker_info.model_dump() if self.speaker_info else None,
            activation_reason=self.activation_reason,
            status=status,
            error=self.error,
            source_module="stt",
            metadata=combined_metadata,
        )


class VoiceActivityEvent(BaseModel):
    """語音活動事件"""
    event_type: str            # "speech_start", "speech_end", "silence"
    timestamp: float
    confidence: float
    energy_level: float


class SpeakerModel(BaseModel):
    """說話人模型"""
    speaker_id: str
    feature_vectors: List[List[float]]
    sample_count: int
    created_at: float
    last_updated: float
    metadata: Dict[str, Any] = Field(default_factory=dict)
