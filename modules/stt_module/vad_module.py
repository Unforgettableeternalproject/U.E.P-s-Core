#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
語音活動檢測 (Voice Activity Detection, VAD) 模組
從 STT 模組中分離出來，專門負責檢測語音活動
"""

import numpy as np
import torch
import time
from typing import Optional, List, Tuple
from dataclasses import dataclass

@dataclass
class VADResult:
    """VAD 檢測結果"""
    has_speech: bool
    confidence: float
    energy_level: float
    speech_segments: List[Tuple[float, float]]  # (start, end) 時間段
    
class VADModule:
    """語音活動檢測模組"""
    
    def __init__(self, 
                 energy_threshold: float = 0.01,
                 speech_min_duration: float = 0.5,
                 silence_timeout: float = 1.0):
        self.energy_threshold = energy_threshold
        self.speech_min_duration = speech_min_duration
        self.silence_timeout = silence_timeout
        
        # 用於高級 VAD 的模型 (可選)
        self.vad_model = None
        self._init_advanced_vad()
    
    def _init_advanced_vad(self):
        """初始化高級 VAD 模型"""
        try:
            # 嘗試使用 silero-vad
            import torch
            self.vad_model, utils = torch.hub.load(
                repo_or_dir='snakers4/silero-vad',
                model='silero_vad',
                force_reload=False
            )
            self.get_speech_timestamps = utils[0]
            print("[VAD] Silero VAD 模型載入成功")
        except Exception as e:
            print(f"[VAD] 無法載入 Silero VAD: {e}")
            self.vad_model = None
    
    def detect_activity_simple(self, audio_data: np.ndarray, sample_rate: int = 16000) -> VADResult:
        """簡單的能量基礎 VAD"""
        # 計算音頻能量
        audio_float = audio_data.astype(np.float32) / 32768.0
        energy = np.sqrt(np.mean(audio_float ** 2))
        
        # 基於能量判斷是否有語音
        has_speech = energy > self.energy_threshold
        confidence = min(energy / self.energy_threshold, 1.0) if has_speech else 0.0
        
        # 簡單的語音段檢測
        speech_segments = []
        if has_speech:
            speech_segments = [(0.0, len(audio_data) / sample_rate)]
        
        return VADResult(
            has_speech=has_speech,
            confidence=confidence,
            energy_level=energy,
            speech_segments=speech_segments
        )
    
    def detect_activity_advanced(self, audio_data: np.ndarray, sample_rate: int = 16000) -> VADResult:
        """高級 VAD 使用 Silero 模型"""
        if self.vad_model is None:
            return self.detect_activity_simple(audio_data, sample_rate)
        
        try:
            # 轉換為 torch tensor
            audio_float = audio_data.astype(np.float32) / 32768.0
            audio_tensor = torch.from_numpy(audio_float)
            
            # 獲取語音時間戳
            speech_timestamps = self.get_speech_timestamps(
                audio_tensor, 
                self.vad_model,
                sampling_rate=sample_rate
            )
            
            has_speech = len(speech_timestamps) > 0
            speech_segments = [(ts['start'] / sample_rate, ts['end'] / sample_rate) 
                             for ts in speech_timestamps]
            
            # 計算信心度
            total_speech_duration = sum(end - start for start, end in speech_segments)
            total_duration = len(audio_data) / sample_rate
            confidence = total_speech_duration / total_duration if total_duration > 0 else 0.0
            
            # 計算能量級別
            energy_level = np.sqrt(np.mean(audio_float ** 2))
            
            return VADResult(
                has_speech=has_speech,
                confidence=confidence,
                energy_level=energy_level,
                speech_segments=speech_segments
            )
            
        except Exception as e:
            print(f"[VAD] 高級 VAD 失敗，使用簡單模式: {e}")
            return self.detect_activity_simple(audio_data, sample_rate)
    
    def detect_activity(self, audio_data: np.ndarray, sample_rate: int = 16000) -> VADResult:
        """統一的 VAD 接口"""
        if self.vad_model is not None:
            return self.detect_activity_advanced(audio_data, sample_rate)
        else:
            return self.detect_activity_simple(audio_data, sample_rate)
