# modules/stt_module/stt_module_transformers.py
# STT Module Phase 2 - Transformers Whisper + pyannote 架構

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
from .schemas import STTInput, STTOutput, ActivationMode, SpeakerInfo

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
    def __init__(self, config=None):
        self.config = config or load_module_config("stt_module")
        
        # 基本配置
        self.device_index = self.config.get("device_index", 1)
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
            
            # 列出可用的音頻設備
            debug_log(3, "[STT] 可用音頻設備：")
            for i in range(self.pyaudio_instance.get_device_count()):
                device_info = self.pyaudio_instance.get_device_info_by_index(i)
                if device_info['maxInputChannels'] > 0:
                    device_name = device_info['name']
                    debug_log(3, f"  設備 {i}: {device_name}")
            
            return True
            
        except Exception as e:
            error_log(f"[STT] 初始化失敗：{e}")
            return False

    def handle(self, data: dict = {}) -> dict:
        """處理 STT 請求"""
        try:
            validated = STTInput(**data)
            debug_log(1, f"[STT] 處理請求: {validated.mode}")
            
            start_time = time.time()
            
            if validated.mode == ActivationMode.MANUAL:
                # 手動模式：立即錄音識別
                result = self._manual_recognition(validated)
            else:
                # 智能模式暫時不支持
                return STTOutput(
                    text="", 
                    confidence=0.0, 
                    error="智能模式暫未實現",
                    activation_reason="不支持的模式"
                ).dict()
                
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
                ).dict()
            
            info_log("[STT] 使用 Transformers Whisper 進行語音識別...")
            
            # 正規化音頻數據到 [-1, 1] 範圍
            audio_float = audio_data.astype(np.float32) / 32768.0
            
            # 生成參數配置
            generate_kwargs = {
                "max_new_tokens": 448,
                "num_beams": 1,
                "condition_on_prev_tokens": False,
                "compression_ratio_threshold": 1.35,
                "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                "logprob_threshold": -1.0,
                "no_speech_threshold": 0.6,
                "return_timestamps": True,
            }
            
            # 使用 Transformers pipeline 進行語音識別
            result = self.pipe(
                audio_float,
                generate_kwargs=generate_kwargs
            )
            
            text = result["text"].strip()
            text = correct_stt(text)
            confidence = self._calculate_transformers_confidence(result)
            
            # 說話人識別 (暫時跳過)
            speaker_info = None
            if input_data.enable_speaker_id:
                speaker_info = self._identify_speaker_simple(audio_data)
            
            return STTOutput(
                text=text,
                confidence=confidence,
                speaker_info=speaker_info,
                activation_reason="manual",
                error=None
            ).dict()
            
        except Exception as e:
            error_log(f"[STT] 識別失敗: {str(e)}")
            return STTOutput(
                text="", 
                confidence=0.0, 
                error=f"識別失敗: {str(e)}",
                activation_reason="識別失敗：未知錯誤"
            ).dict()

    def _record_audio(self, duration: float) -> np.ndarray:
        """使用 PyAudio 錄製音頻"""
        try:
            # 創建音頻流
            stream = self.pyaudio_instance.open(
                format=self.pa_config["format"],
                channels=self.pa_config["channels"],
                rate=self.pa_config["rate"],
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=self.pa_config["frames_per_buffer"]
            )
            
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

    def _identify_speaker_simple(self, audio_data: np.ndarray) -> SpeakerInfo:
        """簡化的說話人識別（暫時返回未知說話人）"""
        # TODO: 實現基於 pyannote 的說話人識別
        return SpeakerInfo(
            speaker_id="unknown",
            confidence=0.0,
            is_new_speaker=False,
            voice_features=None
        )

    def shutdown(self):
        """關閉模組"""
        if self.pyaudio_instance:
            self.pyaudio_instance.terminate()
        
        # 清理 GPU 記憶體
        if self.model is not None:
            del self.model
        if self.pipe is not None:
            del self.pipe
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        
        info_log("[STT] 模組已關閉")
