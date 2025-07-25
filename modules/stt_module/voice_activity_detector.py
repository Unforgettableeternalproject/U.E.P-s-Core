"""
modules/stt_module/voice_activity_detector.py
語音活動檢測 (Voice Activity Detection) 實現
支持真正的串流處理
"""

import numpy as np
import librosa
import threading
import queue
import time
from typing import Callable, Optional, List, Dict
from utils.debug_helper import debug_log, info_log, error_log

class VoiceActivityDetector:
    """語音活動檢測器 - 支持真正的串流處理"""
    
    def __init__(self, config: dict):
        self.config = config
        self.vad_config = config.get("always_on", {})
        
        # VAD 參數
        self.sensitivity = self.vad_config.get("vad_sensitivity", 0.6)
        self.min_speech_duration = self.vad_config.get("min_speech_duration", 0.5)
        self.max_silence_duration = self.vad_config.get("max_silence_duration", 2.0)
        self.energy_threshold = self.vad_config.get("energy_threshold", 4000)
        self.dynamic_threshold = self.vad_config.get("dynamic_threshold", True)
        
        # 狀態追蹤
        self.is_speaking = False
        self.speech_start_time = None
        self.last_speech_time = None
        self.background_energy = []
        self.callback = None
        
        # 動態閾值調整
        self.energy_history = []
        self.history_size = 50
        
        # 串流處理相關
        self.audio_buffer = queue.Queue()
        self.is_streaming = False
        self.stream_thread = None
        self.stream_chunk_size = 1024  # 每次處理的音頻塊大小
        
        info_log(f"[VAD] 初始化完成，敏感度: {self.sensitivity}")
        
    def set_callback(self, callback: Callable):
        """設置語音活動事件回調"""
        self.callback = callback
        
    def calculate_energy(self, audio_data: np.ndarray) -> float:
        """計算音頻能量"""
        return np.sum(audio_data ** 2)
        
    def update_background_energy(self, energy: float):
        """更新背景能量估計"""
        self.energy_history.append(energy)
        if len(self.energy_history) > self.history_size:
            self.energy_history.pop(0)
            
        if self.dynamic_threshold and len(self.energy_history) > 10:
            # 動態調整閾值
            background_avg = np.mean(self.energy_history)
            background_std = np.std(self.energy_history)
            self.energy_threshold = background_avg + (background_std * 2)
            
    def detect_speech(self, audio_data: np.ndarray, sample_rate: int = 16000) -> dict:
        """
        檢測語音活動
        
        Args:
            audio_data: 音頻數據
            sample_rate: 採樣率
            
        Returns:
            dict: 檢測結果
        """
        current_time = time.time()
        
        # 計算能量
        energy = self.calculate_energy(audio_data)
        self.update_background_energy(energy)
        
        # 判斷是否有語音
        has_speech = energy > self.energy_threshold
        
        result = {
            "has_speech": has_speech,
            "energy": energy,
            "threshold": self.energy_threshold,
            "timestamp": current_time,
            "speech_state_changed": False,
            "event_type": None
        }
        
        # 語音狀態轉換檢測
        if has_speech and not self.is_speaking:
            # 語音開始
            self.is_speaking = True
            self.speech_start_time = current_time
            self.last_speech_time = current_time
            result["speech_state_changed"] = True
            result["event_type"] = "speech_start"
            
            if self.callback:
                self.callback({
                    "event_type": "speech_start",
                    "timestamp": current_time,
                    "confidence": min(energy / self.energy_threshold, 1.0),
                    "energy_level": energy
                })
                
        elif has_speech and self.is_speaking:
            # 語音持續
            self.last_speech_time = current_time
            
        elif not has_speech and self.is_speaking:
            # 可能的語音結束
            silence_duration = current_time - self.last_speech_time
            
            if silence_duration > self.max_silence_duration:
                # 語音結束
                speech_duration = self.last_speech_time - self.speech_start_time
                
                if speech_duration >= self.min_speech_duration:
                    # 有效語音結束
                    self.is_speaking = False
                    result["speech_state_changed"] = True
                    result["event_type"] = "speech_end"
                    result["speech_duration"] = speech_duration
                    
                    if self.callback:
                        self.callback({
                            "event_type": "speech_end",
                            "timestamp": current_time,
                            "confidence": 0.9,
                            "energy_level": energy,
                            "speech_duration": speech_duration
                        })
                else:
                    # 語音太短，重置
                    self.is_speaking = False
                    
        # 只在調試級別為 4 時輸出詳細日誌
        debug_log(4, f"[VAD] 能量: {energy:.2f}, 閾值: {self.energy_threshold:.2f}, "
                     f"語音: {has_speech}, 狀態: {'speaking' if self.is_speaking else 'silent'}")
        
        return result
        
    def reset(self):
        """重置檢測器狀態"""
        self.is_speaking = False
        self.speech_start_time = None
        self.last_speech_time = None
        
        # 清空音頻緩衝區
        while not self.audio_buffer.empty():
            try:
                self.audio_buffer.get_nowait()
            except queue.Empty:
                break
                
        info_log("[VAD] 檢測器狀態已重置")
        
    def start_streaming(self):
        """開始串流處理"""
        if self.is_streaming:
            info_log("[VAD] 串流處理已在運行中")
            return
            
        self.is_streaming = True
        self.stream_thread = threading.Thread(target=self._stream_processor, daemon=True)
        self.stream_thread.start()
        info_log("[VAD] 串流處理已啟動")
        
    def stop_streaming(self):
        """停止串流處理"""
        if not self.is_streaming:
            return
            
        self.is_streaming = False
        if self.stream_thread:
            self.stream_thread.join(timeout=2.0)
        self.reset()
        info_log("[VAD] 串流處理已停止")
        
    def add_audio_chunk(self, audio_chunk: np.ndarray):
        """添加音頻數據到處理緩衝區"""
        if self.is_streaming:
            self.audio_buffer.put(audio_chunk)
            
    def _stream_processor(self):
        """串流處理線程"""
        debug_log(2, "[VAD] 串流處理線程已啟動")
        
        try:
            while self.is_streaming:
                try:
                    # 從緩衝區獲取音頻塊，最多等待 0.1 秒
                    audio_chunk = self.audio_buffer.get(timeout=0.1)
                    
                    # 處理音頻塊
                    result = self.detect_speech(audio_chunk)
                    
                    # 處理結果
                    if result["speech_state_changed"]:
                        debug_log(3, f"[VAD] 語音狀態變更: {result['event_type']}")
                        
                except queue.Empty:
                    # 超時是正常的，繼續等待
                    continue
                    
                except Exception as e:
                    error_log(f"[VAD] 串流處理錯誤: {str(e)}")
                    time.sleep(0.1)
                    
        except Exception as e:
            error_log(f"[VAD] 串流處理線程異常: {str(e)}")
            
        finally:
            self.is_streaming = False
            debug_log(2, "[VAD] 串流處理線程已停止")
