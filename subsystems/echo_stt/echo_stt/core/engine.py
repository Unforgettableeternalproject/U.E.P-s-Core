"""
echo_stt 核心 STT 引擎

雙後端支援：
- faster-whisper (CTranslate2) — 預設，支援 num_workers 並行
- transformers — fallback

Bug #2 修復：duration=0.0 使用 is not None 判定
Audit 2026-06-15：修復 15 項 deep audit 問題
"""

import re
import time
import logging
import os
import warnings
from typing import Optional, Dict, Any, Callable, List, cast

import numpy as np

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", message=".*forced_decoder_ids.*")
warnings.filterwarnings("ignore", message=".*past_key_values.*")

import torch
import pyaudio

from echo_stt.core.config import load_config, get
from echo_stt.pipeline.segment_filter import (
    SegmentFilterConfig,
    clean_whisper_garbage,
    english_guard_reason,
    filter_segments,
    is_hallucination,
    normalize_text,
)
from echo_stt.core.schemas import (
    STTInput, STTOutput, STTModuleData, ActivationMode, SpeakerInfo,
)
from echo_stt.vad.energy_vad import VoiceActivityDetection
from echo_stt.speaker.identification import SpeakerIdentification
from echo_stt.keyword.detector import SmartKeywordDetector

logger = logging.getLogger(__name__)

# CTranslate2 CPU 支援的 compute_type — 動態查詢優先
try:
    import ctranslate2 as _ct2
    _CT2_CPU_TYPES = set(_ct2.get_supported_compute_types("cpu"))
except Exception:
    # int8_float16 在多數 CPU 不被支援，不列入靜態 fallback
    _CT2_CPU_TYPES = {"float32", "int16", "int8", "int8_float32"}
_CT2_CPU_DEFAULT = "int8"


