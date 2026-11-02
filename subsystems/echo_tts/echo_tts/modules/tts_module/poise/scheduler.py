"""
PoiseScheduler — 觸發時機決策器

決定在哪些段落位置插入休息段，核心邏輯：
- Count 為基礎間隔，帶 jitter 隨機抖動
- 總段數 ≤ min_segments_to_trigger 時不觸發
- 首段和最後段不觸發
- 兩次休息之間至少累積 min_accumulation 段
- 優先在句子結束點（句號、問號、驚嘆號）觸發
"""

import random
from typing import List, Optional

from .config import PoiseConfig
from .state import PoiseState


# Sentence-ending punctuation (both CJK and ASCII)
_SENTENCE_ENDINGS = set("。！？.!?")

# Maximum number of segments to look ahead for a sentence boundary
_MAX_LOOKAHEAD = 3


def is_sentence_boundary(seg_tokens: list) -> bool:
    """Check if a segment's tokens end at a sentence boundary.

    Looks at the last few tokens for sentence-ending punctuation.
    """
    if not seg_tokens:
        return False
    # Check last 3 tokens (punctuation might be a separate token)
    tail = seg_tokens[-3:] if len(seg_tokens) >= 3 else seg_tokens
    text = "".join(tail).replace("▁", " ").strip()
    return any(text.endswith(ch) for ch in _SENTENCE_ENDINGS)


class PoiseScheduler:
    """
    Determines WHEN to insert poise rest segments.

    Uses a frequency-based model (count) with jitter, respecting
    hard constraints on minimum accumulation and boundary protection.
    Prefers sentence boundaries over mid-sentence positions.
    """

    def __init__(self, config: PoiseConfig, seed: Optional[int] = None):
        self.config = config
        self._rng = random.Random(seed)
        self._next_trigger_at: Optional[float] = None
        self._schedule_next_trigger(start_from=0)

    def _schedule_next_trigger(self, start_from: int) -> None:
        """Calculate the next trigger point with jitter."""
        jitter_factor = 1.0 + self.config.count_jitter * self._rng.uniform(-1.0, 1.0)
        interval = self.config.count * jitter_factor
        self._next_trigger_at = start_from + interval

    def should_trigger(
        self,
        seg_idx: int,
        total_segments: int,
        state: PoiseState,
    ) -> bool:
        """
        Decide whether to insert a rest AFTER segment seg_idx.

        This is the basic check without sentence-boundary awareness.
        Use plan_with_boundaries() for sentence-aware scheduling.
        """
        # Hard constraint: system disabled
        if not self.config.enabled:
            return False

        # Hard constraint: too few segments overall
        if total_segments <= self.config.min_segments_to_trigger:
            return False

        # Hard constraint: never rest after the very first segment
        if seg_idx == 0:
            return False

        # Hard constraint: protect the tail
        remaining = total_segments - 1 - seg_idx
        if remaining < 2:
            return False

        # Hard constraint: minimum accumulation since last rest
        if state.accumulated_segments < self.config.min_accumulation:
            return False

        # Soft trigger: have we passed the scheduled trigger point?
        if self._next_trigger_at is not None and seg_idx >= self._next_trigger_at:
            self._schedule_next_trigger(start_from=seg_idx + 1)
            return True

        return False

    def find_best_trigger(
        self,
        seg_idx: int,
        total_segments: int,
        state: PoiseState,
        segments: List[list],
    ) -> int:
        """
        Find the best segment index to trigger a rest, preferring sentence
        boundaries. Returns -1 if no suitable position found.

        When the scheduler wants to trigger at seg_idx, this method checks:
        1. Is seg_idx itself a sentence boundary? → use it
        2. Look ahead up to _MAX_LOOKAHEAD segments for a boundary
        3. If no boundary found, use the original seg_idx (fallback)

        Args:
            seg_idx: The segment where the scheduler first wants to trigger.
            total_segments: Total content segment count.
            state: Current poise state.
            segments: The actual token lists for content segments.

        Returns:
            The adjusted segment index, or -1 if trigger should be skipped.
        """
        # Check current position first
        if seg_idx < len(segments) and is_sentence_boundary(segments[seg_idx]):
            return seg_idx

        # Look ahead for a sentence boundary
        for offset in range(1, _MAX_LOOKAHEAD + 1):
            candidate = seg_idx + offset
            # Safety checks
            remaining = total_segments - 1 - candidate
            if candidate >= total_segments or remaining < 2:
                break
            if candidate < len(segments) and is_sentence_boundary(segments[candidate]):
                return candidate

        # Fallback: use original position (still better than skipping entirely)
        return seg_idx

    def reset(self, seed: Optional[int] = None) -> None:
        """Reset scheduler state for a new synthesis run."""
        if seed is not None:
            self._rng = random.Random(seed)
        self._next_trigger_at = None
        self._schedule_next_trigger(start_from=0)

    def preview_triggers(self, total_segments: int) -> list[int]:
        """Dry-run the scheduler to preview where triggers would fire."""
        saved_trigger = self._next_trigger_at
        saved_rng_state = self._rng.getstate()

        self._schedule_next_trigger(start_from=0)
        state = PoiseState()
        triggers = []

        for seg_idx in range(total_segments):
            state.accumulate()
            if self.should_trigger(seg_idx, total_segments, state):
                triggers.append(seg_idx)
                state.mark_rest(seg_idx)

        self._next_trigger_at = saved_trigger
        self._rng.setstate(saved_rng_state)
        return triggers
