"""Tests for Chinese semantic pre-segmentation using jieba."""

import pytest

from legacy_tts.modules.tts_module.tts_utils.chinese_segmenter import (
    pre_segment_chinese,
    JIEBA_AVAILABLE,
    _has_chinese,
)


# Skip all tests if jieba is not installed
pytestmark = pytest.mark.skipif(
    not JIEBA_AVAILABLE, reason="jieba not installed"
)


# ── helper tests ───────────────────────────────────────────────

class TestHasChinese:
    def test_pure_chinese(self):
        assert _has_chinese("你好世界") is True

    def test_pure_english(self):
        assert _has_chinese("hello world") is False

    def test_mixed(self):
        assert _has_chinese("hello 你好") is True

    def test_empty(self):
        assert _has_chinese("") is False


# ── core segmentation tests ───────────────────────────────────

class TestPreSegmentChinese:
    """Test that long unpunctuated Chinese is split at semantic boundaries."""

    DEFAULT_CFG = {
        "enabled": True,
        "min_segment_chars": 10,
        "max_segment_chars": 40,
    }

    def _join(self, segments: list[str]) -> str:
        """Concatenation of segments must equal the original text."""
        return "".join(segments)

    def test_lossless_concatenation(self):
        """Segments must join back to the original text."""
        text = "今天天氣很好我們去公園吧他不想走因為太累了"
        segments = pre_segment_chinese(text, self.DEFAULT_CFG)
        assert self._join(segments) == text

    def test_splits_at_particle_ba(self):
        """Should split after 吧 (語氣詞)."""
        text = "今天天氣很好我們去公園吧他說他也想去"
        segments = pre_segment_chinese(text, self.DEFAULT_CFG)
        assert len(segments) >= 2, f"Expected split, got: {segments}"
        # First segment should end with or include 吧
        assert "吧" in segments[0], f"Expected 吧 in first segment: {segments}"

    def test_splits_before_conjunction(self):
        """Should split before 因為 (連詞)."""
        text = "他今天不想出門因為外面下雨了而且風也很大"
        segments = pre_segment_chinese(text, self.DEFAULT_CFG)
        assert len(segments) >= 2, f"Expected split, got: {segments}"
        # Find the segment starting with 因為
        conj_segments = [s for s in segments if s.startswith("因為") or s.startswith("而且")]
        assert len(conj_segments) >= 1, f"Expected conjunction at segment start: {segments}"

    def test_splits_at_le(self):
        """Should split after 了 (助詞) when min_segment_chars is low enough."""
        text = "我已經吃完飯了現在想去散步然後再回家寫作業"
        # Lower min to let 了 at position 7 trigger
        cfg = {"enabled": True, "min_segment_chars": 6, "max_segment_chars": 40}
        segments = pre_segment_chinese(text, cfg)
        assert len(segments) >= 2, f"Expected split, got: {segments}"

    def test_no_split_for_short_text(self):
        """Short text should remain as one segment."""
        text = "你好"
        segments = pre_segment_chinese(text, self.DEFAULT_CFG)
        assert segments == ["你好"]

    def test_english_text_passes_through(self):
        """English text should not be split by jieba."""
        text = "Hello world this is a test sentence"
        segments = pre_segment_chinese(text, self.DEFAULT_CFG)
        assert segments == [text]

    def test_mixed_chinese_english(self):
        """Mixed text should still be handled."""
        text = "今天天氣很好我們去park吧然後回家"
        segments = pre_segment_chinese(text, self.DEFAULT_CFG)
        assert self._join(segments) == text

    # ── config toggle tests ───────────────────────────────────

    def test_disabled_by_config(self):
        """When enabled=False, returns text unchanged."""
        text = "今天天氣很好我們去公園吧他不想走因為太累了"
        segments = pre_segment_chinese(text, {"enabled": False})
        assert segments == [text]

    def test_none_config_uses_defaults(self):
        """None config should use defaults and not crash."""
        text = "今天天氣很好我們去公園吧他不想走因為太累了這是一個很長的中文句子沒有任何標點符號"
        segments = pre_segment_chinese(text, None)
        assert self._join(segments) == text

    def test_max_segment_chars_forces_split(self):
        """Very long text should be force-split even without natural boundaries."""
        # 80+ chars of repetitive content with no particles
        text = "他走路走路走路走路走路走路走路走路走路走路走路走路走路走路走路走路走路走路走路走路"
        cfg = {"enabled": True, "min_segment_chars": 10, "max_segment_chars": 30}
        segments = pre_segment_chinese(text, cfg)
        assert len(segments) >= 2, f"Expected force-split: {segments}"
        assert self._join(segments) == text

    # ── edge cases ────────────────────────────────────────────

    def test_empty_string(self):
        segments = pre_segment_chinese("", self.DEFAULT_CFG)
        assert segments == [""]

    def test_single_char(self):
        segments = pre_segment_chinese("好", self.DEFAULT_CFG)
        assert segments == ["好"]

    def test_multiple_conjunctions(self):
        """Text with multiple conjunctions should produce multiple segments."""
        text = "雖然他很累了但是他還是堅持跑步因為他想要保持健康所以每天都去運動"
        cfg = {"enabled": True, "min_segment_chars": 8, "max_segment_chars": 50}
        segments = pre_segment_chinese(text, cfg)
        assert len(segments) >= 3, f"Expected >=3 segments: {segments}"
        assert self._join(segments) == text
