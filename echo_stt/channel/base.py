"""
音訊通道抽象基底 + 測試用 FileChannel

AudioChannel 定義了通道的共用介面：
- 每個通道持有自己的 AdvancedVAD 實例（獨立狀態）
- push_audio() 餵音訊 → VAD 判斷 → 段落完成時產生 AudioSegment
- start() / stop() 管理通道生命週期
"""

from __future__ import annotations

import logging
import time
import threading
from abc import ABC, abstractmethod
from typing import Callable, Dict, Any, Optional

import numpy as np

from echo_stt.core.multi_channel_schemas import (
    AudioSegment,
    ChannelState,
    ChannelStatus,
    ChannelType,
)
from echo_stt.vad.advanced_vad import AdvancedVAD

logger = logging.getLogger(__name__)


class AudioChannel(ABC):
    """單一音訊通道的抽象基底類別。

    每個 channel 持有：
    - 自己的 AdvancedVAD（獨立的 muzzle 狀態機）
    - segment_callback: 段落完成時呼叫
    - 狀態追蹤（ChannelState）

    子類別需實作 start() / stop()，並在音訊到達時呼叫 push_audio()。
    """

    def __init__(
        self,
        channel_id: str,
        channel_type: ChannelType,
        vad: AdvancedVAD,
        segment_callback: Callable[[AudioSegment], None],
        sample_rate: int = 16000,
        speaker_label: Optional[str] = None,
        speaker_name: Optional[str] = None,
    ):
        self.channel_id = channel_id
        self.channel_type = channel_type
        self.vad = vad
        self.segment_callback = segment_callback
        self.sample_rate = sample_rate
        self.speaker_label = speaker_label
        self.speaker_name = speaker_name

        self._state = ChannelState.IDLE
        self._lock = threading.Lock()

        # 統計
        self._segments_produced = 0
        self._last_speech_at: Optional[float] = None
        self._last_segment_at: Optional[float] = None
        self._created_at = time.time()

    @property
    def state(self) -> ChannelState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state in (
            ChannelState.LISTENING,
            ChannelState.SPEECH,
            ChannelState.PROCESSING,
        )

    @abstractmethod
    def start(self) -> None:
        """啟動通道（開始接收音訊）。"""
        ...

    @abstractmethod
    def stop(self) -> None:
        """停止通道（停止接收音訊，flush 殘留 buffer）。"""
        ...

    def push_audio(self, audio_chunk: np.ndarray, abs_timestamp: Optional[float] = None) -> None:
        """推入一個音訊 chunk。

        由子類別在收到音訊時呼叫。VAD 處理後，
        若段落完成則觸發 segment_callback。

        Args:
            audio_chunk: int16 或 float32 音訊 chunk
            abs_timestamp: 絕對時間戳（預設 time.time()）
        """
        if abs_timestamp is None:
            abs_timestamp = time.time()

        segment = self.vad.process_streaming_chunk(
            audio_chunk,
            channel_id=self.channel_id,
            channel_type=self.channel_type,
            abs_timestamp=abs_timestamp,
            speaker_label=self.speaker_label,
        )

        # 更新狀態
        if self.vad._muzzle.is_collecting:
            self._state = ChannelState.SPEECH
            self._last_speech_at = abs_timestamp
        else:
            if self._state == ChannelState.SPEECH:
                self._state = ChannelState.LISTENING

        if segment is not None:
            segment.speaker_name = self.speaker_name
            self._segments_produced += 1
            self._last_segment_at = time.time()
            self._state = ChannelState.LISTENING

            try:
                self.segment_callback(segment)
            except Exception as e:
                logger.error("[Channel:%s] segment_callback 失敗: %s", self.channel_id, e)

    def flush(self) -> None:
        """強制 flush VAD 殘留 buffer（通道停止時呼叫）。"""
        # 傳入最後一次 push_audio 的 audio 時間戳，讓 flush 段落的 end_timestamp
        # 與 audio 時間軸一致（非即時場景如 FileChannel 尤其重要）。
        segment = self.vad.flush_streaming(
            channel_id=self.channel_id,
            channel_type=self.channel_type,
            speaker_label=self.speaker_label,
            audio_timestamp=self._last_speech_at,
        )
        if segment is not None:
            segment.speaker_name = self.speaker_name
            self._segments_produced += 1
            self._last_segment_at = time.time()
            try:
                self.segment_callback(segment)
            except Exception as e:
                logger.error("[Channel:%s] flush callback 失敗: %s", self.channel_id, e)

    def get_status(self) -> ChannelStatus:
        """取得通道狀態快照。"""
        return ChannelStatus(
            channel_id=self.channel_id,
            channel_type=self.channel_type,
            state=self._state,
            speaker_label=self.speaker_label,
            segments_produced=self._segments_produced,
            vad_is_speech=self.vad._muzzle.is_collecting,
            buffer_duration=self.vad._muzzle.current_duration,
            last_speech_at=self._last_speech_at,
            last_segment_at=self._last_segment_at,
            created_at=self._created_at,
        )


