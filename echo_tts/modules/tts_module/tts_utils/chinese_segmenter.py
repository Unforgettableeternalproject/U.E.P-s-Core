"""
Chinese semantic pre-segmentation using jieba.

Splits long Chinese text at natural linguistic boundaries (particles,
conjunctions, exclamatory words) BEFORE BPE tokenization, so that
`split_segments_by_token` receives semantically coherent chunks rather
than hitting the hard token-count cutoff mid-phrase.

Usage in lite_engine.py (before tokenize):
    from .tts_utils.chinese_segmenter import pre_segment_chinese
    sub_texts = pre_segment_chinese(normalized_text, config)
    for sub in sub_texts:
        tokens = tokenizer.tokenize(sub)
        segments.extend(tokenizer.split_segments(tokens, ...))

Graceful degradation: if jieba is not installed, returns [text] unchanged.
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Optional jieba import — never crash if unavailable
# ---------------------------------------------------------------------------
try:
    import jieba.posseg as _pseg
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False
    _pseg = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Default config knobs (overridable via config.yaml → chunking.jieba_segmentation)
# ---------------------------------------------------------------------------
DEFAULT_MIN_SEGMENT_CHARS = 15
"""Don't bother splitting segments shorter than this."""

DEFAULT_MAX_SEGMENT_CHARS = 60
"""Force a split search when accumulated chars exceed this."""

# POS flags that signal a natural sentence boundary
# Based on ICTCLAS / PKU tagset used by jieba.posseg:
#   y  = 語氣詞 (吧, 啊, 哦, 嘛, 哈)
#   ul = 助詞「了」
#   uj = 助詞「的」(only when terminal)
#   c  = 連詞 (但, 而, 因為, 所以, 然後, 不過)
#   p  = 介詞 (在, 從, 把, 被, 向) — split before, not after
BOUNDARY_AFTER_FLAGS = {"y", "ul"}
"""Cut right after these POS flags unconditionally."""

BOUNDARY_AFTER_WORDS = {"了", "嗎", "啊", "吧", "呢", "哦", "嘛", "哈", "呀", "囉"}
"""Specific particles that always mark a boundary (regardless of POS tagging errors)."""

BOUNDARY_BEFORE_FLAGS = {"c"}
"""Cut right before words with these POS flags (conjunctions)."""

BOUNDARY_BEFORE_WORDS = {"但", "但是", "而", "而且", "然後", "因為", "所以", "不過", "雖然", "如果", "可是"}
"""Specific conjunctions where we split before."""


def _has_chinese(text: str) -> bool:
    """Return True if text contains any CJK character."""
    return bool(re.search(r"[一-鿿]", text))


def _segment_chinese_impl(
    text: str,
    min_segment_chars: int,
    max_segment_chars: int,
) -> list[str]:
    """Core segmentation logic using jieba.posseg.

    Returns a list of sub-strings whose concatenation equals the original text.
    """
    assert _pseg is not None

    words = list(_pseg.cut(text))
    if not words:
        return [text]

    segments: list[str] = []
    current_chars: list[str] = []
    current_len = 0

    for i, pair in enumerate(words):
        word, flag = pair.word, pair.flag

        # Check if we should split BEFORE this word
        should_split_before = (
            current_len >= min_segment_chars
            and (
                flag in BOUNDARY_BEFORE_FLAGS
                or word in BOUNDARY_BEFORE_WORDS
            )
        )

        if should_split_before and current_chars:
            segments.append("".join(current_chars))
            current_chars = []
            current_len = 0

        current_chars.append(word)
        current_len += len(word)

        # Check if we should split AFTER this word
        should_split_after = (
            current_len >= min_segment_chars
            and (
                flag in BOUNDARY_AFTER_FLAGS
                or word in BOUNDARY_AFTER_WORDS
            )
        )

        # Force split when segment gets too long — find any reasonable break
        force_split = current_len >= max_segment_chars

        if (should_split_after or force_split) and i < len(words) - 1:
            segments.append("".join(current_chars))
            current_chars = []
            current_len = 0

    # Flush remaining
    if current_chars:
        segments.append("".join(current_chars))

    # Merge very short trailing segments back into the previous one
    if len(segments) > 1 and len(segments[-1]) < min_segment_chars // 2:
        segments[-2] += segments[-1]
        segments.pop()

    return segments


def pre_segment_chinese(
    text: str,
    config: Optional[dict] = None,
) -> list[str]:
    """Split text into semantically coherent sub-strings for TTS.

    Parameters
    ----------
    text:
        Normalized text (after TextNormalizer, before tokenize_by_CJK_char).
    config:
        Optional dict from ``config.yaml → chunking.jieba_segmentation``.
        Keys: ``enabled``, ``min_segment_chars``, ``max_segment_chars``.

    Returns
    -------
    list[str]
        Sub-strings whose concatenation equals *text*.
        If jieba is unavailable or disabled, returns ``[text]``.
    """
    cfg = config or {}

    # Disabled by config
    if not cfg.get("enabled", True):
        return [text]

    # jieba not installed
    if not JIEBA_AVAILABLE:
        return [text]

    # Short text or no Chinese — skip
    if len(text) < cfg.get("min_segment_chars", DEFAULT_MIN_SEGMENT_CHARS):
        return [text]

    if not _has_chinese(text):
        return [text]

    return _segment_chinese_impl(
        text,
        min_segment_chars=cfg.get("min_segment_chars", DEFAULT_MIN_SEGMENT_CHARS),
        max_segment_chars=cfg.get("max_segment_chars", DEFAULT_MAX_SEGMENT_CHARS),
    )
