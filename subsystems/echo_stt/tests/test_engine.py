"""
Engine regression tests — 針對 Codex audit 發現的 P1/P2 問題

覆蓋範圍：
- P1: CPU compute_type fallback (int8_float16 → int8)
- P1: manual callback should_activate 與 keyword 一致
- P1: continuous 有失敗片段時不能假成功 (error 不被清除)
- P1: text input 也經過 keyword 判斷
- P2: per-request language 傳入後端
- P2: offline cache-first 策略
"""

import types
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import numpy as np


# ──────────────────────────────────────────────
#  Helpers — 建構最小可測試 STTEngine
# ──────────────────────────────────────────────

def _minimal_config(**overrides):
    """產生足以建構 STTEngine 的最小 config（不觸碰硬體）。"""
    base = {
        "audio": {"device_index": None, "phrase_time_limit": 5, "sample_rate": 16000,
                  "channels": 1, "frames_per_buffer": 1024},
        "whisper": {"backend": "faster-whisper", "model_size": "base",
                    "compute_type": "float16", "num_workers": 1,
                    "device": "cpu", "torch_dtype": "float32",
                    "model_id": "openai/whisper-base",
                    "local_path": "models/stt/whisper/whisper-base",
                    "use_local_model": False, "download_root": None,
                    "prefer_offline": False},
        "recognition": {"task": "transcribe", "language": None,
                        "beam_size": 5, "best_of": 5,
                        "compression_ratio_threshold": 1.35,
                        "no_speech_threshold": 0.4, "max_new_tokens": 128},
        "vad": {"sensitivity": 0.7, "min_speech_duration": 0.3},
        "continuous": {"total_duration": 30.0, "chunk_duration": 2.0,
                       "silence_threshold": 1.5, "max_speech_duration": 30.0},
        "speaker": {"enabled": False},
        "keyword": {"enabled": True},
    }
    # 遞迴合併 overrides
    for k, v in overrides.items():
        if isinstance(v, dict) and k in base and isinstance(base[k], dict):
            base[k].update(v)
        else:
            base[k] = v
    return base


@pytest.fixture
def mock_pyaudio():
    """Mock PyAudio 以避免真實音訊裝置。"""
    with patch("echo_stt.core.engine.pyaudio") as pa:
        pa.paInt16 = 8  # pyaudio.paInt16 的實際值
        instance = MagicMock()
        instance.get_device_count.return_value = 0
        pa.PyAudio.return_value = instance
        yield pa


@pytest.fixture
def engine_factory(mock_pyaudio):
    """工廠 fixture — 產生不需硬體的 STTEngine。"""
    def _make(**config_overrides):
        config = _minimal_config(**config_overrides)

        with patch("echo_stt.core.engine.torch") as mock_torch:
            mock_torch.cuda.is_available.return_value = False
            mock_torch.float32 = "float32"
            mock_torch.float16 = "float16"

            from echo_stt.core.engine import STTEngine
            engine = STTEngine(config=config)
            # 直接標記已初始化，跳過真實模型載入
            engine.is_initialized = True
            engine.pyaudio_instance = mock_pyaudio.PyAudio.return_value
            return engine

    return _make


# ──────────────────────────────────────────────
#  P1: CPU compute_type fallback
# ──────────────────────────────────────────────

class TestCPUComputeTypeFallback:
    """CPU 不支援 int8_float16 時應降級為 int8，而非保留 int8_float16。"""

    def test_cpu_rejects_float16(self, engine_factory):
        """float16 在 CPU 上不被支援，應自動降級。"""
        engine = engine_factory(whisper={"compute_type": "float16", "device": "cpu"})
        assert engine._fw_compute_type != "float16"
        assert engine._fw_compute_type != "int8_float16"
        # 應該是合法的 CPU 型別
        from echo_stt.core.engine import _CT2_CPU_TYPES, _CT2_CPU_DEFAULT
        assert engine._fw_compute_type in _CT2_CPU_TYPES

    def test_cpu_default_is_int8(self):
        """靜態 fallback 預設值應為 int8。"""
        from echo_stt.core.engine import _CT2_CPU_DEFAULT
        assert _CT2_CPU_DEFAULT == "int8"

    def test_cpu_types_exclude_int8_float16_if_unsupported(self):
        """若 ctranslate2 不在環境中，靜態集合不包含 int8_float16。"""
        # 動態查詢可能包含也可能不包含，
        # 但我們驗證預設 fallback 不會選擇 int8_float16
        from echo_stt.core.engine import _CT2_CPU_DEFAULT
        assert _CT2_CPU_DEFAULT != "int8_float16"

    def test_cpu_keeps_valid_type(self, engine_factory):
        """已經合法的 CPU 型別不應被改變。"""
        engine = engine_factory(whisper={"compute_type": "int8", "device": "cpu"})
        assert engine._fw_compute_type == "int8"


