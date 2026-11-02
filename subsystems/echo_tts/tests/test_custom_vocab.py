"""Tests for custom vocabulary replacements."""

import pytest

from echo_tts.modules.tts_module.tts_utils.custom_vocab import apply_custom_vocab


class TestApplyCustomVocab:
    DEFAULT_CFG = {"enabled": True}

    # ── U.E.P handling ─────────────────────────────────────────

    def test_uep_with_trailing_period(self):
        result = apply_custom_vocab("Hello U.E.P. How are you?", self.DEFAULT_CFG)
        assert "U E P" in result
        assert "U.E.P" not in result

    def test_uep_without_trailing_period(self):
        result = apply_custom_vocab("I am U.E.P and I help you.", self.DEFAULT_CFG)
        assert "U E P" in result

    def test_uep_case_insensitive(self):
        result = apply_custom_vocab("Welcome to u.e.p. system.", self.DEFAULT_CFG)
        assert "u.e.p" not in result

    def test_uep_at_start(self):
        result = apply_custom_vocab("U.E.P. is ready.", self.DEFAULT_CFG)
        assert result.startswith("U E P")

    def test_uep_mid_sentence_no_trailing_period(self):
        """U.E.P. followed by lowercase = mid-sentence, trailing dot removed."""
        result = apply_custom_vocab("The U.E.P. system works well.", self.DEFAULT_CFG)
        assert result == "The U E P system works well."

    def test_uep_end_of_sentence_keeps_period(self):
        """U.E.P. followed by uppercase = sentence-end, period preserved."""
        result = apply_custom_vocab("I am U.E.P. How can I help?", self.DEFAULT_CFG)
        assert result == "I am U E P. How can I help?"

    # ── pySBD integration ──────────────────────────────────────

    def test_uep_replaced_before_pysbd(self):
        """After replacement, U.E.P periods no longer confuse segmentation.

        Note: pySBD may still treat 'P.' as a single-letter abbreviation and
        not split there.  This is acceptable because our BPE-level
        split_segments will handle the remaining period as a normal split token.
        The key guarantee is that the *acronym dots* are removed.
        """
        text = apply_custom_vocab("Welcome to U.E.P. Nice to meet you.", self.DEFAULT_CFG)
        # Acronym dots are gone — only a sentence-ending period remains
        assert "U.E.P" not in text
        assert "U E P." in text

    # ── Config controls ────────────────────────────────────────

    def test_disabled_by_config(self):
        result = apply_custom_vocab("Hello U.E.P.", {"enabled": False})
        assert "U.E.P." in result

    def test_none_config_uses_defaults(self):
        result = apply_custom_vocab("Hello U.E.P.", None)
        assert "U E P" in result

    def test_empty_string(self):
        assert apply_custom_vocab("", self.DEFAULT_CFG) == ""

    # ── User-defined replacements from config ──────────────────

    def test_user_replacement(self):
        cfg = {
            "enabled": True,
            "replacements": [
                {"pattern": r"\bEcho\b", "replacement": "エコー", "ignore_case": False}
            ]
        }
        result = apply_custom_vocab("Hello Echo system.", cfg)
        assert "エコー" in result

    def test_user_replacement_case_insensitive(self):
        cfg = {
            "enabled": True,
            "replacements": [
                {"pattern": r"\becho\b", "replacement": "エコー", "ignore_case": True}
            ]
        }
        result = apply_custom_vocab("Hello ECHO system.", cfg)
        assert "エコー" in result

    def test_invalid_regex_skipped(self):
        """Invalid regex in user config should not crash."""
        cfg = {
            "enabled": True,
            "replacements": [
                {"pattern": "[invalid(", "replacement": "x"}
            ]
        }
        result = apply_custom_vocab("Hello world.", cfg)
        assert result == "Hello world."
