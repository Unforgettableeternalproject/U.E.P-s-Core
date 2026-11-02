"""
音訊雜訊處理 — 多人場景的品質前處理

提供：
- 高通濾波（移除低頻環境噪音 / 風切聲）
- 音量正規化（避免不同說話人音量差異影響辨識）
- 可選頻譜減法（需 noisereduce 套件）
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class NoiseFilter:
    """多人場景的音訊品質前處理。

    處理順序：
    1. 高通濾波（移除 < cutoff Hz 的低頻）
    2. 音量正規化（peak normalization 到 target_db）
    3. 可選頻譜減法（需要 noisereduce 套件）

    所有操作都在 float32 [-1.0, 1.0] 範圍內進行。
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        highpass_cutoff: int = 80,
        enable_highpass: bool = True,
        enable_normalize: bool = True,
        target_peak: float = 0.9,
        use_spectral_subtraction: bool = False,
    ):
        self.sample_rate = sample_rate
        self.highpass_cutoff = highpass_cutoff
        self.enable_highpass = enable_highpass
        self.enable_normalize = enable_normalize
        self.target_peak = target_peak
        self.use_spectral_subtraction = use_spectral_subtraction

        # Pre-compute highpass filter coefficients (simple 1st-order IIR)
        self._hp_alpha = self._compute_hp_alpha(highpass_cutoff, sample_rate)

        # Check noisereduce availability
        self._has_noisereduce = False
        if use_spectral_subtraction:
            try:
                import noisereduce  # noqa: F401
                self._has_noisereduce = True
            except ImportError:
                logger.warning(
                    "[NoiseFilter] noisereduce 未安裝，頻譜減法不可用。"
                    "  Install with: pip install noisereduce"
                )

    @staticmethod
    def _compute_hp_alpha(cutoff: float, sample_rate: int) -> float:
        """計算一階高通濾波器的 alpha 係數。"""
        if cutoff <= 0 or sample_rate <= 0:
            return 1.0
        dt = 1.0 / sample_rate
        rc = 1.0 / (2.0 * np.pi * cutoff)
        return rc / (rc + dt)

    def highpass_filter(self, audio: np.ndarray) -> np.ndarray:
        """一階 IIR 高通濾波。

        移除低於 cutoff Hz 的頻率成分（風切聲、空調嗡嗡聲等）。
        """
        if not self.enable_highpass or len(audio) < 2:
            return audio

        alpha = self._hp_alpha
        output = np.empty_like(audio)
        output[0] = audio[0]

        for i in range(1, len(audio)):
            output[i] = alpha * (output[i - 1] + audio[i] - audio[i - 1])

        return output

    def normalize(self, audio: np.ndarray) -> np.ndarray:
        """Peak normalization — 將最大振幅縮放到 target_peak。

        避免不同說話人 / 麥克風距離造成的音量差異。
        """
        if not self.enable_normalize or len(audio) == 0:
            return audio

        peak = np.max(np.abs(audio))
        if peak < 1e-8:  # 幾乎靜音，不處理
            return audio

        return audio * (self.target_peak / peak)

    def spectral_subtraction(self, audio: np.ndarray) -> np.ndarray:
        """頻譜減法降噪（需要 noisereduce 套件）。"""
        if not self.use_spectral_subtraction or not self._has_noisereduce:
            return audio

        try:
            import noisereduce as nr
            return nr.reduce_noise(
                y=audio,
                sr=self.sample_rate,
                stationary=True,
                prop_decrease=0.75,
            )
        except Exception as e:
            logger.warning("[NoiseFilter] 頻譜減法失敗: %s", e)
            return audio

    def process(self, audio: np.ndarray) -> np.ndarray:
        """完整處理管線。

        Args:
            audio: float32 或 int16 音訊

        Returns:
            float32 處理後的音訊，範圍 [-1.0, 1.0]
        """
        # 轉為 float32
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        else:
            audio = audio.astype(np.float32)

        if len(audio) == 0:
            return audio

        # 1. 高通濾波
        audio = self.highpass_filter(audio)

        # 2. 頻譜減法（可選）
        audio = self.spectral_subtraction(audio)

        # 3. 正規化
        audio = self.normalize(audio)

        # Clip to safe range
        return np.clip(audio, -1.0, 1.0)
