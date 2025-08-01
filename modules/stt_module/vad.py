#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
語音活動檢測 (VAD) 模組
"""

import numpy as np
import time
from typing import Dict, List, Tuple, Optional

try:
    import torch
    import torchaudio
    from pyannote.audio import Model
    VAD_AVAILABLE = True
except ImportError:
    VAD_AVAILABLE = False

from utils.debug_helper import debug_log, info_log, error_log

class VoiceActivityDetection:
    """語音活動檢測類"""
    
    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.vad_model = None
        
        # VAD 參數
        self.energy_threshold = 0.001  # 降低能量閾值，更容易檢測到語音
        self.silence_duration_threshold = 1.0  # 靜音持續時間閾值（秒）
        self.speech_duration_threshold = 0.1   # 降低語音持續時間閾值（秒）
        
        info_log("[VAD] 語音活動檢測模組初始化")
    
    def initialize(self) -> bool:
        """初始化 VAD 模型"""
        try:
            if VAD_AVAILABLE:
                info_log("[VAD] 嘗試載入 pyannote VAD 模型...")
                # 可以嘗試載入 pyannote 的 VAD 模型
                # self.vad_model = Model.from_pretrained("pyannote/voice-activity-detection")
                # 暫時先用簡化實現
                pass
            
            info_log("[VAD] 使用基於能量的 VAD 實現")
            return True
            
        except Exception as e:
            error_log(f"[VAD] 初始化失敗: {e}")
            return False
    
    def detect_voice_activity(self, audio_data: np.ndarray, window_size: float = 0.025) -> List[Dict]:
        """檢測語音活動
        
        Args:
            audio_data: 音頻數據
            window_size: 窗口大小（秒）
            
        Returns:
            語音活動事件列表
        """
        try:
            debug_log(3, "[VAD] 開始語音活動檢測...")
            
            # 正規化音頻
            audio_float = audio_data.astype(np.float32) / 32768.0
            
            # 計算窗口大小
            window_samples = int(window_size * self.sample_rate)
            n_windows = len(audio_float) // window_samples
            
            events = []
            current_state = "silence"
            current_start_time = 0.0
            
            for i in range(n_windows):
                start_idx = i * window_samples
                end_idx = min((i + 1) * window_samples, len(audio_float))
                window = audio_float[start_idx:end_idx]
                
                # 計算窗口能量
                energy = self._compute_energy(window)
                time_stamp = i * window_size
                
                # 判斷語音活動
                is_speech = self._is_speech(energy)
                
                if current_state == "silence" and is_speech:
                    # 從靜音轉為語音
                    events.append({
                        'event_type': 'speech_start',
                        'timestamp': time_stamp,
                        'confidence': min(energy / self.energy_threshold, 1.0),
                        'energy_level': energy
                    })
                    current_state = "speech"
                    current_start_time = time_stamp
                    
                elif current_state == "speech" and not is_speech:
                    # 從語音轉為靜音
                    speech_duration = time_stamp - current_start_time
                    if speech_duration >= self.speech_duration_threshold:
                        events.append({
                            'event_type': 'speech_end',
                            'timestamp': time_stamp,
                            'confidence': 0.8,
                            'energy_level': energy,
                            'duration': speech_duration
                        })
                    current_state = "silence"
                    current_start_time = time_stamp
            
            debug_log(3, f"[VAD] 檢測到 {len(events)} 個語音事件")
            return events
            
        except Exception as e:
            error_log(f"[VAD] 語音活動檢測失敗: {str(e)}")
            return []
    
    def _compute_energy(self, window: np.ndarray) -> float:
        """計算窗口能量"""
        if len(window) == 0:
            return 0.0
        return np.mean(window ** 2)
    
    def _is_speech(self, energy: float) -> bool:
        """判斷是否為語音"""
        return energy > self.energy_threshold
    
    def has_sufficient_speech(self, audio_data: np.ndarray, min_duration: float = 0.5) -> bool:
        """檢查音頻是否包含足夠的語音內容
        
        Args:
            audio_data: 音頻數據
            min_duration: 最小語音持續時間（秒）
            
        Returns:
            是否包含足夠語音
        """
        try:
            events = self.detect_voice_activity(audio_data)
            
            # 計算總語音時間
            total_speech_time = 0.0
            for event in events:
                if event['event_type'] == 'speech_end':
                    total_speech_time += event.get('duration', 0.0)
            
            has_speech = total_speech_time >= min_duration
            debug_log(3, f"[VAD] 語音時間: {total_speech_time:.2f}s, 需求: {min_duration:.2f}s, 通過: {has_speech}")
            
            return has_speech
            
        except Exception as e:
            error_log(f"[VAD] 語音檢查失敗: {str(e)}")
            return False
    
    def extract_speech_segments(self, audio_data: np.ndarray) -> List[Tuple[float, float, np.ndarray]]:
        """提取語音片段
        
        Returns:
            List[(start_time, end_time, audio_segment)]
        """
        try:
            events = self.detect_voice_activity(audio_data)
            segments = []
            
            speech_start = None
            for event in events:
                if event['event_type'] == 'speech_start':
                    speech_start = event['timestamp']
                elif event['event_type'] == 'speech_end' and speech_start is not None:
                    speech_end = event['timestamp']
                    
                    # 提取音頻片段
                    start_idx = int(speech_start * self.sample_rate)
                    end_idx = int(speech_end * self.sample_rate)
                    segment = audio_data[start_idx:end_idx]
                    
                    segments.append((speech_start, speech_end, segment))
                    speech_start = None
            
            debug_log(3, f"[VAD] 提取 {len(segments)} 個語音片段")
            return segments
            
        except Exception as e:
            error_log(f"[VAD] 語音片段提取失敗: {str(e)}")
            return []
    
    def update_thresholds(self, energy_threshold: Optional[float] = None, 
                         silence_duration: Optional[float] = None,
                         speech_duration: Optional[float] = None):
        """更新 VAD 閾值"""
        if energy_threshold is not None:
            self.energy_threshold = max(0.001, energy_threshold)
            info_log(f"[VAD] 更新能量閾值: {self.energy_threshold}")
            
        if silence_duration is not None:
            self.silence_duration_threshold = max(0.1, silence_duration)
            info_log(f"[VAD] 更新靜音閾值: {self.silence_duration_threshold}")
            
        if speech_duration is not None:
            self.speech_duration_threshold = max(0.1, speech_duration)
            info_log(f"[VAD] 更新語音閾值: {self.speech_duration_threshold}")
    
    def get_audio_quality_score(self, audio_data: np.ndarray) -> float:
        """評估音頻品質分數 (0-1)"""
        try:
            audio_float = audio_data.astype(np.float32) / 32768.0
            
            # 計算信噪比相關指標
            energy = np.mean(audio_float ** 2)
            peak_energy = np.max(np.abs(audio_float))
            zero_crossing_rate = np.mean(np.diff(np.sign(audio_float)) != 0)
            
            # 簡單的品質評分
            energy_score = min(energy * 100, 1.0)  # 能量分數
            peak_score = min(peak_energy * 2, 1.0)  # 峰值分數
            clarity_score = max(0, 1 - zero_crossing_rate * 2)  # 清晰度分數
            
            # 加權平均
            quality_score = (energy_score * 0.4 + peak_score * 0.3 + clarity_score * 0.3)
            
            debug_log(3, f"[VAD] 音頻品質: {quality_score:.3f} (能量:{energy_score:.3f}, 峰值:{peak_score:.3f}, 清晰度:{clarity_score:.3f})")
            
            return quality_score
            
        except Exception as e:
            error_log(f"[VAD] 品質評估失敗: {str(e)}")
            return 0.5  # 中等品質
    
    def shutdown(self):
        """關閉 VAD 模組"""
        info_log("[VAD] 語音活動檢測模組已關閉")
