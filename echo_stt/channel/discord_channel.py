"""
Discord 語音通道 — 接收 Discord voice sink 推送的 PCM 音訊

Discord 送來的原始格式：48kHz、16-bit、stereo
echo_stt 管線需要：16kHz、16-bit、mono

此模組負責格式轉換，並將轉換後的音訊餵入 VAD pipeline。
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import numpy as np

from echo_stt.core.multi_channel_schemas import AudioSegment, ChannelState, ChannelType
from echo_stt.channel.base import AudioChannel
from echo_stt.vad.advanced_vad import AdvancedVAD

logger = logging.getLogger(__name__)

# 嘗試匯入 scipy 以進行正確的 anti-aliasing 降採樣
try:
    from scipy.signal import resample_poly as _resample_poly
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False
    logger.warning(
        "[DiscordChannel] scipy 未安裝，將使用 decimation（每 3 取 1）做降採樣。"
        "此方式可能有 aliasing，建議安裝 scipy 以獲得較佳音質。"
    )

# Discord 原始音訊規格
_DISCORD_SAMPLE_RATE = 48000   # Hz
_DISCORD_CHANNELS = 2           # stereo
_DISCORD_BITS = 16              # bit depth

# 降採樣比例：48kHz → 16kHz（3:1）
_DOWN_UP = 1
_DOWN_DOWN = 3


def _convert_discord_pcm(pcm_bytes: bytes) -> np.ndarray:
    """將 Discord 48kHz/stereo PCM bytes 轉換為 16kHz/mono int16 array。

    轉換步驟：
    1. bytes → numpy int16 array（interleaved stereo：L R L R ...）
    2. stereo → mono（取左右聲道平均，避免裁切失真）
    3. 48kHz → 16kHz（使用 scipy resample_poly 或 decimation fallback）

    Args:
        pcm_bytes: Discord AudioSink 傳來的原始 PCM bytes
                   格式：48kHz、16-bit、stereo（interleaved）
                   Discord 每 20ms 送一個封包 = 3840 bytes

    Returns:
        16kHz、mono、int16 numpy array
    """
    # 1. bytes → int16 array（interleaved stereo）
    audio_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)

    # 2. stereo → mono（reshape 為 [N, 2]，取左右聲道平均）
    if audio_int16.size % 2 != 0:
        # 資料長度不是偶數（理論上不應發生，但做防禦性處理）
        audio_int16 = audio_int16[:-1]

    stereo = audio_int16.reshape(-1, 2)
    # 轉為 float32 再平均，避免兩個 int16 相加溢位
    mono_float = stereo.astype(np.float32).mean(axis=1)

    # 3. 48kHz → 16kHz 降採樣
    if _HAS_SCIPY:
        # scipy resample_poly 做 anti-aliasing 低通濾波後再降採樣
        mono_16k_float = _resample_poly(mono_float, _DOWN_UP, _DOWN_DOWN)
    else:
        # Fallback：每 3 個樣本取 1（decimation，有 aliasing）
        mono_16k_float = mono_float[::_DOWN_DOWN]

    # 4. 轉回 int16（clip 避免溢位）
    mono_16k = np.clip(mono_16k_float, -32768, 32767).astype(np.int16)

    return mono_16k


class DiscordChannel(AudioChannel):
    """Discord 語音通道。

    接收 Discord voice sink 推送的 48kHz/stereo PCM，
    轉換為 16kHz/mono 後餵入 VAD pipeline。

    與 FileChannel / LocalMicChannel 不同，DiscordChannel 不需要背景執行緒：
    音訊由外部 Discord AudioSink 主動呼叫 receive_audio() 推送進來。

    Usage:
        channel = DiscordChannel(
            channel_id="user_12345",
            vad=vad_instance,
            segment_callback=on_segment,
            speaker_label="user_12345",
            speaker_name="Bernie",
        )
        channel.start()

        # 在 Discord AudioSink.write() 中呼叫：
        channel.receive_audio(pcm_bytes)

        # 使用者離開時：
        channel.stop()
    """

    def __init__(
        self,
        channel_id: str,
        vad: AdvancedVAD,
        segment_callback: Callable[[AudioSegment], None],
        speaker_label: Optional[str] = None,
        speaker_name: Optional[str] = None,
    ):
        """
        Args:
            channel_id: 通道唯一 ID（建議使用 Discord user_id）
            vad: 該通道專屬的 AdvancedVAD 實例（獨立狀態）
            segment_callback: 音訊段落完成時的 callback
            speaker_label: 說話人唯一標籤（通常是 Discord user_id）
            speaker_name: 說話人人類可讀名稱（通常是 Discord username）
        """
        super().__init__(
            channel_id=channel_id,
            channel_type=ChannelType.DISCORD,
            vad=vad,
            segment_callback=segment_callback,
            sample_rate=16000,  # 轉換後的目標取樣率
            speaker_label=speaker_label,
            speaker_name=speaker_name,
        )

    def receive_audio(self, pcm_bytes: bytes) -> None:
        """從 Discord AudioSink 接收 48kHz stereo PCM bytes。

        此方法應在 Discord AudioSink 的 write() 或對應回呼中呼叫。
        內部自動完成格式轉換（stereo→mono、48kHz→16kHz），
        然後將轉換後的音訊推入 VAD pipeline。

        Args:
            pcm_bytes: Discord voice sink 傳來的原始 PCM bytes。
                       格式：48kHz、16-bit、stereo（interleaved）。
                       Discord 每 20ms 一個封包（= 3840 bytes）。

        Note:
            若通道尚未啟動（state != LISTENING/SPEECH），音訊將被靜默丟棄。
        """
        if not self.is_active:
            # 通道尚未啟動或已停止，丟棄音訊
            return

        if not pcm_bytes:
            return

        try:
            # 格式轉換：48kHz stereo → 16kHz mono
            mono_16k = _convert_discord_pcm(pcm_bytes)

            if mono_16k.size == 0:
                return

            # 推入 VAD pipeline
            self.push_audio(mono_16k)

        except Exception as e:
            logger.error(
                "[DiscordChannel:%s] receive_audio 處理失敗: %s",
                self.channel_id, e,
            )

    def start(self) -> None:
        """啟動通道（設定為 LISTENING 狀態）。

        DiscordChannel 不需要背景執行緒，只需設定狀態。
        音訊由外部 Discord AudioSink 主動呼叫 receive_audio() 推送。
        """
        if self._state == ChannelState.STOPPED:
            logger.debug("[DiscordChannel:%s] 從 STOPPED 重新啟動", self.channel_id)

        self._state = ChannelState.LISTENING
        logger.info(
            "[DiscordChannel:%s] 已啟動 (speaker=%s)",
            self.channel_id,
            self.speaker_label or "未知",
        )

    def stop(self) -> None:
        """停止通道，flush 殘留 buffer。

        呼叫此方法後，receive_audio() 推送的音訊將被丟棄。
        """
        self.flush()
        self._state = ChannelState.STOPPED
        logger.info("[DiscordChannel:%s] 已停止", self.channel_id)
