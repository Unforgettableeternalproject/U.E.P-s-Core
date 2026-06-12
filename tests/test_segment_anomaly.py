"""Tests for segment anomaly detection."""

import pytest

from legacy_tts.modules.tts_module.tts_utils.segment_guard import (
    check_segment_anomaly,
    get_retry_params,
    AnomalyCheckResult,
)


class TestCheckSegmentAnomaly:
    """Unit tests for check_segment_anomaly."""

    SAMPLE_RATE = 22050

    def test_normal_segment_not_anomalous(self):
        """A normal segment should not be flagged."""
        # 60 tokens → expect ~15s of audio; actual 12s → ratio ~0.8
        result = check_segment_anomaly(
            audio_samples=12 * self.SAMPLE_RATE,  # 12 seconds
            sample_rate=self.SAMPLE_RATE,
            num_text_tokens=60,
            gpt_gen_time_ms=8000,  # 8s for 12s audio → RTF 0.67
        )
        assert result.is_anomalous is False
        assert result.reason == ""

    def test_abnormally_long_audio_flagged(self):
        """Audio 5x longer than expected should be flagged."""
        # 10 tokens → expect ~2.5s; actual 20s → ratio 8.0
        result = check_segment_anomaly(
            audio_samples=20 * self.SAMPLE_RATE,
            sample_rate=self.SAMPLE_RATE,
            num_text_tokens=10,
            gpt_gen_time_ms=3000,
        )
        assert result.is_anomalous is True
        assert "exceeds threshold" in result.reason
        assert result.audio_ratio > 3.0

    def test_high_gpt_rtf_flagged(self):
        """Very slow GPT generation should be flagged."""
        # 30 tokens → expect ~7.5s; actual audio 2s, gpt took 15s → RTF 7.5
        result = check_segment_anomaly(
            audio_samples=2 * self.SAMPLE_RATE,
            sample_rate=self.SAMPLE_RATE,
            num_text_tokens=30,
            gpt_gen_time_ms=15000,
        )
        assert result.is_anomalous is True
        assert "RTF" in result.reason

    def test_short_gpt_time_not_flagged_even_if_high_rtf(self):
        """High RTF but short absolute time should NOT be flagged."""
        # 2 tokens, 0.5s audio, 1s gen → RTF 2.0 (below threshold) but even if higher...
        result = check_segment_anomaly(
            audio_samples=int(0.5 * self.SAMPLE_RATE),
            sample_rate=self.SAMPLE_RATE,
            num_text_tokens=2,
            gpt_gen_time_ms=3000,  # 3s for 0.5s audio → RTF 6.0 but < 5s absolute
        )
        # Should NOT be flagged because absolute time is < 5000ms
        assert result.is_anomalous is False

    def test_custom_thresholds(self):
        """Custom thresholds should be respected."""
        result = check_segment_anomaly(
            audio_samples=5 * self.SAMPLE_RATE,
            sample_rate=self.SAMPLE_RATE,
            num_text_tokens=10,
            gpt_gen_time_ms=2000,
            max_audio_ratio=1.5,  # strict
        )
        assert result.is_anomalous is True

    def test_zero_samples(self):
        """Edge case: 0 audio samples."""
        result = check_segment_anomaly(
            audio_samples=0,
            sample_rate=self.SAMPLE_RATE,
            num_text_tokens=10,
            gpt_gen_time_ms=1000,
        )
        # audio_ratio would be 0, not anomalous by ratio
        assert isinstance(result, AnomalyCheckResult)

    def test_zero_tokens(self):
        """Edge case: 0 text tokens (uses minimum expected duration)."""
        result = check_segment_anomaly(
            audio_samples=2 * self.SAMPLE_RATE,
            sample_rate=self.SAMPLE_RATE,
            num_text_tokens=0,
            gpt_gen_time_ms=500,
        )
        assert isinstance(result, AnomalyCheckResult)


class TestGetRetryParams:
    def test_first_retry_increases_temperature(self):
        temp, top_p = get_retry_params(0.0, 1.0, retry_attempt=1)
        assert temp > 0.0
        assert top_p < 1.0

    def test_second_retry_increases_more(self):
        temp1, _ = get_retry_params(0.0, 1.0, retry_attempt=1)
        temp2, _ = get_retry_params(0.0, 1.0, retry_attempt=2)
        assert temp2 > temp1

    def test_top_p_never_below_floor(self):
        _, top_p = get_retry_params(0.0, 0.5, retry_attempt=10)
        assert top_p >= 0.5
