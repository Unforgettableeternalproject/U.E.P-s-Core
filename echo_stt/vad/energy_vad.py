#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
語音活動檢測 (VAD) 模組

基於能量分析的 VAD 實現。
從 U.E.P Core 遷出：將 debug_helper 替換為標準 logging。
"""

import logging
from typing import Dict, List, Tuple, Optional

import numpy as np

try:
    import torch
    import torchaudio
    from echo_stt._torchaudio_compat import ensure_torchaudio_compat
    ensure_torchaudio_compat()
    from pyannote.audio import Model
    VAD_AVAILABLE = True
except ImportError:
    VAD_AVAILABLE = False

logger = logging.getLogger(__name__)


class VoiceActivityDetection:
    """語音活動檢測類"""

    def __init__(self, sample_rate: int = 16000, sensitivity: float = 0.7,
                 min_speech_duration: float = 0.3):
        self.sample_rate = sample_rate
        self.vad_model = None

        # VAD 參數
        self.sensitivity = sensitivity
        self.energy_threshold = 0.0005 * (2.0 - sensitivity)
        self.silence_duration_threshold = 0.8
        self.speech_duration_threshold = min_speech_duration

        logger.info(
            "[VAD] 語音活動檢測模組初始化 (靈敏度: %.2f, 最小語音時長: %.2fs)",
            sensitivity, min_speech_duration,
        )

    def initialize(self) -> bool:
        """初始化 VAD 模型"""
        try:
            if VAD_AVAILABLE:
                logger.info("[VAD] 嘗試載入 pyannote VAD 模型...")
                # 暫時先用簡化實現
                pass

            logger.info("[VAD] 使用基於能量的 VAD 實現")
            return True

        except Exception as e:
            logger.error("[VAD] 初始化失敗: %s", e)
            return False

    def detect_voice_activity(self, audio_data: np.ndarray,
                              window_size: float = 0.025) -> List[Dict]:
        """檢測語音活動

        Args:
            audio_data: 音頻數據
            window_size: 窗口大小（秒）

        Returns:
            語音活動事件列表
        """
        try:
            logger.debug("[VAD] 開始語音活動檢測...")

            audio_float = audio_data.astype(np.float32) / 32768.0

            window_samples = int(window_size * self.sample_rate)
            n_windows = len(audio_float) // window_samples

            events: List[Dict] = []
            current_state = "silence"
            current_start_time = 0.0

            for i in range(n_windows):
                start_idx = i * window_samples
                end_idx = min((i + 1) * window_samples, len(audio_float))
                window = audio_float[start_idx:end_idx]

                energy = self._compute_energy(window)
                time_stamp = i * window_size

                is_speech = self._is_speech(energy)

                if current_state == "silence" and is_speech:
                    events.append({
                        'event_type': 'speech_start',
                        'timestamp': time_stamp,
                        'confidence': min(energy / self.energy_threshold, 1.0),
                        'energy_level': energy,
                    })
                    current_state = "speech"
                    current_start_time = time_stamp

                elif current_state == "speech" and not is_speech:
                    speech_duration = time_stamp - current_start_time
                    if speech_duration >= self.speech_duration_threshold:
                        events.append({
                            'event_type': 'speech_end',
                            'timestamp': time_stamp,
                            'confidence': 0.8,
                            'energy_level': energy,
                            'duration': speech_duration,
                        })
                    current_state = "silence"
                    current_start_time = time_stamp

            # BUGFIX: 如果音頻末尾還在說話，補充一個 speech_end 事件
            if current_state == "speech":
                final_duration = (n_windows * window_size) - current_start_time
                if final_duration >= self.speech_duration_threshold:
                    events.append({
                        'event_type': 'speech_end',
                        'timestamp': n_windows * window_size,
                        'confidence': 0.8,
                        'energy_level': 0.0,
                        'duration': final_duration,
                        'is_incomplete': True,
                    })
                    logger.debug("[VAD] 檢測到未完成的語音片段: %.3fs", final_duration)

            logger.debug("[VAD] 檢測到 %d 個語音事件", len(events))
            return events

        except Exception as e:
            logger.error("[VAD] 語音活動檢測失敗: %s", e)
            return []

    def _compute_energy(self, window: np.ndarray) -> float:
        """計算窗口能量"""
        if len(window) == 0:
            return 0.0
        return float(np.mean(window ** 2))

    def _is_speech(self, energy: float) -> bool:
        """判斷是否為語音"""
        return energy > self.energy_threshold

    def has_sufficient_speech(self, audio_data: np.ndarray,
                              min_duration: float = 0.05) -> bool:
        """檢查音頻是否包含足夠的語音內容

        Args:
            audio_data: 音頻數據
            min_duration: 最小語音持續時間（秒）

        Returns:
            是否包含足夠語音
        """
        try:
            events = self.detect_voice_activity(audio_data)

            total_speech_time = 0.0
            for event in events:
                if event['event_type'] == 'speech_end':
                    total_speech_time += event.get('duration', 0.0)

            has_speech = total_speech_time >= min_duration
            logger.debug(
                "[VAD] 語音時間: %.2fs, 需求: %.2fs, 通過: %s",
                total_speech_time, min_duration, has_speech,
            )
            return has_speech

        except Exception as e:
            logger.error("[VAD] 語音檢查失敗: %s", e)
            return False

    def extract_speech_segments(self, audio_data: np.ndarray) -> List[Tuple[float, float, np.ndarray]]:
        """提取語音片段

        Returns:
            List[(start_time, end_time, audio_segment)]
        """
        try:
            events = self.detect_voice_activity(audio_data)
            segments: List[Tuple[float, float, np.ndarray]] = []

            speech_start: Optional[float] = None
            for event in events:
                if event['event_type'] == 'speech_start':
                    speech_start = event['timestamp']
                elif event['event_type'] == 'speech_end' and speech_start is not None:
                    speech_end = event['timestamp']

                    start_idx = int(speech_start * self.sample_rate)
                    end_idx = int(speech_end * self.sample_rate)
                    segment = audio_data[start_idx:end_idx]

                    segments.append((speech_start, speech_end, segment))
                    speech_start = None

            logger.debug("[VAD] 提取 %d 個語音片段", len(segments))
            return segments

        except Exception as e:
            logger.error("[VAD] 語音片段提取失敗: %s", e)
            return []

    def update_thresholds(self, energy_threshold: Optional[float] = None,
                          silence_duration: Optional[float] = None,
                          speech_duration: Optional[float] = None):
        """更新 VAD 閾值"""
        if energy_threshold is not None:
            self.energy_threshold = max(0.001, energy_threshold)
            logger.info("[VAD] 更新能量閾值: %s", self.energy_threshold)

        if silence_duration is not None:
            self.silence_duration_threshold = max(0.1, silence_duration)
            logger.info("[VAD] 更新靜音閾值: %s", self.silence_duration_threshold)

        if speech_duration is not None:
            self.speech_duration_threshold = max(0.1, speech_duration)
            logger.info("[VAD] 更新語音閾值: %s", self.speech_duration_threshold)

    def get_audio_quality_score(self, audio_data: np.ndarray) -> float:
        """評估音頻品質分數 (0-1)"""
        try:
            audio_float = audio_data.astype(np.float32) / 32768.0

            energy = np.mean(audio_float ** 2)
            peak_energy = np.max(np.abs(audio_float))
            zero_crossing_rate = np.mean(np.diff(np.sign(audio_float)) != 0)

            energy_score = min(float(energy * 100), 1.0)
            peak_score = min(float(peak_energy * 2), 1.0)
            clarity_score = max(0.0, 1.0 - float(zero_crossing_rate * 2))

            quality_score = (energy_score * 0.4 + peak_score * 0.3 + clarity_score * 0.3)

            logger.debug(
                "[VAD] 音頻品質: %.3f (能量:%.3f, 峰值:%.3f, 清晰度:%.3f)",
                quality_score, energy_score, peak_score, clarity_score,
            )
            return quality_score

        except Exception as e:
            logger.error("[VAD] 品質評估失敗: %s", e)
            return 0.5

    def shutdown(self):
        """關閉 VAD 模組"""
        logger.info("[VAD] 語音活動檢測模組已關閉")