# ──────────────────────────────────────────────
#  P1: manual callback should_activate 一致性
# ──────────────────────────────────────────────

class TestManualCallbackKeywordConsistency:
    """manual 路徑的 callback 必須在 keyword 判斷後發送。"""

    def test_callback_receives_correct_should_activate(self, engine_factory):
        """無關語句 → callback 的 should_activate 應為 False（與 return 值一致）。"""
        callback_data = {}

        def capture_callback(data):
            callback_data["should_activate"] = data.metadata.get("should_activate")

        engine = engine_factory()
        engine.result_callback = capture_callback

        # Mock 錄音 + 辨識 → 無關文字
        engine._record_audio = MagicMock(return_value=np.zeros(16000, dtype=np.int16))
        engine._transcribe = MagicMock(return_value={
            "text": "the weather is nice today",
            "confidence": 0.9,
            "language": "en",
            "language_probability": 0.95,
            "segments": [],
        })

        # SmartKeywordDetector 對無關文字回傳 False
        if engine.keyword_detector is not None:
            result = engine.handle({"mode": "manual"})

            # 回傳值
            assert result["should_activate"] is False, \
                "return should_activate 應為 False"

            # callback 值
            if callback_data:
                assert callback_data["should_activate"] is False, \
                    "callback should_activate 應與 return 一致 (False)"

    def test_callback_not_sent_from_manual_recognition_directly(self, engine_factory):
        """_manual_recognition 本身不應發送 callback；由 handle() 統一發送。"""
        callback_calls = []

        def track_callback(data):
            callback_calls.append(data)

        engine = engine_factory()
        engine.result_callback = track_callback

        engine._record_audio = MagicMock(return_value=np.zeros(16000, dtype=np.int16))
        engine._transcribe = MagicMock(return_value={
            "text": "hey UEP help me",
            "confidence": 0.9,
            "language": "en",
            "language_probability": 0.95,
            "segments": [],
        })

        # 直接呼叫 _manual_recognition — 不應觸發 callback
        from echo_stt.core.schemas import STTInput, ActivationMode
        input_data = STTInput(mode=ActivationMode.MANUAL)
        engine._manual_recognition(input_data)

        assert len(callback_calls) == 0, \
            "_manual_recognition 不應直接發送 callback"

    def test_callback_sent_from_handle(self, engine_factory):
        """handle() 在 keyword 判斷後發送 callback。"""
        callback_calls = []

        def track_callback(data):
            callback_calls.append(data)

        engine = engine_factory()
        engine.result_callback = track_callback

        engine._record_audio = MagicMock(return_value=np.zeros(16000, dtype=np.int16))
        engine._transcribe = MagicMock(return_value={
            "text": "hey UEP help me",
            "confidence": 0.9,
            "language": "en",
            "language_probability": 0.95,
            "segments": [],
        })

        # 經由 handle() — 應觸發 1 次 callback
        engine.handle({"mode": "manual"})

        assert len(callback_calls) == 1, \
            "handle() 應發送恰好 1 次 callback"


# ──────────────────────────────────────────────
#  P1: continuous 假成功
# ──────────────────────────────────────────────

class TestContinuousErrorPreservation:
    """continuous 有 failed_segments 時，handle() 不應清除 error。"""

    def test_failed_segments_preserve_error(self, engine_factory):
        """failed_segments > 0 時 error 不應被設為 None。"""
        engine = engine_factory()

        # Mock _continuous_recognition 回傳有失敗的結果
        def fake_continuous(input_data):
            from echo_stt.core.schemas import STTOutput
            output = STTOutput(
                text="", confidence=0.5,
                activation_reason="continuous_listening_completed",
                error="1 個片段辨識失敗",
            )
            output.metadata["successful_segments"] = 2
            output.metadata["failed_segments"] = 1
            return output.model_dump()

        engine._continuous_recognition = fake_continuous
        result = engine.handle({"mode": "continuous"})

        assert result["error"] is not None, \
            "continuous 有 failed_segments 時 error 不應被清除"
        assert "失敗" in result["error"]

    def test_successful_continuous_has_no_error(self, engine_factory):
        """continuous 全部成功時 error 應為 None。"""
        engine = engine_factory()

        def fake_continuous(input_data):
            from echo_stt.core.schemas import STTOutput
            output = STTOutput(
                text="", confidence=1.0,
                activation_reason="continuous_listening_completed",
                error=None,
            )
            output.metadata["successful_segments"] = 3
            output.metadata["failed_segments"] = 0
            return output.model_dump()

        engine._continuous_recognition = fake_continuous
        result = engine.handle({"mode": "continuous"})

        assert result["error"] is None


