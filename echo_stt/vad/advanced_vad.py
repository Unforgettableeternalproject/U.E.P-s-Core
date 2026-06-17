"""
進階 VAD 模組 — 支援多通道串流處理

組件：
- SileroVAD: 基於 Silero 模型的高精度 VAD 後端
- AdaptiveThreshold: 自適應背景噪音閾值
- VadMuzzle: 語音段落狀態機（偵測 speech_start / speech_end）
- AdvancedVAD: 組合以上組件，繼承 VoiceActivityDetection 介面
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np

from echo_stt.core.multi_channel_schemas import AudioSegment, ChannelType
from echo_stt.vad.energy_vad import VoiceActivityDetection

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
#  SileroVAD — Silero VAD 模型包裝
# ──────────────────────────────────────────────────────────────


class SileroVAD:
    """Silero VAD 模型包裝器。

    使用 torch.hub 載入 snakers4/silero-vad 模型。
    比 energy-based VAD 準確得多，特別是在噪音環境中。

    注意：Silero 嚴格要求 16kHz 下每次 exactly 512 samples。
    本包裝器內部做 rebuffering，接受任意大小的 chunk。

    如果 Silero 不可用（沒裝 torch 或下載失敗），
    自動 fallback 到 energy-based。
    """

    # Silero 在 16kHz 下要求 exactly 512 samples per call
    WINDOW_SIZE_16K = 512

    def __init__(
        self,
        threshold: float = 0.5,
        sampling_rate: int = 16000,
    ):
        self.threshold = threshold
        self.sampling_rate = sampling_rate
        self._model = None
        self._available = False
        self._buffer = np.array([], dtype=np.float32)  # rebuffer 用

    def initialize(self) -> bool:
        """載入 Silero VAD 模型。"""
        try:
            import torch

            model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
            self._model = model
            self._available = True
            logger.info("[SileroVAD] 模型載入成功")
            return True
        except Exception as e:
            logger.warning("[SileroVAD] 載入失敗，將 fallback 到 energy VAD: %s", e)
            self._available = False
            return False

    @property
    def is_available(self) -> bool:
        return self._available

    def is_speech(self, audio_chunk: np.ndarray) -> Tuple[bool, float]:
        """判斷音訊 chunk 是否包含語音。

        接受任意大小的 chunk，內部 rebuffer 成 512-sample 批次
        送入 Silero 模型。取所有批次的最大機率作為結果。

        Args:
            audio_chunk: float32 array，16kHz mono

        Returns:
            (is_speech, confidence) — confidence 為 0.0~1.0
        """
        if not self._available or self._model is None:
            return False, 0.0

        try:
            import torch

            if audio_chunk.dtype != np.float32:
                audio_chunk = audio_chunk.astype(np.float32)

            # 加入 rebuffer
            self._buffer = np.concatenate([self._buffer, audio_chunk])

            ws = self.WINDOW_SIZE_16K
            max_confidence = 0.0

            # 每次取 512 samples 送入模型
            while len(self._buffer) >= ws:
                window = self._buffer[:ws]
                self._buffer = self._buffer[ws:]

                tensor = torch.from_numpy(window)
                prob = self._model(tensor, self.sampling_rate).item()
                if prob > max_confidence:
                    max_confidence = prob

            is_speech = max_confidence >= self.threshold
            return is_speech, float(max_confidence)

        except Exception as e:
            logger.debug("[SileroVAD] 推論失敗: %s", e)
            return False, 0.0

    def reset_states(self):
        """重置模型內部狀態（切換通道時需要）。"""
        if self._model is not None and hasattr(self._model, "reset_states"):
            self._model.reset_states()
        self._buffer = np.array([], dtype=np.float32)


# ──────────────────────────────────────────────────────────────
#  AdaptiveThreshold — 自適應背景噪音閾值
# ──────────────────────────────────────────────────────────────


class AdaptiveThreshold:
    """根據近期噪音環境動態調整 VAD 靈敏度。

    使用滑動視窗計算背景雜訊能量，在靜音時持續更新閾值。
    語音時不更新（避免語音能量汙染背景估計）。
    """

    def __init__(
        self,
        window_size: int = 50,
        adapt_rate: float = 0.05,
        initial_threshold: float = 0.001,
        min_threshold: float = 0.0001,
        max_threshold: float = 0.05,
        noise_multiplier: float = 3.0,
    ):
        self.window_size = window_size
        self.adapt_rate = adapt_rate
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.noise_multiplier = noise_multiplier

        self._noise_history: List[float] = []
        self._noise_estimate: float = initial_threshold / noise_multiplier
        self._threshold: float = initial_threshold

    def update(self, energy: float, is_speech: bool) -> None:
        """更新閾值。只在非語音（靜音/噪音）時更新。"""
        if not is_speech:
            self._noise_history.append(energy)
            if len(self._noise_history) > self.window_size:
                self._noise_history.pop(0)

            if self._noise_history:
                # 用中位數而非平均值，更抗離群值
                self._noise_estimate = float(np.median(self._noise_history))

                new_threshold = self._noise_estimate * self.noise_multiplier
                # 平滑更新
                self._threshold = (
                    (1 - self.adapt_rate) * self._threshold
                    + self.adapt_rate * new_threshold
                )
                self._threshold = np.clip(
                    self._threshold, self.min_threshold, self.max_threshold
                )

    def get_threshold(self) -> float:
        return self._threshold

    @property
    def noise_estimate(self) -> float:
        return self._noise_estimate

    def reset(self) -> None:
        self._noise_history.clear()


# ──────────────────────────────────────────────────────────────
#  VadMuzzle — 語音段落狀態機
# ──────────────────────────────────────────────────────────────


class MuzzleState(str, Enum):
    """VadMuzzle 狀態"""
    SILENCE = "silence"
    SPEECH = "speech"
    TRAILING_SILENCE = "trailing_silence"


class VadMuzzle:
    """語音段落偵測狀態機。

    管理 speech_start / speech_end 的判斷邏輯：

    狀態轉移：
        SILENCE → (偵測到語音) → SPEECH
        SPEECH → (靜音開始) → TRAILING_SILENCE
        TRAILING_SILENCE → (語音恢復) → SPEECH （續講）
        TRAILING_SILENCE → (靜音持續超過 trailing_silence_ms) → SILENCE （段落結束，emit）
        SPEECH → (超過 max_segment_ms) → SILENCE （強制截斷，emit）

    Args:
        trailing_silence_ms: 語音結束後等多久確認結束（預設 600ms）
        max_segment_ms: 單段最大長度，超過強制截斷（預設 30s）
        min_speech_ms: 最短語音長度，低於此忽略（預設 200ms）
    """

    def __init__(
        self,
        trailing_silence_ms: int = 600,
        max_segment_ms: int = 30000,
        min_speech_ms: int = 200,
    ):
        self.trailing_silence_s = trailing_silence_ms / 1000.0
        self.max_segment_s = max_segment_ms / 1000.0
        self.min_speech_s = min_speech_ms / 1000.0

        self._state = MuzzleState.SILENCE
        self._speech_start: Optional[float] = None
        self._silence_start: Optional[float] = None
        self._audio_buffer: List[np.ndarray] = []

    @property
    def state(self) -> MuzzleState:
        return self._state

    @property
    def is_collecting(self) -> bool:
        """是否正在收集音訊（SPEECH 或 TRAILING_SILENCE 中）"""
        return self._state in (MuzzleState.SPEECH, MuzzleState.TRAILING_SILENCE)

    @property
    def current_duration(self) -> float:
        """目前收集中的音訊時長"""
        if self._speech_start is None:
            return 0.0
        return time.time() - self._speech_start

    def push(
        self,
        is_speech: bool,
        timestamp: float,
        audio_chunk: Optional[np.ndarray] = None,
    ) -> Optional[Tuple[float, float, Optional[np.ndarray]]]:
        """推入一個判斷結果。

        Args:
            is_speech: VAD 判斷結果
            timestamp: 絕對時間戳
            audio_chunk: 對應的音訊 chunk（可選，用於收集完整段落）

        Returns:
            若段落完成，回傳 (speech_start, speech_end, merged_audio)；
            否則 None。
        """
        result = None

        if self._state == MuzzleState.SILENCE:
            if is_speech:
                self._state = MuzzleState.SPEECH
                self._speech_start = timestamp
                self._silence_start = None
                self._audio_buffer = []
                if audio_chunk is not None:
                    self._audio_buffer.append(audio_chunk)

        elif self._state == MuzzleState.SPEECH:
            if audio_chunk is not None:
                self._audio_buffer.append(audio_chunk)

            if not is_speech:
                self._state = MuzzleState.TRAILING_SILENCE
                self._silence_start = timestamp
            elif (timestamp - self._speech_start) >= self.max_segment_s:
                # 強制截斷
                result = self._emit(timestamp)

        elif self._state == MuzzleState.TRAILING_SILENCE:
            if audio_chunk is not None:
                self._audio_buffer.append(audio_chunk)

            if is_speech:
                # 語音恢復，繼續收集
                self._state = MuzzleState.SPEECH
                self._silence_start = None
            else:
                silence_duration = timestamp - self._silence_start
                if silence_duration >= self.trailing_silence_s:
                    # 確認段落結束
                    result = self._emit(self._silence_start)

        return result

    def _emit(self, end_timestamp: float) -> Optional[Tuple[float, float, Optional[np.ndarray]]]:
        """產生一個完成的段落。"""
        start = self._speech_start
        self._state = MuzzleState.SILENCE
        self._speech_start = None
        self._silence_start = None

        if start is None:
            return None

        duration = end_timestamp - start
        if duration < self.min_speech_s:
            self._audio_buffer = []
            return None

        merged = None
        if self._audio_buffer:
            merged = np.concatenate(self._audio_buffer)
        self._audio_buffer = []

        return (start, end_timestamp, merged)

    def flush(self, current_timestamp: float) -> Optional[Tuple[float, float, Optional[np.ndarray]]]:
        """強制清空——用於 session 結束時。"""
        if self._speech_start is not None:
            return self._emit(current_timestamp)
        return None

    def reset(self) -> None:
        """重置狀態機。"""
        self._state = MuzzleState.SILENCE
        self._speech_start = None
        self._silence_start = None
        self._audio_buffer = []


# ──────────────────────────────────────────────────────────────
#  AdvancedVAD — 組合所有組件
# ──────────────────────────────────────────────────────────────


class AdvancedVAD(VoiceActivityDetection):
    """進階 VAD，繼承現有介面並擴展串流處理能力。

    後端優先用 SileroVAD，fallback 到 energy-based。
    新增 process_streaming_chunk() 方法供多通道管線使用。
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        sensitivity: float = 0.7,
        min_speech_duration: float = 0.3,
        use_silero: bool = True,
        silero_threshold: float = 0.5,
        trailing_silence_ms: int = 600,
        max_segment_ms: int = 30000,
        min_speech_ms: int = 200,
        adaptive_threshold: bool = True,
    ):
        super().__init__(sample_rate, sensitivity, min_speech_duration)

        self.use_silero = use_silero
        self._silero: Optional[SileroVAD] = None
        self._adaptive: Optional[AdaptiveThreshold] = None
        self._muzzle = VadMuzzle(
            trailing_silence_ms=trailing_silence_ms,
            max_segment_ms=max_segment_ms,
            min_speech_ms=min_speech_ms,
        )

        if use_silero:
            self._silero = SileroVAD(
                threshold=silero_threshold,
                sampling_rate=sample_rate,
            )

        if adaptive_threshold:
            self._adaptive = AdaptiveThreshold(
                initial_threshold=self.energy_threshold,
            )

        self._sequence_counter: int = 0

    def initialize(self) -> bool:
        """初始化所有子組件。"""
        base_ok = super().initialize()

        if self._silero is not None:
            silero_ok = self._silero.initialize()
            if not silero_ok:
                logger.info("[AdvancedVAD] Silero 不可用，使用 energy fallback")
                self._silero = None

        return base_ok

    @property
    def backend_name(self) -> str:
        if self._silero is not None and self._silero.is_available:
            return "silero"
        return "energy"

    def is_speech_advanced(self, audio_chunk: np.ndarray) -> Tuple[bool, float]:
        """進階語音偵測，回傳 (is_speech, confidence)。

        優先使用 Silero，fallback 到 energy。
        """
        # 確保是 float32
        if audio_chunk.dtype == np.int16:
            audio_f32 = audio_chunk.astype(np.float32) / 32768.0
        else:
            audio_f32 = audio_chunk.astype(np.float32)

        # Silero 優先
        if self._silero is not None and self._silero.is_available:
            is_speech, confidence = self._silero.is_speech(audio_f32)
        else:
            # Energy fallback
            energy = self._compute_energy(audio_f32)
            threshold = (
                self._adaptive.get_threshold()
                if self._adaptive
                else self.energy_threshold
            )
            is_speech = energy > threshold
            confidence = min(energy / max(threshold, 1e-10), 1.0) if is_speech else 0.0

            # 更新自適應閾值
            if self._adaptive:
                self._adaptive.update(energy, is_speech)

        return is_speech, confidence

    def process_streaming_chunk(
        self,
        audio_chunk: np.ndarray,
        channel_id: str,
        channel_type: ChannelType = ChannelType.LOCAL_MIC,
        abs_timestamp: Optional[float] = None,
        speaker_label: Optional[str] = None,
    ) -> Optional[AudioSegment]:
        """串流處理介面——推入一個音訊 chunk，完成段落時回傳 AudioSegment。

        這是多通道管線的核心介面。每個 channel 應該有自己的
        AdvancedVAD 實例（各自維護 muzzle 狀態）。

        Args:
            audio_chunk: int16 或 float32 音訊 chunk
            channel_id: 通道 ID
            channel_type: 通道類型
            abs_timestamp: 絕對時間戳（預設 time.time()）
            speaker_label: 說話人標籤（Discord 場景）

        Returns:
            AudioSegment 若段落完成，否則 None
        """
        if abs_timestamp is None:
            abs_timestamp = time.time()

        # 確保 int16 for buffer
        if audio_chunk.dtype == np.float32:
            chunk_int16 = (audio_chunk * 32768).clip(-32768, 32767).astype(np.int16)
        else:
            chunk_int16 = audio_chunk.astype(np.int16)

        is_speech, confidence = self.is_speech_advanced(audio_chunk)

        result = self._muzzle.push(is_speech, abs_timestamp, chunk_int16)

        if result is not None:
            start_ts, end_ts, merged_audio = result
            if merged_audio is not None and len(merged_audio) > 0:
                self._sequence_counter += 1
                return AudioSegment.from_numpy(
                    merged_audio,
                    channel_id=channel_id,
                    channel_type=channel_type,
                    start_timestamp=start_ts,
                    end_timestamp=end_ts,
                    sample_rate=self.sample_rate,
                    speaker_label=speaker_label,
                    sequence_num=self._sequence_counter,
                )

        return None

    def flush_streaming(
        self,
        channel_id: str,
        channel_type: ChannelType = ChannelType.LOCAL_MIC,
        speaker_label: Optional[str] = None,
    ) -> Optional[AudioSegment]:
        """強制清空 muzzle buffer（session 結束時呼叫）。"""
        result = self._muzzle.flush(time.time())
        if result is not None:
            start_ts, end_ts, merged_audio = result
            if merged_audio is not None and len(merged_audio) > 0:
                self._sequence_counter += 1
                return AudioSegment.from_numpy(
                    merged_audio,
                    channel_id=channel_id,
                    channel_type=channel_type,
                    start_timestamp=start_ts,
                    end_timestamp=end_ts,
                    sample_rate=self.sample_rate,
                    speaker_label=speaker_label,
                    sequence_num=self._sequence_counter,
                )
        return None

    def reset_streaming(self) -> None:
        """重置串流狀態（切換通道或重新開始時）。"""
        self._muzzle.reset()
        if self._silero is not None:
            self._silero.reset_states()
        if self._adaptive is not None:
            self._adaptive.reset()
        self._sequence_counter = 0
