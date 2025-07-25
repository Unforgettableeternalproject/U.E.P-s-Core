# modules/stt_module/stt_module.py
# STT Module Phase 2 - 感應式啟動與聲音識別

import threading
import queue
import time
import re
import numpy as np
import speech_recognition as sr
import librosa
from core.module_base import BaseModule
from utils.debug_helper import debug_log, info_log, error_log
from configs.config_loader import load_module_config
from .schemas import STTInput, STTOutput, ActivationMode, SpeakerInfo
from .voice_activity_detector import VoiceActivityDetector
from .speaker_identification import SpeakerIdentifier
from .smart_activation import SmartActivationDetector

def correct_stt(text):
    """STT 結果修正 - 主要針對英文識別優化"""
    corrections = {
        # UEP 相關修正
        "you ep": "UEP",
        "youpee": "UEP", 
        "uvp": "UEP",
        "u e p": "UEP",
        "u.e.p": "UEP",
        "uep": "UEP",
        "you e p": "UEP",
        "yu ep": "UEP",
        "yup": "UEP",
        
        # 常見英文修正
        "cant": "can't",
        "wont": "won't",
        "dont": "don't",
        "isnt": "isn't",
        "arent": "aren't",
        "wasnt": "wasn't",
        "werent": "weren't",
        "hasnt": "hasn't",
        "havent": "haven't",
        "hadnt": "hadn't",
        "shouldnt": "shouldn't",
        "wouldnt": "wouldn't",
        "couldnt": "couldn't",
        
        # 語音助手常見誤識別
        "hey you ep": "hey UEP",
        "hello you ep": "hello UEP",
        "hi you ep": "hi UEP"
    }
    
    result = text.lower()
    for wrong, correct in corrections.items():
        result = result.replace(wrong, correct)
    
    # 保持原有大小寫格式，但確保 UEP 是大寫
    if "uep" in result.lower():
        result = re.sub(r'\buep\b', 'UEP', result, flags=re.IGNORECASE)
    
    return result

