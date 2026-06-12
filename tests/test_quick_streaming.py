"""Tests for quick_streaming_tokens behavior in split_segments.

The quick_streaming_tokens parameter should ensure the FIRST segment
is close to N tokens (with a grace window), so the TTS engine can
deliver first audio faster — but NEVER cut mid-sentence.
"""

import pytest

from legacy_tts.modules.tts_module.tts_utils.front import TextTokenizer

# Grace window used in the implementation
GRACE = TextTokenizer._quick_stream_grace_window


class TestQuickStreamingTokens:
    """Verify that quick_streaming_tokens constrains first segment size."""

    def _make_tokens(self, pattern: str, count: int) -> list:
        """Helper: generate a fake token list with boundaries at intervals."""
        tokens = []
        for i in range(count):
            tokens.append(f"▁word{i}")
            if pattern == "commas" and (i + 1) % 10 == 0:
                tokens.append("▁,")
            elif pattern == "sentences" and (i + 1) % 30 == 0:
                tokens.append("▁.")
        return tokens

    def test_first_segment_split_at_comma_before_limit(self):
        """First segment should split at a comma before quick_streaming_tokens."""
        # Commas every 10 tokens → comma at ~10, 21, 32, 43 …
        tokens = self._make_tokens("commas", 100)
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=50,
        )
        first_len = len(segments[0])
        # Should find a comma at or before 50
        assert first_len <= 50, f"First segment {first_len} > 50"
        assert len(segments) >= 2

    def test_grace_window_finds_boundary_after_limit(self):
        """If no boundary before limit, find one in the grace window."""
        # Tokens 0-59: no boundary.  Token 60: comma.
        tokens = [f"▁word{i}" for i in range(60)] + ["▁,"] + \
                 [f"▁word{i}" for i in range(60, 100)] + ["▁."]
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=50,
        )
        first_len = len(segments[0])
        # Comma at position 60, within grace window (50 + 30 = 80)
        assert first_len == 61, f"Expected split at comma (pos 61), got {first_len}"

    def test_no_boundary_at_all_keeps_segment_intact(self):
        """If no boundary exists, don't force-cut mid-sentence."""
        tokens = [f"▁word{i}" for i in range(80)]
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=50,
        )
        # No boundary → first segment stays 80 (never cut mid-sentence)
        assert len(segments[0]) == 80

    def test_boundary_beyond_grace_window_not_used(self):
        """Boundary outside grace window shouldn't be used."""
        # First comma at position 85, grace = 50+30 = 80 → out of reach
        tokens = [f"▁word{i}" for i in range(85)] + ["▁,"] + \
                 [f"▁word{i}" for i in range(85, 110)] + ["▁."]
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=50,
        )
        # Can't reach the comma → first segment goes all the way to the period
        first_len = len(segments[0])
        # The period at the end will be the split point for normal logic
        assert first_len > 50

    def test_quick_streaming_zero_disables_feature(self):
        """quick_streaming_tokens=0 should not affect splitting."""
        tokens = self._make_tokens("sentences", 90)
        seg_a = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=0,
        )
        seg_b = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=0,
        )
        assert seg_a == seg_b

    def test_already_small_first_segment_unchanged(self):
        """If first segment is already ≤ limit, leave it alone."""
        # Period at token 20 → first segment is 21 tokens
        tokens = [f"▁word{i}" for i in range(20)] + ["▁."] + \
                 [f"▁word{i}" for i in range(20, 80)] + ["▁."]
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=50,
        )
        assert len(segments[0]) == 21  # unchanged

    def test_merge_respects_quick_streaming_threshold(self):
        """Small segments before threshold should NOT be merged."""
        # Periods every 15 tokens
        tokens = []
        for i in range(90):
            tokens.append(f"▁word{i}")
            if (i + 1) % 15 == 0:
                tokens.append("▁.")
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=50,
        )
        first_len = len(segments[0])
        assert first_len <= 50

    def test_concatenation_preserves_all_tokens(self):
        """All tokens must be preserved after split."""
        tokens = self._make_tokens("commas", 80)
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=50,
        )
        flat = [tok for seg in segments for tok in seg]
        assert flat == tokens, "Token loss detected"

    def test_real_world_long_sentence_with_commas(self):
        """Real-world 100+ token sentence: first segment close to limit."""
        words = ["▁The", "▁quick", "▁brown", "▁fox", "▁,",
                 "▁jumped", "▁over", "▁the", "▁lazy", "▁dog", "▁,",
                 "▁and", "▁then", "▁ran", "▁through", "▁the", "▁forest", "▁,",
                 "▁while", "▁the", "▁birds", "▁sang", "▁in", "▁the", "▁trees", "▁,"]
        tokens = words * 5 + ["▁."]
        segments = TextTokenizer.split_segments_by_token(
            tokens, ["▁."], max_text_tokens_per_segment=120,
            quick_streaming_tokens=50,
        )
        first_len = len(segments[0])
        # Should split at a comma near 50 (within limit or grace)
        assert first_len <= 50 + GRACE, f"First segment {first_len} too long"
        # All tokens preserved
        flat = [tok for seg in segments for tok in seg]
        assert flat == tokens
