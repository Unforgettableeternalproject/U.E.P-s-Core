"""
echo_stt 核心 STT 引擎

從 U.E.P Core 的 stt_module.py 遷出並解耦：
- 移除 BaseModule 繼承 → 獨立 class
- 移除 config_loader / user_settings_manager → 本地 config
- 移除 event_bus → callback pattern
- 移除 session_manager / system_loop / status_manager
- 修復 Bug #2: duration=0.0 被 falsy 判定誤吃
"""

import re
import time
import logging
import tempfile
import os
import warnings
from typing import Optional, Dict, Any, Callable, cast

import numpy as np

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", message=".*forced_decoder_ids.*")
warnings.filterwarnings("ignore", message=".*past_key_values.*")

import torch
import pyaudio
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

from echo_stt.core.config import load_config, get
from echo_stt.core.schemas import (
    STTInput, STTOutput, STTModuleData, ActivationMode, SpeakerInfo,
)
from echo_stt.vad.energy_vad import VoiceActivityDetection
from echo_stt.speaker.identification import SpeakerIdentification
from echo_stt.keyword.detector import SmartKeywordDetector

logger = logging.getLogger(__name__)


def correct_stt(text: str) -> str:
    """STT 結果修正 — 主要針對英文識別優化"""
    corrections = {
        "you ep": "UEP", "youpee": "UEP", "uvp": "UEP",
        "u e p": "UEP", "u.e.p": "UEP", "uep": "UEP",
        "you e p": "UEP", "yu ep": "UEP", "yup": "UEP",
        "cant": "can't", "wont": "won't", "dont": "don't", "isnt": "isn't",
        "hey you ep": "hey UEP", "hello you ep": "hello UEP", "hi you ep": "hi UEP",
    }
    result = text.lower()
    for wrong, correct in corrections.items():
        result = result.replace(wrong, correct)
    if "uep" in result.lower():
        result = re.sub(r'\buep\b', 'UEP', result, flags=re.IGNORECASE)
    return result


