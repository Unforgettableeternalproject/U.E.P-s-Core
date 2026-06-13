"""
English sentence-level pre-segmentation using pySBD.

Splits English text into sentences BEFORE BPE tokenization, so that
abbreviations like "Mr.", "Dr.", "e.g.", "U.S.A." are correctly handled
and NOT treated as sentence boundaries.

Usage in lite_engine.py (before tokenize, when language != 'zh'):
    from .tts_utils.english_segmenter import pre_segment_english
    sub_texts = pre_segment_english(text, config)

Graceful degradation: if pySBD is not installed, returns [text] unchanged
and lets the existing BPE-level split_segments handle everything.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Optional pySBD import — never crash if unavailable
# ---------------------------------------------------------------------------
try:
    import pysbd as _pysbd
    PYSBD_AVAILABLE = True
    # Create a singleton segmenter (thread-safe, stateless)
    _segmenter = _pysbd.Segmenter(language="en", clean=False)
except ImportError:
    PYSBD_AVAILABLE = False
    _segmenter = None  # type: ignore[assignment]


def pre_segment_english(
    text: str,
    config: Optional[dict] = None,
) -> list[str]:
    """Split English text into sentences for TTS.

    Parameters
    ----------
    text:
        Raw text (before TextNormalizer, before BPE tokenization).
    config:
        Optional dict from ``config.yaml → chunking.english_segmentation``.
        Keys: ``enabled`` (bool).

    Returns
    -------
    list[str]
        Sentence-level sub-strings.
        If pySBD is unavailable or disabled, returns ``[text]``.
    """
    cfg = config or {}

    if not cfg.get("enabled", True):
        return [text]

    if not PYSBD_AVAILABLE:
        return [text]

    if not text or not text.strip():
        return [text]

    sentences = _segmenter.segment(text)

    # pySBD may return trailing whitespace per sentence; strip it
    # but preserve the original concatenation so no characters are lost.
    # We keep the whitespace attached to each sentence rather than stripping,
    # because TextNormalizer will handle cleanup.
    return [s for s in sentences if s]
