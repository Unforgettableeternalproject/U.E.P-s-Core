"""
進階 VAD 測試

覆蓋：
- AdaptiveThreshold: 自適應閾值更新、靜音/語音分離
- VadMuzzle: 狀態機轉移、trailing silence、max segment、min speech、flush
- NoiseFilter: 高通濾波、正規化、int16/float32 輸入
- AdvancedVAD: 繼承介面相容、串流處理、energy fallback
- SileroVAD: 初始化（可能 skip 如果 torch.hub 不可用）
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from echo_stt.core.multi_channel_schemas import AudioSegment, ChannelType
from echo_stt.vad.advanced_vad import (
    AdaptiveThreshold,
    AdaptiveTrailingSilenceConfig,
    AdvancedVAD,
    MuzzleState,
    SileroVAD,
    VadMuzzle,
)
from echo_stt.vad.noise_filter import NoiseFilter


# ──────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────


def make_silence(duration_s: float, sample_rate: int = 16000) -> np.ndarray:
    """產生靜音 int16"""
    return np.zeros(int(duration_s * sample_rate), dtype=np.int16)


def make_tone(
    freq: float = 440,
    duration_s: float = 0.5,
    amplitude: float = 0.5,
    sample_rate: int = 16000,
) -> np.ndarray:
    """產生純音 int16（模擬語音的能量水準）"""
    t = np.linspace(0, duration_s, int(duration_s * sample_rate), endpoint=False)
    audio_f32 = (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.float32)
    return (audio_f32 * 32768).clip(-32768, 32767).astype(np.int16)


def make_noise(
    duration_s: float = 0.5,
    amplitude: float = 0.02,
    sample_rate: int = 16000,
) -> np.ndarray:
    """產生低能量噪音 int16"""
    rng = np.random.default_rng(42)
    return (rng.normal(0, amplitude, int(duration_s * sample_rate)) * 32768).clip(
        -32768, 32767
    ).astype(np.int16)


# ──────────────────────────────────────────────────────────────
#  AdaptiveThreshold 測試
# ──────────────────────────────────────────────────────────────


class TestAdaptiveThreshold:

    def test_initial_threshold(self):
        at = AdaptiveThreshold(initial_threshold=0.001)
        assert at.get_threshold() == pytest.approx(0.001)

    def test_adapts_to_low_noise(self):
        at = AdaptiveThreshold(
            initial_threshold=0.01,
            adapt_rate=0.5,  # 快速適應方便測試
            noise_multiplier=3.0,
        )
        # 餵入低能量靜音
        for _ in range(20):
            at.update(0.0001, is_speech=False)

        # 閾值應該降低
        assert at.get_threshold() < 0.01

    def test_adapts_to_high_noise(self):
        at = AdaptiveThreshold(
            initial_threshold=0.001,
            adapt_rate=0.5,
            noise_multiplier=3.0,
        )
        # 餵入高能量噪音
        for _ in range(20):
            at.update(0.01, is_speech=False)

        # 閾值應該升高
        assert at.get_threshold() > 0.001

    def test_speech_does_not_affect_threshold(self):
        at = AdaptiveThreshold(initial_threshold=0.005, adapt_rate=0.5)
        initial = at.get_threshold()

        # 語音時更新不應改變閾值
        for _ in range(10):
            at.update(0.5, is_speech=True)

        assert at.get_threshold() == pytest.approx(initial)

    def test_threshold_clamped(self):
        at = AdaptiveThreshold(
            initial_threshold=0.001,
            min_threshold=0.0001,
            max_threshold=0.05,
            adapt_rate=1.0,  # 直接跳到目標
            noise_multiplier=3.0,
        )
        # 餵超高值
        for _ in range(10):
            at.update(1.0, is_speech=False)
        assert at.get_threshold() <= 0.05

        # 餵超低值
        at.reset()
        for _ in range(10):
            at.update(0.0, is_speech=False)
        assert at.get_threshold() >= 0.0001

    def test_reset(self):
        at = AdaptiveThreshold(initial_threshold=0.005)
        for _ in range(10):
            at.update(0.1, is_speech=False)
        at.reset()
        assert len(at._noise_history) == 0


# ──────────────────────────────────────────────────────────────
#  VadMuzzle 測試
# ──────────────────────────────────────────────────────────────


class TestVadMuzzle:

    def test_initial_state(self):
        m = VadMuzzle()
        assert m.state == MuzzleState.SILENCE
        assert not m.is_collecting

    def test_speech_then_silence_emits(self):
        """語音 + 超過 trailing silence → 產生段落"""
        m = VadMuzzle(trailing_silence_ms=500, min_speech_ms=100)
        t = 100.0

        # 語音 0.3s
        for i in range(6):  # 6 chunks × 50ms
            result = m.push(True, t + i * 0.05)
            assert result is None
        assert m.state == MuzzleState.SPEECH

        # 靜音 1.0s（遠超 trailing 0.5s，確保觸發）
        result = None
        for i in range(20):
            r = m.push(False, t + 0.3 + i * 0.05)
            if r is not None:
                result = r
        assert result is not None
        start, end, audio = result
        assert start == pytest.approx(100.0)
        assert m.state == MuzzleState.SILENCE

    def test_speech_resume_no_emit(self):
        """語音中斷後恢復 → 不產生段落（續講）"""
        m = VadMuzzle(trailing_silence_ms=500, min_speech_ms=100)
        t = 100.0

        # 語音 0.2s
        for i in range(4):
            m.push(True, t + i * 0.05)

        # 短暫靜音 0.2s（< trailing 0.5s）
        for i in range(4):
            result = m.push(False, t + 0.2 + i * 0.05)
            assert result is None

        assert m.state == MuzzleState.TRAILING_SILENCE

        # 語音恢復
        result = m.push(True, t + 0.4)
        assert result is None
        assert m.state == MuzzleState.SPEECH

    def test_max_segment_forces_emit(self):
        """超過 max_segment → 強制截斷"""
        m = VadMuzzle(max_segment_ms=1000, trailing_silence_ms=500, min_speech_ms=100)
        t = 100.0

        # 持續語音 1.1s
        result = None
        for i in range(22):  # 22 × 50ms = 1.1s
            result = m.push(True, t + i * 0.05)
            if result is not None:
                break

        assert result is not None
        start, end, _ = result
        assert end - start >= 1.0

    def test_too_short_speech_ignored(self):
        """短於 min_speech → 忽略"""
        m = VadMuzzle(trailing_silence_ms=200, min_speech_ms=500)
        t = 100.0

        # 短語音 0.1s
        m.push(True, t)
        m.push(True, t + 0.05)

        # 長靜音
        result = None
        for i in range(10):
            result = m.push(False, t + 0.1 + i * 0.05)

        # 不應該 emit（太短了）
        assert result is None

    def test_flush_emits_remaining(self):
        """flush 清空剩餘的 buffer"""
        m = VadMuzzle(trailing_silence_ms=500, min_speech_ms=100)
        t = 100.0

        # 語音 0.3s
        for i in range(6):
            chunk = make_tone(duration_s=0.05)
            m.push(True, t + i * 0.05, audio_chunk=chunk)

        # Flush
        result = m.flush(t + 0.3)
        assert result is not None
        start, end, audio = result
        assert audio is not None
        assert len(audio) > 0

    def test_flush_empty_returns_none(self):
        m = VadMuzzle()
        assert m.flush(100.0) is None

    def test_audio_buffer_collection(self):
        """push 帶 audio_chunk 時，emit 回傳合併後的音訊"""
        m = VadMuzzle(trailing_silence_ms=200, min_speech_ms=100)
        t = 100.0

        chunks = []
        for i in range(4):  # 0.2s 語音
            chunk = make_tone(duration_s=0.05)
            chunks.append(chunk)
            m.push(True, t + i * 0.05, audio_chunk=chunk)

        # 靜音觸發 emit
        result = None
        for i in range(10):
            result = m.push(False, t + 0.2 + i * 0.05)
            if result is not None:
                break

        assert result is not None
        _, _, merged_audio = result
        expected_len = sum(len(c) for c in chunks)
        # merged 包含語音 + trailing silence 期間的 chunk
        assert len(merged_audio) >= expected_len

    def test_reset(self):
        m = VadMuzzle()
        m.push(True, 100.0)
        assert m.state == MuzzleState.SPEECH
        m.reset()
        assert m.state == MuzzleState.SILENCE


# ──────────────────────────────────────────────────────────────
#  NoiseFilter 測試
# ──────────────────────────────────────────────────────────────


class TestNoiseFilter:

    def test_output_same_length(self):
        nf = NoiseFilter()
        audio = make_tone(duration_s=0.5)
        result = nf.process(audio)
        assert len(result) == len(audio)

    def test_output_float32(self):
        nf = NoiseFilter()
        audio = make_tone(duration_s=0.1)
        result = nf.process(audio)
        assert result.dtype == np.float32

    def test_output_in_range(self):
        nf = NoiseFilter()
        audio = make_tone(duration_s=0.5, amplitude=0.9)
        result = nf.process(audio)
        assert np.all(result >= -1.0)
        assert np.all(result <= 1.0)

    def test_int16_input(self):
        nf = NoiseFilter()
        audio = make_tone(duration_s=0.1)
        assert audio.dtype == np.int16
        result = nf.process(audio)
        assert result.dtype == np.float32

    def test_float32_input(self):
        nf = NoiseFilter()
        audio = make_tone(duration_s=0.1).astype(np.float32) / 32768.0
        result = nf.process(audio)
        assert result.dtype == np.float32

    def test_empty_audio(self):
        nf = NoiseFilter()
        result = nf.process(np.array([], dtype=np.float32))
        assert len(result) == 0

    def test_silence_not_amplified(self):
        nf = NoiseFilter()
        audio = make_silence(0.1)
        result = nf.process(audio)
        assert np.max(np.abs(result)) < 0.01

    def test_highpass_removes_dc(self):
        """高通濾波應移除 DC offset（0 Hz）"""
        nf = NoiseFilter(highpass_cutoff=80, enable_normalize=False)
        # 帶 DC offset 的信號
        t = np.linspace(0, 0.5, 8000, endpoint=False)
        audio = (0.3 + 0.2 * np.sin(2 * np.pi * 500 * t)).astype(np.float32)
        result = nf.process(audio)
        # DC 應該被大幅削減
        assert abs(np.mean(result)) < abs(np.mean(audio))

    def test_normalize_equalizes_volume(self):
        """正規化應將不同音量的音訊拉到相同 peak"""
        nf = NoiseFilter(enable_highpass=False, target_peak=0.9)
        loud = make_tone(amplitude=0.9, duration_s=0.1).astype(np.float32) / 32768.0
        quiet = make_tone(amplitude=0.1, duration_s=0.1).astype(np.float32) / 32768.0

        loud_result = nf.process(loud)
        quiet_result = nf.process(quiet)

        # 兩者 peak 都應接近 0.9
        assert np.max(np.abs(loud_result)) == pytest.approx(0.9, abs=0.05)
        assert np.max(np.abs(quiet_result)) == pytest.approx(0.9, abs=0.05)

    def test_disabled_filters(self):
        """關閉所有濾波器 → 只做 dtype 轉換"""
        nf = NoiseFilter(
            enable_highpass=False,
            enable_normalize=False,
            use_spectral_subtraction=False,
        )
        audio = make_tone(duration_s=0.1)
        audio_f32 = audio.astype(np.float32) / 32768.0
        result = nf.process(audio)
        np.testing.assert_allclose(result, audio_f32, atol=1e-6)


# ──────────────────────────────────────────────────────────────
#  AdvancedVAD 測試
# ──────────────────────────────────────────────────────────────


class TestAdvancedVAD:

    def test_inherits_interface(self):
        """AdvancedVAD 繼承 VoiceActivityDetection 的所有公開方法"""
        vad = AdvancedVAD(use_silero=False)
        assert hasattr(vad, "has_sufficient_speech")
        assert hasattr(vad, "detect_voice_activity")
        assert hasattr(vad, "extract_speech_segments")

    def test_legacy_has_sufficient_speech(self):
        """繼承的 has_sufficient_speech 應正常運作"""
        vad = AdvancedVAD(use_silero=False, sensitivity=0.5)
        vad.initialize()

        speech = make_tone(duration_s=0.5, amplitude=0.5)
        silence = make_silence(0.5)

        assert vad.has_sufficient_speech(speech, min_duration=0.05)
        assert not vad.has_sufficient_speech(silence, min_duration=0.05)

    def test_backend_name_energy_fallback(self):
        vad = AdvancedVAD(use_silero=False)
        assert vad.backend_name == "energy"

    def test_is_speech_advanced_energy(self):
        """energy fallback 的 is_speech_advanced"""
        vad = AdvancedVAD(use_silero=False, sensitivity=0.5)
        vad.initialize()

        speech = make_tone(duration_s=0.05, amplitude=0.5)
        silence = make_silence(0.05)

        is_sp, conf = vad.is_speech_advanced(speech)
        assert is_sp is True
        assert conf > 0

        is_sp, conf = vad.is_speech_advanced(silence)
        assert is_sp is False

    def test_streaming_produces_segment(self):
        """process_streaming_chunk 在語音+靜音後產生 AudioSegment"""
        vad = AdvancedVAD(
            use_silero=False,
            sensitivity=0.3,
            trailing_silence_ms=200,
            min_speech_ms=100,
        )
        vad.initialize()

        t = 1000.0
        result = None

        # 餵入語音 chunks
        for i in range(8):  # 0.4s 語音
            chunk = make_tone(duration_s=0.05, amplitude=0.5)
            seg = vad.process_streaming_chunk(
                chunk, channel_id="ch_0", abs_timestamp=t + i * 0.05,
            )
            if seg is not None:
                result = seg

        # 餵入靜音 chunks 觸發 emit
        for i in range(10):  # 0.5s 靜音
            chunk = make_silence(0.05)
            seg = vad.process_streaming_chunk(
                chunk, channel_id="ch_0", abs_timestamp=t + 0.4 + i * 0.05,
            )
            if seg is not None:
                result = seg

        assert result is not None
        assert isinstance(result, AudioSegment)
        assert result.channel_id == "ch_0"
        assert result.num_samples > 0
        assert result.sequence_num >= 1

    def test_streaming_silence_only_no_segment(self):
        """純靜音不應產生 segment"""
        vad = AdvancedVAD(use_silero=False)
        vad.initialize()

        for i in range(20):
            chunk = make_silence(0.05)
            seg = vad.process_streaming_chunk(
                chunk, channel_id="ch_0", abs_timestamp=100 + i * 0.05,
            )
            assert seg is None

    def test_flush_streaming(self):
        """flush 應清空殘留 buffer"""
        vad = AdvancedVAD(
            use_silero=False, sensitivity=0.3, min_speech_ms=100,
        )
        vad.initialize()

        # 餵入語音
        for i in range(6):
            chunk = make_tone(duration_s=0.05, amplitude=0.5)
            vad.process_streaming_chunk(
                chunk, channel_id="ch_0", abs_timestamp=100 + i * 0.05,
            )

        # Flush
        seg = vad.flush_streaming(channel_id="ch_0")
        assert seg is not None
        assert seg.channel_id == "ch_0"

    def test_reset_streaming(self):
        """reset 後狀態乾淨"""
        vad = AdvancedVAD(use_silero=False)
        vad.initialize()

        for i in range(4):
            chunk = make_tone(duration_s=0.05, amplitude=0.5)
            vad.process_streaming_chunk(
                chunk, channel_id="ch_0", abs_timestamp=100 + i * 0.05,
            )

        vad.reset_streaming()
        assert vad._muzzle.state == MuzzleState.SILENCE
        assert vad._sequence_counter == 0


# ──────────────────────────────────────────────────────────────
#  AdaptiveTrailingSilence 測試
# ──────────────────────────────────────────────────────────────


class TestAdaptiveTrailingSilenceConfig:
    """AdaptiveTrailingSilenceConfig 預設值與建構測試"""

    def test_default_values(self):
        cfg = AdaptiveTrailingSilenceConfig()
        assert cfg.short_seg_threshold_s == 3.0
        assert cfg.medium_seg_threshold_s == 10.0
        assert cfg.long_seg_threshold_s == 20.0
        assert cfg.short_trailing_s == 1.1
        assert cfg.medium_trailing_s == 0.65
        assert cfg.long_trailing_s == 0.45
        assert cfg.very_long_trailing_s == 0.3
        assert cfg.soft_max_segment_s == 9.0
        assert cfg.pre_speech_padding_chunks == 3

    def test_custom_values(self):
        cfg = AdaptiveTrailingSilenceConfig(
            short_trailing_s=1.5,
            long_trailing_s=0.3,
        )
        assert cfg.short_trailing_s == 1.5
        assert cfg.long_trailing_s == 0.3
        # 其他保持預設
        assert cfg.medium_trailing_s == 0.65


class TestAdaptiveTrailingSilenceMuzzle:
    """VadMuzzle 自適應 trailing silence 測試"""

    def _make_adaptive_muzzle(self, **kwargs) -> VadMuzzle:
        """建立啟用自適應的 VadMuzzle"""
        cfg = AdaptiveTrailingSilenceConfig(**kwargs)
        return VadMuzzle(
            trailing_silence_ms=600,
            min_speech_ms=100,
            adaptive_config=cfg,
        )

    def test_short_segment_uses_longer_trailing(self):
        """短段落（<3s）應使用較長的 trailing silence（~1.1s）。
        語音 1s + 靜音 0.8s → 不應 emit（固定 600ms 會 emit，但自適應不該）。
        """
        m = self._make_adaptive_muzzle()
        t = 100.0

        # 語音 1.0s（20 chunks × 50ms）
        for i in range(20):
            m.push(True, t + i * 0.05)
        assert m.state == MuzzleState.SPEECH

        # 靜音 0.8s（16 chunks × 50ms）—— 不應觸發 emit（< 1.1s）
        result = None
        for i in range(16):
            r = m.push(False, t + 1.0 + i * 0.05)
            if r is not None:
                result = r

        assert result is None
        assert m.state == MuzzleState.TRAILING_SILENCE

    def test_short_segment_emits_after_full_trailing(self):
        """短段落在 trailing silence 超過閾值後應 emit。
        語音 1s + 靜音 1.5s → 應 emit。
        """
        m = self._make_adaptive_muzzle()
        t = 100.0

        # 語音 1.0s
        for i in range(20):
            m.push(True, t + i * 0.05)

        # 靜音 1.5s（30 chunks × 50ms）
        result = None
        for i in range(30):
            r = m.push(False, t + 1.0 + i * 0.05)
            if r is not None:
                result = r

        assert result is not None
        start, end, _ = result
        assert start == pytest.approx(100.0)
        assert m.state == MuzzleState.SILENCE

    def test_long_segment_uses_shorter_trailing(self):
        """長段落（>10s）應使用較短的 trailing silence。
        語音 15s + 靜音 0.5s → 應 emit（長段落 trailing ~0.45s）。
        """
        m = self._make_adaptive_muzzle()
        t = 100.0

        # 語音 15.0s（300 chunks × 50ms）
        for i in range(300):
            m.push(True, t + i * 0.05)

        # 靜音 0.5s
        result = None
        for i in range(10):
            r = m.push(False, t + 15.0 + i * 0.05)
            if r is not None:
                result = r

        assert result is not None
        start, end, _ = result
        assert start == pytest.approx(100.0)

    def test_very_long_segment_very_short_trailing(self):
        """超長段落（>20s）應在 300ms 停頓就切。
        語音 22s + 靜音 0.5s → 應 emit（閾值 300ms，給 200ms 餘裕）。
        """
        m = self._make_adaptive_muzzle()
        t = 100.0

        # 語音 22.0s（440 chunks × 50ms）
        for i in range(440):
            m.push(True, t + i * 0.05)

        # 靜音 0.5s（10 chunks × 50ms）—— 遠超 300ms 閾值
        result = None
        for i in range(10):
            r = m.push(False, t + 22.0 + i * 0.05)
            if r is not None:
                result = r

        assert result is not None

    def test_soft_max_lowers_threshold(self):
        """超過 soft_max_segment_s (9s) 後，trailing silence 降至 long 級。
        語音 9.5s（在 soft_max 之後但仍在 medium 區間）+ 靜音 0.5s → 應 emit。
        """
        m = self._make_adaptive_muzzle()
        t = 100.0

        # 語音 9.5s（190 chunks × 50ms）
        for i in range(190):
            m.push(True, t + i * 0.05)

        # 靜音 0.5s — 在 soft_max 之後 trailing 降至 long (0.45s)，應 emit
        result = None
        for i in range(10):
            r = m.push(False, t + 9.5 + i * 0.05)
            if r is not None:
                result = r

        assert result is not None

    def test_adaptive_disabled_behaves_same_as_fixed(self):
        """adaptive_config=None 時行為與固定 trailing_silence_ms 完全一致。"""
        m_fixed = VadMuzzle(trailing_silence_ms=500, min_speech_ms=100)
        m_no_adaptive = VadMuzzle(
            trailing_silence_ms=500, min_speech_ms=100, adaptive_config=None,
        )
        t = 100.0

        # 同樣的操作序列
        for muzzle in [m_fixed, m_no_adaptive]:
            for i in range(6):
                muzzle.push(True, t + i * 0.05)

            result = None
            for i in range(20):
                r = muzzle.push(False, t + 0.3 + i * 0.05)
                if r is not None:
                    result = r

            assert result is not None
            start, _, _ = result
            assert start == pytest.approx(100.0)

    def test_compute_trailing_silence_interpolation(self):
        """驗證線性插值在邊界附近行為正確。"""
        cfg = AdaptiveTrailingSilenceConfig()
        m = VadMuzzle(adaptive_config=cfg)

        # 精確邊界值
        assert m._compute_trailing_silence(0.0) == cfg.short_trailing_s
        assert m._compute_trailing_silence(2.9) == cfg.short_trailing_s
        assert m._compute_trailing_silence(3.0) == pytest.approx(cfg.short_trailing_s)

        # 中間值應在兩端之間
        mid_val = m._compute_trailing_silence(6.5)
        assert cfg.medium_trailing_s < mid_val < cfg.short_trailing_s

        # 超長
        assert m._compute_trailing_silence(25.0) == cfg.very_long_trailing_s

    def test_compute_trailing_silence_never_negative(self):
        """不論輸入什麼，trailing silence 不能為負或零。"""
        cfg = AdaptiveTrailingSilenceConfig()
        m = VadMuzzle(adaptive_config=cfg)

        for duration in [0.0, 0.1, 1.0, 5.0, 10.0, 20.0, 50.0, 100.0]:
            val = m._compute_trailing_silence(duration)
            assert val > 0, f"duration={duration} got trailing={val}"


class TestPreSpeechPadding:
    """Pre-speech ring buffer 測試"""

    def test_ring_buffer_prepended_to_audio(self):
        """emit 的音訊應包含 ring buffer 的前置內容。"""
        cfg = AdaptiveTrailingSilenceConfig(pre_speech_padding_chunks=2)
        m = VadMuzzle(
            trailing_silence_ms=200, min_speech_ms=50,
            adaptive_config=cfg,
        )
        t = 100.0

        # SILENCE 階段：送 3 個 chunk（ring buffer 只保留最後 2 個）
        silence_chunks = [
            np.full(800, 100, dtype=np.int16),  # 會被丟掉（ring buffer 容量 2）
            np.full(800, 200, dtype=np.int16),  # 保留
            np.full(800, 300, dtype=np.int16),  # 保留
        ]
        for i, chunk in enumerate(silence_chunks):
            m.push(False, t + i * 0.05, audio_chunk=chunk)

        # 進入 SPEECH：ring buffer 應前置
        speech_chunk = np.full(800, 1000, dtype=np.int16)
        m.push(True, t + 0.15, audio_chunk=speech_chunk)

        # Flush 取得音訊
        result = m.flush(t + 0.25)
        assert result is not None
        _, _, merged = result

        # merged 應該是 [200填充, 300填充, 1000填充]
        assert len(merged) == 800 * 3
        # 前兩段是 ring buffer
        assert merged[0] == 200
        assert merged[800] == 300
        # 最後一段是 speech chunk
        assert merged[1600] == 1000

    def test_ring_buffer_max_size(self):
        """ring buffer 不超過 pre_speech_padding_chunks。"""
        cfg = AdaptiveTrailingSilenceConfig(pre_speech_padding_chunks=2)
        m = VadMuzzle(adaptive_config=cfg)
        t = 100.0

        # 送 10 個 chunk 到 SILENCE 階段
        for i in range(10):
            chunk = np.full(100, i, dtype=np.int16)
            m.push(False, t + i * 0.05, audio_chunk=chunk)

        assert len(m._pre_speech_ring) == 2
        # 保留最後兩個
        assert m._pre_speech_ring[0][0] == 8
        assert m._pre_speech_ring[1][0] == 9

    def test_no_padding_when_disabled(self):
        """pre_speech_padding_chunks=0 或 adaptive_config=None → 無 padding。"""
        # Case 1: adaptive_config=None
        m1 = VadMuzzle(trailing_silence_ms=200, min_speech_ms=50)
        t = 100.0

        silence_chunk = np.full(800, 99, dtype=np.int16)
        m1.push(False, t, audio_chunk=silence_chunk)

        speech_chunk = np.full(800, 1000, dtype=np.int16)
        m1.push(True, t + 0.05, audio_chunk=speech_chunk)
        result = m1.flush(t + 0.15)
        assert result is not None
        _, _, merged = result
        # 只有 speech chunk，無 padding
        assert len(merged) == 800
        assert merged[0] == 1000

        # Case 2: padding_chunks=0
        cfg = AdaptiveTrailingSilenceConfig(pre_speech_padding_chunks=0)
        m2 = VadMuzzle(trailing_silence_ms=200, min_speech_ms=50, adaptive_config=cfg)
        m2.push(False, t, audio_chunk=silence_chunk)
        m2.push(True, t + 0.05, audio_chunk=speech_chunk)
        result2 = m2.flush(t + 0.15)
        assert result2 is not None
        _, _, merged2 = result2
        assert len(merged2) == 800

    def test_ring_buffer_cleared_on_reset(self):
        """reset() 應清空 ring buffer。"""
        cfg = AdaptiveTrailingSilenceConfig(pre_speech_padding_chunks=3)
        m = VadMuzzle(adaptive_config=cfg)
        t = 100.0

        for i in range(5):
            m.push(False, t + i * 0.05, audio_chunk=np.zeros(100, dtype=np.int16))

        assert len(m._pre_speech_ring) == 3
        m.reset()
        assert len(m._pre_speech_ring) == 0


# ──────────────────────────────────────────────────────────────
#  SileroVAD 測試
# ──────────────────────────────────────────────────────────────


def _can_load_silero() -> bool:
    """檢查 Silero VAD 是否可用（用於 skip 標記）。
    只檢查 torch 是否可 import，不實際載入模型（避免 hang）。
    """
    try:
        import torch  # noqa: F401
        import os
        # 只檢查快取是否存在，不走 torch.hub.load
        hub_dir = torch.hub.get_dir()
        silero_dir = os.path.join(hub_dir, "snakers4_silero-vad_master")
        return os.path.exists(silero_dir)
    except Exception:
        return False


_SILERO_AVAILABLE = _can_load_silero()


class TestSileroVAD:

    def test_not_available_before_init(self):
        svad = SileroVAD()
        assert not svad.is_available

    def test_is_speech_when_not_available(self):
        """未初始化時 is_speech 回傳 (False, 0.0)"""
        svad = SileroVAD()
        is_sp, conf = svad.is_speech(np.zeros(1600, dtype=np.float32))
        assert is_sp is False
        assert conf == 0.0

    @pytest.mark.skipif(
        not _SILERO_AVAILABLE,
        reason="torch.hub / silero not available"
    )
    def test_silero_initialization(self):
        svad = SileroVAD()
        assert svad.initialize() is True
        assert svad.is_available

    @pytest.mark.skipif(
        not _SILERO_AVAILABLE,
        reason="torch.hub / silero not available"
    )
    def test_silero_detects_speech(self):
        svad = SileroVAD(threshold=0.3)
        svad.initialize()

        speech = make_tone(duration_s=0.1, amplitude=0.5).astype(np.float32) / 32768.0
        silence = np.zeros(1600, dtype=np.float32)

        sp_result, sp_conf = svad.is_speech(speech)
        si_result, si_conf = svad.is_speech(silence)

        # Silero 對純正弦波的判斷可能不確定，
        # 但至少不應該在靜音時誤判為語音
        assert si_conf < sp_conf or not si_result
