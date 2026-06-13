"""
Custom vocabulary replacements for TTS text normalization.

Handles project-specific terms, acronyms, and proper nouns that the
TTS model cannot pronounce correctly from their written form.

Replacements are applied BEFORE sentence segmentation and BPE tokenization
so that:
  1. Acronyms with periods (U.E.P.) don't confuse sentence boundary detection
  2. The TTS model receives a pronounceable form

Usage:
    from .tts_utils.custom_vocab import apply_custom_vocab
    text = apply_custom_vocab(text, config)
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Built-in replacements (always active)
# ---------------------------------------------------------------------------
# Each entry: (pattern, replacement, flags)
# Pattern is a regex; replacement is a plain string.
# Order matters: longer / more specific patterns should come first.
_BUILTIN_REPLACEMENTS: list[tuple[str, str, int]] = [
    # U.E.P. at end of sentence:
    #   followed by space + uppercase letter, or end-of-string
    #   → "U E P." (preserve the sentence-ending period)
    # Case-sensitive lookahead [A-Z] must NOT be affected by IGNORECASE,
    # so we match the acronym part case-insensitively via [Uu].[Ee].[Pp].
    (r"\b[Uu]\.[Ee]\.[Pp]\.(?=\s+[A-Z]|\s*$)", "U E P.", 0),
    # U.E.P. mid-sentence (followed by lowercase continuation)
    #   → "U E P" (trailing dot was part of the acronym, not sentence-end)
    (r"\bU\.E\.P\.", "U E P", re.IGNORECASE),
    # U.E.P without trailing period
    (r"\bU\.E\.P\b", "U E P", re.IGNORECASE),
]


def apply_custom_vocab(
    text: str,
    config: Optional[dict] = None,
) -> str:
    """Apply custom vocabulary replacements to text.

    Parameters
    ----------
    text:
        Raw input text (before any normalization or tokenization).
    config:
        Optional dict from ``config.yaml → text_processing.custom_vocab``.
        Keys:
            ``enabled`` (bool): Master switch.  Default True.
            ``replacements`` (list[dict]): Additional user-defined replacements.
                Each dict: ``{"pattern": str, "replacement": str, "ignore_case": bool}``

    Returns
    -------
    str
        Text with custom vocab applied.
    """
    cfg = config or {}

    if not cfg.get("enabled", True):
        return text

    if not text:
        return text

    # Apply built-in replacements
    for pattern, replacement, flags in _BUILTIN_REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=flags)

    # Apply user-defined replacements from config
    user_replacements = cfg.get("replacements", [])
    for entry in user_replacements:
        pat = entry.get("pattern", "")
        repl = entry.get("replacement", "")
        flags = re.IGNORECASE if entry.get("ignore_case", False) else 0
        if pat:
            try:
                text = re.sub(pat, repl, text, flags=flags)
            except re.error:
                pass  # Skip invalid regex patterns silently

    return text
