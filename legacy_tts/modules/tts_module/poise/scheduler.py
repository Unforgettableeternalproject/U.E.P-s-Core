"""
PoiseScheduler — 觸發時機決策器

決定在哪些段落位置插入休息段，核心邏輯：
- Count 為基礎間隔，帶 jitter 隨機抖動
- 總段數 ≤ min_segments_to_trigger 時不觸發
- 首段和最後段不觸發
- 兩次休息之間至少累積 min_accumulation 段
"""

import random
from typing import Optional

from .config import PoiseConfig
from .state import PoiseState


class PoiseScheduler:
    """
    Determines WHEN to insert poise rest segments.

    Uses a frequency-based model (count) with jitter, respecting
    hard constraints on minimum accumulation and boundary protection.
    """

    def __init__(self, config: PoiseConfig, seed: Optional[int] = None):
        """
        Args:
            config: Poise configuration.
            seed: Optional random seed for reproducible scheduling (testing).
        """
        self.config = config
        self._rng = random.Random(seed)
        # Pre-compute the next trigger point
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

        Args:
            seg_idx: Current segment index (0-based).
            total_segments: Total number of content segments.
            state: Current poise state (accumulation counters).

        Returns:
            True if a rest should be inserted after this segment.
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

        # Hard constraint: protect the tail — don't rest if fewer than 2
        # content segments remain after this one
        remaining = total_segments - 1 - seg_idx
        if remaining < 2:
            return False

        # Hard constraint: minimum accumulation since last rest
        if state.accumulated_segments < self.config.min_accumulation:
            return False

        # Soft trigger: have we passed the scheduled trigger point?
        if self._next_trigger_at is not None and seg_idx >= self._next_trigger_at:
            # Schedule the next trigger from after this rest
            self._schedule_next_trigger(start_from=seg_idx + 1)
            return True

        return False

    def reset(self, seed: Optional[int] = None) -> None:
        """Reset scheduler state for a new synthesis run."""
        if seed is not None:
            self._rng = random.Random(seed)
        self._next_trigger_at = None
        self._schedule_next_trigger(start_from=0)

    def preview_triggers(self, total_segments: int) -> list[int]:
        """
        Dry-run the scheduler to preview where triggers would fire.

        Useful for debugging and testing. Does not modify internal state.

        Returns:
            List of segment indices after which a rest would be inserted.
        """
        # Save and restore state
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

        # Restore
        self._next_trigger_at = saved_trigger
        self._rng.setstate(saved_rng_state)
        return triggers