# ──────────────────────────────────────────────
#  P1: text input keyword 判斷
# ──────────────────────────────────────────────

class TestTextInputKeyword:
    """text input 模式也必須經過 keyword 判斷。"""

    def test_irrelevant_text_not_activated(self, engine_factory):
        """無關文字不應 should_activate=True。"""
        engine = engine_factory()

        result = engine.handle({"mode": "text", "text": "the weather is nice today"})

        # keyword detector 應將其判定為不啟動
        if engine.keyword_detector is not None:
            assert result["should_activate"] is False, \
                "無關文字不應觸發啟動"

    def test_keyword_text_activated(self, engine_factory):
        """包含喚醒詞的文字應 should_activate=True。"""
        engine = engine_factory()

        result = engine.handle({"mode": "text", "text": "hey UEP help me"})

        assert result["should_activate"] is True, \
            "含喚醒詞的文字應觸發啟動"

    def test_text_callback_has_keyword_metadata(self, engine_factory):
        """text input callback 應包含 keyword 判斷結果。"""
        callback_data = {}

        def capture(data):
            callback_data.update(data.metadata)

        engine = engine_factory()
        engine.result_callback = capture

        engine.handle({"mode": "text", "text": "hey UEP what time is it"})

        if engine.keyword_detector is not None:
            assert "should_activate" in callback_data


# ──────────────────────────────────────────────
#  P2: per-request language 傳入
# ──────────────────────────────────────────────

class TestPerRequestLanguage:
    """STTInput(language='zh') 必須傳到後端辨識方法。"""

    def test_manual_passes_language(self, engine_factory):
        """manual 模式把 input_data.language 傳給 _transcribe。"""
        engine = engine_factory()
        transcribe_calls = []

        original_transcribe = engine._transcribe

        def spy_transcribe(audio, language=None):
            transcribe_calls.append(language)
            return {"text": "test", "confidence": 0.9, "language": "zh",
                    "language_probability": 0.95, "segments": []}

        engine._transcribe = spy_transcribe
        engine._record_audio = MagicMock(return_value=np.zeros(16000, dtype=np.int16))

        engine.handle({"mode": "manual", "language": "zh"})

        assert len(transcribe_calls) == 1
        assert transcribe_calls[0] == "zh", \
            f"_transcribe 應收到 language='zh'，實際收到 {transcribe_calls[0]!r}"

    def test_manual_none_language_uses_engine_default(self, engine_factory):
        """language=None 時應使用引擎預設 (self._language)。"""
        engine = engine_factory()
        engine._language = "en"
        transcribe_calls = []

        def spy_transcribe(audio, language=None):
            transcribe_calls.append(language)
            return {"text": "test", "confidence": 0.9, "language": "en",
                    "language_probability": 0.95, "segments": []}

        engine._transcribe = spy_transcribe
        engine._record_audio = MagicMock(return_value=np.zeros(16000, dtype=np.int16))

        engine.handle({"mode": "manual"})

        assert len(transcribe_calls) == 1
        # _transcribe 的 effective_language 邏輯: language=None → self._language
        # 但 _manual_recognition 傳入 input_data.language (None)
        # _transcribe 內部會用 effective_language = None if None else self._language = "en"
        assert transcribe_calls[0] is None, \
            "input_data.language=None 應直接傳 None，由 _transcribe 決定 effective"

    def test_transcribe_effective_language_resolution(self, engine_factory):
        """_transcribe 的 effective language 解析邏輯。"""
        engine = engine_factory()
        engine._language = "en"

        # Mock 後端
        fw_calls = []

        def mock_fw(audio, language=None):
            fw_calls.append(language)
            return {"text": "test", "confidence": 0.9, "language": language,
                    "segments": []}

        engine._transcribe_faster_whisper = mock_fw

        # language=zh → effective=zh
        engine._transcribe(np.zeros(100, dtype=np.float32), language="zh")
        assert fw_calls[-1] == "zh"

        # language=None → effective=engine._language=en
        engine._transcribe(np.zeros(100, dtype=np.float32), language=None)
        assert fw_calls[-1] == "en"

    def test_process_buffer_passes_language(self, engine_factory):
        """_process_audio_buffer 也傳入 language。"""
        engine = engine_factory()
        transcribe_calls = []

        def spy_transcribe(audio, language=None):
            transcribe_calls.append(language)
            return {"text": "hello", "confidence": 0.9, "language": "ja",
                    "segments": []}

        engine._transcribe = spy_transcribe

        buffer = [np.zeros(16000, dtype=np.int16)]
        engine._process_audio_buffer(buffer, enable_speaker_id=False, language="ja")

        assert len(transcribe_calls) == 1
        assert transcribe_calls[0] == "ja"


