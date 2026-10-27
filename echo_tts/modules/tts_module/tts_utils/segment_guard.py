"""
Segment anomaly detection for TTS pipeline.

Detects runaway GPT generation (repetition loops, abnormally long audio)
and provides per-segment guard-rail checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Default thresholds (overridable via config.yaml → synthesis.anomaly_detection)
# ---------------------------------------------------------------------------
DEFAULT_MAX_AUDIO_RATIO = 3.0
"""Maximum ratio of audio_duration / expected_duration before flagging anomaly."""

DEFAULT_EXPECTED_CHARS_PER_SECOND = 4.0
"""Rough estimate: ~4 CJK chars per second (or ~12 English chars/sec at ~3 chars/word)."""

DEFAULT_MAX_SEGMENT_RTF = 5.0
"""Max RTF (realtime-factor) for GPT generation before flagging anomaly.
RTF = gpt_gen_time / audio_duration.  > 5 means extremely slow."""

DEFAULT_MAX_RETRIES = 1
"""How many times to retry a failed segment with adjusted sampling params."""

DEFAULT_RETRY_TEMPERATURE_DELTA = 0.15
"""Increase temperature by this much on retry to break repetition patterns."""

DEFAULT_RETRY_TOP_P_DELTA = -0.1
"""Decrease top_p by this much on retry."""


@dataclass
class AnomalyCheckResult:
    """Result of a single segment anomaly check."""
    is_anomalous: bool = False
    reason: str = ""
    audio_duration_s: float = 0.0
    expected_duration_s: float = 0.0
    audio_ratio: float = 0.0
    gpt_rtf: float = 0.0


@dataclass
class SegmentGuardStats:
    """Accumulated stats across all segments in one synthesis call."""
    anomaly_count: int = 0
    retry_count: int = 0
    anomalous_segments: list = field(default_factory=list)


def check_segment_anomaly(
    audio_samples: int,
    sample_rate: int,
    num_text_tokens: int,
    gpt_gen_time_ms: float,
    *,
    max_audio_ratio: float = DEFAULT_MAX_AUDIO_RATIO,
    expected_chars_per_second: float = DEFAULT_EXPECTED_CHARS_PER_SECOND,
    max_segment_rtf: float = DEFAULT_MAX_SEGMENT_RTF,
) -> AnomalyCheckResult:
    """Check whether a generated audio segment is anomalous.

    Parameters
    ----------
    audio_samples:
        Number of audio samples in the segment.
    sample_rate:
        Audio sample rate (typically 22050).
    num_text_tokens:
        Number of BPE text tokens in the segment.
    gpt_gen_time_ms:
        Time spent in GPT generation for this segment, in ms.
    max_audio_ratio:
        Flag if audio_duration / expected_duration > this.
    expected_chars_per_second:
        Estimated characters/tokens per second of speech.
    max_segment_rtf:
        Flag if GPT RTF > this.

    Returns
    -------
    AnomalyCheckResult
    """
    audio_duration_s = audio_samples / sample_rate if sample_rate > 0 else 0.0
    # Rough expected duration: each text token ≈ 1 CJK char ≈ 0.25s
    expected_duration_s = max(num_text_tokens / expected_chars_per_second, 0.5)

    audio_ratio = audio_duration_s / expected_duration_s if expected_duration_s > 0 else 0.0
    gpt_rtf = (gpt_gen_time_ms / 1000.0) / audio_duration_s if audio_duration_s > 0 else 0.0

    result = AnomalyCheckResult(
        audio_duration_s=audio_duration_s,
        expected_duration_s=expected_duration_s,
        audio_ratio=audio_ratio,
        gpt_rtf=gpt_rtf,
    )

    # Check 1: audio is abnormally long relative to text
    if audio_ratio > max_audio_ratio:
        result.is_anomalous = True
        result.reason = (
            f"Audio duration ({audio_duration_s:.1f}s) is {audio_ratio:.1f}x "
            f"the expected duration ({expected_duration_s:.1f}s), "
            f"exceeds threshold {max_audio_ratio}x"
        )
        return result

    # Check 2: GPT took too long (possible repetition loop)
    if gpt_rtf > max_segment_rtf and gpt_gen_time_ms > 5000:
        # Only flag if generation took > 5 seconds absolute (avoid false positives on short segments)
        result.is_anomalous = True
        result.reason = (
            f"GPT RTF ({gpt_rtf:.1f}x) exceeds threshold {max_segment_rtf}x "
            f"(gen time: {gpt_gen_time_ms:.0f}ms for {audio_duration_s:.1f}s audio)"
        )
        return result

    return result


def get_retry_params(
    base_temperature: float,
    base_top_p: float,
    retry_attempt: int,
    *,
    temperature_delta: float = DEFAULT_RETRY_TEMPERATURE_DELTA,
    top_p_delta: float = DEFAULT_RETRY_TOP_P_DELTA,
) -> tuple[float, float]:
    """Compute adjusted sampling params for a retry attempt.

    Returns (temperature, top_p) with small perturbations to break
    repetition patterns.
    """
    new_temp = base_temperature + temperature_delta * retry_attempt
    new_top_p = max(0.5, base_top_p + top_p_delta * retry_attempt)
    return new_temp, new_top_p


def log_synthesis_failure(exc: BaseException, debug_fn, error_fn) -> bool:
    """依例外性質記錄合成中止原因。

    呼叫端（如 Echo-Stream）在 segment callback 內拋出的取消例外會帶
    ``is_cancellation = True``，屬於正常中止（barge-in／取消），只走 debug 級；
    其餘例外維持 ERROR。回傳是否為取消，僅供呼叫端判斷用，不影響回傳語意。
    """
    if getattr(exc, "is_cancellation", False):
        debug_fn(2, "⏹️ 合成已取消")
        return True
    error_fn(f"❌ 合成失敗: {exc}")
    return False
