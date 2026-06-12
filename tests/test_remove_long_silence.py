"""Tests for GPT token-level silence removal."""

import torch
import pytest

from legacy_tts.modules.tts_module.lite_engine import IndexTTSLite


class TestRemoveLongSilence:
    """Unit tests for _remove_long_silence static method."""

    STOP = 8193
    SILENT = 52

    def test_no_modification_when_few_silences(self):
        """Below threshold: codes unchanged."""
        codes = torch.tensor([[10, 20, 52, 30, 52, 40, self.STOP]])
        new_codes, lens = IndexTTSLite._remove_long_silence(
            codes, stop_mel_token=self.STOP, silent_token=self.SILENT,
            max_consecutive=30, keep_consecutive=10,
        )
        # Only 2 silent tokens, below threshold 30 → should just trim to stop token
        assert lens[0].item() == 6  # [10, 20, 52, 30, 52, 40] (stop excluded)
        assert new_codes.shape[1] == 6

    def test_compresses_long_silence_run(self):
        """Many consecutive silent tokens should be compressed."""
        # 5 normal tokens + 50 silences + 5 normal + stop
        normal1 = [10, 20, 30, 40, 50]
        silences = [self.SILENT] * 50
        normal2 = [60, 70, 80, 90, 100]
        codes = torch.tensor([normal1 + silences + normal2 + [self.STOP]])

        new_codes, lens = IndexTTSLite._remove_long_silence(
            codes, stop_mel_token=self.STOP, silent_token=self.SILENT,
            max_consecutive=30, keep_consecutive=10,
        )
        # 50 silences should be compressed to 10
        expected_len = 5 + 10 + 5  # normal1 + compressed_silences + normal2
        assert lens[0].item() == expected_len
        assert new_codes.shape[1] == expected_len

    def test_preserves_non_silent_tokens(self):
        """Normal tokens should not be removed."""
        normal = list(range(10, 60))  # 50 non-silent tokens
        codes = torch.tensor([normal + [self.STOP]])

        new_codes, lens = IndexTTSLite._remove_long_silence(
            codes, stop_mel_token=self.STOP, silent_token=self.SILENT,
        )
        assert lens[0].item() == 50
        assert torch.equal(new_codes[0], torch.tensor(normal))

    def test_no_stop_token(self):
        """Codes without stop token should use full length."""
        codes = torch.tensor([[10, 20, 30, self.SILENT, 40]])
        new_codes, lens = IndexTTSLite._remove_long_silence(
            codes, stop_mel_token=self.STOP, silent_token=self.SILENT,
        )
        assert lens[0].item() == 5

    def test_all_silence(self):
        """All-silence codes should be compressed heavily."""
        codes = torch.tensor([[self.SILENT] * 100 + [self.STOP]])
        new_codes, lens = IndexTTSLite._remove_long_silence(
            codes, stop_mel_token=self.STOP, silent_token=self.SILENT,
            max_consecutive=30, keep_consecutive=10,
        )
        # 100 silences → keep first 10
        assert lens[0].item() == 10

    def test_custom_keep_consecutive(self):
        """Custom keep_consecutive should be respected."""
        silences = [self.SILENT] * 50
        codes = torch.tensor([[10] + silences + [20, self.STOP]])
        new_codes, lens = IndexTTSLite._remove_long_silence(
            codes, stop_mel_token=self.STOP, silent_token=self.SILENT,
            max_consecutive=5, keep_consecutive=3,
        )
        # 50 silences → keep 3
        expected = 1 + 3 + 1  # [10] + 3 silences + [20]
        assert lens[0].item() == expected
