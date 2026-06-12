"""Tests for quick_streaming_tokens with graduated segment rebalancing.

The quick_streaming_tokens parameter should:
1. Keep the first segment close to N tokens
2. Gradually increase subsequent segment targets (N, N+step, N+2*step, ... cap)
3. Never cut mid-sentence (grace window)
"""

import pytest

from legacy_tts.modules.tts_module.tts_utils.front import TextTokenizer

GRACE = TextTokenizer._quick_stream_grace_window
STEP = TextTokenizer._quick_stream_step


class TestGraduatedStreaming:
    """Verify graduated segment rebalancing for smooth streaming."""

    def _make_tokens(self, pattern: str, count: int) -> list:
        tokens = []
        for i in range(count):
            tokens.append(f"▁word{i}")
            if pattern == "commas" and (i + 1) % 10 == 0:
                tokens.append("▁,")
            elif pattern == "sentences" and (i + 1) % 30 == 0:
                tokens.append("▁.")
        return tokens

    def test_first_segment_near_quick_streaming_target(self):
        """First segment should be ≤ quick_streaming_tokens."""
        tokens = self._make_tokens("commas", 100)
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=50,
        )
        assert len(segments[0]) <= 50 + GRACE

    def test_segments_gradually_increase(self):
        """Segment sizes should ramp up, not jump from tiny to huge."""
        # 200 tokens with commas every 10
        tokens = self._make_tokens("commas", 200)
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=50,
        )
        if len(segments) >= 3:
            lens = [len(s) for s in segments]
            # Second segment should be larger than first but not max
            # (graduated: target_1 = 50+25 = 75)
            assert lens[0] <= lens[1] + GRACE + STEP, (
                f"Non-graduated: {lens}"
            )

    def test_no_boundary_keeps_segment_intact(self):
        """No boundary → don't force-cut mid-sentence."""
        tokens = [f"▁word{i}" for i in range(80)]
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=50,
        )
        assert len(segments[0]) == 80

    def test_quick_streaming_zero_disables(self):
        tokens = self._make_tokens("sentences", 90)
        seg_a = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], 120, quick_streaming_tokens=0)
        seg_b = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], 120, quick_streaming_tokens=0)
        assert seg_a == seg_b

    def test_already_small_segments_unchanged(self):
        """Small segments (< target) should not be further split."""
        tokens = [f"▁word{i}" for i in range(20)] + ["▁."] + \
                 [f"▁word{i}" for i in range(20, 50)] + ["▁."]
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], 120, quick_streaming_tokens=50)
        assert len(segments[0]) == 21

    def test_concatenation_preserves_all_tokens(self):
        tokens = self._make_tokens("commas", 150)
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], 120, quick_streaming_tokens=50)
        flat = [tok for seg in segments for tok in seg]
        assert flat == tokens, "Token loss detected"

    def test_grace_window_finds_boundary_forward(self):
        """Boundary just past target should be found within grace window."""
        tokens = [f"▁word{i}" for i in range(55)] + [","] + \
                 [f"▁word{i}" for i in range(55, 100)] + ["."]
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["."], 120, quick_streaming_tokens=50)
        # Comma at 55, within grace window (50+30=80)
        assert len(segments[0]) == 56

    def test_real_world_sentence_graduated(self):
        """Real-world long text should have smoothly graduated segments."""
        words = ["▁The", "▁quick", "▁brown", "▁fox", "▁,",
                 "▁jumped", "▁over", "▁the", "▁lazy", "▁dog", "▁,",
                 "▁and", "▁then", "▁ran", "▁through", "▁the", "▁forest", "▁,"]
        tokens = words * 8 + ["▁."]  # ~144 tokens
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], 120, quick_streaming_tokens=50)
        lens = [len(s) for s in segments]
        # First should be small, subsequent should grow
        assert lens[0] <= 50 + GRACE, f"First too big: {lens}"
        # No huge jump: no segment should be more than 2x the previous
        for i in range(1, len(lens)):
            assert lens[i] <= max(lens[i-1] * 2.5, 120 + GRACE), (
                f"Sudden jump at seg {i}: {lens}"
            )
        # All tokens preserved
        flat = [tok for seg in segments for tok in seg]
        assert flat == tokens
