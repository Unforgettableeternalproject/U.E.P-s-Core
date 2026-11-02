"""
PoiseState — 執行期狀態追蹤器

追蹤合成過程中的累積狀態，供 Scheduler 和 LevelSelector 使用。
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class PoiseState:
    """
    Poise 系統的執行期狀態。

    在段落迴圈中隨每段更新，記錄距上次休息的累積量。

    Attributes:
        last_rest_at_idx: 上次休息段的 segment index（-1 = 尚未休息過）
        accumulated_segments: 距上次休息累積的正常段數
        accumulated_duration_s: 距上次休息累積的估算音頻時長（秒）
        rest_history: 所有已觸發休息的 segment index 列表
        total_rests: 已插入的休息段總數
    """
    last_rest_at_idx: int = -1
    accumulated_segments: int = 0
    accumulated_duration_s: float = 0.0
    rest_history: List[int] = field(default_factory=list)
    total_rests: int = 0

    def accumulate(self, segment_duration_s: float = 0.0) -> None:
        """
        Record that a normal segment has been processed.

        Args:
            segment_duration_s: Estimated audio duration of this segment (seconds).
                                Can be 0 if not yet known (pre-planning phase).
        """
        self.accumulated_segments += 1
        self.accumulated_duration_s += segment_duration_s

    def mark_rest(self, at_idx: int) -> None:
        """
        Record that a rest was inserted at the given segment index.

        Resets the accumulation counters.
        """
        self.last_rest_at_idx = at_idx
        self.accumulated_segments = 0
        self.accumulated_duration_s = 0.0
        self.rest_history.append(at_idx)
        self.total_rests += 1

    def reset(self) -> None:
        """Reset all state for a new synthesis run."""
        self.last_rest_at_idx = -1
        self.accumulated_segments = 0
        self.accumulated_duration_s = 0.0
        self.rest_history.clear()
        self.total_rests = 0

    @property
    def has_rested(self) -> bool:
        """Whether any rest has been taken."""
        return self.total_rests > 0

    def __repr__(self) -> str:
        return (
            f"PoiseState(acc_segs={self.accumulated_segments}, "
            f"acc_dur={self.accumulated_duration_s:.1f}s, "
            f"rests={self.total_rests}, "
            f"last_rest_at={self.last_rest_at_idx})"
        )