# ──────────────────────────────────────────────────────────────
#  FileChannel — 預錄音檔通道（測試用）
# ──────────────────────────────────────────────────────────────


class FileChannel(AudioChannel):
    """從預錄音檔推送音訊的通道。

    用於測試和離線分析。start() 後在背景執行緒
    按 chunk_duration_ms 分段推送整個音檔。
    """

    def __init__(
        self,
        channel_id: str,
        audio_data: np.ndarray,
        vad: AdvancedVAD,
        segment_callback: Callable[[AudioSegment], None],
        sample_rate: int = 16000,
        chunk_duration_ms: int = 50,
        speaker_label: Optional[str] = None,
        speaker_name: Optional[str] = None,
        realtime: bool = False,
    ):
        super().__init__(
            channel_id=channel_id,
            channel_type=ChannelType.FILE,
            vad=vad,
            segment_callback=segment_callback,
            sample_rate=sample_rate,
            speaker_label=speaker_label,
            speaker_name=speaker_name,
        )
        self._audio_data = audio_data
        self._chunk_samples = int(sample_rate * chunk_duration_ms / 1000)
        self._realtime = realtime
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._done_event = threading.Event()  # 音檔推送完畢

    def start(self) -> None:
        """在背景執行緒推送音訊。"""
        if self._state != ChannelState.IDLE and self._state != ChannelState.STOPPED:
            return

        self._state = ChannelState.LISTENING
        self._stop_event.clear()
        self._done_event.clear()
        self._thread = threading.Thread(
            target=self._push_loop,
            name=f"FileChannel-{self.channel_id}",
            daemon=True,
        )
        self._thread.start()

    def wait_for_completion(self, timeout: Optional[float] = None) -> bool:
        """等待音檔推送完畢（不中斷）。

        Args:
            timeout: 最長等待秒數（None = 無限等待）

        Returns:
            True 若正常完成，False 若超時
        """
        return self._done_event.wait(timeout=timeout)

    def stop(self) -> None:
        """停止推送（可中斷，或等自然結束後呼叫）。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self.flush()
        self._state = ChannelState.STOPPED

    def _push_loop(self) -> None:
        """背景推送迴圈。"""
        base_time = time.time()
        offset = 0

        while offset < len(self._audio_data) and not self._stop_event.is_set():
            end = min(offset + self._chunk_samples, len(self._audio_data))
            chunk = self._audio_data[offset:end]

            chunk_time = base_time + offset / self.sample_rate
            self.push_audio(chunk, abs_timestamp=chunk_time)

            offset = end

            if self._realtime:
                # 模擬即時——等待 chunk 時長
                time.sleep(self._chunk_samples / self.sample_rate)

        # 推送完畢，flush 殘留
        self.flush()
        self._done_event.set()
        if not self._stop_event.is_set():
            self._state = ChannelState.STOPPED
