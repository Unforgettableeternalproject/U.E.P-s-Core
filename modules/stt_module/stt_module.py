# modules/stt_module/stt_module.py
# STT Module Phase 3 - 持續背景監聽 + 實時語音識別整合 + NLP模組連接

import threading
import queue
import time
import re
import numpy as np
import tempfile
import os
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

# 新的核心依賴
import torch
import pyaudio
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

from core.module_base import BaseModule
from utils.debug_helper import debug_log, info_log, error_log
from configs.config_loader import load_module_config
from core.schemas import STTModuleData, create_stt_data
from core.schema_adapter import STTSchemaAdapter
from .schemas import STTInput, STTOutput, ActivationMode, SpeakerInfo

# 獨立模組
from .vad import VoiceActivityDetection
from .speaker_identification import SpeakerIdentification

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
    def __init__(self, config=None, working_context_manager=None, result_callback=None):
        self.config = config or load_module_config("stt_module")
        
        # 工作上下文管理器
        self.working_context_manager = working_context_manager
        
        # 結果回調函數，用於將識別結果發送給NLP模組
        self.result_callback = result_callback
        
        # 基本配置
        self.device_index = self.config.get("device_index", None)  # 允許自動選擇麥克風
        self.phrase_time_limit = self.config.get("phrase_time_limit", 5)
        self.sample_rate = 16000  # Whisper 標準採樣率
        
        # Transformers Whisper 模型配置
        self.whisper_model_id = self.config.get("whisper_model_id", "openai/whisper-large-v3")
        self.whisper_local_path = self.config.get("whisper_local_path", "models/stt/whisper/whisper-large-v3")
        self.use_local_model = self.config.get("use_local_model", True)
        
        # 設備配置
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self.torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        
        # 模型組件
        self.model = None
        self.processor = None
        self.pipe = None
        
        # 說話人識別模式配置 (現在使用統一系統，但保留此變數以兼容現有配置)
        self.speaker_recognition_mode = self.config.get("speaker_recognition_mode", "unified")
        
        # 獨立模組
        self.vad_module = VoiceActivityDetection(self.sample_rate)
        self.speaker_module = SpeakerIdentification(config=self.config)  # 增強版語者識別系統
        
        # PyAudio 配置
        self.pyaudio_instance = None
        self.audio_stream = None
        self.pa_config = {
            "format": pyaudio.paInt16,
            "channels": 1,
            "rate": self.sample_rate,
            "frames_per_buffer": 1024,
        }
        
        # 當前狀態
        self._current_mode = ActivationMode.MANUAL
        self._listening_active = False
        
        info_log("[STT] Transformers Whisper + pyannote 架構模組初始化完成")

    def debug(self):
        debug_log(1, "[STT] Debug 模式啟用")
        debug_log(2, f"[STT] 基本設定: 設備={self.device_index}, 採樣率={self.sample_rate}")
        debug_log(2, f"[STT] 模型 ID: {self.whisper_model_id}")
        debug_log(2, f"[STT] 本地路徑: {self.whisper_local_path}")
        debug_log(2, f"[STT] 使用本地模型: {self.use_local_model}")
        debug_log(2, f"[STT] 計算設備: {self.device}, 數據類型: {self.torch_dtype}")
        debug_log(2, f"[STT] PyAudio 配置: {self.pa_config}")
        debug_log(2, f"[STT] 模式: 持續背景監聽，實時傳送結果給NLP模組")

    def initialize(self):
        debug_log(1, "[STT] 初始化中...")
        self.debug()

        try:
            # 初始化 Transformers Whisper 模型
            model_path = None
            if self.use_local_model and os.path.exists(self.whisper_local_path):
                model_path = self.whisper_local_path
                info_log(f"[STT] 使用本地 Transformers 模型: {model_path}")
            else:
                model_path = self.whisper_model_id
                info_log(f"[STT] 使用遠端 Transformers 模型: {model_path}")
            
            # 載入模型
            info_log("[STT] 載入 Whisper 模型...")
            self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
                model_path,
                torch_dtype=self.torch_dtype,
                low_cpu_mem_usage=True,
                use_safetensors=True
            )
            self.model.to(self.device)
            
            # 載入處理器
            info_log("[STT] 載入處理器...")
            if self.use_local_model and os.path.exists(self.whisper_local_path):
                self.processor = AutoProcessor.from_pretrained(self.whisper_local_path)
            else:
                self.processor = AutoProcessor.from_pretrained(self.whisper_model_id)
            
            # 創建 pipeline
            info_log("[STT] 創建語音識別 pipeline...")
            self.pipe = pipeline(
                "automatic-speech-recognition",
                model=self.model,
                tokenizer=self.processor.tokenizer,
                feature_extractor=self.processor.feature_extractor,
                torch_dtype=self.torch_dtype,
                device=self.device,
            )
            
            info_log(f"[STT] Transformers Whisper 模型載入成功 (設備: {self.device})")
            
            # 初始化 PyAudio
            self.pyaudio_instance = pyaudio.PyAudio()
            info_log("[STT] PyAudio 初始化成功")
            
            # 初始化新的獨立模組
            info_log("[STT] 初始化 VAD 模組...")
            if not self.vad_module.initialize():
                error_log("[STT] VAD 模組初始化失敗，但不影響基本 STT 功能")
            
            info_log("[STT] 初始化說話人識別模組...")
            if not self.speaker_module.initialize():
                info_log("[STT] 說話人識別模組使用 fallback 模式，基本功能仍可使用")
            else:
                info_log("[STT] 說話人識別模組初始化成功")
            
            # 語者識別已經初始化完畢
            
            # 列出可用的音頻設備
            debug_log(3, "[STT] 可用音頻設備：")
            for i in range(self.pyaudio_instance.get_device_count()):
                device_info = self.pyaudio_instance.get_device_info_by_index(i)
                if device_info['maxInputChannels'] > 0:
                    device_name = device_info['name']
                    debug_log(3, f"  設備 {i}: {device_name}")
            
            # 設置初始化完成標誌
            self.is_initialized = True
            
            return True
            
        except Exception as e:
            error_log(f"[STT] 初始化失敗：{e}")
            return False

    def handle(self, data: dict = {}) -> dict:
        """處理 STT 請求"""
        try:
            # 使用 schema adapter 轉換輸入數據
            schema_adapter = STTSchemaAdapter()
            adapted_input = schema_adapter.adapt_input(data)
            
            # 轉換為模組內部使用的格式
            validated = STTInput(**adapted_input)
            debug_log(1, f"[STT] 處理請求: {validated.mode}")
            
            start_time = time.time()
            
            if validated.mode == ActivationMode.MANUAL:
                # 手動模式：立即錄音識別
                result = self._manual_recognition(validated)
            elif validated.mode == ActivationMode.CONTINUOUS:
                # 持續背景監聽模式：持續錄音並實時傳送結果給NLP
                result = self._continuous_recognition(validated)
            else:
                # 不支持的模式
                raw_result = STTOutput(
                    text="", 
                    confidence=0.0, 
                    error="不支持的模式",
                    activation_reason="不支持的模式"
                ).model_dump()
                # 使用 schema adapter 轉換輸出數據
                return schema_adapter.adapt_output(raw_result)
                
            processing_time = time.time() - start_time
            result["processing_time"] = processing_time
            
            # 將結果轉換為 STTOutput 物件
            stt_output = STTOutput(**result)
            
            # 檢查是否有識別出文本
            if not stt_output.text or not stt_output.text.strip():
                info_log("[STT] 🔇 未識別到有效語音內容")
                # 更新錯誤信息
                stt_output.error = "未識別到有效語音內容"
                result["error"] = "未識別到有效語音內容"
            else:
                # 確保當有識別文本時，移除可能的錯誤信息
                stt_output.error = None
                result["error"] = None
            
            # 使用 STTOutput 的方法轉換為統一格式
            unified_data = stt_output.to_unified_format()
            
            # 將統一格式轉換為 API 輸出格式
            return schema_adapter.adapt_output(result)
            
        except Exception as e:
            error_log(f"[STT] 處理失敗: {str(e)}")
            return STTOutput(
                text="",
                confidence=0.0,
                error=f"處理失敗: {str(e)}"
            ).model_dump()

    def _manual_recognition(self, input_data: STTInput) -> dict:
        """手動語音識別 - 使用 Transformers Whisper"""
        try:
            info_log("[STT] 開始錄音...")
            
            # 使用 PyAudio 直接錄音
            duration = input_data.duration if input_data.duration else self.phrase_time_limit
            audio_data = self._record_audio(duration)
            
            if audio_data is None or len(audio_data) == 0:
                return STTOutput(
                    text="", 
                    confidence=0.0, 
                    error="錄音失敗或音頻為空",
                    activation_reason="錄音失敗"
                ).model_dump()
            
            info_log("[STT] 使用 Transformers Whisper 進行語音識別...")
            
            # 正規化音頻數據到 [-1, 1] 範圍
            audio_float = audio_data.astype(np.float32) / 32768.0
            
            # 生成參數配置
            generate_kwargs = {
                "max_new_tokens": 128,  # 降低到安全範圍
                "num_beams": 1,
                "condition_on_prev_tokens": False,
                "compression_ratio_threshold": 1.35,
                "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                "logprob_threshold": -1.0,
                "no_speech_threshold": 0.4,  # 降低閾值以提高敏感度
                "return_timestamps": True,
                "language": "en",  # 使用標準代碼
            }
            
            # 檢查音頻數據是否有語音內容
            if not self.vad_module.has_sufficient_speech(audio_data):
                info_log("[STT] VAD 檢測：未檢測到足夠語音內容，但仍嘗試識別")
            
            # 使用 Transformers pipeline 進行語音識別
            result = self.pipe(
                audio_float,
                generate_kwargs=generate_kwargs
            )
            
            text = result["text"].strip()
            text = correct_stt(text)
            confidence = self._calculate_transformers_confidence(result)
            
            # 檢查結果是否為空
            if not text or text.isspace():
                info_log("[STT] 🔇 未識別到有效語音內容")
                return STTOutput(
                    text="",
                    confidence=0.0,
                    speaker_info=None,
                    activation_reason="manual",
                    error="未識別到有效語音內容"
                ).model_dump()
            
            # 說話人識別 - 根據配置選擇模式
            speaker_info = None
            if input_data.enable_speaker_id:
                speaker_info = self._identify_speaker_with_mode(audio_data)
            
            # 顯示識別結果
            info_log(f"[STT] 識別結果: '{text}' (信心度: {confidence:.2f})")
            
            return STTOutput(
                text=text,
                confidence=confidence,
                speaker_info=speaker_info,
                activation_reason="manual",
                error=None
            ).model_dump()
            
        except Exception as e:
            error_log(f"[STT] 識別失敗: {str(e)}")
            return STTOutput(
                text="", 
                confidence=0.0, 
                error=f"識別失敗: {str(e)}",
                activation_reason="識別失敗：未知錯誤"
            ).model_dump()

    def _record_audio(self, duration: float) -> np.ndarray:
        """使用 PyAudio 錄製音頻"""
        try:
            # 創建音頻流
            stream_params = {
                "format": self.pa_config["format"],
                "channels": self.pa_config["channels"],
                "rate": self.pa_config["rate"],
                "input": True,
                "frames_per_buffer": self.pa_config["frames_per_buffer"]
            }
            
            # 只有當設備索引被明確指定時才添加
            if self.device_index is not None:
                stream_params["input_device_index"] = self.device_index
                
            stream = self.pyaudio_instance.open(**stream_params)
            
            frames = []
            frames_to_record = int(self.sample_rate * duration / self.pa_config["frames_per_buffer"])
            
            info_log(f"[STT] 開始錄音 {duration} 秒...")
            for _ in range(frames_to_record):
                data = stream.read(self.pa_config["frames_per_buffer"])
                frames.append(data)
            
            stream.stop_stream()
            stream.close()
            
            # 轉換為 numpy 數組
            audio_data = b''.join(frames)
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
            
            # 簡單的音頻前處理：歸一化並增強
            if len(audio_array) > 0:
                # 檢查音頻是否全為靜音
                if np.max(np.abs(audio_array)) > 0:
                    # 歸一化到最大振幅的 90%
                    norm_factor = 0.9 * 32767 / np.max(np.abs(audio_array))
                    audio_array = (audio_array * norm_factor).astype(np.int16)
            
            info_log(f"[STT] 錄音完成，長度: {len(audio_array) / self.sample_rate:.2f} 秒")
            return audio_array
            
        except Exception as e:
            error_log(f"[STT] 錄音失敗: {str(e)}")
            return None

    def _calculate_transformers_confidence(self, result: dict) -> float:
        """計算 Transformers Whisper 結果的信心度"""
        try:
            # 基於文本內容和時間戳資訊估算信心度
            text = result.get("text", "").strip()
            
            if not text:
                return 0.0
            
            base_confidence = 0.8  # Transformers 模型通常有更高的基礎信心度
            
            # 基於文本長度調整
            text_length = len(text.split())
            if 1 <= text_length <= 30:
                length_bonus = 0.15
            elif text_length > 30:
                length_bonus = 0.1
            else:
                length_bonus = 0.0
            
            # 基於是否包含常見詞彙
            common_words = ["UEP", "help", "please", "can", "you", "the", "a", "an", "is", "are"]
            common_word_count = sum(1 for word in text.lower().split() if word in common_words)
            common_word_bonus = min(common_word_count * 0.03, 0.1)
            
            # 檢查是否有時間戳資訊（表示模型對結果有信心）
            if "chunks" in result and result["chunks"]:
                timestamp_bonus = 0.05
            else:
                timestamp_bonus = 0.0
            
            confidence = min(base_confidence + length_bonus + common_word_bonus + timestamp_bonus, 1.0)
            return confidence
            
        except Exception as e:
            debug_log(3, f"[STT] 計算信心度失敗: {str(e)}")
            return 0.8  # 默認信心度

    def shutdown(self):
        """關閉模組"""
        if self.pyaudio_instance:
            self.pyaudio_instance.terminate()
        
        # 關閉新的獨立模組
        if hasattr(self, 'vad_module'):
            self.vad_module.shutdown()
        if hasattr(self, 'speaker_module'):
            self.speaker_module.shutdown()

    def _continuous_recognition(self, input_data: STTInput) -> dict:
        """持續背景監聽 - 持續錄音並實時傳送結果給NLP模組"""
        try:
            info_log("[STT] 開始持續背景監聽模式...")
            
            # 設定監聽時長，如果未指定則使用默認值
            duration = input_data.duration or 30.0
            start_time = time.time()
            
            info_log(f"[STT] 持續監聽時長: {duration} 秒")
            
            # 創建語者上下文，用於累積語者資訊
            context_id = None
            if self.working_context_manager:
                from core.working_context import ContextType
                # 創建或獲取SPEAKER_ACCUMULATION上下文
                context_id = self.working_context_manager.create_context(
                    ContextType.SPEAKER_ACCUMULATION, 
                    threshold=15,  # 樣本閾值
                    timeout=300.0  # 5分鐘過期
                )
                debug_log(2, f"[STT] 已建立持續監聽的語音累積上下文: {context_id}")
            
            # 持續監聽直到達到指定時間
            while time.time() - start_time < duration:
                # 短暫錄音檢測
                chunk_duration = 2.0
                audio_data = self._record_audio(chunk_duration)
                
                if audio_data is None:
                    continue
                
                # 使用VAD檢查是否有語音內容
                if not self.vad_module.has_sufficient_speech(audio_data, min_duration=0.05):
                    debug_log(3, "[STT] 音頻中語音內容不足，繼續監聽")
                    continue
                
                # 將音頻數據添加到語者上下文
                if context_id and self.working_context_manager:
                    self.working_context_manager.add_data_to_context(
                        context_id, 
                        audio_data,
                        metadata={"timestamp": time.time(), "type": "audio_sample"}
                    )
                
                # 使用Whisper進行語音識別
                info_log("[STT] 檢測到語音，進行識別...")
                audio_float = audio_data.astype(np.float32) / 32768.0
                
                # 識別參數
                recognition_kwargs = {
                    "max_new_tokens": 128,
                    "num_beams": 1,
                    "condition_on_prev_tokens": False,
                    "compression_ratio_threshold": 1.35,  # 與手動模式保持一致
                    "temperature": 0.0,  # 在連續模式下使用固定溫度以提高速度
                    "logprob_threshold": -1.0,  # 添加此參數避免 logprobs 錯誤
                    "no_speech_threshold": 0.4,  # 較低的閾值
                    "return_timestamps": True,
                    "language": "en",  # 使用標準代碼
                }
                
                result = self.pipe(audio_float, generate_kwargs=recognition_kwargs)
                text = result["text"].strip()
                text = correct_stt(text)  # 應用STT修正
                
                # 計算信心度
                confidence = self._calculate_transformers_confidence(result)
                
                # 檢查是否有識別出文本
                if not text or text.isspace():
                    debug_log(2, "[STT] 未識別到有效語音內容，繼續監聽")
                    continue
                
                info_log(f"[STT] 識別到語音內容: '{text}' (信心度: {confidence:.2f})")
                
                # 進行說話人識別
                speaker_info = None
                if input_data.enable_speaker_id:
                    speaker_info = self._identify_speaker_with_mode(audio_data)
                    debug_log(2, f"[STT] 識別語者: {speaker_info.speaker_id} (信心度: {speaker_info.confidence:.2f})")
                
                # 創建輸出物件
                output = STTOutput(
                    text=text,
                    confidence=confidence,
                    speaker_info=speaker_info,
                    activation_reason="continuous_listening",
                    error=None
                )
                
                # 轉換為統一格式
                unified_data = output.to_unified_format()
                
                # 如果有上下文ID，添加到metadata
                if context_id:
                    unified_data.metadata["context_id"] = context_id
                
                # 通過回調將結果發送給NLP模組
                if self.result_callback:
                    try:
                        # 將結果發送給回調函數
                        self.result_callback(unified_data)
                        info_log(f"[STT] 將識別結果實時發送給NLP模組：'{text}' (語者: {speaker_info.speaker_id if speaker_info else 'unknown'})")
                    except Exception as e:
                        error_log(f"[STT] 發送識別結果失敗: {e}")
                else:
                    debug_log(2, "[STT] 未設定結果回調函數，識別結果將不會發送給NLP模組")
                
                # 短暫休息
                time.sleep(0.1)
            
            # 監聽結束
            if context_id and self.working_context_manager:
                # 不要標記為完成，因為是持續監聽
                # self.working_context_manager.mark_context_completed(context_id)
                pass
                
            # 返回最後的監聽狀態
            return STTOutput(
                text="",
                confidence=0.0,
                speaker_info=None,
                activation_reason="continuous_listening_completed",
                error=None
            ).model_dump()
            
        except Exception as e:
            error_log(f"[STT] 持續監聽失敗: {str(e)}")
            return STTOutput(
                text="",
                confidence=0.0,
                error=f"持續監聽失敗: {str(e)}",
                activation_reason="continuous_listening_failed"
            ).model_dump()
            
    def _identify_speaker_with_mode(self, audio_data: np.ndarray) -> SpeakerInfo:
        """根據配置的模式進行說話人識別"""
        try:
            # 我們現在只有一種語者識別系統，無需再根據模式選擇
            debug_log(2, f"[STT] 使用統一的語者識別系統")
            return self.speaker_module.identify_speaker(audio_data)
                
        except Exception as e:
            error_log(f"[STT] 說話人識別完全失敗: {e}")
            # 返回默認結果
            return SpeakerInfo(
                speaker_id="unknown",
                confidence=0.0,
                is_new_speaker=False,
                voice_features={"error": str(e)}
            )

    def shutdown(self):
        # 清理 GPU 記憶體
        if self.model is not None:
            del self.model
        if self.pipe is not None:
            del self.pipe
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        
        info_log("[STT] 模組已關閉")
