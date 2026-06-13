"""Tests for English sentence pre-segmentation using pySBD."""

import pytest

from echo_tts.modules.tts_module.tts_utils.english_segmenter import (
    pre_segment_english,
    PYSBD_AVAILABLE,
)

pytestmark = pytest.mark.skipif(
    not PYSBD_AVAILABLE, reason="pySBD not installed"
)


class TestPreSegmentEnglish:
    DEFAULT_CFG = {"enabled": True}

    def test_basic_sentence_split(self):
        text = "Hello world. How are you? I am fine!"
        result = pre_segment_english(text, self.DEFAULT_CFG)
        assert len(result) == 3

    def test_abbreviation_mr_not_split(self):
        """Mr. should NOT be treated as sentence boundary."""
        text = "Hello Mr. Smith. How are you?"
        result = pre_segment_english(text, self.DEFAULT_CFG)
        assert len(result) == 2
        assert "Mr. Smith." in result[0]

    def test_abbreviation_dr_not_split(self):
        text = "Dr. Jones said hello. Then he left."
        result = pre_segment_english(text, self.DEFAULT_CFG)
        assert len(result) == 2
        assert "Dr. Jones" in result[0]

    def test_abbreviation_eg_not_split(self):
        text = "Use tools e.g. a hammer. Also use i.e. a screwdriver."
        result = pre_segment_english(text, self.DEFAULT_CFG)
        assert len(result) == 2
        assert "e.g." in result[0]

    def test_usa_abbreviation(self):
        text = "The U.S.A. is a country. It is large."
        result = pre_segment_english(text, self.DEFAULT_CFG)
        assert len(result) == 2
        assert "U.S.A." in result[0]

    def test_no_punctuation_returns_whole(self):
        """Text without sentence-ending punctuation stays as one segment."""
        text = "This is a long sentence without any ending punctuation and it goes on"
        result = pre_segment_english(text, self.DEFAULT_CFG)
        assert len(result) == 1
        assert result[0] == text

    def test_concatenation_preserves_content(self):
        """Joined result should contain all original content."""
        text = "First sentence. Second one! Third? Fourth."
        result = pre_segment_english(text, self.DEFAULT_CFG)
        joined = "".join(result)
        # pySBD may add/keep trailing spaces, but all words should be present
        for word in text.split():
            assert word.rstrip(".,!?") in joined or word in joined

    def test_disabled_by_config(self):
        text = "Hello world. How are you?"
        result = pre_segment_english(text, {"enabled": False})
        assert result == [text]

    def test_none_config_uses_defaults(self):
        text = "Hello world. How are you?"
        result = pre_segment_english(text, None)
        assert len(result) == 2

    def test_empty_string(self):
        assert pre_segment_english("", self.DEFAULT_CFG) == [""]

    def test_single_sentence(self):
        text = "Just one sentence."
        result = pre_segment_english(text, self.DEFAULT_CFG)
        assert len(result) == 1

    def test_ellipsis_handling(self):
        """Ellipsis should not cause excessive splits."""
        text = "Wait... I think... maybe not. Let us go."
        result = pre_segment_english(text, self.DEFAULT_CFG)
        # Should be 2-3 segments, not one per dot
        assert len(result) <= 4
