"""Tests for dynamic max_generate_length calculation."""

import pytest

from legacy_tts.modules.tts_module.tts_utils.gpt_length import (
    calc_dynamic_max_generate_length,
    DEFAULT_MEL_PER_TOKEN_RATIO,
    DEFAULT_SAFETY_MARGIN,
    DEFAULT_MIN_GENERATE_LENGTH,
)


class TestCalcDynamicMaxGenerateLength:
    """Unit tests for calc_dynamic_max_generate_length."""

    CFG_MAX = 1815  # typical config value

    # ── basic computation ──────────────────────────────────────────

    def test_normal_segment(self):
        """A normal segment (e.g. 60 tokens) should get ratio * n + margin."""
        result = calc_dynamic_max_generate_length(60, self.CFG_MAX)
        expected = int(60 * DEFAULT_MEL_PER_TOKEN_RATIO) + DEFAULT_SAFETY_MARGIN
        assert result == expected  # 600 + 150 = 750

    def test_short_segment_floors_to_min(self):
        """Very short text should not go below min_generate_length."""
        result = calc_dynamic_max_generate_length(1, self.CFG_MAX)
        assert result == DEFAULT_MIN_GENERATE_LENGTH  # 10 + 150 = 160 < 300

    def test_zero_tokens_floors_to_min(self):
        """Edge case: 0 tokens (empty segment) still gets the floor."""
        result = calc_dynamic_max_generate_length(0, self.CFG_MAX)
        assert result == DEFAULT_MIN_GENERATE_LENGTH

    def test_large_segment_caps_to_config_max(self):
        """A very large segment must not exceed cfg_max_mel_tokens."""
        result = calc_dynamic_max_generate_length(500, self.CFG_MAX)
        assert result == self.CFG_MAX  # 5000 + 150 = 5150 > 1815

    # ── boundary conditions ────────────────────────────────────────

    def test_result_always_leq_cfg_max(self):
        """Result should always be <= cfg_max_mel_tokens."""
        for n in [0, 1, 10, 50, 100, 200, 500, 1000]:
            result = calc_dynamic_max_generate_length(n, self.CFG_MAX)
            assert result <= self.CFG_MAX, f"Failed for n={n}: {result} > {self.CFG_MAX}"

    def test_result_always_geq_min(self):
        """Result should always be >= min_generate_length."""
        for n in [0, 1, 5, 10, 50]:
            result = calc_dynamic_max_generate_length(n, self.CFG_MAX)
            assert result >= DEFAULT_MIN_GENERATE_LENGTH, (
                f"Failed for n={n}: {result} < {DEFAULT_MIN_GENERATE_LENGTH}"
            )

    # ── custom parameters ──────────────────────────────────────────

    def test_custom_ratio(self):
        result = calc_dynamic_max_generate_length(
            50, self.CFG_MAX, mel_per_token_ratio=8.0
        )
        expected = int(50 * 8.0) + DEFAULT_SAFETY_MARGIN  # 400 + 150 = 550
        assert result == expected

    def test_custom_safety_margin(self):
        result = calc_dynamic_max_generate_length(
            50, self.CFG_MAX, safety_margin=200
        )
        expected = int(50 * DEFAULT_MEL_PER_TOKEN_RATIO) + 200  # 500 + 200 = 700
        assert result == expected

    def test_custom_min_generate_length(self):
        result = calc_dynamic_max_generate_length(
            1, self.CFG_MAX, min_generate_length=500
        )
        assert result == 500

    def test_small_cfg_max_overrides_everything(self):
        """When cfg_max_mel_tokens itself is small, that's the ceiling."""
        result = calc_dynamic_max_generate_length(100, 200)
        assert result == DEFAULT_MIN_GENERATE_LENGTH  # min(1150, 200) = 200, max(300, 200) = 300

    def test_cfg_max_below_min_uses_min(self):
        """If cfg_max < min_generate_length, min wins (safety)."""
        result = calc_dynamic_max_generate_length(100, 100)
        assert result == DEFAULT_MIN_GENERATE_LENGTH

    # ── monotonicity ───────────────────────────────────────────────

    def test_monotonically_non_decreasing(self):
        """More text tokens should never produce a shorter limit (within caps)."""
        prev = 0
        for n in range(0, 200):
            cur = calc_dynamic_max_generate_length(n, self.CFG_MAX)
            assert cur >= prev, f"Non-monotonic at n={n}: {cur} < {prev}"
            prev = cur
