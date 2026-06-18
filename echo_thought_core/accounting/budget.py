"""
Token Budget — 預算管理與閾值判斷

實時追蹤 token 使用量，判斷是否需要觸發壓縮。
"""

import logging
from typing import Dict, Optional

from echo_thought_core.schemas.session import CompressionTier

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# TokenBudget 類
# ──────────────────────────────────────────────

class TokenBudget:
    """
    Token 預算管理器。

    原有功能：
    - 追蹤 system / history / tools / sacred 各部分 token 數
    - check_tier()：判斷應觸發的壓縮層級
    - get_usage_report()：取得使用量報告
    """

    def __init__(
        self,
        context_window: int = 200_000,
        thresholds: Optional[Dict[str, float]] = None,
        reserve_for_output: int = 4_096,
    ):
        self.context_window = context_window
        self.thresholds = thresholds or {
            "micro_prune": 0.60,
            "rolling_summary": 0.80,
            "full_compact": 0.92,
        }
        # 保留給輸出回應的 token 數（不計入輸入預算）
        self.reserve_for_output: int = reserve_for_output

        # ── 各組成部分的 token 追蹤（原有）────────────
        self._system_tokens: int = 0
        self._history_tokens: int = 0
        self._tool_tokens: int = 0
        self._sacred_tokens: int = 0

        # ── 實際 API 使用量追蹤 ────────────────────────
        # 由外部（如 Preflight 壓縮或 LLMClient 回調）寫入
        self._input_tokens_used: int = 0
        self._output_tokens_used: int = 0
        self.cache_read_tokens: int = 0
        self.cache_write_tokens: int = 0

    # ──────────────────────────────────────────
    # 原有 properties
    # ──────────────────────────────────────────

    @property
    def total_tokens(self) -> int:
        """總 token 使用量（system + history + tools + sacred）"""
        return (
            self._system_tokens
            + self._history_tokens
            + self._tool_tokens
            + self._sacred_tokens
        )

    @property
    def usage_percentage(self) -> float:
        """Token 使用率（相對於 context_window）"""
        if self.context_window == 0:
            return 0.0
        return self.total_tokens / self.context_window

    @property
    def available_tokens(self) -> int:
        """剩餘可用 token 數"""
        return max(0, self.context_window - self.total_tokens)

    # ──────────────────────────────────────────
    # 新增 properties（DW 合併）
    # ──────────────────────────────────────────

    @property
    def max_input_tokens(self) -> int:
        """最大可用輸入 token 數（context_window 扣除輸出保留）"""
        return max(0, self.context_window - self.reserve_for_output)

    @property
    def remaining_input_budget(self) -> int:
        """剩餘輸入預算（max_input_tokens 扣除已使用的輸入 tokens）"""
        return max(0, self.max_input_tokens - self.total_tokens)

    @property
    def remaining_output_budget(self) -> int:
        """剩餘輸出預算（reserve_for_output 扣除已使用的輸出 tokens）"""
        return max(0, self.reserve_for_output - self._output_tokens_used)

    # ──────────────────────────────────────────
    # 原有方法（保留不變）
    # ──────────────────────────────────────────

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

    def update_api_usage(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """更新 API 實際使用量（由 LLMClient 或 Preflight 呼叫）"""
        self._input_tokens_used = input_tokens
        self._output_tokens_used = output_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_write_tokens = cache_write_tokens

    def get_usage_report(self) -> Dict[str, object]:
        """取得使用量報告"""
        has_api_data = (
            self._input_tokens_used > 0 or self._output_tokens_used > 0
        )

        report: Dict[str, object] = {
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
            "budget": {
                "max_input_tokens": self.max_input_tokens,
                "remaining_input": self.remaining_input_budget,
                "remaining_output": self.remaining_output_budget,
                "reserve_for_output": self.reserve_for_output,
            },
        }

        # 僅在有實際 API 數據時才回傳 api_usage，避免誤導
        if has_api_data:
            report["api_usage"] = {
                "input_tokens": self._input_tokens_used,
                "output_tokens": self._output_tokens_used,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
            }

        return report

