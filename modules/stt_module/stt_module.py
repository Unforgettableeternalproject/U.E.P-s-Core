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
from typing import Optional, Dict, Any, cast
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", message=".*forced_decoder_ids.*")
warnings.filterwarnings("ignore", message=".*past_key_values.*")

# 新的核心依賴
import torch
import pyaudio
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

from core.bases.module_base import BaseModule
from utils.debug_helper import debug_log, info_log, error_log
from configs.config_loader import load_module_config
from configs.user_settings_manager import user_settings_manager, get_user_setting
from core.schemas import STTModuleData
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
        super().__init__()
        
        self.config = config or load_module_config("stt_module")
        
        # 工作上下文管理器
        self.working_context_manager = working_context_manager
        
        # 結果回調函數，用於將識別結果發送給NLP模組
        self.result_callback = result_callback
        
        # 基本配置
        # 優先從 user_settings.yaml 讀取麥克風索引
        user_mic_index = get_user_setting("interaction.speech_input.microphone_device_index", None)
        self.device_index = user_mic_index if user_mic_index is not None else self.config.get("device_index", None)
        self.phrase_time_limit = self.config.get("phrase_time_limit", 5)
        debug_log(2, f"[STT] 麥克風裝置索引: {self.device_index} (來源: {'user_settings' if user_mic_index is not None else 'stt_module'})")
        self.sample_rate = 16000  # Whisper 標準採樣率
        
        # Transformers Whisper 模型配置（已切換到輕量級模型）
        self.whisper_model_id = self.config.get("whisper_model_id", "openai/whisper-base")
        self.whisper_local_path = self.config.get("whisper_local_path", "models/stt/whisper/whisper-base")
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
        
        # 監聽控制
        self.should_stop_listening = False
        
        # PyAudio 配置
        self.pyaudio_instance = None
        self.audio_stream = None
        self.pa_config = {
            "format": pyaudio.paInt16,
            "channels": 1,
            "rate": self.sample_rate,
            "frames_per_buffer": 1024,
        }
        
        # 🔧 從 user_settings 讀取啟動模式（預設為 MANUAL）
        enable_continuous = get_user_setting("interaction.speech_input.enable_continuous_mode", False)
        self._current_mode = ActivationMode.CONTINUOUS if enable_continuous else ActivationMode.MANUAL
        self._listening_active = False
        
        # VAD 和語音設定（從 user_settings 讀取，供熱重載使用）
        self.vad_sensitivity = float(get_user_setting("interaction.speech_input.vad_sensitivity", 0.7))
        self.min_speech_duration = float(get_user_setting("interaction.speech_input.min_speech_duration", 0.3))
        self.wake_word_confidence = float(get_user_setting("interaction.speech_input.wake_word_confidence", 0.8))

        self.is_initialized = False
        
        # 效能指標追蹤
        self.total_audio_duration = 0.0
        self.total_recognitions = 0
        self.vad_triggers = 0
        
        # 註冊使用者設定熱重載回調（在 initialize 之前註冊，避免循環）
        user_settings_manager.register_reload_callback("stt_module", self._reload_from_user_settings)
        
        info_log("[STT] Transformers Whisper + pyannote 架構模組初始化完成")

    def debug(self):
        debug_log(1, "[STT] Debug 模式啟用")
        debug_log(2, f"[STT] 基本設定: 設備={self.device_index}, 採樣率={self.sample_rate}")
        debug_log(2, f"[STT] 模型 ID: {self.whisper_model_id}")
        debug_log(2, f"[STT] 本地路徑: {self.whisper_local_path}")
        debug_log(3, f"[STT] 使用本地模型: {self.use_local_model}")
        debug_log(3, f"[STT] 計算設備: {self.device}, 數據類型: {self.torch_dtype}")
        debug_log(3, f"[STT] PyAudio 配置: {self.pa_config}")
        debug_log(3, f"[STT] 模式: 持續背景監聽，實時傳送結果給NLP模組")
        debug_log(4, f"[STT] 完整模組設定: {self.config}")

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
            
            # 🔧 明確清除模型配置中的 forced_decoder_ids
            # Whisper 模型預設有此設定，但會與 task="translate" 衝突
            if hasattr(self.model.config, 'forced_decoder_ids'):
                self.model.config.forced_decoder_ids = None
                debug_log(3, "[STT] 已清除模型的 forced_decoder_ids 設定")
            
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
                max_input = device_info.get('maxInputChannels', 0)
                if isinstance(max_input, int) and max_input > 0:
                    device_name = device_info.get('name', 'Unknown')
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
            # 🔧 文字模式特殊處理（直接調用實現方法）
            if data.get("mode") == "text" and "text" in data:
                return self._handle_text_input_impl(data["text"])
            
            # 直接轉換為模組內部使用的格式
            validated = STTInput(**data)
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
                return STTOutput(
                    text="", 
                    confidence=0.0, 
                    error="不支持的模式",
                    activation_reason="不支持的模式"
                ).model_dump()
                
            processing_time = time.time() - start_time
            result["processing_time"] = processing_time
            
            # 將結果轉換為 STTOutput 物件
            stt_output = STTOutput(**result)
            
            # 檢查是否有識別出文本（但持續監聽完成不算錯誤）
            is_listening_completed = stt_output.activation_reason == "continuous_listening_completed"
            if not stt_output.text or not stt_output.text.strip():
                if is_listening_completed:
                    # 持續監聽正常結束，不是錯誤
                    stt_output.error = None
                else:
                    # 其他情況下，空文本是錯誤
                    info_log("[STT] 🔇 未識別到有效語音內容")
                    stt_output.error = "未識別到有效語音內容"
            else:
                stt_output.error = None
            
            # 更新效能指標
            self.total_recognitions += 1
            if stt_output.text and stt_output.text.strip():
                self.update_custom_metric('recognition_confidence', stt_output.confidence)
                self.update_custom_metric('speaker_id', stt_output.speaker_id or 'unknown')
                self.update_custom_metric('text_length', len(stt_output.text))
            
            # 如果有音頻時長信息
            if validated.mode == ActivationMode.MANUAL:
                audio_duration = validated.duration if hasattr(validated, 'duration') and validated.duration else self.phrase_time_limit
                self.total_audio_duration += audio_duration
                self.update_custom_metric('audio_duration', audio_duration)
            
            # 返回字典格式
            return stt_output.model_dump()
            
        except Exception as e:
            error_log(f"[STT] 處理失敗: {str(e)}")
            return STTOutput(
                text="",
                confidence=0.0,
                error=f"處理失敗: {str(e)}"
            ).model_dump()
    
    def stop_listening(self):
        """停止持續監聽"""
        self.should_stop_listening = True
        debug_log(2, "[STT] 設置停止監聽標誌")
    
    def resume_listening(self):
        """恢復監聽能力"""
        self.should_stop_listening = False
        debug_log(2, "[STT] 清除停止監聽標誌")
    
    def handle_text_input(self, text: str) -> dict:
        """
        處理文字輸入 - 繞過語音識別和說話人辨識
        
        這是一個特殊的入口點,用於:
        1. 不需要語音活動檢測的情況
        2. 用戶選擇關閉 STT 功能但仍想使用系統的情況
        3. 測試和開發目的
        
        特性:
        - 不進行語音識別 (直接使用文字)
        - 不進行說話人辨識 (speaker_info=None)
        - 不創建 speaker_accumulation 上下文
        - NLP 將使用預設身份處理
        
        Args:
            text: 用戶輸入的文字內容
            
        Returns:
            dict: 統一格式的輸出結果
        """
        # 🔧 通過 handle() 方法以自動記錄效能指標
        return self.handle({
            "mode": "text",
            "text": text
        })
    
    def _handle_text_input_impl(self, text: str) -> dict:
        """
        處理文字輸入的實際實現
        
        （原 handle_text_input 的邏輯）
        """
        try:
            if not text or text.isspace():
                debug_log(2, "[STT] 文字輸入為空，忽略")
                return STTOutput(
                    text="",
                    confidence=0.0,
                    speaker_info=None,
                    activation_reason="text_input_empty",
                    error="文字輸入為空"
                ).model_dump()
            
            # 🆕 檢查是否有任何 cycle 正在處理輸入
            # 如果有，等待當前所有 cycle 完成（模擬 VAD 在 cycle 未結束時不接受新輸入的行為）
            from core.sessions.session_manager import unified_session_manager
            from core.system_loop import system_loop
            import time
            
            active_cs = unified_session_manager.get_active_chatting_session_ids()
            active_ws = unified_session_manager.get_active_workflow_session_ids()
            
            debug_log(2, f"[STT] 文字輸入等待檢查: active_cs={len(active_cs) if active_cs else 0}, active_ws={len(active_ws) if active_ws else 0}")
            
            if active_cs or active_ws:
                debug_log(2, f"[STT] 檢測到活躍會話，檢查 cycle tracking")
                # 有活躍會話，檢查是否有任何 cycle 正在處理
                if hasattr(system_loop, '_cycle_layer_tracking'):
                    max_wait_time = 60.0  # 最多等待 60 秒
                    wait_start = time.time()
                    
                    with system_loop._cycle_tracking_lock:
                        tracking_count = len(system_loop._cycle_layer_tracking)
                        debug_log(2, f"[STT] 當前 cycle tracking 數量: {tracking_count}")
                    
                    if tracking_count > 0:
                        info_log(f"[STT] ⏳ 等待當前 cycle 完成（模擬 VAD 行為）...")
                    
                    while time.time() - wait_start < max_wait_time:
                        with system_loop._cycle_tracking_lock:
                            # 如果沒有任何 cycle 正在追蹤，表示可以接受新輸入
                            if len(system_loop._cycle_layer_tracking) == 0:
                                debug_log(2, f"[STT] ✓ 所有 cycle 已完成，接受新輸入")
                                break
                            
                            # 記錄等待的 cycle
                            tracking_keys = list(system_loop._cycle_layer_tracking.keys())
                            debug_log(3, f"[STT] 等待 cycle 完成: {tracking_keys}")
                        
                        time.sleep(0.1)
                    else:
                        # 等待超時
                        debug_log(1, f"[STT] ⚠️ 等待 cycle 完成超時，強制接受輸入")
                else:
                    debug_log(2, f"[STT] system_loop 沒有 _cycle_layer_tracking 屬性")
            else:
                debug_log(2, f"[STT] 沒有活躍會話，直接接受輸入")
            
            info_log(f"[STT] 文字輸入模式: '{text}'")
            
            # 🎤 發布 INTERACTION_STARTED 事件（與 VAD 模式保持一致）
            try:
                from core.event_bus import event_bus, SystemEvent
                event_bus.publish(
                    SystemEvent.INTERACTION_STARTED,
                    {
                        "source": "stt_text_input",
                        "input_mode": "text"
                    },
                    source="stt_module"
                )
                debug_log(2, "[STT] 已發布 INTERACTION_STARTED 事件（文字輸入模式）")
            except Exception as e:
                debug_log(2, f"[STT] 無法發布 INTERACTION_STARTED 事件: {e}")
            
            # 創建輸出物件 - 不包含說話人資訊
            output = STTOutput(
                text=text.strip(),
                confidence=1.0,  # 文字輸入視為 100% 信心度
                speaker_info=None,  # 明確設為 None,表示繞過說話人識別
                activation_reason="text_input",
                error=None
            )
            
            # 轉換為統一格式
            unified_data = output.to_unified_format()
            
            # 添加特殊標記到 metadata
            unified_data.metadata["input_mode"] = "text"
            unified_data.metadata["bypass_speaker_id"] = True
            
            # 通過回調將結果發送給 NLP 模組
            if self.result_callback:
                try:
                    self.result_callback(unified_data)
                    info_log(f"[STT] 文字輸入已發送給 NLP 模組: '{text}'")
                except Exception as e:
                    error_log(f"[STT] 發送文字輸入結果失敗: {e}")
            else:
                debug_log(2, "[STT] 未設定結果回調函數")
            
            return output.model_dump()
            
        except Exception as e:
            error_log(f"[STT] 處理文字輸入失敗: {str(e)}")
            return STTOutput(
                text="",
                confidence=0.0,
                speaker_info=None,
                activation_reason="text_input_error",
                error=f"處理文字輸入失敗: {str(e)}"
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
                "no_speech_threshold": 0.4,
                "return_timestamps": True,
                "task": "translate",  # 翻譯成英文（系統內部運作需要）
            }
            
            # 檢查音頻數據是否有語音內容
            if not self.vad_module.has_sufficient_speech(audio_data):
                info_log("[STT] VAD 檢測：未檢測到足夠語音內容，但仍嘗試識別")
            
            # 使用 Transformers pipeline 進行語音識別
            if self.pipe is None:
                error_log("[STT] Pipeline 未初始化")
                return STTOutput(text="", confidence=0.0, error="Pipeline 未初始化").model_dump()
            
            # 🔧 使用 pipeline 進行識別，新版 transformers 會自動處理 input_features
            result = self.pipe(
                audio_float,
                generate_kwargs=generate_kwargs
            )
            
            # 🔍 調試：輸出原始結果
            debug_log(2, f"[STT] Pipeline 原始結果: {result}")
            
            # 類型轉換 - Transformers pipeline 返回 dict
            result_dict = cast(Dict[str, Any], result)
            text = str(result_dict.get("text", "")).strip()
            text = correct_stt(text)
            confidence = self._calculate_transformers_confidence(result_dict)
            
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
                
                # 將音頻樣本添加到 Speaker_Accumulation 上下文
                self._add_audio_sample_to_accumulation(audio_data, speaker_info)
            
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
            
            if self.pyaudio_instance is None:
                error_log("[STT] PyAudio 未初始化")
                return np.array([])
                
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
            return np.array([])

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
        # 清理 PyAudio
        if self.pyaudio_instance:
            self.pyaudio_instance.terminate()
        
        # 清理 GPU 記憶體
        if self.model is not None:
            del self.model
        if self.pipe is not None:
            del self.pipe
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        
        # 關閉獨立模組
        if hasattr(self, 'vad_module'):
            self.vad_module.shutdown()
        if hasattr(self, 'speaker_module'):
            self.speaker_module.shutdown()
        
        info_log("[STT] 模組已關閉")

    def _continuous_recognition(self, input_data: STTInput) -> dict:
        """持續背景監聽 - 持續錄音，檢測到完整語音片段後合併發送給NLP模組"""
        try:
            info_log("[STT] 開始持續背景監聽模式（智能語音片段合併）...")
            
            # 設定監聽時長，如果未指定則使用默認值
            duration = input_data.duration or 30.0
            start_time = time.time()
            
            # VAD 參數配置
            chunk_duration = 2.0  # 每次錄音的片段長度（秒）
            silence_threshold = 1.5  # 靜音持續時間閾值（秒），超過此時間視為語音結束
            max_speech_duration = 30.0  # 單次語音最大長度（秒），防止無限累積
            
            info_log(f"[STT] 持續監聽配置: 總時長={duration}s, 片段長度={chunk_duration}s, "
                    f"靜音閾值={silence_threshold}s, 最大語音長度={max_speech_duration}s")
            
            # 創建語者上下文，用於累積語者資訊
            context_id = None
            if self.working_context_manager:
                from core.working_context import ContextType
                context_id = self.working_context_manager.create_context(
                    ContextType.SPEAKER_ACCUMULATION, 
                    threshold=15,  # 樣本閾值
                    timeout=300.0  # 5分鐘過期
                )
                debug_log(2, f"[STT] 已建立持續監聽的語音累積上下文: {context_id}")
            
            # 語音片段緩衝區
            audio_buffer = []  # 存儲待合併的音頻片段
            last_speech_time = None  # 最後檢測到語音的時間
            speech_start_time = None  # 當前語音片段開始時間
            
            # 持續監聽直到達到指定時間或收到停止信號
            while time.time() - start_time < duration and not self.should_stop_listening:
                current_time = time.time()
                
                # 錄製音頻片段
                audio_chunk = self._record_audio(chunk_duration)
                
                if audio_chunk is None or len(audio_chunk) == 0:
                    debug_log(3, "[STT] 錄音失敗或音頻為空，繼續監聽")
                    continue
                
                # 使用VAD檢查是否有語音內容
                has_speech = self.vad_module.has_sufficient_speech(audio_chunk, min_duration=0.05)
                
                if has_speech:
                    # 檢測到語音
                    debug_log(3, "[STT] 檢測到語音內容，加入緩衝區")
                    
                    # 如果這是新的語音片段開始
                    if speech_start_time is None:
                        speech_start_time = current_time
                        info_log("[STT] 🎤 語音開始...")
                    
                    # 將音頻添加到緩衝區
                    audio_buffer.append(audio_chunk)
                    last_speech_time = current_time
                    
                    # 檢查是否超過最大語音長度
                    if current_time - speech_start_time > max_speech_duration:
                        info_log(f"[STT] 語音片段達到最大長度 ({max_speech_duration}s)，強制處理")
                        self._process_audio_buffer(
                            audio_buffer, 
                            context_id, 
                            input_data.enable_speaker_id
                        )
                        # 重置緩衝區
                        audio_buffer = []
                        last_speech_time = None
                        speech_start_time = None
                    
                else:
                    # 未檢測到語音（靜音）
                    if audio_buffer:
                        # 計算靜音持續時間
                        silence_duration = current_time - last_speech_time if last_speech_time else 0
                        debug_log(3, f"[STT] 靜音持續: {silence_duration:.2f}s / {silence_threshold}s")
                        
                        # 如果靜音時間超過閾值，處理緩衝區中的音頻
                        if silence_duration >= silence_threshold:
                            speech_duration = (last_speech_time - speech_start_time) if (last_speech_time and speech_start_time) else 0
                            info_log(f"[STT] 📝 語音結束 (時長: {speech_duration:.2f}s)，開始處理...")
                            
                            self._process_audio_buffer(
                                audio_buffer, 
                                context_id, 
                                input_data.enable_speaker_id
                            )
                            
                            # 重置緩衝區
                            audio_buffer = []
                            last_speech_time = None
                            speech_start_time = None
                    else:
                        debug_log(3, "[STT] 靜音狀態，等待語音...")
                
                # 短暫休息避免過度佔用CPU
                time.sleep(0.05)
            
            # 監聽結束時，如果緩衝區還有未處理的音頻，處理它
            if audio_buffer:
                info_log("[STT] 監聽結束，處理剩餘音頻緩衝區...")
                self._process_audio_buffer(
                    audio_buffer, 
                    context_id, 
                    input_data.enable_speaker_id
                )
            
            # 監聽結束
            if context_id and self.working_context_manager:
                # 不要標記為完成，因為是持續監聽
                pass
                
            info_log("[STT] 持續監聽模式正常結束")
            return STTOutput(
                text="",
                confidence=1.0,
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
    
    def _process_audio_buffer(self, audio_buffer: list, context_id: Optional[str], 
                             enable_speaker_id: bool) -> None:
        """處理音頻緩衝區 - 合併音頻並進行識別"""
        try:
            if not audio_buffer:
                debug_log(2, "[STT] 音頻緩衝區為空，跳過處理")
                return
            
            # 合併所有音頻片段
            info_log(f"[STT] 合併 {len(audio_buffer)} 個音頻片段...")
            merged_audio = np.concatenate(audio_buffer)
            total_duration = len(merged_audio) / self.sample_rate
            info_log(f"[STT] 合併後音頻長度: {total_duration:.2f} 秒")
            
            # 發布 INTERACTION_STARTED 事件，通知前端使用者開始互動
            try:
                from core.event_bus import event_bus, SystemEvent
                event_bus.publish(
                    SystemEvent.INTERACTION_STARTED,
                    {
                        "module": "stt",
                        "input_type": "voice",
                        "audio_duration": total_duration,
                        "num_chunks": len(audio_buffer)
                    },
                    source="stt_module"
                )
                debug_log(2, "[STT] 已發布 INTERACTION_STARTED 事件")
            except Exception as e:
                debug_log(2, f"[STT] 無法發布 INTERACTION_STARTED 事件: {e}")
            
            # 將音頻數據添加到語者上下文
            if context_id and self.working_context_manager:
                self.working_context_manager.add_data_to_context(
                    context_id, 
                    merged_audio,
                    metadata={"timestamp": time.time(), "type": "merged_audio_sample"}
                )
            
            # 使用Whisper進行語音識別
            info_log("[STT] 對合併音頻進行語音識別...")
            audio_float = merged_audio.astype(np.float32) / 32768.0
            
            # 識別參數
            recognition_kwargs = {
                "max_new_tokens": 128,
                "num_beams": 1,
                "condition_on_prev_tokens": False,
                "compression_ratio_threshold": 1.35,
                "temperature": 0.0,
                "logprob_threshold": -1.0,
                "no_speech_threshold": 0.4,
                "return_timestamps": True,
                "task": "translate",  # 翻譯任務：將所有語言翻譯成英文
            }
            
            if self.pipe is None:
                error_log("[STT] Pipeline 未初始化，無法識別")
                return
            
            result = self.pipe(audio_float, generate_kwargs=recognition_kwargs)  # type: ignore
            result_dict = cast(Dict[str, Any], result)
            text = str(result_dict.get("text", "")).strip()
            text = correct_stt(text)  # 應用STT修正
            
            # 計算信心度
            confidence = self._calculate_transformers_confidence(result_dict)
            
            # 檢查是否有識別出文本
            if not text or text.isspace():
                debug_log(2, "[STT] 未識別到有效語音內容")
                return
            
            info_log(f"[STT] ✅ 識別結果: '{text}' (信心度: {confidence:.2f})")
            
            # 進行說話人識別
            speaker_info = None
            if enable_speaker_id:
                speaker_info = self._identify_speaker_with_mode(merged_audio)
                if speaker_info:
                    speaker_id = speaker_info.speaker_id
                    speaker_confidence = speaker_info.confidence
                    debug_log(2, f"[STT] 識別語者: {speaker_id} (信心度: {speaker_confidence:.2f})")
            
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
            unified_data.metadata["audio_duration"] = total_duration
            unified_data.metadata["num_chunks_merged"] = len(audio_buffer)
            
            # 通過回調將結果發送給NLP模組
            if self.result_callback:
                try:
                    self.result_callback(unified_data)
                    speaker_id = unified_data.speaker_info.get('speaker_id', 'unknown') if unified_data.speaker_info else 'unknown'
                    info_log(f"[STT] 📤 識別結果已發送給NLP模組: '{text}' (語者: {speaker_id})")
                except Exception as e:
                    error_log(f"[STT] 發送識別結果失敗: {e}")
            else:
                debug_log(2, "[STT] 未設定結果回調函數（minimal 模式）")
            
            # 🔧 在 console 輸出結果（用於 debugger 和 minimal 模式）
            speaker_id = speaker_info.speaker_id if speaker_info else "unknown"
            print(f"\n🎤 [{speaker_id}] {text}")
            if confidence < 0.7:
                print(f"   ⚠️  低置信度: {confidence:.2f}")
            
        except Exception as e:
            error_log(f"[STT] 處理音頻緩衝區失敗: {str(e)}")
            
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

    def _add_audio_sample_to_accumulation(self, audio_data: np.ndarray, speaker_info: Optional[SpeakerInfo] = None):
        """將音頻樣本添加到 Speaker_Accumulation 上下文中
        
        ⚠️ 重要：只有在已指定使用者身分時才會累積樣本
        檢查 Working Context 中是否有 declared_identity 來判斷
        """
        try:
            if not self.working_context_manager:
                debug_log(3, "[STT] Working Context 管理器不可用，跳過樣本累積")
                return
            
            # 🆕 檢查是否有已聲明的 Identity
            # 只有在使用者已明確指定身分時才累積樣本
            has_declared_identity = self._check_has_declared_identity()
            if not has_declared_identity:
                debug_log(3, "[STT] 無已聲明的 Identity，跳過 Speaker 樣本累積")
                return
            
            debug_log(3, "[STT] 檢測到已聲明的 Identity，開始累積 Speaker 樣本")
            
            from core.working_context import ContextType
            import time
            
            # 查找或創建 SPEAKER_ACCUMULATION 上下文
            contexts = self.working_context_manager.get_contexts_by_type("speaker_accumulation")
            
            context_id = None
            if contexts:
                # 使用最新的上下文
                latest_context = max(contexts, key=lambda c: c.get('created_at', 0))
                context_id = latest_context['id']
                debug_log(3, f"[STT] 使用現有 Speaker_Accumulation 上下文: {context_id}")
            else:
                # 創建新的上下文
                context_id = self.working_context_manager.create_context(
                    ContextType.SPEAKER_ACCUMULATION,
                    threshold=15,  # 樣本閾值
                    timeout=300.0  # 5分鐘過期
                )
                debug_log(2, f"[STT] 創建新的 Speaker_Accumulation 上下文: {context_id}")
            
            if context_id:
                # 添加音頻樣本到上下文
                # 安全地獲取說話人資訊
                if speaker_info:
                    speaker_id = speaker_info.speaker_id
                    confidence = speaker_info.confidence
                else:
                    speaker_id = "unknown"
                    confidence = 0.0
                
                sample_metadata = {
                    "timestamp": time.time(),
                    "type": "audio_sample",
                    "speaker_id": speaker_id,
                    "confidence": confidence,
                    "audio_length": len(audio_data)
                }
                
                self.working_context_manager.add_data_to_context(
                    context_id,
                    audio_data,
                    metadata=sample_metadata
                )
                
                debug_log(3, f"[STT] 音頻樣本已添加到累積上下文: {context_id} "
                         f"(說話人: {sample_metadata['speaker_id']}, "
                         f"長度: {sample_metadata['audio_length']})")
            
        except Exception as e:
            error_log(f"[STT] 添加音頻樣本到累積上下文失敗: {e}")
    
    def _check_has_declared_identity(self) -> bool:
        """檢查 Working Context 中是否有已聲明的 Identity
        
        Returns:
            bool: 是否有已聲明的 Identity
        """
        try:
            if not self.working_context_manager:
                return False
            
            # 檢查全局上下文數據中的 declared_identity 標記
            global_data = self.working_context_manager.global_context_data
            
            # 方法1: 檢查 declared_identity 標記
            if global_data.get('declared_identity'):
                debug_log(3, "[STT] 檢測到 declared_identity 標記")
                return True
            
            # 方法2: 檢查 current_identity_id
            current_identity_id = global_data.get('current_identity_id')
            if current_identity_id and current_identity_id != 'unknown':
                debug_log(3, f"[STT] 檢測到 current_identity_id: {current_identity_id}")
                return True
            
            # 方法3: 檢查 StatusManager 的當前 Identity
            try:
                from core.status_manager import status_manager
                if status_manager.current_identity_id and status_manager.current_identity_id != 'unknown':
                    debug_log(3, f"[STT] StatusManager 有當前 Identity: {status_manager.current_identity_id}")
                    return True
            except Exception:
                pass
            
            return False
            
        except Exception as e:
            error_log(f"[STT] 檢查 declared_identity 失敗: {e}")
            return False
    
    def _reload_from_user_settings(self, key_path: str, value: Any) -> bool:
        """
        從 user_settings.yaml 重載設定
        
        Args:
            key_path: 設定路徑
            value: 新值
            
        Returns:
            是否成功
        """
        try:
            info_log(f"[STT] 🔄 重載使用者設定: {key_path} = {value}")
            
            if key_path == "interaction.speech_input.enabled":
                # STT 模組開關（需要重啟音頻流）
                if not value and self._listening_active:
                    info_log("[STT] STT 已禁用，停止監聽")
                    self.should_stop_listening = True
                    if self.audio_stream:
                        self.audio_stream.stop_stream()
                        self.audio_stream.close()
                        self.audio_stream = None
                elif value and not self._listening_active:
                    info_log("[STT] STT 已啟用，準備開始監聽")
                    # 實際啟動由外部控制
                    
            elif key_path == "interaction.speech_input.microphone_device_index":
                # 麥克風裝置索引（需要重新初始化音頻流）
                old_index = self.device_index
                self.device_index = int(value) if value is not None else None
                info_log(f"[STT] 麥克風裝置索引已更新: {old_index} → {self.device_index}")
                
                # 如果正在監聽，需要重啟音頻流
                if self._listening_active and self.audio_stream:
                    info_log("[STT] 重新初始化音頻流以套用新麥克風")
                    self.audio_stream.stop_stream()
                    self.audio_stream.close()
                    self.audio_stream = None
                    # 會在下次錄音時自動重新初始化
                    
            elif key_path == "interaction.speech_input.vad_sensitivity":
                # VAD 靈敏度（即時生效）
                old_sensitivity = self.vad_sensitivity
                self.vad_sensitivity = float(value)
                if hasattr(self, 'vad_module') and self.vad_module:
                    self.vad_module.sensitivity = float(value)
                    # 重新計算能量閾值
                    self.vad_module.energy_threshold = 0.0005 * (2.0 - float(value))
                    info_log(f"[STT] VAD 靈敏度已更新: {old_sensitivity:.2f} → {value:.2f}")
                    
            elif key_path == "interaction.speech_input.min_speech_duration":
                # 最小語音持續時間（即時生效）
                old_duration = self.min_speech_duration
                self.min_speech_duration = float(value)
                if hasattr(self, 'vad_module') and self.vad_module:
                    self.vad_module.speech_duration_threshold = float(value)
                    info_log(f"[STT] 最小語音持續時間已更新: {old_duration:.2f}s → {value:.2f}s")
                
            elif key_path == "interaction.speech_input.enable_continuous_mode":
                # 連續模式開關（動態切換監聽模式）
                new_mode = ActivationMode.CONTINUOUS if value else ActivationMode.MANUAL
                old_mode = self._current_mode
                self._current_mode = new_mode
                info_log(f"[STT] 啟動模式已切換: {old_mode.value} → {new_mode.value}")
                
                # 如果正在監聽，發出警告（可能需要重啟監聽）
                if self._listening_active:
                    info_log(f"[STT] ⚠️ 模式切換生效，建議重啟監聽以應用新模式")
                
            elif key_path == "interaction.speech_input.wake_word_confidence":
                # 喚醒詞信心度閾值（即時生效）
                old_confidence = self.wake_word_confidence
                self.wake_word_confidence = float(value)
                info_log(f"[STT] 喚醒詞信心度閾值已更新: {old_confidence:.2f} → {value:.2f}")
                # 此參數儲存於實例變數，供 NLP 喚醒詞檢測使用
                
            else:
                debug_log(2, f"[STT] 未處理的設定路徑: {key_path}")
                return False
            
            return True
            
        except Exception as e:
            error_log(f"[STT] 重載使用者設定失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
            return False
    
    def get_performance_window(self) -> dict:
        """獲取效能數據窗口（包含 STT 特定指標）"""
        window = super().get_performance_window()
        window['total_audio_duration'] = self.total_audio_duration
        window['total_recognitions'] = self.total_recognitions
        window['vad_triggers'] = self.vad_triggers
        window['avg_audio_duration'] = (
            self.total_audio_duration / self.total_recognitions
            if self.total_recognitions > 0 else 0.0
        )
        return window
