"""
Dynamic max_generate_length calculation for GPT AR decoding.

Instead of using a global max_mel_tokens (1815) for every segment regardless
of text length, this module computes a per-segment upper bound based on the
actual number of text tokens in the segment.

This prevents GPT from "running away" — generating excessively long audio
for short text segments.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Default tuning knobs (overridable via config.yaml → gpt.dynamic_length.*)
# ---------------------------------------------------------------------------
DEFAULT_MEL_PER_TOKEN_RATIO: float = 10.0
"""Empirical ratio: ~10 mel tokens per text BPE token on average."""

DEFAULT_SAFETY_MARGIN: int = 150
"""Extra mel tokens above the ratio-based estimate, absorbs variance in
speaking rate and prosody."""

DEFAULT_MIN_GENERATE_LENGTH: int = 300
"""Absolute floor.  Even for very short text (1–2 tokens), allow at least
this many mel tokens so the model can produce a complete utterance."""


def calc_dynamic_max_generate_length(
    num_text_tokens: int,
    cfg_max_mel_tokens: int,
    *,
    mel_per_token_ratio: float = DEFAULT_MEL_PER_TOKEN_RATIO,
    safety_margin: int = DEFAULT_SAFETY_MARGIN,
    min_generate_length: int = DEFAULT_MIN_GENERATE_LENGTH,
) -> int:
    """Compute a reasonable ``max_generate_length`` for one segment.

    Parameters
    ----------
    num_text_tokens:
        Number of BPE text tokens in the current segment.
    cfg_max_mel_tokens:
        Global ceiling from ``config.yaml → gpt.max_mel_tokens`` (1815).
    mel_per_token_ratio:
        Expected mel-tokens per text-token.  Default 10.0.
    safety_margin:
        Extra tokens added on top of the ratio-based estimate.
    min_generate_length:
        Absolute floor to avoid cutting off very short utterances.

    Returns
    -------
    int
        Clamped to ``[min_generate_length, cfg_max_mel_tokens]``.
    """
    ratio_based = int(num_text_tokens * mel_per_token_ratio) + safety_margin
    return max(min_generate_length, min(ratio_based, cfg_max_mel_tokens))