def correct_stt(text: str) -> str:
    """STT 結果修正"""
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
    """獨立 STT 引擎 — 支援 faster-whisper / transformers 雙後端"""

    def __init__(
        self,
        config: Optional[dict] = None,
        result_callback: Optional[Callable] = None,
        on_interaction_start: Optional[Callable[[Dict[str, Any]], None]] = None,
        context_manager: Optional[Any] = None,
    ):
        self.config = config if config is not None else load_config()

        # Callbacks
        self.result_callback = result_callback
        self.on_interaction_start = on_interaction_start

        # 基本配置
        self.device_index = get("audio.device_index", None, config=self.config)
        self.phrase_time_limit = get("audio.phrase_time_limit", 5, config=self.config)
        self.sample_rate: int = get("audio.sample_rate", 16000, config=self.config)

        # ── 後端選擇 ──
        self._backend_type: str = get("whisper.backend", "faster-whisper", config=self.config)

        # faster-whisper 設定
        self._fw_model_size: str = get("whisper.model_size", "base", config=self.config)
        self._fw_compute_type: str = get("whisper.compute_type", "int8_float16", config=self.config)
        self._fw_num_workers: int = get("whisper.num_workers", 1, config=self.config)
        self._fw_download_root: Optional[str] = get("whisper.download_root", None, config=self.config)

        # 逐段過濾與解碼參數（預設值 = Echo-Stream／bot 的 DEFAULT_STT_SETTINGS）
        self._seg_filter = SegmentFilterConfig.from_mapping(
            get("segment_filter", {}, config=self.config)
        )
        self._hotwords_max: int = int(get("recognition.hotwords_max", 30, config=self.config))
        self._hotwords: tuple = ()
        self.set_hotwords(get("recognition.hotwords", [], config=self.config))
        self._initial_prompt: str = get("recognition.initial_prompt", "", config=self.config) or ""
        # 被過濾的累計計數（原因 → 段數），給診斷與測試用
        self.filter_stats: Dict[str, int] = {}

        # transformers fallback 設定
        self.whisper_model_id: str = get("whisper.model_id", "openai/whisper-base", config=self.config)
        self.whisper_local_path: str = get("whisper.local_path", "models/stt/whisper/whisper-base", config=self.config)
        self.use_local_model: bool = get("whisper.use_local_model", True, config=self.config)

        # 設備
        device_pref = get("whisper.device", "auto", config=self.config)
        if device_pref == "auto":
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = device_pref

        # Audit fix: compute_type 正規化 — CPU 不支援 float16
        if self.device == "cpu" and self._fw_compute_type not in _CT2_CPU_TYPES:
            logger.warning(
                "[STT] compute_type '%s' 不被 CPU 支援，自動降級為 '%s'",
                self._fw_compute_type, _CT2_CPU_DEFAULT,
            )
            self._fw_compute_type = _CT2_CPU_DEFAULT

        dtype_pref = get("whisper.torch_dtype", "auto", config=self.config)
        if dtype_pref == "auto":
            self.torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        elif dtype_pref == "float16":
            self.torch_dtype = torch.float16
        else:
            self.torch_dtype = torch.float32

        # 模型組件
        self._fw_model = None        # faster_whisper.WhisperModel
        self.model = None             # transformers model (fallback)
        self.processor = None
        self.pipe = None

        # 辨識設定
        self._recognition_task: str = get("recognition.task", "transcribe", config=self.config)
        self._language: Optional[str] = get("recognition.language", None, config=self.config)

        # 子模組
        vad_sensitivity = get("vad.sensitivity", 0.7, config=self.config)
        vad_min_speech = get("vad.min_speech_duration", 0.3, config=self.config)
        self.vad_module = VoiceActivityDetection(self.sample_rate, vad_sensitivity, vad_min_speech)

        # Audit fix: 尊重 speaker.enabled — disabled 時不建立模組
        self._speaker_enabled: bool = get("speaker.enabled", False, config=self.config)
        speaker_config = self.config.get("speaker", {})
        if self._speaker_enabled:
            self.speaker_module: Optional[SpeakerIdentification] = SpeakerIdentification(
                config={"speaker_recognition": speaker_config} if speaker_config else None,
                context_manager=context_manager,
            )
        else:
            self.speaker_module = None
            logger.info("[STT] 語者識別已停用 (speaker.enabled=false)")

        # Audit fix: 尊重 keyword.enabled
        self._keyword_enabled: bool = get("keyword.enabled", True, config=self.config)
        keyword_config = self.config.get("keyword", {})
        if self._keyword_enabled:
            self.keyword_detector: Optional[SmartKeywordDetector] = SmartKeywordDetector(
                config={"smart_activation": keyword_config} if keyword_config else None,
            )
        else:
            self.keyword_detector = None
            logger.info("[STT] 智能喚醒已停用 (keyword.enabled=false)")

        # 監聽控制
        self.should_stop_listening = False

        # PyAudio
        self.pyaudio_instance = None
        self.audio_stream = None
        self.pa_config = {
            "format": pyaudio.paInt16,
            "channels": get("audio.channels", 1, config=self.config),
            "rate": self.sample_rate,
            "frames_per_buffer": get("audio.frames_per_buffer", 1024, config=self.config),
        }

        self.is_initialized = False

        # 效能指標
        self._metrics: Dict[str, Any] = {}
        self.total_audio_duration = 0.0
        self.total_recognitions = 0
        self.vad_triggers = 0

        logger.info("[STT] echo_stt 引擎初始化完成 (backend: %s)", self._backend_type)

    # ──────────────────────────────────────────────
    #  效能指標
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
        logger.debug("[STT] 初始化中... (backend: %s)", self._backend_type)

        try:
            # Audit fix: offline 策略 — 檢測 HF_HUB_OFFLINE 或設定
            if os.environ.get("HF_HUB_OFFLINE") != "1":
                # 檢查是否有本地快取可用，若有則自動設為 offline 嘗試
                self._try_offline = get("whisper.prefer_offline", False, config=self.config)
            else:
                self._try_offline = True
                logger.info("[STT] 偵測到 HF_HUB_OFFLINE=1，使用離線模式")

            if self._backend_type == "faster-whisper":
                success = self._init_faster_whisper()
            else:
                success = self._init_transformers()

            if not success:
                return False

            # PyAudio
            self.pyaudio_instance = pyaudio.PyAudio()
            logger.info("[STT] PyAudio 初始化成功")

            # 子模組
            if not self.vad_module.initialize():
                logger.error("[STT] VAD 模組初始化失敗")

            # Audit fix: speaker.enabled=false 時跳過初始化
            if self.speaker_module is not None:
                if not self.speaker_module.initialize():
                    logger.info("[STT] 說話人識別模組使用 fallback 模式")
                else:
                    logger.info("[STT] 說話人識別模組初始化成功")

            # 列出音頻設備
            if self.pyaudio_instance:
                for i in range(self.pyaudio_instance.get_device_count()):
                    info = self.pyaudio_instance.get_device_info_by_index(i)
                    max_input = info.get('maxInputChannels', 0)
                    if isinstance(max_input, int) and max_input > 0:
                        logger.debug("  設備 %d: %s", i, info.get('name', 'Unknown'))

            self.is_initialized = True
            return True

        except Exception as e:
            logger.error("[STT] 初始化失敗: %s", e)
            return False

    def _init_faster_whisper(self) -> bool:
        """初始化 faster-whisper 後端"""
        try:
            from faster_whisper import WhisperModel

            logger.info(
                "[STT] 載入 faster-whisper: model=%s, compute=%s, device=%s, workers=%d",
                self._fw_model_size, self._fw_compute_type, self.device, self._fw_num_workers,
            )

            fw_kwargs: Dict[str, Any] = {
                "device": self.device,
                "compute_type": self._fw_compute_type,
                "num_workers": self._fw_num_workers,
                "download_root": self._fw_download_root,
            }

            # Audit fix: cache-first 策略 — 始終先嘗試本地快取，避免不必要的網路請求
            try:
                self._fw_model = WhisperModel(
                    self._fw_model_size, local_files_only=True, **fw_kwargs,
                )
                logger.info("[STT] faster-whisper 從本地快取載入成功")
            except Exception as cache_err:
                if self._try_offline:
                    # 明確離線模式 — 不 fallback 到線上
                    raise
                logger.info("[STT] 本地快取不可用 (%s)，嘗試線上下載...", cache_err)
                self._fw_model = WhisperModel(self._fw_model_size, **fw_kwargs)

            # 更新顯示用的 model ID
            self.whisper_model_id = f"faster-whisper/{self._fw_model_size} ({self._fw_compute_type})"

            logger.info("[STT] faster-whisper 載入成功 (設備: %s)", self.device)
            return True

        except Exception as e:
            logger.error("[STT] faster-whisper 初始化失敗: %s", e)
            logger.info("[STT] 嘗試 fallback 到 transformers...")
            self._backend_type = "transformers"
            return self._init_transformers()

    def _init_transformers(self) -> bool:
        """初始化 transformers 後端（fallback）"""
        try:
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

            model_path: str
            if self.use_local_model and os.path.exists(self.whisper_local_path):
                model_path = self.whisper_local_path
                logger.info("[STT] 使用本地 Transformers 模型: %s", model_path)
            else:
                model_path = self.whisper_model_id
                logger.info("[STT] 使用遠端 Transformers 模型: %s", model_path)

            self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
                model_path, torch_dtype=self.torch_dtype,
                low_cpu_mem_usage=True, use_safetensors=True,
            )
            self.model.to(self.device if ":" in self.device else f"{self.device}:0")

            if hasattr(self.model.config, 'forced_decoder_ids'):
                self.model.config.forced_decoder_ids = None

            processor_path = self.whisper_local_path if (self.use_local_model and os.path.exists(self.whisper_local_path)) else self.whisper_model_id
            self.processor = AutoProcessor.from_pretrained(processor_path)

            self.pipe = pipeline(
                "automatic-speech-recognition",
                model=self.model,
                tokenizer=self.processor.tokenizer,
                feature_extractor=self.processor.feature_extractor,
                torch_dtype=self.torch_dtype,
                device=self.device if ":" in self.device else f"{self.device}:0",
            )

            logger.info("[STT] Transformers Whisper 載入成功 (設備: %s)", self.device)
            return True

        except Exception as e:
            logger.error("[STT] Transformers 初始化失敗: %s", e)
            return False

    # ──────────────────────────────────────────────
    #  核心辨識方法（雙後端）
    # ──────────────────────────────────────────────

    def _transcribe(
        self, audio_float: np.ndarray, language: Optional[str] = None,
    ) -> Dict[str, Any]:
        """統一辨識介面 — 根據後端路由到不同實現。

        Args:
            audio_float: float32 音訊 [-1, 1]
            language: 每次請求的語言覆寫（ISO 639-1），None 時使用引擎預設

        Returns:
            {"text": str, "confidence": float, "language": str | None, "segments": list}
        """
        effective_language = language if language is not None else self._language
        if self._backend_type == "faster-whisper":
            return self._transcribe_faster_whisper(audio_float, language=effective_language)
        else:
            return self._transcribe_transformers(audio_float, language=effective_language)

    def _transcribe_faster_whisper(
        self, audio_float: np.ndarray, language: Optional[str] = None,
    ) -> Dict[str, Any]:
        """faster-whisper 辨識"""
        # Audit fix: 從 config 讀取 VAD 參數，不再硬編碼
        vad_min_speech_ms = int(get("vad.min_speech_duration", 0.3, config=self.config) * 1000)
        vad_min_silence_ms = int(get("continuous.silence_threshold", 1.5, config=self.config) * 1000)
        flt = self._seg_filter

        # 低能量門檻：整段 RMS 太低直接視為靜音，不送模型（Whisper 對靜音最愛幻聽）
        rms = float(np.sqrt(np.mean(np.square(audio_float)))) if audio_float.size else 0.0
        if rms < flt.min_rms:
            logger.debug("[STT] 丟棄 low_energy rms=%.5f<%s", rms, flt.min_rms)
            self.filter_stats["low_energy"] = self.filter_stats.get("low_energy", 0) + 1
            return {
                "text": "", "confidence": 0.0, "language": None,
                "language_probability": None, "segments": [],
            }

        vad_parameters = dict(
            min_speech_duration_ms=vad_min_speech_ms,
            min_silence_duration_ms=min(vad_min_silence_ms, 500),
            speech_pad_ms=200,
            threshold=0.5,
        )
        vad_parameters.update(get("recognition.vad_parameters", {}, config=self.config) or {})
        kwargs: Dict[str, Any] = dict(
            task=self._recognition_task,
            language=language,
            beam_size=get("recognition.beam_size", 5, config=self.config),
            best_of=get("recognition.best_of", 5, config=self.config),
            compression_ratio_threshold=get("recognition.compression_ratio_threshold", 2.4, config=self.config),
            no_speech_threshold=get("recognition.no_speech_threshold", 0.6, config=self.config),
            condition_on_previous_text=bool(
                get("recognition.condition_on_previous_text", False, config=self.config)
            ),
            repetition_penalty=float(get("recognition.repetition_penalty", 1.0, config=self.config)),
            temperature=list(get("recognition.temperature", [0.0, 0.2], config=self.config)),
            vad_filter=True,
            vad_parameters=vad_parameters,
        )
        if self._initial_prompt:
            kwargs["initial_prompt"] = self._initial_prompt
        if self._hotwords:
            kwargs["hotwords"] = ", ".join(self._hotwords)

        segments_iter, info = self._fw_model.transcribe(audio_float, **kwargs)

        # 逐段過濾：字幕署名、prompt／hotword 洩漏、低品質段、結尾套語
        kept = filter_segments(
            list(segments_iter), flt,
            prompt=self._initial_prompt, hotwords=self._hotwords, stats=self.filter_stats,
        )
        segments: List[Dict[str, Any]] = [
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "avg_logprob": seg.avg_logprob,
                "no_speech_prob": seg.no_speech_prob,
                "compression_ratio": seg.compression_ratio,
            }
            for seg in kept
        ]
        empty = {
            "text": "", "confidence": 0.0,
            "language": info.language if info else None,
            "language_probability": info.language_probability if info else None,
            "segments": [],
        }
        if not kept:
            return empty

        full_text = " ".join(seg["text"] for seg in segments).strip()
        if flt.clean_garbage:
            full_text = clean_whisper_garbage(full_text)
        if len(normalize_text(full_text)) < flt.min_text_chars:
            return empty
        if flt.filter_hallucination:
            reason = is_hallucination(full_text)
            if reason:
                logger.info("[STT] 整段命中幻覺黑名單 %s: %r", reason, full_text)
                self.filter_stats["hallucination"] = self.filter_stats.get("hallucination", 0) + 1
                return empty

        # 真實信心度：基於 avg_logprob
        mean_logprob = sum(seg["avg_logprob"] for seg in segments) / len(segments)
        # logprob 通常在 -1.0 ~ 0.0 範圍，映射到 0~1
        confidence = max(0.0, min(1.0, 1.0 + mean_logprob))
        if flt.english_guard and english_guard_reason(
            full_text, confidence, rms, flt.english_guard_max_words,
            flt.english_guard_max_conf, flt.english_guard_max_rms,
        ):
            self.filter_stats["english_guard"] = self.filter_stats.get("english_guard", 0) + 1
            return empty

        return {
            "text": full_text,
            "confidence": confidence,
            "language": info.language if info else None,
            "language_probability": info.language_probability if info else None,
            "segments": segments,
            # 保留段落中最差的解碼品質（avg_logprob 取最小、compression_ratio 取最大）
            "avg_logprob": min(seg["avg_logprob"] for seg in segments),
            "compression_ratio": max(seg["compression_ratio"] for seg in segments),
        }

    def set_hotwords(self, words) -> None:
        """替換熱詞：去空白、不分大小寫去重、超過 hotwords_max 截斷。空清單 = 不傳熱詞。"""
        merged: List[str] = []
        seen = set()
        for w in words or ():
            w = str(w).strip()
            if w and w.casefold() not in seen:
                seen.add(w.casefold())
                merged.append(w)
        limit = int(getattr(self, "_hotwords_max", 0) or 0)
        if limit and len(merged) > limit:
            logger.warning(
                "[STT] 詞表共 %d 個詞，超過 hotwords_max=%d，截斷最後 %d 個",
                len(merged), limit, len(merged) - limit,
            )
            merged = merged[:limit]
        self._hotwords = tuple(merged)

    def _transcribe_transformers(
        self, audio_float: np.ndarray, language: Optional[str] = None,
    ) -> Dict[str, Any]:
        """transformers pipeline 辨識"""
        generate_kwargs = {
            "max_new_tokens": get("recognition.max_new_tokens", 128, config=self.config),
            "num_beams": 1,
            "condition_on_prev_tokens": False,
            "compression_ratio_threshold": get("recognition.compression_ratio_threshold", 1.35, config=self.config),
            "temperature": (0.0, 0.2),
            "logprob_threshold": -1.0,
            "no_speech_threshold": get("recognition.no_speech_threshold", 0.4, config=self.config),
            "return_timestamps": True,
            "task": self._recognition_task,
        }
        if language:
            generate_kwargs["language"] = language

        if self.pipe is None:
            return {"text": "", "confidence": 0.0, "language": None, "segments": []}

        result = self.pipe(audio_float, generate_kwargs=generate_kwargs)
        result_dict = cast(Dict[str, Any], result)
        text = str(result_dict.get("text", "")).strip()
        confidence = self._calculate_transformers_confidence(result_dict)

        return {
            "text": text,
            "confidence": confidence,
            "language": None,
            "segments": result_dict.get("chunks", []),
        }

    # ──────────────────────────────────────────────
    #  請求處理
    # ──────────────────────────────────────────────

    def handle(self, data: Optional[dict] = None) -> dict:
        if data is None:
            data = {}

        try:
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
                    error="不支持的模式", activation_reason="不支持的模式",
                ).model_dump()

            processing_time = time.time() - start_time
            result["processing_time"] = processing_time

            stt_output = STTOutput(**result)

            is_listening_completed = stt_output.activation_reason == "continuous_listening_completed"
            if not stt_output.text or not stt_output.text.strip():
                if is_listening_completed:
                    # Audit fix: 保留 _continuous_recognition 設定的錯誤，不覆蓋
                    pass
                else:
                    logger.info("[STT] 🔇 未識別到有效語音內容")
                    stt_output.error = "未識別到有效語音內容"
            else:
                stt_output.error = None

            # Audit fix: 接入 keyword_detector 的 should_activate 判斷
            if stt_output.text and stt_output.text.strip() and self.keyword_detector is not None:
                activate, matches = self.keyword_detector.should_activate(stt_output.text)
                stt_output.should_activate = activate
                if matches:
                    stt_output.metadata["keyword_matches"] = [
                        {"keyword": m.keyword, "confidence": m.confidence, "type": m.match_type}
                        for m in matches
                    ]
                    stt_output.metadata["activation_reason_detail"] = \
                        self.keyword_detector.get_activation_reason(matches)

            # Audit fix: callback 在 keyword 判斷之後發送，確保 should_activate 正確
            if validated.mode == ActivationMode.MANUAL and stt_output.text and stt_output.text.strip():
                unified_data = stt_output.to_unified_format()
                unified_data.metadata["input_mode"] = "manual"
                audio_duration = (
                    validated.duration if validated.duration is not None
                    else self.phrase_time_limit
                )
                unified_data.metadata["audio_duration"] = audio_duration
                if self.result_callback:
                    try:
                        self.result_callback(unified_data)
                    except Exception as cb_err:
                        logger.error("[STT] 發送手動辨識結果失敗: %s", cb_err)

            self.total_recognitions += 1
            if stt_output.text and stt_output.text.strip():
                self.update_custom_metric('recognition_confidence', stt_output.confidence)
                self.update_custom_metric('text_length', len(stt_output.text))

            if validated.mode == ActivationMode.MANUAL:
                audio_duration = (
                    validated.duration if validated.duration is not None
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

    def resume_listening(self):
        self.should_stop_listening = False

    def handle_text_input(self, text: str) -> dict:
        return self.handle({"mode": "text", "text": text})

    def _handle_text_input(self, text: str) -> dict:
        try:
            if not text or text.isspace():
                return STTOutput(
                    text="", confidence=0.0, speaker_info=None,
                    activation_reason="text_input_empty", error="文字輸入為空",
                ).model_dump()

            logger.info("[STT] 文字輸入模式: '%s'", text)

            if self.on_interaction_start:
                self.on_interaction_start({"source": "stt_text_input", "input_mode": "text"})

            output = STTOutput(
                text=text.strip(), confidence=1.0, speaker_info=None,
                activation_reason="text_input", error=None,
            )

            # Audit fix: text 輸入也要經過 keyword 判斷
            if self.keyword_detector is not None:
                activate, matches = self.keyword_detector.should_activate(text.strip())
                output.should_activate = activate
                if matches:
                    output.metadata["keyword_matches"] = [
                        {"keyword": m.keyword, "confidence": m.confidence, "type": m.match_type}
                        for m in matches
                    ]
                    output.metadata["activation_reason_detail"] = \
                        self.keyword_detector.get_activation_reason(matches)

            unified_data = output.to_unified_format()
            unified_data.metadata["input_mode"] = "text"
            unified_data.metadata["bypass_speaker_id"] = True

            if self.result_callback:
                try:
                    self.result_callback(unified_data)
                except Exception as e:
                    logger.error("[STT] 發送文字輸入結果失敗: %s", e)

            return output.model_dump()

        except Exception as e:
            logger.error("[STT] 處理文字輸入失敗: %s", e)
            return STTOutput(
                text="", confidence=0.0, speaker_info=None,
                activation_reason="text_input_error", error=f"處理文字輸入失敗: {e}",
            ).model_dump()

    # ──────────────────────────────────────────────
    #  手動辨識
    # ──────────────────────────────────────────────

    def _manual_recognition(self, input_data: STTInput) -> dict:
        try:
            logger.info("[STT] 開始錄音...")
            duration = input_data.duration if input_data.duration is not None else self.phrase_time_limit
            audio_data = self._record_audio(duration)

            if audio_data is None or len(audio_data) == 0:
                return STTOutput(
                    text="", confidence=0.0,
                    error="錄音失敗或音頻為空", activation_reason="錄音失敗",
                ).model_dump()

            logger.info("[STT] 進行語音識別 (backend: %s)...", self._backend_type)

            has_speech = self.vad_module.has_sufficient_speech(audio_data)
            if has_speech:
                self.vad_triggers += 1  # Audit fix: 遞增 vad_triggers
            else:
                logger.info("[STT] VAD：未檢測到足夠語音，但仍嘗試識別")

            audio_float = audio_data.astype(np.float32) / 32768.0

            # Audit fix: 統一呼叫 on_interaction_start（manual 也要呼叫）
            if self.on_interaction_start:
                self.on_interaction_start({
                    "module": "stt", "input_type": "voice",
                    "input_mode": "manual",
                    "audio_duration": duration,
                })

            # Audit fix: 傳入 per-request language（若有）
            result = self._transcribe(audio_float, language=input_data.language)
            text = correct_stt(result["text"])
            confidence = result["confidence"]

            if not text or text.isspace():
                logger.info("[STT] 🔇 未識別到有效語音內容")
                return STTOutput(
                    text="", confidence=0.0, speaker_info=None,
                    activation_reason="manual", error="未識別到有效語音內容",
                ).model_dump()

            # 語言偵測結果
            detected_lang = result.get("language")
            lang_prob = result.get("language_probability")
            if detected_lang:
                logger.info("[STT] 偵測語言: %s (%.1f%%)", detected_lang, (lang_prob or 0) * 100)

            speaker_info = None
            if input_data.enable_speaker_id and self.speaker_module is not None:
                speaker_info = self.speaker_module.identify_speaker(audio_data)

            logger.info("[STT] 識別結果: '%s' (信心度: %.2f)", text, confidence)

            output = STTOutput(
                text=text, confidence=confidence,
                speaker_info=speaker_info,
                activation_reason="manual", error=None,
            )
            if detected_lang:
                output.metadata["detected_language"] = detected_lang
                output.metadata["language_probability"] = lang_prob

            # NOTE: callback 已移至 handle() 的 keyword 判斷之後統一發送
            return output.model_dump()

        except Exception as e:
            logger.error("[STT] 識別失敗: %s", e)
            return STTOutput(
                text="", confidence=0.0,
                error=f"識別失敗: {e}", activation_reason="識別失敗",
            ).model_dump()

    # ──────────────────────────────────────────────
    #  持續監聽
    # ──────────────────────────────────────────────

    def _continuous_recognition(self, input_data: STTInput) -> dict:
        try:
            logger.info("[STT] 開始持續背景監聽模式...")

            duration = input_data.duration if input_data.duration is not None else get("continuous.total_duration", 30.0, config=self.config)
            start_time = time.time()

            chunk_duration = get("continuous.chunk_duration", 2.0, config=self.config)
            silence_threshold = get("continuous.silence_threshold", 1.5, config=self.config)
            max_speech_duration = get("continuous.max_speech_duration", 30.0, config=self.config)

            audio_buffer: list = []
            last_speech_time: Optional[float] = None
            speech_start_time: Optional[float] = None

            # Audit fix: 追蹤 continuous 結果與錯誤
            successful_segments = 0
            failed_segments = 0

            while time.time() - start_time < duration and not self.should_stop_listening:
                current_time = time.time()
                audio_chunk = self._record_audio(chunk_duration)

                if audio_chunk is None or len(audio_chunk) == 0:
                    continue

                has_speech = self.vad_module.has_sufficient_speech(audio_chunk, min_duration=0.05)

                if has_speech:
                    self.vad_triggers += 1  # Audit fix: 遞增 vad_triggers
                    if speech_start_time is None:
                        speech_start_time = current_time
                        logger.info("[STT] 🎤 語音開始...")

                    audio_buffer.append(audio_chunk)
                    last_speech_time = current_time

                    if current_time - speech_start_time > max_speech_duration:
                        if self._process_audio_buffer(
                            audio_buffer, input_data.enable_speaker_id,
                            language=input_data.language,
                        ):
                            successful_segments += 1
                        else:
                            failed_segments += 1
                        audio_buffer = []
                        last_speech_time = None
                        speech_start_time = None
                else:
                    if audio_buffer:
                        silence_duration = current_time - last_speech_time if last_speech_time else 0
                        if silence_duration >= silence_threshold:
                            if self._process_audio_buffer(
                                audio_buffer, input_data.enable_speaker_id,
                                language=input_data.language,
                            ):
                                successful_segments += 1
                            else:
                                failed_segments += 1
                            audio_buffer = []
                            last_speech_time = None
                            speech_start_time = None

                time.sleep(0.05)

            if audio_buffer:
                if self._process_audio_buffer(
                    audio_buffer, input_data.enable_speaker_id,
                    language=input_data.language,
                ):
                    successful_segments += 1
                else:
                    failed_segments += 1

            logger.info(
                "[STT] 持續監聽模式正常結束 (成功: %d, 失敗: %d)",
                successful_segments, failed_segments,
            )

            # Audit fix: 回報真實的完成狀態
            output = STTOutput(
                text="", confidence=1.0 if failed_segments == 0 else 0.5,
                speaker_info=None,
                activation_reason="continuous_listening_completed",
                error=f"{failed_segments} 個片段辨識失敗" if failed_segments > 0 else None,
            )
            output.metadata["successful_segments"] = successful_segments
            output.metadata["failed_segments"] = failed_segments
            return output.model_dump()

        except Exception as e:
            logger.error("[STT] 持續監聽失敗: %s", e)
            return STTOutput(
                text="", confidence=0.0,
                error=f"持續監聯失敗: {e}",
                activation_reason="continuous_listening_failed",
            ).model_dump()

    def _process_audio_buffer(
        self, audio_buffer: list, enable_speaker_id: bool,
        language: Optional[str] = None,
    ) -> bool:
        """處理音訊緩衝區。

        Audit fix: 回傳 bool 表示是否成功，不再靜默吞掉錯誤。
        Audit fix: 加入 per-request language 傳入 + keyword 判斷。

        Returns:
            True 如果成功辨識到文字，False 如果失敗或無結果。
        """
        try:
            if not audio_buffer:
                return False

            merged_audio = np.concatenate(audio_buffer)
            total_duration = len(merged_audio) / self.sample_rate
            logger.info("[STT] 合併 %d 個片段 (%.2fs)", len(audio_buffer), total_duration)

            if self.on_interaction_start:
                self.on_interaction_start({
                    "module": "stt", "input_type": "voice",
                    "input_mode": "continuous",
                    "audio_duration": total_duration,
                })

            audio_float = merged_audio.astype(np.float32) / 32768.0

            # Audit fix: 傳入 per-request language
            result = self._transcribe(audio_float, language=language)
            text = correct_stt(result["text"])
            confidence = result["confidence"]

            if not text or text.isspace():
                return False

            logger.info("[STT] ✅ '%s' (信心度: %.2f)", text, confidence)

            speaker_info = None
            # Audit fix: speaker_module 可能為 None
            if enable_speaker_id and self.speaker_module is not None:
                speaker_info = self.speaker_module.identify_speaker(merged_audio)

            output = STTOutput(
                text=text, confidence=confidence,
                speaker_info=speaker_info,
                activation_reason="continuous_listening", error=None,
            )

            detected_lang = result.get("language")
            if detected_lang:
                output.metadata["detected_language"] = detected_lang

            # Audit fix: continuous 每段也經過 keyword 判斷
            if self.keyword_detector is not None:
                activate, matches = self.keyword_detector.should_activate(text)
                output.should_activate = activate
                if matches:
                    output.metadata["keyword_matches"] = [
                        {"keyword": m.keyword, "confidence": m.confidence, "type": m.match_type}
                        for m in matches
                    ]

            unified_data = output.to_unified_format()
            unified_data.metadata["audio_duration"] = total_duration
            unified_data.metadata["num_chunks_merged"] = len(audio_buffer)

            if self.result_callback:
                try:
                    self.result_callback(unified_data)
                except Exception as e:
                    logger.error("[STT] 發送識別結果失敗: %s", e)

            speaker_id = speaker_info.speaker_id if speaker_info else "unknown"
            if not self.result_callback:
                # 只在沒有 callback 時使用 raw print（向後相容）
                print(f"\n🎤 [{speaker_id}] {text}")
                if confidence < 0.7:
                    print(f"   ⚠️  低置信度: {confidence:.2f}")

            return True

        except Exception as e:
            logger.error("[STT] 處理音頻緩衝區失敗: %s", e)
            return False

    # ──────────────────────────────────────────────
    #  音訊錄製
    # ──────────────────────────────────────────────

    def _record_audio(self, duration: float) -> np.ndarray:
        stream = None
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
        finally:
            # Audit fix: 確保 stream 在任何情況下都被關閉（包括 KeyboardInterrupt）
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

    # ──────────────────────────────────────────────
    #  輔助（transformers fallback 用）
    # ──────────────────────────────────────────────

    def _calculate_transformers_confidence(self, result: dict) -> float:
        try:
            text = result.get("text", "").strip()
            if not text:
                return 0.0
            base_confidence = 0.8
            text_length = len(text.split())
            length_bonus = 0.15 if 1 <= text_length <= 30 else (0.1 if text_length > 30 else 0.0)
            common_words = ["UEP", "help", "please", "can", "you", "the", "a", "an", "is", "are"]
            common_word_count = sum(1 for word in text.lower().split() if word in common_words)
            common_word_bonus = min(common_word_count * 0.03, 0.1)
            timestamp_bonus = 0.05 if ("chunks" in result and result["chunks"]) else 0.0
            return min(base_confidence + length_bonus + common_word_bonus + timestamp_bonus, 1.0)
        except Exception:
            return 0.8

    # ──────────────────────────────────────────────
    #  關閉
    # ──────────────────────────────────────────────

    def shutdown(self):
        if self.pyaudio_instance:
            self.pyaudio_instance.terminate()

        # faster-whisper cleanup
        if self._fw_model is not None:
            del self._fw_model
            self._fw_model = None

        # transformers cleanup
        if self.model is not None:
            del self.model
        if self.pipe is not None:
            del self.pipe
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        if hasattr(self, 'vad_module'):
            self.vad_module.shutdown()
        if hasattr(self, 'speaker_module') and self.speaker_module is not None:
            self.speaker_module.shutdown()

        logger.info("[STT] 引擎已關閉")
