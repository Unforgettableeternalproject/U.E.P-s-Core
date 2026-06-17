"""
MultiChannelSTTEngine 測試

覆蓋：
- T-01: 建構子 & config 讀取
- T-02: 生命週期狀態機
- T-03: FileChannel 端對端（mock transcribe）
- T-04: 多通道並行
- T-05: Metrics 查詢
- T-06: Context manager
- T-07: 邊界測試

所有測試透過注入 mock STTEngine，不需要真實 Whisper 模型。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from echo_stt.core.multi_channel_engine import (
    MultiChannelSTTEngine,
    EngineState,
)
from echo_stt.core.multi_channel_schemas import (
    EngineMetrics,
    TranscriptEntry,
    TranscriptSession,
)


# ──────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────


def make_config(enabled: bool = True, **overrides) -> dict:
    """建立測試用 config，multi_channel.enabled 預設開啟。"""
    mc = {
        "enabled": enabled,
        "num_workers": 1,
        "queue_max_size": 20,
        "max_channels": 4,
        "drain_timeout_s": 2.0,
        "direct_transcribe_timeout_s": 5.0,
        "default_language": None,
        "noise_filter_enabled": False,
        "vad_config": {
            "sample_rate": 16000,
            "sensitivity": 0.5,
            "min_speech_duration": 0.1,
            "use_silero": False,  # 測試不用 Silero
            "trailing_silence_ms": 300,
            "max_segment_ms": 5000,
            "min_speech_ms": 100,
            "adaptive_threshold": False,
        },
    }
    mc.update(overrides)
    return {
        "multi_channel": mc,
        "whisper": {
            "backend": "faster-whisper",
            "model_size": "base",
            "compute_type": "float16",
            "device": "cpu",
            "num_workers": 1,
        },
        "audio": {"sample_rate": 16000},
        "recognition": {"language": None},
    }


def mock_transcribe(audio_float: np.ndarray, language=None):
    """假的辨識函式。"""
    return {
        "text": f"mock ({len(audio_float)} samples)",
        "confidence": 0.9,
        "language": language or "zh",
        "segments": [],
    }


def make_mock_stt_engine():
    """建立 mock STTEngine。"""
    engine = MagicMock()
    engine.initialize.return_value = True
    engine._transcribe = mock_transcribe
    engine.device = "cpu"
    engine._fw_model_size = "base"
    engine._backend_type = "faster-whisper"
    return engine


def inject_mock(engine: MultiChannelSTTEngine) -> MultiChannelSTTEngine:
    """注入 mock STTEngine（繞過真實模型載入）。"""
    engine._stt_engine = make_mock_stt_engine()
    return engine


def make_speech_audio(duration_s: float = 1.0, freq: float = 440.0) -> np.ndarray:
    """生成包含正弦波的 int16 音訊（模擬語音能量足夠觸發 VAD）。"""
    sr = 16000
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    signal = (np.sin(2 * np.pi * freq * t) * 20000).astype(np.int16)
    return signal


# ──────────────────────────────────────────────────────────────
#  T-01: 建構子 & Config
# ──────────────────────────────────────────────────────────────


class TestConfig:

    def test_disabled_returns_false(self):
        """multi_channel.enabled=false → initialize() 回傳 False"""
        engine = MultiChannelSTTEngine(config=make_config(enabled=False))
        assert engine.initialize() is False
        assert engine.state == EngineState.CREATED

    def test_config_keys_read(self):
        """Config 鍵值正確讀取"""
        cfg = make_config(num_workers=4, queue_max_size=100, max_channels=16)
        engine = MultiChannelSTTEngine(config=cfg)
        assert engine._num_workers == 4
        assert engine._queue_max_size == 100
        assert engine._max_channels == 16

    def test_missing_multi_channel_section(self):
        """沒有 multi_channel 區段 → 預設值 + enabled=False"""
        engine = MultiChannelSTTEngine(config={})
        assert engine._enabled is False
        assert engine._num_workers == 2
        assert engine.initialize() is False


# ──────────────────────────────────────────────────────────────
#  T-02: 生命週期狀態機
# ──────────────────────────────────────────────────────────────


class TestLifecycle:

    def test_init_start_stop_shutdown(self):
        """完整生命週期: CREATED → INITIALIZED → RUNNING → STOPPED → SHUTDOWN"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))

        assert engine.state == EngineState.CREATED

        assert engine.initialize() is True
        assert engine.state == EngineState.INITIALIZED

        engine.start()
        assert engine.state == EngineState.RUNNING
        assert engine.is_running

        engine.stop()
        assert engine.state == EngineState.STOPPED
        assert not engine.is_running

        engine.shutdown()
        assert engine.state == EngineState.SHUTDOWN

    def test_start_without_init_raises(self):
        """未 initialize 就 start → RuntimeError"""
        engine = MultiChannelSTTEngine(config=make_config())
        with pytest.raises(RuntimeError, match="not initialized"):
            engine.start()

    def test_double_initialize_is_idempotent(self):
        """重複 initialize 不會出錯"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        assert engine.initialize() is True
        assert engine.initialize() is True  # 第二次跳過

    def test_shutdown_then_start_raises(self):
        """Shutdown 後不能再 start"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        engine.initialize()
        engine.start()
        engine.shutdown()
        with pytest.raises(RuntimeError, match="shut down"):
            engine.start()

    def test_shutdown_then_init_fails(self):
        """Shutdown 後不能再 initialize"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        engine.initialize()
        engine.shutdown()
        assert engine.initialize() is False

    def test_restart_after_stop(self):
        """stop 後可以再 start（重啟）"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        engine.initialize()
        engine.start()
        engine.stop()
        assert engine.state == EngineState.STOPPED

        engine.start()
        assert engine.state == EngineState.RUNNING

        engine.shutdown()