class STTEngine:
    """獨立 STT 引擎

    不再繼承 BaseModule，所有依賴透過建構函式注入或從 config 讀取。
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        result_callback: Optional[Callable] = None,
        on_interaction_start: Optional[Callable[[Dict[str, Any]], None]] = None,
        context_manager: Optional[Any] = None,
    ):
        # 載入配置
        self.config = config if config is not None else load_config()

        # Callbacks
        self.result_callback = result_callback
        self.on_interaction_start = on_interaction_start

        # 基本配置
        self.device_index = get("audio.device_index", None, config=self.config)
        self.phrase_time_limit = get("audio.phrase_time_limit", 5, config=self.config)
        self.sample_rate: int = get("audio.sample_rate", 16000, config=self.config)

        # Whisper 模型配置
        self.whisper_model_id: str = get("whisper.model_id", "openai/whisper-base", config=self.config)
        self.whisper_local_path: str = get("whisper.local_path", "models/stt/whisper/whisper-base", config=self.config)
        self.use_local_model: bool = get("whisper.use_local_model", True, config=self.config)

        # 設備配置
        device_pref = get("whisper.device", "auto", config=self.config)
        if device_pref == "auto":
            self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        else:
            self.device = device_pref

        dtype_pref = get("whisper.torch_dtype", "auto", config=self.config)
        if dtype_pref == "auto":
            self.torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        elif dtype_pref == "float16":
            self.torch_dtype = torch.float16
        else:
            self.torch_dtype = torch.float32

        # 模型組件
        self.model = None
        self.processor = None
        self.pipe = None

        # 辨識設定
        self._recognition_task: str = get("recognition.task", "transcribe", config=self.config)

        # 獨立子模組
        vad_sensitivity = get("vad.sensitivity", 0.7, config=self.config)
        vad_min_speech = get("vad.min_speech_duration", 0.3, config=self.config)
        self.vad_module = VoiceActivityDetection(self.sample_rate, vad_sensitivity, vad_min_speech)

        speaker_config = self.config.get("speaker", {})
        self.speaker_module = SpeakerIdentification(
            config={"speaker_recognition": speaker_config} if speaker_config else None,
            context_manager=context_manager,
        )

        keyword_config = self.config.get("keyword", {})
        self.keyword_detector = SmartKeywordDetector(
            config={"smart_activation": keyword_config} if keyword_config else None,
        )

        # 監聽控制
        self.should_stop_listening = False

        # PyAudio 配置
        self.pyaudio_instance = None
        self.audio_stream = None
        self.pa_config = {
            "format": pyaudio.paInt16,
            "channels": get("audio.channels", 1, config=self.config),
            "rate": self.sample_rate,
            "frames_per_buffer": get("audio.frames_per_buffer", 1024, config=self.config),
        }

        # 啟動模式
        default_mode = get("recognition.default_mode", "manual", config=self.config)
        self._current_mode = ActivationMode.CONTINUOUS if default_mode == "continuous" else ActivationMode.MANUAL
        self._listening_active = False

        self.is_initialized = False

        # 效能指標
        self._metrics: Dict[str, Any] = {}
        self.total_audio_duration = 0.0
        self.total_recognitions = 0
        self.vad_triggers = 0

        logger.info("[STT] echo_stt 引擎初始化完成")

    # ──────────────────────────────────────────────
    #  效能指標（替代 BaseModule 的 update_custom_metric）
    # ──────────────────────────────────────────────

    def update_custom_metric(self, key: str, value: Any) -> None:
        self._metrics[key] = value

    def get_performance_window(self) -> dict:
        return {
            **self._metrics,
            'total_audio_duration': self.total_audio_duration,
            'total_recognitions': self.total_recognitions,
            'vad_triggers': self.vad_triggers,
            'avg_audio_duration': (
                self.total_audio_duration / self.total_recognitions
                if self.total_recognitions > 0 else 0.0
            ),
        }

    # ──────────────────────────────────────────────
    #  初始化
    # ──────────────────────────────────────────────

    def initialize(self) -> bool:
        logger.debug("[STT] 初始化中...")

        try:
            model_path: str
            if self.use_local_model and os.path.exists(self.whisper_local_path):
                model_path = self.whisper_local_path
                logger.info("[STT] 使用本地 Transformers 模型: %s", model_path)
            else:
                model_path = self.whisper_model_id
                logger.info("[STT] 使用遠端 Transformers 模型: %s", model_path)

            logger.info("[STT] 載入 Whisper 模型...")
            self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
                model_path,
                torch_dtype=self.torch_dtype,
                low_cpu_mem_usage=True,
                use_safetensors=True,
            )
            self.model.to(self.device)

            if hasattr(self.model.config, 'forced_decoder_ids'):
                self.model.config.forced_decoder_ids = None
                logger.debug("[STT] 已清除模型的 forced_decoder_ids 設定")

            logger.info("[STT] 載入處理器...")
            processor_path = self.whisper_local_path if (self.use_local_model and os.path.exists(self.whisper_local_path)) else self.whisper_model_id
            self.processor = AutoProcessor.from_pretrained(processor_path)

            logger.info("[STT] 創建語音識別 pipeline...")
            self.pipe = pipeline(
                "automatic-speech-recognition",
                model=self.model,
                tokenizer=self.processor.tokenizer,
                feature_extractor=self.processor.feature_extractor,
                torch_dtype=self.torch_dtype,
                device=self.device,
            )

            logger.info("[STT] Transformers Whisper 模型載入成功 (設備: %s)", self.device)

            # 初始化 PyAudio
            self.pyaudio_instance = pyaudio.PyAudio()
            logger.info("[STT] PyAudio 初始化成功")

            # 初始化子模組
            if not self.vad_module.initialize():
                logger.error("[STT] VAD 模組初始化失敗，但不影響基本 STT 功能")

            if not self.speaker_module.initialize():
                logger.info("[STT] 說話人識別模組使用 fallback 模式")
            else:
                logger.info("[STT] 說話人識別模組初始化成功")

            # 列出音頻設備
            if self.pyaudio_instance:
                for i in range(self.pyaudio_instance.get_device_count()):
                    device_info = self.pyaudio_instance.get_device_info_by_index(i)
                    max_input = device_info.get('maxInputChannels', 0)
                    if isinstance(max_input, int) and max_input > 0:
                        device_name = device_info.get('name', 'Unknown')
                        logger.debug("  設備 %d: %s", i, device_name)

            self.is_initialized = True
            return True

        except Exception as e:
            logger.error("[STT] 初始化失敗: %s", e)
            return False

    # ──────────────────────────────────────────────
    #  請求處理
    # ──────────────────────────────────────────────

    def handle(self, data: Optional[dict] = None) -> dict:
        """處理 STT 請求"""
        if data is None:
            data = {}

        try:
            # 文字模式特殊處理
            if data.get("mode") == "text" and "text" in data:
                return self._handle_text_input(data["text"])

            validated = STTInput(**data)
            logger.debug("[STT] 處理請求: %s", validated.mode)

            start_time = time.time()

            if validated.mode == ActivationMode.MANUAL:
                result = self._manual_recognition(validated)
            elif validated.mode == ActivationMode.CONTINUOUS:
                result = self._continuous_recognition(validated)
            else:
                return STTOutput(
                    text="", confidence=0.0,
                    error="不支持的模式",
                    activation_reason="不支持的模式",
                ).model_dump()

            processing_time = time.time() - start_time
            result["processing_time"] = processing_time

            stt_output = STTOutput(**result)

            is_listening_completed = stt_output.activation_reason == "continuous_listening_completed"
            if not stt_output.text or not stt_output.text.strip():
                if is_listening_completed:
                    stt_output.error = None
                else:
                    logger.info("[STT] 🔇 未識別到有效語音內容")
                    stt_output.error = "未識別到有效語音內容"
            else:
                stt_output.error = None

            self.total_recognitions += 1
            if stt_output.text and stt_output.text.strip():
                self.update_custom_metric('recognition_confidence', stt_output.confidence)
                self.update_custom_metric('text_length', len(stt_output.text))

            # Bug #2 修復: 使用 is not None 而非 falsy 判定
            if validated.mode == ActivationMode.MANUAL:
                audio_duration = (
                    validated.duration
                    if validated.duration is not None
                    else self.phrase_time_limit
                )
                self.total_audio_duration += audio_duration
                self.update_custom_metric('audio_duration', audio_duration)

            return stt_output.model_dump()

        except Exception as e:
            logger.error("[STT] 處理失敗: %s", e)
            return STTOutput(text="", confidence=0.0, error=f"處理失敗: {e}").model_dump()

    def stop_listening(self):
        self.should_stop_listening = True
        logger.debug("[STT] 設置停止監聽標誌")

    def resume_listening(self):
        self.should_stop_listening = False
        logger.debug("[STT] 清除停止監聽標誌")

    def handle_text_input(self, text: str) -> dict:
        """處理文字輸入 — 繞過語音識別和說話人辨識"""
        return self.handle({"mode": "text", "text": text})

    def _handle_text_input(self, text: str) -> dict:
        """處理文字輸入的實際實現"""
        try:
            if not text or text.isspace():
                logger.debug("[STT] 文字輸入為空，忽略")
                return STTOutput(
                    text="", confidence=0.0, speaker_info=None,
                    activation_reason="text_input_empty",
                    error="文字輸入為空",
                ).model_dump()

            # 移除 cycle tracking 等待邏輯（獨立系統不需要）
            logger.info("[STT] 文字輸入模式: '%s'", text)

            # 觸發 interaction callback
            if self.on_interaction_start:
                self.on_interaction_start({"source": "stt_text_input", "input_mode": "text"})

            output = STTOutput(
                text=text.strip(),
                confidence=1.0,
                speaker_info=None,
                activation_reason="text_input",
                error=None,
            )

            unified_data = output.to_unified_format()
            unified_data.metadata["input_mode"] = "text"
            unified_data.metadata["bypass_speaker_id"] = True

            if self.result_callback:
                try:
                    self.result_callback(unified_data)
                    logger.info("[STT] 文字輸入已發送: '%s'", text)
                except Exception as e:
                    logger.error("[STT] 發送文字輸入結果失敗: %s", e)

            return output.model_dump()

        except Exception as e:
            logger.error("[STT] 處理文字輸入失敗: %s", e)
            return STTOutput(
                text="", confidence=0.0, speaker_info=None,
                activation_reason="text_input_error",
                error=f"處理文字輸入失敗: {e}",
            ).model_dump()

    # ──────────────────────────────────────────────
    #  手動辨識
    # ──────────────────────────────────────────────

    def _manual_recognition(self, input_data: STTInput) -> dict:
        """手動語音識別"""
        try:
            logger.info("[STT] 開始錄音...")

            # Bug #2 修復: 使用 is not None
            duration = input_data.duration if input_data.duration is not None else self.phrase_time_limit
            audio_data = self._record_audio(duration)

            if audio_data is None or len(audio_data) == 0:
                return STTOutput(
                    text="", confidence=0.0,
                    error="錄音失敗或音頻為空",
                    activation_reason="錄音失敗",
                ).model_dump()

            logger.info("[STT] 使用 Transformers Whisper 進行語音識別...")

            audio_float = audio_data.astype(np.float32) / 32768.0

            generate_kwargs = {
                "max_new_tokens": get("recognition.max_new_tokens", 128, config=self.config),
                "num_beams": 1,
                "condition_on_prev_tokens": False,
                "compression_ratio_threshold": get("recognition.compression_ratio_threshold", 1.35, config=self.config),
                "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                "logprob_threshold": -1.0,
                "no_speech_threshold": get("recognition.no_speech_threshold", 0.4, config=self.config),
                "return_timestamps": True,
                "task": self._recognition_task,
            }

            if not self.vad_module.has_sufficient_speech(audio_data):
                logger.info("[STT] VAD 檢測：未檢測到足夠語音內容，但仍嘗試識別")

            if self.pipe is None:
                logger.error("[STT] Pipeline 未初始化")
                return STTOutput(text="", confidence=0.0, error="Pipeline 未初始化").model_dump()

            result = self.pipe(audio_float, generate_kwargs=generate_kwargs)
            logger.debug("[STT] Pipeline 原始結果: %s", result)

            result_dict = cast(Dict[str, Any], result)
            text = str(result_dict.get("text", "")).strip()
            text = correct_stt(text)
            confidence = self._calculate_transformers_confidence(result_dict)

            if not text or text.isspace():
                logger.info("[STT] 🔇 未識別到有效語音內容")
                return STTOutput(
                    text="", confidence=0.0, speaker_info=None,
                    activation_reason="manual",
                    error="未識別到有效語音內容",
                ).model_dump()

            speaker_info = None
            if input_data.enable_speaker_id:
                speaker_info = self.speaker_module.identify_speaker(audio_data)

            logger.info("[STT] 識別結果: '%s' (信心度: %.2f)", text, confidence)

            return STTOutput(
                text=text, confidence=confidence,
                speaker_info=speaker_info,
                activation_reason="manual", error=None,
            ).model_dump()

        except Exception as e:
            logger.error("[STT] 識別失敗: %s", e)
            return STTOutput(
                text="", confidence=0.0,
                error=f"識別失敗: {e}",
                activation_reason="識別失敗：未知錯誤",
            ).model_dump()

    # ──────────────────────────────────────────────
    #  持續監聽
    # ──────────────────────────────────────────────

    def _continuous_recognition(self, input_data: STTInput) -> dict:
        """持續背景監聽"""
        try:
            logger.info("[STT] 開始持續背景監聽模式...")

            # Bug #2 修復: is not None
            duration = input_data.duration if input_data.duration is not None else get("continuous.total_duration", 30.0, config=self.config)
            start_time = time.time()

            chunk_duration = get("continuous.chunk_duration", 2.0, config=self.config)
            silence_threshold = get("continuous.silence_threshold", 1.5, config=self.config)
            max_speech_duration = get("continuous.max_speech_duration", 30.0, config=self.config)

            logger.info(
                "[STT] 持續監聽配置: 總時長=%.1fs, 片段=%.1fs, 靜音閾值=%.1fs, 最大語音=%.1fs",
                duration, chunk_duration, silence_threshold, max_speech_duration,
            )

            audio_buffer: list = []
            last_speech_time: Optional[float] = None
            speech_start_time: Optional[float] = None

            while time.time() - start_time < duration and not self.should_stop_listening:
                current_time = time.time()
                audio_chunk = self._record_audio(chunk_duration)

                if audio_chunk is None or len(audio_chunk) == 0:
                    logger.debug("[STT] 錄音失敗或音頻為空，繼續監聽")
                    continue

                has_speech = self.vad_module.has_sufficient_speech(audio_chunk, min_duration=0.05)

                if has_speech:
                    if speech_start_time is None:
                        speech_start_time = current_time
                        logger.info("[STT] 🎤 語音開始...")

                    audio_buffer.append(audio_chunk)
                    last_speech_time = current_time

                    if current_time - speech_start_time > max_speech_duration:
                        logger.info("[STT] 語音片段達到最大長度 (%.1fs)，強制處理", max_speech_duration)
                        self._process_audio_buffer(audio_buffer, input_data.enable_speaker_id)
                        audio_buffer = []
                        last_speech_time = None
                        speech_start_time = None
                else:
                    if audio_buffer:
                        silence_duration = current_time - last_speech_time if last_speech_time else 0
                        logger.debug("[STT] 靜音持續: %.2fs / %.2fs", silence_duration, silence_threshold)

                        if silence_duration >= silence_threshold:
                            speech_duration = (last_speech_time - speech_start_time) if (last_speech_time and speech_start_time) else 0
                            logger.info("[STT] 📝 語音結束 (時長: %.2fs)，開始處理...", speech_duration)
                            self._process_audio_buffer(audio_buffer, input_data.enable_speaker_id)
                            audio_buffer = []
                            last_speech_time = None
                            speech_start_time = None
                    else:
                        logger.debug("[STT] 靜音狀態，等待語音...")

                time.sleep(0.05)

            if audio_buffer:
                logger.info("[STT] 監聽結束，處理剩餘音頻緩衝區...")
                self._process_audio_buffer(audio_buffer, input_data.enable_speaker_id)

            logger.info("[STT] 持續監聽模式正常結束")
            return STTOutput(
                text="", confidence=1.0, speaker_info=None,
                activation_reason="continuous_listening_completed", error=None,
            ).model_dump()

        except Exception as e:
            logger.error("[STT] 持續監聽失敗: %s", e)
            return STTOutput(
                text="", confidence=0.0,
                error=f"持續監聯失敗: {e}",
                activation_reason="continuous_listening_failed",
            ).model_dump()

    def _process_audio_buffer(self, audio_buffer: list, enable_speaker_id: bool) -> None:
        """處理音頻緩衝區"""
        try:
            if not audio_buffer:
                logger.debug("[STT] 音頻緩衝區為空，跳過處理")
                return

            logger.info("[STT] 合併 %d 個音頻片段...", len(audio_buffer))
            merged_audio = np.concatenate(audio_buffer)
            total_duration = len(merged_audio) / self.sample_rate
            logger.info("[STT] 合併後音頻長度: %.2f 秒", total_duration)

            # 觸發 interaction callback
            if self.on_interaction_start:
                self.on_interaction_start({
                    "module": "stt",
                    "input_type": "voice",
                    "audio_duration": total_duration,
                    "num_chunks": len(audio_buffer),
                })

            # 語音識別
            audio_float = merged_audio.astype(np.float32) / 32768.0

            recognition_kwargs = {
                "max_new_tokens": get("recognition.max_new_tokens", 128, config=self.config),
                "num_beams": 1,
                "condition_on_prev_tokens": False,
                "compression_ratio_threshold": get("recognition.compression_ratio_threshold", 1.35, config=self.config),
                "temperature": 0.0,
                "logprob_threshold": -1.0,
                "no_speech_threshold": get("recognition.no_speech_threshold", 0.4, config=self.config),
                "return_timestamps": True,
                "task": self._recognition_task,
            }

            if self.pipe is None:
                logger.error("[STT] Pipeline 未初始化，無法識別")
                return

            result = self.pipe(audio_float, generate_kwargs=recognition_kwargs)  # type: ignore
            result_dict = cast(Dict[str, Any], result)
            text = str(result_dict.get("text", "")).strip()
            text = correct_stt(text)
            confidence = self._calculate_transformers_confidence(result_dict)

            if not text or text.isspace():
                logger.debug("[STT] 未識別到有效語音內容")
                return

            logger.info("[STT] ✅ 識別結果: '%s' (信心度: %.2f)", text, confidence)

            speaker_info = None
            if enable_speaker_id:
                speaker_info = self.speaker_module.identify_speaker(merged_audio)
                if speaker_info:
                    logger.debug("[STT] 識別語者: %s (信心度: %.2f)", speaker_info.speaker_id, speaker_info.confidence)

            output = STTOutput(
                text=text, confidence=confidence,
                speaker_info=speaker_info,
                activation_reason="continuous_listening", error=None,
            )

            unified_data = output.to_unified_format()
            unified_data.metadata["audio_duration"] = total_duration
            unified_data.metadata["num_chunks_merged"] = len(audio_buffer)

            if self.result_callback:
                try:
                    self.result_callback(unified_data)
                    speaker_id = unified_data.speaker_info.get('speaker_id', 'unknown') if unified_data.speaker_info else 'unknown'
                    logger.info("[STT] 📤 識別結果已發送: '%s' (語者: %s)", text, speaker_id)
                except Exception as e:
                    logger.error("[STT] 發送識別結果失敗: %s", e)

            # 終端輸出
            speaker_id = speaker_info.speaker_id if speaker_info else "unknown"
            print(f"\n🎤 [{speaker_id}] {text}")
            if confidence < 0.7:
                print(f"   ⚠️  低置信度: {confidence:.2f}")

        except Exception as e:
            logger.error("[STT] 處理音頻緩衝區失敗: %s", e)

    # ──────────────────────────────────────────────
    #  音訊錄製
    # ──────────────────────────────────────────────

    def _record_audio(self, duration: float) -> np.ndarray:
        """使用 PyAudio 錄製音頻"""
        try:
            stream_params: Dict[str, Any] = {
                "format": self.pa_config["format"],
                "channels": self.pa_config["channels"],
                "rate": self.pa_config["rate"],
                "input": True,
                "frames_per_buffer": self.pa_config["frames_per_buffer"],
            }

            if self.device_index is not None:
                stream_params["input_device_index"] = self.device_index

            if self.pyaudio_instance is None:
                logger.error("[STT] PyAudio 未初始化")
                return np.array([])

            stream = self.pyaudio_instance.open(**stream_params)

            frames: list = []
            frames_to_record = int(self.sample_rate * duration / self.pa_config["frames_per_buffer"])

            logger.info("[STT] 開始錄音 %.1f 秒...", duration)
            for _ in range(frames_to_record):
                data = stream.read(self.pa_config["frames_per_buffer"])
                frames.append(data)

            stream.stop_stream()
            stream.close()

            audio_data = b''.join(frames)
            audio_array = np.frombuffer(audio_data, dtype=np.int16)

            if len(audio_array) > 0 and np.max(np.abs(audio_array)) > 0:
                norm_factor = 0.9 * 32767 / np.max(np.abs(audio_array))
                audio_array = (audio_array * norm_factor).astype(np.int16)

            logger.info("[STT] 錄音完成，長度: %.2f 秒", len(audio_array) / self.sample_rate)
            return audio_array

        except Exception as e:
            logger.error("[STT] 錄音失敗: %s", e)
            return np.array([])

    # ──────────────────────────────────────────────
    #  輔助方法
    # ──────────────────────────────────────────────

    def _calculate_transformers_confidence(self, result: dict) -> float:
        """計算 Transformers Whisper 結果的信心度"""
        try:
            text = result.get("text", "").strip()
            if not text:
                return 0.0

            base_confidence = 0.8
            text_length = len(text.split())
            if 1 <= text_length <= 30:
                length_bonus = 0.15
            elif text_length > 30:
                length_bonus = 0.1
            else:
                length_bonus = 0.0

            common_words = ["UEP", "help", "please", "can", "you", "the", "a", "an", "is", "are"]
            common_word_count = sum(1 for word in text.lower().split() if word in common_words)
            common_word_bonus = min(common_word_count * 0.03, 0.1)

            timestamp_bonus = 0.05 if ("chunks" in result and result["chunks"]) else 0.0

            confidence = min(base_confidence + length_bonus + common_word_bonus + timestamp_bonus, 1.0)
            return confidence

        except Exception as e:
            logger.debug("[STT] 計算信心度失敗: %s", e)
            return 0.8

    def shutdown(self):
        """關閉引擎"""
        if self.pyaudio_instance:
            self.pyaudio_instance.terminate()

        if self.model is not None:
            del self.model
        if self.pipe is not None:
            del self.pipe
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        if hasattr(self, 'vad_module'):
            self.vad_module.shutdown()
        if hasattr(self, 'speaker_module'):
            self.speaker_module.shutdown()

        logger.info("[STT] 引擎已關閉")
