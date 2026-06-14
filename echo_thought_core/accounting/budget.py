"""
Token Budget — 預算管理與閾值判斷

實時追蹤 token 使用量，判斷是否需要觸發壓縮。
同時提供成本估算、剩餘輸入/輸出預算計算，以及摘要/尾部保護預算工具函數。
"""

import logging
from typing import Dict, Optional, Tuple

from echo_thought_core.schemas.session import CompressionTier

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 預算工具函數（獨立，不依賴 TokenBudget 實例）
# ──────────────────────────────────────────────

def calculate_context_threshold(
    context_length: int,
    threshold_percent: float = 0.80,
) -> int:
    """
    計算壓縮觸發閾值（token 數）。

    最小保障 64K tokens，避免短 context 模型過早觸發。
    """
    raw = int(context_length * threshold_percent)
    return max(raw, 65_536)  # 最小 64K


def calculate_tail_budget(
    threshold_tokens: int,
    ratio: float = 0.20,
) -> int:
    """
    計算尾部保護預算（token 數）。

    尾部保護佔閾值的一定比例，確保最近對話不會被壓縮捨棄。
    """
    return int(threshold_tokens * ratio)


def calculate_summary_budget(
    compressed_tokens: int,
    ratio: float = 0.20,
    min_tokens: int = 2_000,
    max_tokens: int = 12_000,
) -> int:
    """
    計算摘要預算（token 數）。

    壓縮後內容的一定比例用於摘要，並夾在 [min_tokens, max_tokens] 區間內。
    """
    raw = int(compressed_tokens * ratio)
    return max(min_tokens, min(raw, max_tokens))


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

    新增功能（來自 Destiny-Weavers 合併）：
    - reserve_for_output：保留給輸出的 token 預算
    - max_input_tokens / remaining_input_budget / remaining_output_budget
    - can_fit()：判斷是否容得下新的 tokens
    - record_usage()：記錄實際 API 使用量（含 cache tokens）
    - estimate_cost()：依模型資訊估算費用
    - should_compress()：判斷是否達到壓縮閾值
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

        # ── 實際 API 使用量追蹤（新增）───────────────
        self._input_tokens_used: int = 0
        self._output_tokens_used: int = 0
        self.cache_read_tokens: int = 0    # 從快取讀取的 tokens
        self.cache_write_tokens: int = 0   # 寫入快取的 tokens

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

    def get_usage_report(self) -> Dict[str, object]:
        """取得使用量報告（含新增的 API 使用量與快取資訊）"""
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
            # 新增欄位
            "budget": {
                "max_input_tokens": self.max_input_tokens,
                "remaining_input": self.remaining_input_budget,
                "remaining_output": self.remaining_output_budget,
                "reserve_for_output": self.reserve_for_output,
            },
            "api_usage": {
                "input_tokens": self._input_tokens_used,
                "output_tokens": self._output_tokens_used,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
            },
        }

    # ──────────────────────────────────────────
    # 新增方法（DW 合併）
    # ──────────────────────────────────────────

    def can_fit(self, estimated_tokens: int, is_output: bool = False) -> bool:
        """
        判斷新的 tokens 是否能放入當前預算。

        Args:
            estimated_tokens: 預估的 token 數
            is_output: True 表示是輸出 tokens，False 表示是輸入 tokens
        """
        if is_output:
            return estimated_tokens <= self.remaining_output_budget
        return estimated_tokens <= self.remaining_input_budget

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """
        記錄一次 API 呼叫的實際 token 使用量。

        Args:
            input_tokens: 實際輸入 token 數
            output_tokens: 實際輸出 token 數
            cache_read_tokens: 從提示快取讀取的 token 數
            cache_write_tokens: 寫入提示快取的 token 數
        """
        self._input_tokens_used += input_tokens
        self._output_tokens_used += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cache_write_tokens += cache_write_tokens

        logger.debug(
            "記錄 API 使用量：input=%d, output=%d, cache_read=%d, cache_write=%d",
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
        )

    def estimate_cost(self, model_info: Optional[object] = None) -> Tuple[float, float]:
        """
        估算本次累積使用的費用。

        Args:
            model_info: ModelInfo 實例（來自 counter.py 的 get_model_info()）

        Returns:
            (input_cost_usd, output_cost_usd) 的 tuple
            若 model_info 為 None，返回 (0.0, 0.0)
        """
        if model_info is None:
            logger.debug("estimate_cost：未提供 model_info，返回 (0.0, 0.0)")
            return (0.0, 0.0)

        # 快取讀取通常有折扣（約 0.1x），寫入無折扣
        # 這裡假設 model_info 有 input_cost_per_1m 和 output_cost_per_1m
        try:
            rate_in = model_info.input_cost_per_1m / 1_000_000
            rate_out = model_info.output_cost_per_1m / 1_000_000

            # 快取讀取按 10% 計費，寫入按正常輸入計費
            effective_input = (
                (self._input_tokens_used - self.cache_read_tokens - self.cache_write_tokens)
                + self.cache_write_tokens
                + self.cache_read_tokens * 0.1
            )
            effective_input = max(0.0, effective_input)

            input_cost = effective_input * rate_in
            output_cost = self._output_tokens_used * rate_out

            return (round(input_cost, 6), round(output_cost, 6))

        except (AttributeError, TypeError) as e:
            logger.warning("estimate_cost 計算失敗：%s", e)
            return (0.0, 0.0)

    def should_compress(self, threshold: Optional[float] = None) -> bool:
        """
        判斷是否達到壓縮閾值。

        Args:
            threshold: 自訂閾值百分比（0~1）。若為 None，使用 rolling_summary 閾值。
        """
        if threshold is None:
            threshold = self.thresholds.get("rolling_summary", 0.80)
        return self.usage_percentage >= threshold

    def get_status(self) -> Dict[str, object]:
        """
        取得完整狀態快照（含預算、API 使用量、快取追蹤）。
        比 get_usage_report() 更詳細，適合監控用途。
        """
        input_cost, output_cost = self.estimate_cost()
        return {
            **self.get_usage_report(),
            "compression_needed": self.should_compress(),
            "estimated_cost_usd": {
                "input": input_cost,
                "output": output_cost,
                "total": round(input_cost + output_cost, 6),
            },
        }