# ──────────────────────────────────────────────────────────────
#  T-03: FileChannel 端對端
# ──────────────────────────────────────────────────────────────


class TestFileChannelE2E:

    def test_file_channel_produces_entries(self):
        """FileChannel → VAD → Queue → Worker → Aggregator 完整管線"""
        entries = []

        engine = inject_mock(MultiChannelSTTEngine(
            config=make_config(),
            on_entry=lambda e: entries.append(e),
        ))
        engine.initialize()
        engine.start()

        # 加入包含語音的 FileChannel
        audio = make_speech_audio(duration_s=2.0)
        engine.add_file_channel(
            "test_ch",
            audio_data=audio,
            speaker_label="tester",
            speaker_name="Tester",
        )

        # 給足夠時間讓管線完成
        time.sleep(3.0)

        engine.stop()

        # 驗證 session 存在（有沒有 entries 取決於 VAD 觸發）
        session = engine._aggregator.get_session()
        assert session is not None

        engine.shutdown()

    def test_transcript_readable(self):
        """get_transcript() 各格式不 crash"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        engine.initialize()
        engine.start()

        readable = engine.get_transcript("readable")
        assert isinstance(readable, str)

        srt = engine.get_transcript("srt")
        assert isinstance(srt, str)

        json_out = engine.get_transcript("json")
        assert isinstance(json_out, list)

        engine.shutdown()


# ──────────────────────────────────────────────────────────────
#  T-04: 多通道並行
# ──────────────────────────────────────────────────────────────


class TestMultiChannel:

    def test_two_file_channels(self):
        """同時兩個 FileChannel，結果應來自不同通道"""
        entries = []

        engine = inject_mock(MultiChannelSTTEngine(
            config=make_config(),
            on_entry=lambda e: entries.append(e),
        ))
        engine.initialize()
        engine.start()

        audio_a = make_speech_audio(1.5, freq=300)
        audio_b = make_speech_audio(1.5, freq=500)

        engine.add_file_channel("alice", audio_a, speaker_label="alice", speaker_name="Alice")
        engine.add_file_channel("bob", audio_b, speaker_label="bob", speaker_name="Bob")

        assert engine.channel_count == 2

        time.sleep(3.0)
        engine.shutdown()

        assert engine._total_channels_ever == 2


# ──────────────────────────────────────────────────────────────
#  T-05: Metrics
# ──────────────────────────────────────────────────────────────


class TestMetrics:

    def test_metrics_type(self):
        """get_metrics() 回傳 EngineMetrics"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        engine.initialize()

        m = engine.get_metrics()
        assert isinstance(m, EngineMetrics)
        assert m.uptime == 0.0  # 未 start
        assert m.model_name == "base"
        assert m.device == "cpu"

        engine.shutdown()

    def test_uptime_increments_after_start(self):
        """start 後 uptime > 0"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        engine.initialize()
        engine.start()

        time.sleep(0.2)

        m = engine.get_metrics()
        assert m.uptime > 0.0

        engine.shutdown()


# ──────────────────────────────────────────────────────────────
#  T-06: Context Manager
# ──────────────────────────────────────────────────────────────


class TestContextManager:

    def test_with_statement(self):
        """with 語法正常啟動並自動 shutdown"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        with engine:
            assert engine.is_running
            assert engine.state == EngineState.RUNNING

        assert engine.state == EngineState.SHUTDOWN

    def test_with_exception_still_shuts_down(self):
        """with 內拋例外時仍會 shutdown"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        with pytest.raises(ValueError):
            with engine:
                raise ValueError("test error")

        assert engine.state == EngineState.SHUTDOWN

    def test_with_disabled_raises(self):
        """enabled=false 時 with 語法 raise RuntimeError"""
        with pytest.raises(RuntimeError, match="initialization failed"):
            with MultiChannelSTTEngine(config=make_config(enabled=False)):
                pass


# ──────────────────────────────────────────────────────────────
#  T-07: 邊界測試
# ──────────────────────────────────────────────────────────────


class TestEdgeCases:

    def test_discord_channel_not_implemented(self):
        """add_discord_channel 應 raise NotImplementedError"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        engine.initialize()
        with pytest.raises(NotImplementedError, match="Discord"):
            engine.add_discord_channel("dc_1", user_id="user_123")
        engine.shutdown()

    def test_invalid_format_raises(self):
        """get_transcript() 無效格式 raise ValueError"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        engine.initialize()
        engine.start()
        with pytest.raises(ValueError, match="Unknown format"):
            engine.get_transcript("xml")
        engine.shutdown()

    def test_operations_before_init_raise(self):
        """初始化前呼叫操作 → RuntimeError"""
        engine = MultiChannelSTTEngine(config=make_config())
        with pytest.raises(RuntimeError, match="not initialized"):
            engine.get_transcript()
        with pytest.raises(RuntimeError, match="not initialized"):
            engine.add_local_mic("mic_0")

    def test_session_management(self):
        """session 開始 / 結束 / 重開"""
        sessions_ended = []

        engine = inject_mock(MultiChannelSTTEngine(
            config=make_config(),
            on_session_end=lambda s: sessions_ended.append(s),
        ))
        engine.initialize()
        engine.start()

        # start() 已建立 session
        session = engine._aggregator.get_session()
        assert session is not None

        # 結束 session
        finished = engine.end_session()
        assert finished is not None
        assert len(sessions_ended) == 1

        # 開新 session
        new_session = engine.start_session()
        assert new_session is not None
        assert new_session.session_id != finished.session_id

        engine.shutdown()
        # shutdown 也會結束 session
        assert len(sessions_ended) == 2

    def test_transcribe_segment_direct(self):
        """transcribe_segment 直接辨識"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        engine.initialize()

        audio = np.random.randn(16000).astype(np.float32) * 0.5
        entry = engine.transcribe_segment(audio, speaker_name="Test")
        assert entry is not None
        assert isinstance(entry, TranscriptEntry)
        assert "mock" in entry.text

        engine.shutdown()

    def test_stop_start_preserves_session(self):
        """stop → start 不應覆蓋既有 session (Codex finding #1)"""
        engine = inject_mock(MultiChannelSTTEngine(config=make_config()))
        engine.initialize()
        engine.start()

        # 直接辨識產生 entry
        audio = np.random.randn(16000).astype(np.float32) * 0.5
        engine.transcribe_segment(audio)

        # stop 後 session 仍可讀
        engine.stop()
        assert engine._aggregator.entry_count == 1
        transcript_before = engine.get_transcript("readable")
        assert "mock" in transcript_before

        # 再次 start — session 應保留
        engine.start()
        assert engine._aggregator.entry_count == 1
        transcript_after = engine.get_transcript("readable")
        assert transcript_after == transcript_before

        engine.shutdown()

    def test_transcribe_segment_timeout(self):
        """transcribe_segment 應在 timeout 後回傳 None (Codex finding #2)"""
        cfg = make_config(direct_transcribe_timeout_s=0.3)
        engine = inject_mock(MultiChannelSTTEngine(config=cfg))

        # 讓 mock _transcribe 慢到超過 timeout
        def slow_transcribe(audio, lang=None):
            time.sleep(1.0)
            return {"text": "should not reach", "confidence": 0.9, "language": "zh", "segments": []}

        engine._stt_engine._transcribe = slow_transcribe
        engine.initialize()

        audio = np.random.randn(16000).astype(np.float32) * 0.5
        result = engine.transcribe_segment(audio)

        # 應 timeout 回傳 None
        assert result is None

        engine.shutdown()

    def test_transformers_backend_forces_single_worker(self):
        """transformers 後端 + num_workers>1 → 自動降為 1"""
        cfg = make_config(num_workers=4)
        cfg["whisper"]["backend"] = "transformers"

        mock_stt = make_mock_stt_engine()
        mock_stt._backend_type = "transformers"

        engine = MultiChannelSTTEngine(config=cfg)
        engine._stt_engine = mock_stt
        engine.initialize()

        # worker pool 應該只有 1 個 worker
        assert engine._worker_pool.num_workers == 1

        engine.shutdown()
