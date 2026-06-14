"""
Token Budget — 預算管理與閾值判斷

實時追蹤 token 使用量，判斷是否需要觸發壓縮。
"""

from typing import Dict, Optional
from session_control.schemas.session import CompressionTier


class TokenBudget:
    """Token 預算管理器"""

    def __init__(
        self,
        context_window: int = 200000,
        thresholds: Optional[Dict[str, float]] = None,
    ):
        self.context_window = context_window
        self.thresholds = thresholds or {
            "micro_prune": 0.60,
            "rolling_summary": 0.80,
            "full_compact": 0.92,
        }

        # 追蹤各組成部分的 token 數
        self._system_tokens: int = 0
        self._history_tokens: int = 0
        self._tool_tokens: int = 0
        self._sacred_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """總 token 使用量"""
        return (
            self._system_tokens
            + self._history_tokens
            + self._tool_tokens
            + self._sacred_tokens
        )

    @property
    def usage_percentage(self) -> float:
        """Token 使用率"""
        if self.context_window == 0:
            return 0.0
        return self.total_tokens / self.context_window

    @property
    def available_tokens(self) -> int:
        """剩餘可用 token 數"""
        return max(0, self.context_window - self.total_tokens)

    def update(
        self,
        system: Optional[int] = None,
        history: Optional[int] = None,
        tools: Optional[int] = None,
        sacred: Optional[int] = None,
    ) -> None:
        """更新各部分的 token 數"""
        if system is not None:
            self._system_tokens = system
        if history is not None:
            self._history_tokens = history
        if tools is not None:
            self._tool_tokens = tools
        if sacred is not None:
            self._sacred_tokens = sacred

    def check_tier(self) -> CompressionTier:
        """根據當前使用量判斷應觸發的壓縮層級"""
        pct = self.usage_percentage

        if pct >= self.thresholds["full_compact"]:
            return CompressionTier.TIER_3_FULL_COMPACTION
        elif pct >= self.thresholds["rolling_summary"]:
            return CompressionTier.TIER_2_ROLLING_SUMMARY
        elif pct >= self.thresholds["micro_prune"]:
            return CompressionTier.TIER_1_MICRO_PRUNE
        else:
            return CompressionTier.TIER_0_NORMAL

    def get_usage_report(self) -> Dict[str, object]:
        """取得使用量報告"""
        return {
            "total_tokens": self.total_tokens,
            "context_window": self.context_window,
            "usage_percentage": round(self.usage_percentage, 4),
            "available_tokens": self.available_tokens,
            "current_tier": self.check_tier().name,
            "breakdown": {
                "system": self._system_tokens,
                "history": self._history_tokens,
                "tools": self._tool_tokens,
                "sacred": self._sacred_tokens,
            },
        }
