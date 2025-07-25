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
        
        # VAD 參數 - 更適合處理所有語音，包括非常簡短的指令
        self.sensitivity = self.vad_config.get("vad_sensitivity", 0.8)  # 進一步提高敏感度
        self.min_speech_duration = self.vad_config.get("min_speech_duration", 0.05)  # 極低的最小語音持續時間閾值
        self.max_silence_duration = self.vad_config.get("max_silence_duration", 2.0)  # 增加靜音時間以防止語句被分割
        self.energy_threshold = self.vad_config.get("energy_threshold", 15)  # 非常低的能量閾值以捕獲任何可能的語音
        self.dynamic_threshold = self.vad_config.get("dynamic_threshold", True)
        
        # 處理極短語音的額外參數
        self.process_all_audio = True  # 標記為處理所有音頻，不論長短
        self.energy_boost_factor = 0.7  # 能量閾值降低係數，用於短語音
        
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
        """更新背景能量估計 - 增強版本，更適應性地調整閾值"""
        # 首先過濾極端值，避免噪音峰值干擾適應性調整
        if len(self.energy_history) > 5:
            median_energy = np.median(self.energy_history)
            # 如果當前能量是極端異常值（超過中位數的5倍），則不更新歷史
            if energy > median_energy * 5:
                debug_log(4, f"[VAD] 跳過極端能量值: {energy:.2f} > {median_energy * 5:.2f}")
                return
        
        # 添加到歷史記錄
        self.energy_history.append(energy)
        if len(self.energy_history) > self.history_size:
            self.energy_history.pop(0)
            
        if self.dynamic_threshold and len(self.energy_history) > 10:
            # 計算最近10個和較長期的能量統計
            recent_energies = self.energy_history[-10:]
            
            # 使用四分位數統計而非簡單均值，以更好處理非高斯分布能量
            recent_median = np.median(recent_energies)
            q75 = np.percentile(recent_energies, 75)
            q25 = np.percentile(recent_energies, 25)
            iqr = q75 - q25  # 四分位範圍
            
            # 更靈活的閾值計算 - 對極低能量環境更敏感
            if recent_median < 10:  # 極安靜環境
                self.energy_threshold = max(5, recent_median + iqr)
                debug_log(4, f"[VAD] 安靜環境閾值調整: {self.energy_threshold:.2f}")
            else:  # 正常或嘈雜環境
                self.energy_threshold = recent_median + (iqr * 1.5)
                debug_log(4, f"[VAD] 標準閾值調整: {self.energy_threshold:.2f}")
            
            # 確保閾值不會過高或過低
            self.energy_threshold = min(max(self.energy_threshold, 5), 100)
            
    def detect_speech(self, audio_data: np.ndarray, sample_rate: int = 16000) -> dict:
        """
        檢測語音活動 - 增強版，使用多重指標檢測語音
        
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
        
        # 檢查短時零交叉率 (Zero Crossing Rate)，語音通常有較高的 ZCR
        zcr = np.sum(np.abs(np.diff(np.signbit(audio_data)))) / len(audio_data)
        zcr_speech_indicator = zcr > 0.05  # ZCR閾值
        
        # 增強版的語音檢測 - 能量或特徵指標超過閾值就視為語音
        primary_speech_detected = energy > self.energy_threshold
        secondary_speech_detected = zcr_speech_indicator and energy > (self.energy_threshold * 0.7)
        
        # 綜合判斷是否有語音
        has_speech = primary_speech_detected or secondary_speech_detected
        
        result = {
            "has_speech": has_speech,
            "energy": energy,
            "zcr": zcr,  # 添加零交叉率信息
            "threshold": self.energy_threshold,
            "timestamp": current_time,
            "speech_state_changed": False,
            "event_type": None,
            "speech_indicators": {  # 添加詳細的語音指標
                "energy_based": primary_speech_detected,
                "zcr_based": secondary_speech_detected
            }
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
        """串流處理線程 - 增強版本，高效處理 PyAudio 串流"""
        debug_log(2, "[VAD] 串流處理線程已啟動")
        speech_buffer = []  # 用於保存當前語音片段
        sample_rate = self.config.get("sample_rate", 16000)
        accumulated_silence = 0  # 累積的靜音時間
        
        try:
            while self.is_streaming:
                try:
                    # 從緩衝區獲取音頻塊，最多等待 0.1 秒
                    audio_chunk = self.audio_buffer.get(timeout=0.1)
                    
                    # 處理音頻塊，使用增強的檢測
                    result = self.detect_speech(audio_chunk)
                    
                    # 語音檢測邏輯增強 - 更靈敏地檢測任何可能的語音活動
                    is_speech = False
                    
                    # 主要語音檢測
                    if result["has_speech"]:
                        is_speech = True
                    # 次要檢測：即使不滿足主要條件，但有較強能量波動也視為潛在語音
                    elif self.process_all_audio:
                        # 檢查能量相對於背景噪音的波動
                        bg_energy = np.mean(self.energy_history) if self.energy_history else self.energy_threshold
                        energy_ratio = result["energy"] / (bg_energy + 0.00001)  # 防止除零
                        
                        # 如果能量比背景高出顯著倍數，或零交叉率顯著，仍視為語音
                        if energy_ratio > 1.5 or result["zcr"] > 0.1:
                            is_speech = True
                            debug_log(4, f"[VAD] 檢測到潛在語音: 能量比={energy_ratio:.2f}, ZCR={result['zcr']:.2f}")
                    
                    # 如果檢測到語音
                    if is_speech:
                        # 如果之前不是語音狀態，發送語音開始事件
                        if not speech_buffer:
                            if self.callback:
                                start_event = {
                                    "event_type": "speech_start",
                                    "timestamp": result["timestamp"],
                                    "detection_type": "enhanced" if not result["has_speech"] else "standard"
                                }
                                self.callback(start_event)
                                debug_log(3, f"[VAD] 語音開始: {'增強檢測' if not result['has_speech'] else '標準檢測'}")
                        
                        # 保存到緩衝區
                        speech_buffer.append(audio_chunk)
                        accumulated_silence = 0  # 重置靜音計數器
                        debug_log(4, f"[VAD] 添加語音塊: 長度={len(audio_chunk)}, 總緩衝區語音塊={len(speech_buffer)}")
                        
                    elif speech_buffer:  # 如果有語音在緩衝區但當前塊無語音
                        # 計算當前塊的時間長度，累積靜音時間
                        chunk_duration = len(audio_chunk) / sample_rate
                        accumulated_silence += chunk_duration
                        
                        # 更寬鬆的靜音檢測邏輯
                        # 如果緩衝區有內容，我們會更謹慎地判斷是否結束語音段
                        current_speech_duration = sum(len(chunk) for chunk in speech_buffer) / sample_rate
                        
                        # 短語音時使用更短的靜音閾值，長語音時允許更長的停頓
                        adjusted_silence_threshold = self.max_silence_duration
                        if current_speech_duration < 1.0:  # 短語音
                            adjusted_silence_threshold = self.max_silence_duration * 0.7
                        elif current_speech_duration > 3.0:  # 長語音
                            adjusted_silence_threshold = self.max_silence_duration * 1.3
                        
                        debug_log(4, f"[VAD] 靜音檢測: 累積={accumulated_silence:.2f}秒, 閾值={adjusted_silence_threshold:.2f}秒, 語音長度={current_speech_duration:.2f}秒")
                        
                        # 如果靜音時間超過調整後的閾值，結束當前語音段
                        if accumulated_silence >= adjusted_silence_threshold:
                            if speech_buffer and self.callback:
                                speech_duration = sum(len(chunk) for chunk in speech_buffer) / sample_rate
                                
                                # 完全移除長度限制，處理所有語音段
                                # 始終嘗試處理語音，無論長度如何
                                avg_energy = np.mean([self.calculate_energy(chunk) for chunk in speech_buffer])
                                
                                # 計算語音段的零交叉率，作為語音複雜度的指標
                                concatenated_audio = np.concatenate(speech_buffer)
                                zcr = np.sum(np.abs(np.diff(np.signbit(concatenated_audio)))) / len(concatenated_audio)
                                
                                # 增加判斷語音段結束的清晰日誌
                                debug_log(2, f"[VAD] 語音段結束判斷: 長度={speech_duration:.2f}秒, 能量={avg_energy:.2f}, ZCR={zcr:.4f}")
                                
                                if speech_duration >= self.min_speech_duration:
                                    # 標準長度語音
                                    speech_data = {
                                        "event_type": "speech_end",
                                        "timestamp": time.time(),
                                        "speech_duration": speech_duration,
                                        "audio_buffer": speech_buffer.copy(),
                                        "avg_energy": float(avg_energy),
                                        "zcr": float(zcr),
                                        "is_complete": True  # 標記為完整語音段
                                    }
                                    self.callback(speech_data)
                                    debug_log(1, f"[VAD] ✓ 完整語音段已發送: {speech_duration:.2f}秒")
                                    
                                elif speech_duration >= 0.05:  # 非常短的語音也處理，只要超過0.05秒
                                    # 所有短語音都嘗試處理，不再根據能量過濾
                                    speech_data = {
                                        "event_type": "speech_end",
                                        "timestamp": time.time(),
                                        "speech_duration": speech_duration,
                                        "audio_buffer": speech_buffer.copy(),
                                        "avg_energy": float(avg_energy),
                                        "zcr": float(zcr),
                                        "is_short_command": True  # 標記為短語音命令
                                    }
                                    self.callback(speech_data)
                                    debug_log(1, f"[VAD] ✓ 短語音命令已發送: {speech_duration:.2f}秒 (能量: {avg_energy:.2f})")
                                else:
                                    # 語音極短（<0.05秒），可能只是雜音
                                    debug_log(3, f"[VAD] 語音片段極短，可能是雜音: {speech_duration:.2f}秒")
                                    
                            # 清空緩衝區，準備下一段語音
                            speech_buffer = []
                            accumulated_silence = 0
                    
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
