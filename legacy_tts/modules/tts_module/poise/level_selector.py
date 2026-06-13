"""
PoiseLevelSelector — 層級決策器

根據 Potency 和累積狀態決定休息段的強度層級。

核心邏輯：
- Potency 決定層級上限（0.0 → 只有 SILENCE，1.0 → 可達 ACTION）
- 累積段數提供 bonus（越久沒休息 → 越可能觸發較重的休息）
- 加入隨機 jitter 避免機械感
"""

import random
from typing import Optional

from .config import PoiseConfig
from .level import PoiseLevel
from .state import PoiseState


class PoiseLevelSelector:
    """
    Determines WHAT level of rest to insert at a given trigger point.
    """

    # Accumulation thresholds: every N segments adds +1 level bonus
    ACCUMULATION_STEP = 5

    # Maximum bonus from accumulation (prevents runaway escalation)
    MAX_ACCUMULATION_BONUS = 2

    def __init__(self, config: PoiseConfig, seed: Optional[int] = None):
        self.config = config
        self._rng = random.Random(seed)

    def select(self, state: PoiseState) -> PoiseLevel:
        """
        Select the rest level based on potency and accumulated state.

        Args:
            state: Current poise state with accumulation info.

        Returns:
            The PoiseLevel to use for this rest segment.
        """
        # Base level from potency (0.0→0, 0.25→1, 0.5→2, 0.75→3, 1.0→4)
        max_level = int(self.config.potency * len(PoiseLevel))
        max_level = min(max_level, len(PoiseLevel) - 1)

        # Accumulation bonus: longer stretches → heavier rest
        acc_bonus = min(
            state.accumulated_segments // self.ACCUMULATION_STEP,
            self.MAX_ACCUMULATION_BONUS,
        )

        # Raw level before jitter
        raw_level = min(max_level, acc_bonus)

        # If potency allows higher and accumulation justifies, allow upgrade
        if acc_bonus > 0 and raw_level < max_level:
            raw_level = min(raw_level + acc_bonus, max_level)

        # Jitter: ±1 level with probability based on potency_jitter
        jitter = 0
        if self.config.potency_jitter > 0:
            roll = self._rng.random()
            if roll < self.config.potency_jitter * 0.5:
                jitter = -1
            elif roll > 1.0 - self.config.potency_jitter * 0.5:
                jitter = +1

        final_level = max(0, min(raw_level + jitter, max_level))

        return PoiseLevel(final_level)

    def reset(self, seed: Optional[int] = None) -> None:
        """Reset RNG for a new synthesis run."""
        if seed is not None:
            self._rng = random.Random(seed)
