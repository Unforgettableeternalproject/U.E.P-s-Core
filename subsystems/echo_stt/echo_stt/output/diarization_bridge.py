"""
DiarizationBridge — 說話人辨識橋接

負責把 AudioSegment 的 speaker_label 與 SpeakerIdentification 的輸出銜接。

- local_mic 場景：segment 沒有 speaker_label → 呼叫 SpeakerIdentification
- Discord 場景：segment 已有 user_id → 直接 pass-through
"""

from __future__ import annotations

import logging
from typing import Optional

from echo_stt.core.multi_channel_schemas import AudioSegment, ChannelType, TranscriptEntry

logger = logging.getLogger(__name__)


class DiarizationBridge:
    """說話人辨識橋接。

    Usage:
        bridge = DiarizationBridge(speaker_module=my_speaker_id)

        # 在 TranscriptEntry 建立後，resolve speaker_label
        entry = bridge.resolve_entry(entry, segment)
    """

    def __init__(self, speaker_module=None):
        """
        Args:
            speaker_module: SpeakerIdentification 實例（可選）。
                          若為 None，local_mic 場景的 speaker 會保持 "unknown"。
        """
        self.speaker_module = speaker_module
        self._speaker_counter = 0
        self._speaker_cache: dict[str, str] = {}  # embedding_hash → speaker_label

    def resolve_speaker(self, segment: AudioSegment) -> tuple[str, str]:
        """為 segment 解析說話人。

        Returns:
            (speaker_label, speaker_name) 元組
        """
        # Discord：直接用 user_id
        if segment.channel_type == ChannelType.DISCORD:
            label = segment.speaker_label or segment.channel_id
            name = segment.speaker_name or label
            return label, name

        # 已有 speaker_label（可能由 channel 預設）
        if segment.speaker_label and segment.speaker_label != "unknown":
            name = segment.speaker_name or segment.speaker_label
            return segment.speaker_label, name

        # local_mic 且有 speaker_module → 辨識
        if self.speaker_module is not None:
            return self._identify_with_module(segment)

        # 沒有 speaker_module → 用 channel_id 當作 label
        return segment.channel_id, segment.channel_id

    def resolve_entry(
        self, entry: TranscriptEntry, segment: AudioSegment
    ) -> TranscriptEntry:
        """更新 TranscriptEntry 的 speaker 欄位。"""
        label, name = self.resolve_speaker(segment)
        entry.speaker_label = label
        entry.speaker_name = name
        return entry

    def _identify_with_module(self, segment: AudioSegment) -> tuple[str, str]:
        """用 SpeakerIdentification 模組辨識說話人。"""
        try:
            audio_np = segment.to_numpy()
            if hasattr(self.speaker_module, "identify_speaker"):
                result = self.speaker_module.identify_speaker(audio_np)
                if result and hasattr(result, "speaker_id"):
                    return result.speaker_id, result.speaker_id
        except Exception as e:
            logger.warning("[DiarizationBridge] 辨識失敗: %s", e)

        # Fallback：遞增編號
        self._speaker_counter += 1
        label = f"speaker_{self._speaker_counter:03d}"
        return label, label
