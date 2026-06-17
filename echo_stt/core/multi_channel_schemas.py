"""
多通道 STT 系統資料模型

定義多通道音訊處理管線中各組件的契約：
- AudioSegment: VAD 切分後的音訊片段
- TranscriptEntry: 單段辨識結果（帶時間戳 + 說話人）
- TranscriptSession: 一個完整 session 的所有辨識結果
- ChannelStatus: 通道即時狀態
"""

from __future__ import annotations

import base64
import time
import uuid
from enum import Enum
from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, Field, PlainSerializer


# ──────────────────────────────────────────────────────────────
#  Enums
# ──────────────────────────────────────────────────────────────


class ChannelType(str, Enum):
    """音訊通道類型"""
    LOCAL_MIC = "local_mic"     # 本地麥克風（需要 speaker diarization）
    DISCORD = "discord"         # Discord 語音（user_id 天然隔離）
    FILE = "file"               # 預錄音檔（測試用）


class ChannelState(str, Enum):
    """通道狀態"""
    IDLE = "idle"               # 已建立但未啟動
    LISTENING = "listening"     # 正在監聽（靜音中）
    SPEECH = "speech"           # 偵測到語音中
    PROCESSING = "processing"   # 音訊片段正在辨識
    STOPPED = "stopped"         # 已停止
    ERROR = "error"             # 錯誤


class SegmentPriority(int, Enum):
    """片段優先權（值越小越優先）"""
    REALTIME = 0        # 即時互動（如 keyword trigger）
    NORMAL = 10         # 一般語音片段
    BACKFILL = 20       # 補填（如 flush 剩餘 buffer）


# ──────────────────────────────────────────────────────────────
#  AudioSegment — VAD 切分後的音訊片段
# ──────────────────────────────────────────────────────────────


class AudioSegment(BaseModel):
    """VAD 切分後的音訊片段，作為辨識佇列的基本單位。

    由 AdvancedVAD / ChannelManager 產生，送入 TranscriptionQueue。

    NOTE: audio_data 儲存為 bytes（int16 PCM 的 raw bytes），
    而非 numpy array，以支援 Pydantic 序列化。
    使用 to_numpy() / from_numpy() 轉換。
    """
    segment_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    channel_id: str
    channel_type: ChannelType
    speaker_label: Optional[str] = None     # Discord: user_id; local_mic: None（待辨識）
    speaker_name: Optional[str] = None      # 人類可讀名稱（Discord username 或辨識後名稱）

    # 時間戳（絕對時間 time.time()）
    start_timestamp: float
    end_timestamp: float

    # 音訊資料（JSON 序列化時自動轉為 base64）
    audio_data: Annotated[
        bytes,
        PlainSerializer(lambda v: base64.b64encode(v).decode("ascii"), return_type=str),
    ]
    sample_rate: int = 16000
    num_samples: int = 0                    # 樣本數

    # 排序 / 佇列
    sequence_num: int = 0                   # 全域遞增序號
    priority: SegmentPriority = SegmentPriority.NORMAL

    # 元資料
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def duration(self) -> float:
        """片段時長（秒）"""
        return self.end_timestamp - self.start_timestamp

    @property
    def duration_from_samples(self) -> float:
        """從樣本數計算時長（更精確）"""
        return self.num_samples / self.sample_rate if self.sample_rate > 0 else 0.0

    def to_numpy(self):
        """轉換為 numpy int16 array"""
        import numpy as np
        return np.frombuffer(self.audio_data, dtype=np.int16)

    def to_float32(self):
        """轉換為 float32 array（-1.0 ~ 1.0），直接餵給 Whisper"""
        import numpy as np
        return np.frombuffer(self.audio_data, dtype=np.int16).astype(np.float32) / 32768.0

    @classmethod
    def from_numpy(
        cls,
        audio_np,
        channel_id: str,
        channel_type: ChannelType,
        start_timestamp: float,
        end_timestamp: float,
        sample_rate: int = 16000,
        **kwargs,
    ) -> "AudioSegment":
        """從 numpy array 建立 AudioSegment"""
        import numpy as np

        if audio_np.dtype == np.float32:
            # float32 → int16
            audio_int16 = (audio_np * 32768).clip(-32768, 32767).astype(np.int16)
        elif audio_np.dtype == np.int16:
            audio_int16 = audio_np
        else:
            audio_int16 = audio_np.astype(np.int16)

        return cls(
            channel_id=channel_id,
            channel_type=channel_type,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            audio_data=audio_int16.tobytes(),
            sample_rate=sample_rate,
            num_samples=len(audio_int16),
            **kwargs,
        )


