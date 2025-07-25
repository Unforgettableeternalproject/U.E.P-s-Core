# modules/stt_module/stt_module.py
# STT Module Phase 2 - 感應式啟動與聲音識別

import threading
import queue
import time
import re
import numpy as np
import speech_recognition as sr
import librosa
import pyaudio  # 新增 PyAudio 導入
import struct
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
        
        # PyAudio 串流配置
        self.pa_config = {
            "format": pyaudio.paInt16,
            "channels": 1,
            "rate": 16000,  # 採樣率
            "frames_per_buffer": 1024,  # 每個緩衝區的幀數
            "stream_chunk_duration": 0.1  # 每個塊的持續時間（秒）
        }
        self.pyaudio_instance = None
        self.audio_stream = None
        
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
            # 初始化麥克風 - 使用更高的採樣率
            self.mic = sr.Microphone(device_index=self.device_index, sample_rate=16000)
            info_log("[STT] 麥克風初始化成功")
            
            # 設置更敏感的基本參數
            self.recognizer.energy_threshold = 10  # 更低的能量閾值，增加敏感度
            self.recognizer.dynamic_energy_threshold = True  # 動態調整以適應環境
            self.recognizer.pause_threshold = 1.0  # 更寬鬆的停頓閾值
            
            # 噪音校正 - 只在需要時進行
            if self.auto_adjust_noise:
                # 創建一個獨立的上下文來校正噪音
                try:
                    with sr.Microphone(device_index=self.device_index) as source:
                        info_log("[STT] 進行環境噪音校正...")
                        self.recognizer.adjust_for_ambient_noise(source, duration=1.0)  # 增加校正時間
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
            
            # 設置更寬鬆的能量閾值以捕捉更多聲音
            self.recognizer.energy_threshold = 10  # 降低能量閾值以增加敏感度
            self.recognizer.dynamic_energy_threshold = True
            self.recognizer.pause_threshold = 1.0  # 更寬鬆的停頓閾值
            debug_log(2, f"[STT] 能量閾值: {self.recognizer.energy_threshold}, 停頓閾值: {self.recognizer.pause_threshold}")
            
            # 錄音 - 避免重複的麥克風調校
            with self.mic as source:
                debug_log(2, f"[STT] 監聽參數: 超時={input_data.duration if input_data.duration else '無'}, 短句限制={self.phrase_time_limit}秒")
                
                # 根據配置設置超時
                if input_data.duration:
                    audio = self.recognizer.listen(source, timeout=input_data.duration)
                else:
                    audio = self.recognizer.listen(source, phrase_time_limit=self.phrase_time_limit)
                
                # 記錄音頻長度等信息
                audio_length = len(audio.frame_data) / (audio.sample_rate * audio.sample_width)
                debug_log(2, f"[STT] 錄音完成: 長度={audio_length:.2f}秒, 採樣率={audio.sample_rate}Hz")
                    
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
                confidence=0.9,  # Google API 沒有提供語音識別信心度，固定使用 0.9 表示識別可靠性
                speaker_info=speaker_info,
                activation_reason="manual",
                error=None
            ).dict()
            
        except sr.UnknownValueError:
            error_log("[STT] 無法辨識語音")
            return STTOutput(
                text="", 
                confidence=0.0, 
                error="無法辨識語音",
                activation_reason="識別失敗：無法辨識語音"
            ).dict()
        except sr.RequestError as e:
            error_log(f"[STT] API 錯誤: {e}")
            return STTOutput(
                text="", 
                confidence=0.0, 
                error=f"API 錯誤: {e}",
                activation_reason="識別失敗：API錯誤"
            ).dict()
        except sr.WaitTimeoutError as e:
            # 特別處理超時錯誤
            error_log(f"[STT] 超時錯誤: {e}")
            return STTOutput(
                text="", 
                confidence=0.0, 
                error=f"超時錯誤: 等待語音輸入超時",
                activation_reason="識別失敗：等待語音輸入超時"
            ).dict()
        except Exception as e:
            error_log(f"[STT] 識別失敗: {str(e)}")
            return STTOutput(
                text="", 
                confidence=0.0, 
                error=f"識別失敗: {str(e)}",
                activation_reason="識別失敗：未知錯誤"
            ).dict()

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
        """啟動智能背景監聽模式 - 使用 PyAudio 串流
        
        Args:
            callback: 智能啟動事件的回調函數
            duration: 監聽時長（秒）, 0 表示持續監聽直到手動停止
        """
        if self._always_on_running:
            info_log("[STT] 智能背景監聽已在運行")
            return
        
        # 明確設置監聽時長到實例變數
        self._background_duration = duration if duration is not None else 30
        
        # 初始化 PyAudio 資源
        try:
            # 確保之前的資源已釋放
            if self.audio_stream:
                try:
                    self.audio_stream.stop_stream()
                    self.audio_stream.close()
                except:
                    pass
            
            if self.pyaudio_instance:
                try:
                    self.pyaudio_instance.terminate()
                except:
                    pass
                    
            self.pyaudio_instance = pyaudio.PyAudio()
            info_log("[STT] PyAudio 初始化成功")
        except Exception as e:
            error_log(f"[STT] PyAudio 初始化失敗: {str(e)}")
            return
                
        # 先啟動 VAD 串流處理 - 確保 VAD 在監聽前已準備好
        self.vad.start_streaming()
        
        self._result_callback = callback
        self._always_on_running = True
        self._always_on_thread = threading.Thread(target=self._smart_background_loop_pyaudio, daemon=True)
        self._always_on_thread.start()
        
        info_log(f"[STT] PyAudio 智能背景監聽已啟動 (時長: {self._background_duration if self._background_duration > 0 else '無限'}秒)")
        print(f"\n🎧 背景語音監聽已啟動，將實時顯示辨識結果...")
        print(f"⏱️  監聽時長: {self._background_duration if self._background_duration > 0 else '無限'}秒")

    def stop_always_on(self):
        """停止智能背景監聽模式"""
        self._always_on_running = False
        
        # 停止 VAD 串流處理
        self.vad.stop_streaming()
        
        # 停止並釋放 PyAudio 資源
        if self.audio_stream:
            try:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
                self.audio_stream = None
            except Exception as e:
                error_log(f"[STT] 停止音頻流錯誤: {str(e)}")
        
        if self.pyaudio_instance:
            try:
                self.pyaudio_instance.terminate()
                self.pyaudio_instance = None
            except Exception as e:
                error_log(f"[STT] 終止 PyAudio 錯誤: {str(e)}")
        
        if self._always_on_thread:
            self._always_on_thread.join(timeout=2.0)
        info_log("[STT] 智能背景監聽已停止")

    def _smart_background_loop(self):
        """智能背景監聽循環 - 使用 VAD 實現真正的串流處理"""
        info_log("[STT] 智能背景監聽循環啟動")
        remaining_time = getattr(self, '_background_duration', 30)  # 獲取實例變數
        start_time = time.time()
        last_update = 0  # 上次顯示時間
        
        # 避免連續使用同一個麥克風對象
        mic = sr.Microphone(device_index=self.device_index)
        
        try:
            with mic as source:
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
                        
                        # 短時間監聽並傳送到 VAD 串流處理器
                        try:
                            # 超時縮短為 0.5 秒，提高響應速度
                            audio = self.recognizer.listen(source, timeout=0.5, phrase_time_limit=1)
                            
                            # 轉換並傳送到 VAD
                            audio_data = self._convert_audio_for_vad(audio)
                            if audio_data is not None:
                                # 添加到 VAD 串流處理器，由回調函數處理結果
                                self.vad.add_audio_chunk(audio_data)
                        except sr.WaitTimeoutError:
                            # 超時是正常的，繼續循環
                            pass
                        
                    except Exception as e:
                        if "This audio source is already inside a context manager" not in str(e):
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

    def _process_smart_detection(self, initial_audio, is_short_command=False):
        """處理智能檢測到的語音 - 全面增強版，支持所有語音處理"""
        try:
            # 獲取音頻能量以進行額外判斷
            audio_data = np.frombuffer(initial_audio.get_raw_data(), dtype=np.int16).astype(np.float32) / 32768.0
            audio_energy = np.sum(audio_data ** 2)
            audio_duration = len(audio_data) / 16000  # 假設採樣率為16000
            
            # 顯示詳細的語音資訊，幫助調試
            info_log(f"[STT] 開始識別語音: 能量={audio_energy:.2f}, 長度={audio_duration:.2f}秒, 短指令標記={is_short_command}")
            
            # 開始計時
            start_time = time.time()
            
            try:
                # 嘗試識別語音 - 對短指令使用更短的超時
                if is_short_command:
                    # 短指令可能需要更長的辨識時間，因為內容少可能更難辨識
                    text = self.recognizer.recognize_google(initial_audio, language="zh-TW")
                else:
                    text = self.recognizer.recognize_google(initial_audio, language="zh-TW")
                
                # 辨識成功，進行文本修正
                text = correct_stt(text)
                
                # 記錄處理時間
                processing_time = time.time() - start_time
                
                # 首先顯示聽到的文字
                print(f"\n🎧 聽到: '{text}'{'(短指令)' if is_short_command else ''}")
                info_log(f"[STT] 識別結果: '{text}' {'(短指令)' if is_short_command else ''}, 處理時間={processing_time:.2f}秒")
                
                # 智能啟動判斷 - 對短指令使用極寬鬆的判斷標準
                if is_short_command:
                    # 所有短指令都嘗試處理
                    # 1. 優先檢查是否包含關鍵字
                    contains_keyword = self.smart_activation.contains_keywords(text)
                    
                    if contains_keyword:
                        activation_result = {
                            "should_activate": True,
                            "confidence": 0.8,
                            "reason": f"短指令包含關鍵字: {text}"
                        }
                        debug_log(2, f"[STT] 短指令關鍵字觸發: '{text}'")
                    else:
                        # 2. 檢查文本長度和上下文
                        words = text.split()
                        if len(words) <= 2:  # 極短命令 (1-2詞)
                            # 極短指令直接進入分析
                            # 大幅降低智能啟動的閾值判斷
                            activation_result = self.smart_activation.should_activate(text, threshold_reduction=0.15)
                            debug_log(2, f"[STT] 極短指令分析: '{text}', 閾值降低: 0.15")
                        else:
                            # 普通短指令
                            activation_result = self.smart_activation.should_activate(text, threshold_reduction=0.1)
                            debug_log(2, f"[STT] 短指令分析: '{text}', 閾值降低: 0.1")
                else:
                    # 正常長度的語音使用標準判斷
                    activation_result = self.smart_activation.should_activate(text)
                
                # 無論是否觸發，都回傳辨識結果，以保證用戶知道系統聽到了什麼
                # 添加聲音識別 - 即使不啟動也進行，以提供完整信息
                speaker_info = None
                if self.speaker_id.si_config.get("enabled", False):
                    audio_data = self._convert_audio_for_speaker_id(initial_audio)
                    if audio_data is not None:
                        speaker_info = self.speaker_id.identify_speaker(audio_data)
                
                # 創建基礎結果
                result = STTOutput(
                    text=text,
                    confidence=0.9,  # 語音識別的信心度固定為0.9
                    speaker_info=speaker_info,
                    activation_reason=activation_result["reason"]
                )
                
                # 無論是否觸發，都先在終端中顯示辨識結果
                print(f"\n📝 辨識結果: 「{text}」")
                
                # 檢查是否應該進行智能啟動
                if activation_result["should_activate"]:
                    # 設置啟動標誌
                    result.should_activate = True
                    
                    # 回調通知
                    if self._result_callback:
                        self._result_callback(result.dict())
                        
                    info_log(f"[STT] 智能觸發: '{text}' (原因: {activation_result['reason']})")
                    print(f"✅ 智能啟動觸發!")
                else:
                    # 設置未啟動標誌，但仍然傳回辨識結果
                    result.should_activate = False
                    
                    # 也回調，但標記為不啟動
                    if self._result_callback:
                        callback_result = result.dict()
                        callback_result["should_activate"] = False
                        self._result_callback(callback_result)
                    
                    debug_log(2, f"[STT] 智能過濾: '{text}' ({activation_result['reason']})")
                    print(f"⚪ 未觸發 (智能判斷分數: {activation_result['confidence']:.2f})")
                
            except sr.UnknownValueError:
                info_log("[STT] 語音辨識失敗: 無法識別語音內容")
                print("\n🔇 無法識別語音")
                
                # 仍然創建一個結果物件，但標記為識別失敗
                if self._result_callback:
                    result = STTOutput(
                        text="",
                        confidence=0.0,
                        error="無法識別語音",
                        should_activate=False
                    )
                    self._result_callback(result.dict())
                    
        except sr.RequestError as e:
            error_log(f"[STT] Google API 請求失敗: {str(e)}")
            print(f"\n⚠️ 辨識服務暫時無法使用: {str(e)}")
        except Exception as e:
            error_log(f"[STT] 智能檢測處理失敗: {str(e)}")
            print(f"\n❌ 處理失敗: {str(e)}")

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
        """轉換音頻用於聲音識別 - 添加能量日誌的改進版"""
        try:
            # 獲取原始音頻數據
            audio_data = np.frombuffer(audio.get_raw_data(), dtype=np.int16)
            
            # 記錄初始能量
            initial_energy = np.sum(audio_data ** 2)
            debug_log(3, f"[STT] 音頻能量 (原始): {initial_energy:.2f}")
            
            # 轉換為浮點數並正規化
            audio_data = audio_data.astype(np.float32) / 32768.0
            
            # 記錄正規化後能量
            normalized_energy = np.sum(audio_data ** 2)
            debug_log(3, f"[STT] 音頻能量 (正規化後): {normalized_energy:.2f}")
            
            # 重採樣到指定採樣率
            target_sr = self.speaker_id.sample_rate
            current_sr = audio.sample_rate
            if current_sr != target_sr:
                audio_data = librosa.resample(audio_data, orig_sr=current_sr, target_sr=target_sr)
                
                # 記錄重採樣後能量
                resampled_energy = np.sum(audio_data ** 2)
                debug_log(3, f"[STT] 音頻能量 (重採樣後): {resampled_energy:.2f}, 採樣率: {current_sr} -> {target_sr}")
            
            # 確保能量不為零
            if np.sum(audio_data ** 2) < 1e-6:
                debug_log(2, "[STT] 警告: 音頻能量接近零，可能影響說話人識別")
            
            return audio_data
        except Exception as e:
            error_log(f"[STT] 音頻轉換失敗 (SpeakerID): {str(e)}")
            return None

    def _on_voice_activity(self, event):
        """VAD 事件回調 - 增強版能夠處理語音緩衝區，包括短語音指令"""
        debug_log(4, f"[STT] VAD 事件: {event['event_type']}")
        
        if event["event_type"] == "speech_start":
            self._speech_buffer = []  # 開始收集音頻
            # 可以選擇性地在這裡顯示語音開始的提示
            # print("\n🔊 檢測到語音開始...", end="", flush=True)
            
        elif event["event_type"] == "speech_end":
            # 檢查是否有音頻緩衝區
            if "audio_buffer" in event and event["audio_buffer"]:
                try:
                    # 將緩衝區中的音頻合併
                    audio_data = np.concatenate(event["audio_buffer"])
                    
                    # 對於短語音命令（標記為短指令的事件），進行特殊處理
                    is_short_command = event.get("is_short_command", False)
                    
                    # 轉換為 sr.AudioData 格式
                    audio = self._convert_array_to_audio(audio_data)
                    
                    # 處理檢測到的語音，傳遞短命令標記
                    self._process_smart_detection(audio, is_short_command)
                    
                except Exception as e:
                    error_log(f"[STT] 處理 VAD 語音緩衝區失敗: {str(e)}")
                
            # 如果沒有音頻緩衝區但有收集的緩衝區
            elif hasattr(self, '_speech_buffer') and self._speech_buffer:
                try:
                    audio_data = np.concatenate(self._speech_buffer)
                    audio = self._convert_array_to_audio(audio_data)
                    self._process_smart_detection(audio)
                    self._speech_buffer = None
                except Exception as e:
                    error_log(f"[STT] 處理已收集的語音緩衝區失敗: {str(e)}")
                    
    def _convert_array_to_audio(self, audio_data: np.ndarray) -> sr.AudioData:
        """將 numpy 數組轉換為 AudioData"""
        sample_rate = self.vad.config.get("sample_rate", 16000)
        # 轉回 16 位整數
        audio_data = (audio_data * 32768.0).astype(np.int16)
        return sr.AudioData(audio_data.tobytes(), sample_rate, 2)

    def get_speaker_stats(self) -> dict:
        """獲取說話人統計信息"""
        return self.speaker_id.get_speaker_stats()

    def get_activation_stats(self) -> dict:
        """獲取啟動統計信息"""
        return self.smart_activation.get_activation_stats()

    def _smart_background_loop_pyaudio(self):
        """智能背景監聽循環 - 使用 PyAudio 實現真正的串流處理"""
        info_log("[STT] PyAudio 智能背景監聽循環啟動")
        print("\n🔊 背景監聽已啟動，識別結果將實時顯示...")
        remaining_time = getattr(self, '_background_duration', 30)  # 獲取實例變數
        start_time = time.time()
        last_update = 0  # 上次顯示時間
        frames = []  # 暫時保存當前塊的音頻幀
        
        # 計算每個塊的採樣數量
        rate = self.pa_config["rate"]
        chunk_size = int(rate * self.pa_config["stream_chunk_duration"])
        
        # 音頻回調函數 - 在有新音頻數據時調用
        def audio_callback(in_data, frame_count, time_info, status):
            if self._always_on_running:
                # 獲取音頻數據，轉換為 numpy 數組
                try:
                    # 將二進制數據轉換為整數數組
                    audio_data = np.frombuffer(in_data, dtype=np.int16)
                    # 轉換為浮點數並正規化
                    normalized_data = audio_data.astype(np.float32) / 32768.0
                    
                    # 添加到 VAD 串流處理器
                    self.vad.add_audio_chunk(normalized_data)
                except Exception as e:
                    error_log(f"[STT] 音頻處理錯誤: {str(e)}")
                
            return (in_data, pyaudio.paContinue)
        
        try:
            # 打開音頻流
            self.audio_stream = self.pyaudio_instance.open(
                format=self.pa_config["format"],
                channels=self.pa_config["channels"],
                rate=self.pa_config["rate"],
                input=True,
                frames_per_buffer=self.pa_config["frames_per_buffer"],
                input_device_index=self.device_index,
                stream_callback=audio_callback
            )
            
            self.audio_stream.start_stream()
            info_log("[STT] PyAudio 串流已開始")
            
            # 監控循環 - 主要用於顯示計時和檢查終止條件
            while self._always_on_running:
                # 顯示剩餘時間 (間隔顯示，減少輸出)
                elapsed = time.time() - start_time
                current_sec = int(elapsed)
                remaining = max(0, int(remaining_time - elapsed))
                
                # 每 5 秒更新一次，或剩餘時間小於 5 秒時每秒更新
                if current_sec % 5 == 0 and current_sec != last_update or remaining < 5 and current_sec != last_update:
                    # 添加動態進度指示
                    animation_chars = ["⋯", "⋯⋯", "⋯⋯⋯"]
                    animation_idx = (current_sec // 1) % len(animation_chars)
                    print(f"\r🎤 語音監聽中{animation_chars[animation_idx]} ({remaining}秒) ", end="", flush=True)
                    last_update = current_sec
                
                # 檢查是否到達時間限制
                if remaining_time > 0 and time.time() - start_time >= remaining_time:
                    print("\n\n⏱️ 監聽時間結束，共監聽了 {:.1f} 秒".format(time.time() - start_time))
                    break
                    
                time.sleep(0.2)  # 減少 CPU 使用率
                
        except Exception as e:
            error_log(f"[STT] PyAudio 監聽循環錯誤: {str(e)}")
            
        finally:
            # 關閉 PyAudio 流
            if self.audio_stream:
                try:
                    self.audio_stream.stop_stream()
                    self.audio_stream.close()
                    self.audio_stream = None
                except Exception as e:
                    error_log(f"[STT] 關閉音頻流錯誤: {str(e)}")
            
            self._always_on_running = False
            total_time = time.time() - start_time
            print(f"\n\n✅ PyAudio 智能背景監聽已停止，總共執行了 {total_time:.1f} 秒")
            print(f"識別結果顯示已結束。")

    def shutdown(self):
        """關閉模組"""
        self.stop_always_on()
        info_log("[STT] 模組已關閉")
