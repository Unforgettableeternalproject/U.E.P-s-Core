"""
ChannelManager — 多通道生命週期管理

統一管理所有音訊通道（LocalMic / Discord / File），
彙總各通道產生的 AudioSegment 到統一的 callback。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from echo_stt.core.multi_channel_schemas import (
    AudioSegment,
    ChannelState,
    ChannelStatus,
    ChannelType,
)
from echo_stt.channel.base import AudioChannel, FileChannel
from echo_stt.channel.local_mic import LocalMicChannel
from echo_stt.channel.discord_channel import DiscordChannel
from echo_stt.vad.advanced_vad import AdvancedVAD

logger = logging.getLogger(__name__)


class ChannelManager:
    """管理所有音訊通道的生命週期。

    職責：
    1. 建立 / 移除 channel（每個 channel 有獨立的 VAD 實例）
    2. 彙總所有 channel 的 AudioSegment 到統一 segment_callback
    3. 偵測多人同時說話（overlapping speech）
    4. 提供通道狀態查詢

    Usage:
        manager = ChannelManager(
            vad_config={...},
            segment_callback=my_callback,
        )
        manager.add_local_mic("mic_0")
        manager.add_file_channel("test", audio_data)
        manager.start_all()
        ...
        manager.stop_all()
    """

    def __init__(
        self,
        segment_callback: Callable[[AudioSegment], None],
        vad_config: Optional[dict] = None,
        max_channels: int = 8,
    ):
        self.segment_callback = segment_callback
        self.vad_config = vad_config or {}
        self.max_channels = max_channels

        self._channels: Dict[str, AudioChannel] = {}
        self._lock = threading.Lock()

        # 統計
        self._total_segments = 0
        self._created_at = time.time()

    def _create_vad(self) -> AdvancedVAD:
        """為新通道建立獨立的 AdvancedVAD 實例。"""
        vad = AdvancedVAD(
            sample_rate=self.vad_config.get("sample_rate", 16000),
            sensitivity=self.vad_config.get("sensitivity", 0.7),
            min_speech_duration=self.vad_config.get("min_speech_duration", 0.3),
            use_silero=self.vad_config.get("use_silero", True),
            silero_threshold=self.vad_config.get("silero_threshold", 0.5),
            trailing_silence_ms=self.vad_config.get("trailing_silence_ms", 600),
            max_segment_ms=self.vad_config.get("max_segment_ms", 30000),
            min_speech_ms=self.vad_config.get("min_speech_ms", 200),
            adaptive_threshold=self.vad_config.get("adaptive_threshold", True),
            # 自適應 trailing silence
            adaptive_trailing=self.vad_config.get("adaptive_trailing", False),
            soft_max_segment_ms=self.vad_config.get("soft_max_segment_ms", 9000),
            pre_speech_padding_chunks=self.vad_config.get("pre_speech_padding_chunks", 3),
            short_seg_threshold_ms=self.vad_config.get("short_seg_threshold_ms", 3000),
            medium_seg_threshold_ms=self.vad_config.get("medium_seg_threshold_ms", 10000),
            long_seg_threshold_ms=self.vad_config.get("long_seg_threshold_ms", 20000),
            short_trailing_ms=self.vad_config.get("short_trailing_ms", 1100),
            medium_trailing_ms=self.vad_config.get("medium_trailing_ms", 650),
            long_trailing_ms=self.vad_config.get("long_trailing_ms", 450),
            very_long_trailing_ms=self.vad_config.get("very_long_trailing_ms", 300),
        )
        vad.initialize()
        return vad

    def _on_segment(self, segment: AudioSegment) -> None:
        """統一的 segment callback（所有 channel 共用）。"""
        self._total_segments += 1
        try:
            self.segment_callback(segment)
        except Exception as e:
            logger.error("[ChannelManager] segment_callback 失敗: %s", e)

    # ── Channel 管理 ────────────────────────────────────────

    def add_local_mic(
        self,
        channel_id: str,
        device_index: Optional[int] = None,
        speaker_label: Optional[str] = None,
        speaker_name: Optional[str] = None,
        chunk_duration_ms: int = 50,
    ) -> str:
        """新增本地麥克風通道。

        Returns:
            channel_id
        """
        with self._lock:
            if channel_id in self._channels:
                raise ValueError(f"Channel '{channel_id}' already exists")
            if len(self._channels) >= self.max_channels:
                raise RuntimeError(
                    f"Max channels ({self.max_channels}) reached"
                )

            vad = self._create_vad()
            channel = LocalMicChannel(
                channel_id=channel_id,
                vad=vad,
                segment_callback=self._on_segment,
                device_index=device_index,
                sample_rate=self.vad_config.get("sample_rate", 16000),
                chunk_duration_ms=chunk_duration_ms,
                speaker_label=speaker_label,
                speaker_name=speaker_name,
            )
            self._channels[channel_id] = channel
            logger.info("[ChannelManager] 新增 LocalMic: %s", channel_id)
            return channel_id

    def add_file_channel(
        self,
        channel_id: str,
        audio_data: np.ndarray,
        sample_rate: int = 16000,
        speaker_label: Optional[str] = None,
        speaker_name: Optional[str] = None,
        chunk_duration_ms: int = 50,
        realtime: bool = False,
    ) -> str:
        """新增預錄音檔通道（測試用）。"""
        with self._lock:
            if channel_id in self._channels:
                raise ValueError(f"Channel '{channel_id}' already exists")
            if len(self._channels) >= self.max_channels:
                raise RuntimeError(
                    f"Max channels ({self.max_channels}) reached"
                )

            vad = self._create_vad()
            channel = FileChannel(
                channel_id=channel_id,
                audio_data=audio_data,
                vad=vad,
                segment_callback=self._on_segment,
                sample_rate=sample_rate,
                chunk_duration_ms=chunk_duration_ms,
                speaker_label=speaker_label,
                speaker_name=speaker_name,
                realtime=realtime,
            )
            self._channels[channel_id] = channel
            logger.info("[ChannelManager] 新增 FileChannel: %s", channel_id)
            return channel_id

    def add_discord_channel(
        self,
        channel_id: str,
        speaker_label: Optional[str] = None,
        speaker_name: Optional[str] = None,
    ) -> str:
        """新增 Discord 語音通道。

        建立一個 DiscordChannel，接收 Discord voice sink 推送的
        48kHz/stereo PCM，內部自動轉換為 16kHz/mono 後餵入 VAD pipeline。

        Args:
            channel_id: 通道唯一 ID（建議使用 Discord user_id）
            speaker_label: 說話人唯一標籤（通常是 Discord user_id）
            speaker_name: 說話人人類可讀名稱（通常是 Discord username）

        Returns:
            channel_id

        Raises:
            ValueError: 若 channel_id 已存在
            RuntimeError: 若已達到最大通道數
        """
        with self._lock:
            if channel_id in self._channels:
                raise ValueError(f"Channel '{channel_id}' already exists")
            if len(self._channels) >= self.max_channels:
                raise RuntimeError(
                    f"Max channels ({self.max_channels}) reached"
                )

            vad = self._create_vad()
            channel = DiscordChannel(
                channel_id=channel_id,
                vad=vad,
                segment_callback=self._on_segment,
                speaker_label=speaker_label,
                speaker_name=speaker_name,
            )
            self._channels[channel_id] = channel
            logger.info("[ChannelManager] 新增 DiscordChannel: %s", channel_id)
            return channel_id

    def remove_channel(self, channel_id: str) -> bool:
        """移除通道（先停止再移除）。"""
        with self._lock:
            channel = self._channels.pop(channel_id, None)

        if channel is None:
            logger.warning("[ChannelManager] Channel '%s' 不存在", channel_id)
            return False

        if channel.is_active:
            channel.stop()

        logger.info("[ChannelManager] 移除 Channel: %s", channel_id)
        return True

    def get_channel(self, channel_id: str) -> Optional[AudioChannel]:
        """取得通道實例。"""
        return self._channels.get(channel_id)

    # ── 生命週期 ────────────────────────────────────────────

    def start_all(self) -> None:
        """啟動所有通道。"""
        with self._lock:
            channels = list(self._channels.values())

        for ch in channels:
            if not ch.is_active:
                try:
                    ch.start()
                except Exception as e:
                    logger.error(
                        "[ChannelManager] Channel '%s' 啟動失敗: %s",
                        ch.channel_id, e,
                    )

        logger.info(
            "[ChannelManager] 啟動 %d 個通道",
            sum(1 for ch in channels if ch.is_active),
        )

    def stop_all(self) -> None:
        """停止所有通道。"""
        with self._lock:
            channels = list(self._channels.values())

        for ch in channels:
            if ch.is_active:
                try:
                    ch.stop()
                except Exception as e:
                    logger.error(
                        "[ChannelManager] Channel '%s' 停止失敗: %s",
                        ch.channel_id, e,
                    )

        logger.info("[ChannelManager] 所有通道已停止")

    def start_channel(self, channel_id: str) -> bool:
        """啟動單一通道。"""
        ch = self._channels.get(channel_id)
        if ch is None:
            return False
        ch.start()
        return True

    def stop_channel(self, channel_id: str) -> bool:
        """停止單一通道。"""
        ch = self._channels.get(channel_id)
        if ch is None:
            return False
        ch.stop()
        return True

    # ── 查詢 ────────────────────────────────────────────────

    def get_active_channels(self) -> List[str]:
        """取得所有活躍通道的 ID。"""
        return [cid for cid, ch in self._channels.items() if ch.is_active]

    def get_all_channel_ids(self) -> List[str]:
        """取得所有通道 ID。"""
        return list(self._channels.keys())

    def get_channel_status(self, channel_id: str) -> Optional[ChannelStatus]:
        """取得單一通道的狀態。"""
        ch = self._channels.get(channel_id)
        return ch.get_status() if ch else None

    def get_all_status(self) -> Dict[str, ChannelStatus]:
        """取得所有通道的狀態。"""
        return {cid: ch.get_status() for cid, ch in self._channels.items()}

    def detect_overlapping_speech(self) -> List[Tuple[str, str]]:
        """偵測目前同時有語音的通道對。

        Returns:
            列表 of (channel_id_a, channel_id_b) — 目前同時在講話的通道對
        """
        speaking = [
            cid for cid, ch in self._channels.items()
            if ch.state == ChannelState.SPEECH
        ]

        pairs = []
        for i in range(len(speaking)):
            for j in range(i + 1, len(speaking)):
                pairs.append((speaking[i], speaking[j]))
        return pairs

    @property
    def channel_count(self) -> int:
        return len(self._channels)

    @property
    def active_count(self) -> int:
        return sum(1 for ch in self._channels.values() if ch.is_active)

    @property
    def total_segments(self) -> int:
        return self._total_segments

    def shutdown(self) -> None:
        """完全關閉——停止所有通道並清空。"""
        self.stop_all()
        with self._lock:
            self._channels.clear()
        logger.info("[ChannelManager] 已關閉")
