"""
本地麥克風通道 — PyAudio callback 模式

使用非阻塞 callback 模式接收音訊，避免多通道互相阻塞。
每個 LocalMicChannel 對應一個實體麥克風裝置。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import numpy as np

from echo_stt.core.multi_channel_schemas import AudioSegment, ChannelState, ChannelType
from echo_stt.channel.base import AudioChannel
from echo_stt.vad.advanced_vad import AdvancedVAD

logger = logging.getLogger(__name__)

try:
    import pyaudio
    _HAS_PYAUDIO = True
except ImportError:
    _HAS_PYAUDIO = False


class LocalMicChannel(AudioChannel):
    """本地麥克風通道。

    使用 PyAudio 的 callback（非阻塞）模式接收音訊。
    這比 engine.py 的 blocking read 更適合多通道——
    不會因為一個通道在讀就阻塞其他通道。

    Args:
        channel_id: 通道唯一 ID
        vad: 該通道專屬的 AdvancedVAD 實例
        segment_callback: 段落完成時的 callback
        device_index: PyAudio 裝置索引（None = 系統預設）
        sample_rate: 取樣率（預設 16000）
        chunk_duration_ms: 每個 callback chunk 的時長（預設 50ms）
    """

    def __init__(
        self,
        channel_id: str,
        vad: AdvancedVAD,
        segment_callback: Callable[[AudioSegment], None],
        device_index: Optional[int] = None,
        sample_rate: int = 16000,
        chunk_duration_ms: int = 50,
        speaker_label: Optional[str] = None,
        speaker_name: Optional[str] = None,
    ):
        super().__init__(
            channel_id=channel_id,
            channel_type=ChannelType.LOCAL_MIC,
            vad=vad,
            segment_callback=segment_callback,
            sample_rate=sample_rate,
            speaker_label=speaker_label,
            speaker_name=speaker_name,
        )
        self.device_index = device_index
        self.chunk_duration_ms = chunk_duration_ms
        self._chunk_samples = int(sample_rate * chunk_duration_ms / 1000)

        self._pa: Optional["pyaudio.PyAudio"] = None
        self._stream = None

    def start(self) -> None:
        """開啟 PyAudio stream（callback 模式）。"""
        if not _HAS_PYAUDIO:
            logger.error("[LocalMic:%s] PyAudio 未安裝", self.channel_id)
            self._state = ChannelState.ERROR
            return

        if self.is_active:
            logger.warning("[LocalMic:%s] 已在運行中", self.channel_id)
            return

        try:
            self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=self._chunk_samples,
                stream_callback=self._audio_callback,
            )
            self._stream.start_stream()
            self._state = ChannelState.LISTENING
            logger.info(
                "[LocalMic:%s] 開始監聽 (device=%s, rate=%d, chunk=%dms)",
                self.channel_id,
                self.device_index or "default",
                self.sample_rate,
                self.chunk_duration_ms,
            )
        except Exception as e:
            logger.error("[LocalMic:%s] 啟動失敗: %s", self.channel_id, e)
            self._state = ChannelState.ERROR
            self._cleanup_pyaudio()

    def stop(self) -> None:
        """停止 PyAudio stream 並清理資源。"""
        if self._stream is not None:
            try:
                if self._stream.is_active():
                    self._stream.stop_stream()
                self._stream.close()
            except Exception as e:
                logger.debug("[LocalMic:%s] stream 關閉時發生: %s", self.channel_id, e)
            finally:
                self._stream = None

        self.flush()
        self._cleanup_pyaudio()
        self._state = ChannelState.STOPPED
        logger.info("[LocalMic:%s] 已停止", self.channel_id)

    def _audio_callback(self, in_data, frame_count, time_info, status_flags):
        """PyAudio callback — 在獨立執行緒中被呼叫。

        NOTE: 這個函式在 PyAudio 的音訊執行緒中運行，
        不是主執行緒。push_audio() 內部的 VAD 和 callback
        都會在這個執行緒中執行。
        """
        if self._state == ChannelState.STOPPED:
            return (None, pyaudio.paComplete)

        try:
            audio_chunk = np.frombuffer(in_data, dtype=np.int16)
            self.push_audio(audio_chunk)
        except Exception as e:
            logger.error("[LocalMic:%s] callback 處理失敗: %s", self.channel_id, e)

        return (None, pyaudio.paContinue)

    def _cleanup_pyaudio(self) -> None:
        """清理 PyAudio 實例。"""
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            finally:
                self._pa = None