class STTModule(BaseModule):
    def __init__(self, config=None):
        self.config = config or load_module_config("stt_module")
        
        # 基本配置
        self.device_index = self.config.get("device_index", 1)
        self.phrase_time_limit = self.config.get("phrase_time_limit", 5)
        self.auto_adjust_noise = self.config.get("auto_adjust_noise", True)
        
        # 語音識別器
        self.recognizer = sr.Recognizer()
        self.mic = None
        
        # 新功能組件
        self.vad = VoiceActivityDetector(self.config)
        self.speaker_id = SpeakerIdentifier(self.config) 
        self.smart_activation = SmartActivationDetector(self.config)
        
        # Always-on 模式狀態
        self._always_on_enabled = self.config.get("always_on", {}).get("enabled", False)
        self._always_on_running = False
        self._always_on_thread = None
        self._audio_queue = queue.Queue()
        self._result_callback = None
        
        # 當前狀態
        self._current_mode = ActivationMode.MANUAL
        self._listening_active = False
        
        info_log("[STT] Phase 2 模組初始化完成")

    def debug(self):
        debug_log(1, "[STT] Debug 模式啟用")
        debug_log(2, f"[STT] 基本設定: 設備={self.device_index}, 時限={self.phrase_time_limit}")
        debug_log(2, f"[STT] Always-on: {self._always_on_enabled}")
        debug_log(2, f"[STT] 智能啟動: {self.smart_activation.enabled}")
        debug_log(2, f"[STT] 聲音識別: {self.speaker_id.si_config.get('enabled', False)}")
        debug_log(3, f"[STT] VAD 配置: {self.vad.config}")

    def initialize(self):
        debug_log(1, "[STT] 初始化中...")
        self.debug()

        try:
            # 初始化麥克風 - 但不開啟連接
            self.mic = sr.Microphone(device_index=self.device_index)
            info_log("[STT] 麥克風初始化成功")
            
            # 噪音校正 - 只在需要時進行
            if self.auto_adjust_noise:
                # 創建一個獨立的上下文來校正噪音
                try:
                    with sr.Microphone(device_index=self.device_index) as source:
                        self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
                    info_log("[STT] 已完成環境噪音校正")
                except Exception as e:
                    error_log(f"[STT] 噪音校正失敗，但繼續執行: {e}")
                
            # 設置 VAD 回調
            self.vad.set_callback(self._on_voice_activity)
            
            # 不再自動啟動 Always-on
            # 由測試函數或外部調用來啟動
                
            return True
            
        except OSError as e:
            error_log(f"[STT] 麥克風初始化失敗：{e}")
            print("[STT] 可用裝置如下：")
            mic_list = sr.Microphone.list_microphone_names()
            for index, name in enumerate(mic_list):
                print(f"Index: {index}, Microphone: {name}")
            raise e

    def handle(self, data: dict = {}) -> dict:
        """處理 STT 請求"""
        try:
            validated = STTInput(**data)
            debug_log(1, f"[STT] 處理請求: {validated.mode}")
            
            start_time = time.time()
            
            if validated.mode == ActivationMode.MANUAL:
                # 手動模式：立即錄音識別，不做智能判斷
                result = self._manual_recognition(validated)
            elif validated.mode == ActivationMode.SMART:
                # 智能模式：手動錄音 + 智能啟動判斷
                result = self._smart_recognition(validated)
            else:
                # 預設使用手動模式
                result = self._manual_recognition(validated)
                
            processing_time = time.time() - start_time
            result["processing_time"] = processing_time
            
            return result
            
        except Exception as e:
            error_log(f"[STT] 處理失敗: {str(e)}")
            return STTOutput(
                text="",
                confidence=0.0,
                error=f"處理失敗: {str(e)}"
            ).dict()

    def _manual_recognition(self, input_data: STTInput) -> dict:
        """手動語音識別"""
        try:
            info_log("[STT] 開始監聽...")
            
            # 錄音 - 避免重複的麥克風調校
            with self.mic as source:
                # 根據配置設置超時
                if input_data.duration:
                    audio = self.recognizer.listen(source, timeout=input_data.duration)
                else:
                    audio = self.recognizer.listen(source, phrase_time_limit=self.phrase_time_limit)
                    
            info_log("[STT] 識別中...")
            
            # 語音識別
            text = self.recognizer.recognize_google(audio, language=input_data.language)
            text = correct_stt(text)
            
            # 聲音識別 (如果啟用)
            speaker_info = None
            if input_data.enable_speaker_id and self.speaker_id.si_config.get("enabled", False):
                # 轉換音頻格式用於聲音識別
                audio_data = self._convert_audio_for_speaker_id(audio)
                if audio_data is not None:
                    speaker_info = self.speaker_id.identify_speaker(audio_data)
            
            return STTOutput(
                text=text,
                confidence=0.9,  # Google API 沒有提供信心度，使用固定值
                speaker_info=speaker_info,
                activation_reason="manual",
                error=None
            ).dict()
            
        except sr.UnknownValueError:
            error_log("[STT] 無法辨識語音")
            return STTOutput(text="", confidence=0.0, error="無法辨識語音").dict()
        except sr.RequestError as e:
            error_log(f"[STT] API 錯誤: {e}")
            return STTOutput(text="", confidence=0.0, error=f"API 錯誤: {e}").dict()
        except Exception as e:
            error_log(f"[STT] 識別失敗: {str(e)}")
            return STTOutput(text="", confidence=0.0, error=f"識別失敗: {str(e)}").dict()

    def _smart_recognition(self, input_data: STTInput) -> dict:
        """智能語音識別"""
        # 先進行基本識別
        result = self._manual_recognition(input_data)
        
        if result.get("error"):
            return result
            
        text = result.get("text", "")
        
        # 智能啟動分析
        activation_result = self.smart_activation.should_activate(text, {"context": input_data.context})
        
        # 更新結果
        result["activation_reason"] = activation_result["reason"]
        
        if activation_result["should_activate"]:
            info_log(f"[STT] 智能啟動觸發: {activation_result['reason']}")
        else:
            debug_log(2, f"[STT] 智能啟動未觸發: {activation_result['reason']}")
            
        return result

    def start_always_on(self, callback=None, duration=30):
        """啟動智能背景監聽模式
        
        Args:
            callback: 智能啟動事件的回調函數
            duration: 監聽時長（秒）, 0 表示持續監聽直到手動停止
        """
        if self._always_on_running:
            info_log("[STT] 智能背景監聽已在運行")
            return
        
        # 保存監聽時長到實例變數
        self._background_duration = duration
            
        # 先啟動 VAD 串流處理
        self.vad.start_streaming()
        
        self._result_callback = callback
        self._always_on_running = True
        self._always_on_thread = threading.Thread(target=self._smart_background_loop, daemon=True)
        self._always_on_thread.start()
        
        info_log(f"[STT] 智能背景監聽已啟動 (時長: {duration if duration > 0 else '無限'}秒)")

    def stop_always_on(self):
        """停止智能背景監聽模式"""
        self._always_on_running = False
        
        # 停止 VAD 串流處理
        self.vad.stop_streaming()
        
        if self._always_on_thread:
            self._always_on_thread.join(timeout=2.0)
        info_log("[STT] 智能背景監聽已停止")

    def _smart_background_loop(self):
        """智能背景監聽循環 - 持續監聽並智能判斷是否啟動"""
        info_log("[STT] 智能背景監聽循環啟動")
        remaining_time = getattr(self, '_background_duration', 30)  # 獲取實例變數
        start_time = time.time()
        last_update = 0  # 上次顯示時間
        
        # 避免連續使用同一個麥克風對象
        mic = sr.Microphone(device_index=self.device_index)
        
        try:
            # 為每次監聽創建一個新的上下文
            while self._always_on_running:
                try:
                    # 顯示剩餘時間 (間隔顯示，減少輸出)
                    elapsed = time.time() - start_time
                    current_sec = int(elapsed)
                    remaining = max(0, int(remaining_time - elapsed))
                    
                    # 每 5 秒更新一次，或剩餘時間小於 5 秒時每秒更新
                    if current_sec % 5 == 0 and current_sec != last_update or remaining < 5 and current_sec != last_update:
                        print(f"\r智能監聽中... ({remaining}s) ", end="", flush=True)
                        last_update = current_sec
                    
                    # 短時間監聽
                    with mic as source:
                        audio = self.recognizer.listen(source, timeout=1, phrase_time_limit=2)
                    
                    # VAD 檢測
                    audio_data = self._convert_audio_for_vad(audio)
                    if audio_data is not None:
                        # 執行 VAD 檢測
                        vad_result = self.vad.detect_speech(audio_data)
                        
                        if vad_result.get("has_speech"):
                            # 檢測到語音，進行智能識別和判斷
                            print()  # 換行，避免覆蓋倒計時
                            self._process_smart_detection(audio)
                    
                except sr.WaitTimeoutError:
                    # 超時是正常的，繼續循環
                    pass
                except Exception as e:
                    error_log(f"[STT] 智能背景監聽錯誤: {str(e)}")
                    time.sleep(0.1)
                
                # 如果設定了時間限制且已經到期，退出循環
                if remaining_time > 0 and time.time() - start_time >= remaining_time:
                    print("\n\n⏱️ 監聽時間結束")
                    break
                    
        except Exception as e:
            error_log(f"[STT] 智能背景監聽失敗: {str(e)}")
        finally:
            self._always_on_running = False
            print("\n\n✅ 智能背景監聽已停止")

    def _process_smart_detection(self, initial_audio):
        """處理智能檢測到的語音 - 結合 VAD + 智能啟動判斷"""
        try:
            # 嘗試識別語音
            text = self.recognizer.recognize_google(initial_audio)
            text = correct_stt(text)
            
            # 首先顯示聽到的文字
            print(f"🎧 聽到: '{text}'")
            
            # 智能啟動判斷 - 這是關鍵步驟
            activation_result = self.smart_activation.should_activate(text)
            
            if activation_result["should_activate"]:
                # 只有智能判斷通過才進行後續處理
                
                # 聲音識別
                speaker_info = None
                if self.speaker_id.si_config.get("enabled", False):
                    audio_data = self._convert_audio_for_speaker_id(initial_audio)
                    if audio_data is not None:
                        speaker_info = self.speaker_id.identify_speaker(audio_data)
                
                # 創建結果
                result = STTOutput(
                    text=text,
                    confidence=activation_result["confidence"], 
                    speaker_info=speaker_info,
                    activation_reason=activation_result["reason"]
                )
                
                # 回調通知
                if self._result_callback:
                    self._result_callback(result.dict())
                    
                info_log(f"[STT] 智能觸發: '{text}' (原因: {activation_result['reason']})")
                print(f"✅ 智能啟動觸發!")
            else:
                # 未通過智能判斷，不觸發
                debug_log(3, f"[STT] 智能過濾: '{text}' ({activation_result['reason']})")
                print(f"⚪ 未觸發 (信心度: {activation_result['confidence']:.2f})")
                
        except sr.UnknownValueError:
            debug_log(3, "[STT] 智能背景監聽無法識別語音")
            print("🔇 無法識別語音")
        except Exception as e:
            error_log(f"[STT] 智能檢測處理失敗: {str(e)}")
            print(f"❌ 處理失敗: {str(e)}")

    def _convert_audio_for_vad(self, audio) -> np.ndarray:
        """轉換音頻用於 VAD 處理"""
        try:
            # 獲取原始音頻數據
            audio_data = np.frombuffer(audio.get_raw_data(), dtype=np.int16)
            # 轉換為浮點數並正規化
            audio_data = audio_data.astype(np.float32) / 32768.0
            return audio_data
        except Exception as e:
            error_log(f"[STT] 音頻轉換失敗 (VAD): {str(e)}")
            return None

    def _convert_audio_for_speaker_id(self, audio) -> np.ndarray:
        """轉換音頻用於聲音識別"""
        try:
            # 獲取原始音頻數據
            audio_data = np.frombuffer(audio.get_raw_data(), dtype=np.int16)
            # 轉換為浮點數並正規化
            audio_data = audio_data.astype(np.float32) / 32768.0
            # 重採樣到指定採樣率
            target_sr = self.speaker_id.sample_rate
            current_sr = audio.sample_rate
            if current_sr != target_sr:
                audio_data = librosa.resample(audio_data, orig_sr=current_sr, target_sr=target_sr)
            return audio_data
        except Exception as e:
            error_log(f"[STT] 音頻轉換失敗 (SpeakerID): {str(e)}")
            return None

    def _on_voice_activity(self, event):
        """VAD 事件回調"""
        debug_log(4, f"[STT] VAD 事件: {event['event_type']}")

    def get_speaker_stats(self) -> dict:
        """獲取說話人統計信息"""
        return self.speaker_id.get_speaker_stats()

    def get_activation_stats(self) -> dict:
        """獲取啟動統計信息"""
        return self.smart_activation.get_activation_stats()

    def shutdown(self):
        """關閉模組"""
        self.stop_always_on()
        info_log("[STT] 模組已關閉")