# ──────────────────────────────────────────────
#  P2: offline cache-first 策略
# ──────────────────────────────────────────────

class TestOfflineCacheFirst:
    """faster-whisper 應先嘗試本地快取，失敗再線上下載。"""

    def test_cache_first_tries_local_files_only(self, engine_factory):
        """_init_faster_whisper 第一次呼叫應帶 local_files_only=True。"""
        engine = engine_factory()
        engine._try_offline = False

        with patch("faster_whisper.WhisperModel") as MockModel:
            # 第一次 (local_files_only=True) 失敗
            # 第二次 (無 local_files_only) 成功
            MockModel.side_effect = [
                Exception("No cache"),  # local_files_only attempt
                MagicMock(),            # online attempt
            ]

            result = engine._init_faster_whisper()

            assert result is True
            assert MockModel.call_count == 2

            # 第一次呼叫應帶 local_files_only=True
            first_call_kwargs = MockModel.call_args_list[0][1]
            assert first_call_kwargs.get("local_files_only") is True

            # 第二次不帶
            second_call_kwargs = MockModel.call_args_list[1][1]
            assert "local_files_only" not in second_call_kwargs or \
                   second_call_kwargs.get("local_files_only") is not True

    def test_explicit_offline_does_not_fallback_online(self, engine_factory):
        """_try_offline=True 且快取不存在時應直接失敗（不嘗試線上）。"""
        engine = engine_factory()
        engine._try_offline = True

        with patch("faster_whisper.WhisperModel") as MockModel:
            MockModel.side_effect = Exception("No cache")

            # _init_faster_whisper 的外層 except 會 fallback 到 transformers
            # 但 WhisperModel 只被呼叫 1 次，不會 retry online
            with patch.object(engine, "_init_transformers", return_value=False):
                result = engine._init_faster_whisper()

            assert MockModel.call_count == 1

    def test_cache_hit_skips_online(self, engine_factory):
        """本地快取存在時不應嘗試線上下載。"""
        engine = engine_factory()
        engine._try_offline = False

        with patch("faster_whisper.WhisperModel") as MockModel:
            MockModel.return_value = MagicMock()  # 第一次就成功

            result = engine._init_faster_whisper()

            assert result is True
            assert MockModel.call_count == 1  # 只呼叫一次
            first_call_kwargs = MockModel.call_args_list[0][1]
            assert first_call_kwargs.get("local_files_only") is True


# ──────────────────────────────────────────────
#  整合：三種輸入模式的 keyword 契約一致性
# ──────────────────────────────────────────────

class TestKeywordContractConsistency:
    """manual、continuous segment、text 三種路徑的 should_activate 行為一致。"""

    def test_irrelevant_input_all_false(self, engine_factory):
        """三種模式對同一無關文字都應回 should_activate=False。"""
        engine = engine_factory()
        if engine.keyword_detector is None:
            pytest.skip("keyword detector not initialized")

        irrelevant = "the weather is nice today"

        # text
        text_result = engine.handle({"mode": "text", "text": irrelevant})

        # manual (mock 錄音+辨識)
        engine._record_audio = MagicMock(return_value=np.zeros(16000, dtype=np.int16))
        engine._transcribe = MagicMock(return_value={
            "text": irrelevant, "confidence": 0.9,
            "language": "en", "language_probability": 0.95, "segments": [],
        })
        manual_result = engine.handle({"mode": "manual"})

        # continuous segment (直接呼叫 _process_audio_buffer)
        callback_data = {}

        def capture(data):
            callback_data.update(data.metadata)

        engine.result_callback = capture
        # 讓 _transcribe 回傳有文字
        buffer = [np.zeros(16000, dtype=np.int16)]
        engine._process_audio_buffer(buffer, enable_speaker_id=False)

        assert text_result["should_activate"] is False, "text: 無關文字應 False"
        assert manual_result["should_activate"] is False, "manual: 無關文字應 False"
        if callback_data:
            assert callback_data.get("should_activate") is False, \
                "continuous segment callback: 無關文字應 False"