# ──────────────────────────────────────────────────────────────
#  TranscriptEntry — 單段辨識結果
# ──────────────────────────────────────────────────────────────


class TranscriptEntry(BaseModel):
    """單段語音辨識結果，帶時間戳和說話人標籤。

    由 STTWorker 辨識後產生，送入 TranscriptAggregator。
    """
    entry_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    segment_id: str                         # 對應的 AudioSegment.segment_id

    # 辨識結果
    text: str
    language: Optional[str] = None          # 偵測到的語言 (e.g. "zh", "en")
    confidence: float = 0.0

    # 說話人
    channel_id: str
    channel_type: ChannelType
    speaker_label: str = "unknown"          # 唯一識別（user_id 或 speaker_XXX）
    speaker_name: str = "Unknown"           # 人類可讀名稱

    # 時間戳（絕對時間）
    start_ts: float
    end_ts: float

    # 處理資訊
    processing_time: float = 0.0            # 辨識耗時（秒）
    model_name: Optional[str] = None        # 使用的模型

    # 元資料
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def duration(self) -> float:
        """語音時長（秒）"""
        return self.end_ts - self.start_ts


# ──────────────────────────────────────────────────────────────
#  TranscriptSession — 完整 session
# ──────────────────────────────────────────────────────────────


class TranscriptSession(BaseModel):
    """一個完整的辨識 session，包含所有辨識結果。

    提供排序、格式化、匯出等功能。
    """
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    started_at: float = Field(default_factory=time.time)
    ended_at: Optional[float] = None

    entries: List[TranscriptEntry] = Field(default_factory=list)

    # 統計
    total_audio_duration: float = 0.0
    total_segments: int = 0
    channels_used: List[str] = Field(default_factory=list)

    def add_entry(self, entry: TranscriptEntry) -> None:
        """加入辨識結果，保持按 start_ts 排序。"""
        self.entries.append(entry)
        self.entries.sort(key=lambda e: e.start_ts)
        self.total_segments += 1
        self.total_audio_duration += entry.duration
        if entry.channel_id not in self.channels_used:
            self.channels_used.append(entry.channel_id)

    def get_entries_since(self, since_ts: float) -> List[TranscriptEntry]:
        """取得某個時間點之後的所有 entries。"""
        return [e for e in self.entries if e.start_ts >= since_ts]

    def get_entries_by_speaker(self, speaker_label: str) -> List[TranscriptEntry]:
        """取得特定說話人的所有 entries。"""
        return [e for e in self.entries if e.speaker_label == speaker_label]

    def get_entries_by_channel(self, channel_id: str) -> List[TranscriptEntry]:
        """取得特定通道的所有 entries。"""
        return [e for e in self.entries if e.channel_id == channel_id]

    def get_speakers(self) -> List[str]:
        """取得所有出現過的說話人標籤。"""
        seen = []
        for e in self.entries:
            if e.speaker_label not in seen:
                seen.append(e.speaker_label)
        return seen

    def format_readable(self, use_relative_time: bool = True) -> str:
        """輸出人類可讀的 transcript 格式。

        格式：
            說話人A [00:01.2]: 一些東西
            說話人B [00:03.5]: 另一些東西

        Args:
            use_relative_time: True 則使用相對於 session 開始的時間，
                             False 則使用絕對時間。
        """
        if not self.entries:
            return "(no transcript entries)"

        lines = []
        for entry in self.entries:
            ts = entry.start_ts
            if use_relative_time:
                ts = max(0.0, ts - self.started_at)

            minutes = int(ts // 60)
            seconds = ts % 60
            time_str = f"[{minutes:02d}:{seconds:04.1f}]"

            name = entry.speaker_name if entry.speaker_name != "Unknown" else entry.speaker_label
            lines.append(f"{name} {time_str}: {entry.text}")

        return "\n".join(lines)

    def format_srt(self) -> str:
        """輸出 SRT 字幕格式。"""
        if not self.entries:
            return ""

        lines = []
        for i, entry in enumerate(self.entries, 1):
            rel_start = max(0.0, entry.start_ts - self.started_at)
            rel_end = max(0.0, entry.end_ts - self.started_at)

            def _srt_time(t: float) -> str:
                h = int(t // 3600)
                m = int((t % 3600) // 60)
                s = int(t % 60)
                ms = int((t % 1) * 1000)
                return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

            name = entry.speaker_name if entry.speaker_name != "Unknown" else entry.speaker_label
            lines.append(str(i))
            lines.append(f"{_srt_time(rel_start)} --> {_srt_time(rel_end)}")
            lines.append(f"[{name}] {entry.text}")
            lines.append("")

        return "\n".join(lines)

    def to_dict_list(self) -> List[Dict[str, Any]]:
        """匯出為 dict list（JSON 友好）。"""
        return [
            {
                "speaker": e.speaker_name or e.speaker_label,
                "start": round(max(0.0, e.start_ts - self.started_at), 2),
                "end": round(max(0.0, e.end_ts - self.started_at), 2),
                "text": e.text,
                "confidence": round(e.confidence, 3),
                "language": e.language,
                "channel": e.channel_id,
            }
            for e in self.entries
        ]

    def end_session(self) -> None:
        """結束 session。"""
        self.ended_at = time.time()


# ──────────────────────────────────────────────────────────────
#  ChannelStatus — 通道狀態快照
# ──────────────────────────────────────────────────────────────


class ChannelStatus(BaseModel):
    """通道的即時狀態快照，供 CLI / 監控使用。"""
    channel_id: str
    channel_type: ChannelType
    state: ChannelState = ChannelState.IDLE
    speaker_label: Optional[str] = None

    # 計數
    segments_produced: int = 0
    segments_transcribed: int = 0

    # 即時數值
    current_energy: float = 0.0             # VAD 能量值
    vad_is_speech: bool = False             # 目前是否偵測到語音
    buffer_duration: float = 0.0            # 目前 buffer 中的音訊時長

    # 時間
    last_speech_at: Optional[float] = None
    last_segment_at: Optional[float] = None
    created_at: float = Field(default_factory=time.time)


# ──────────────────────────────────────────────────────────────
#  QueueMetrics — 佇列統計
# ──────────────────────────────────────────────────────────────


class QueueMetrics(BaseModel):
    """TranscriptionQueue 的統計快照。"""
    queue_size: int = 0
    max_size: int = 50
    total_enqueued: int = 0
    total_dequeued: int = 0
    total_dropped: int = 0                  # 因佇列滿而丟棄的數量
    avg_wait_time: float = 0.0              # 平均等待時間（秒）
    avg_processing_time: float = 0.0        # 平均辨識時間（秒）


# ──────────────────────────────────────────────────────────────
#  EngineMetrics — 整體統計
# ──────────────────────────────────────────────────────────────


class EngineMetrics(BaseModel):
    """MultiChannelSTTEngine 的整體統計。"""
    active_channels: int = 0
    total_channels_ever: int = 0
    queue: QueueMetrics = Field(default_factory=QueueMetrics)

    total_entries: int = 0
    total_audio_duration: float = 0.0

    uptime: float = 0.0
    model_name: Optional[str] = None
    device: Optional[str] = None
    vram_usage_mb: Optional[float] = None
