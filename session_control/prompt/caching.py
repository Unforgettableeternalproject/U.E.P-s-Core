"""
Prompt 快取控制模組

這是 Prompt 面向的核心模組之一，負責 LLM API 層的 prompt cache 優化。
實作 Anthropic 4-breakpoint prompt caching 策略，透過在特定位置注入
cache_control 標記，大幅降低重複 token 的 API 費用。

從 Destiny-Weavers (DW) 的 prompt_caching.py 遷入並改造：
- CacheConfig → PromptCacheConfig（避免與 cache/local_cache.py 混淆）
- 支援 session_control.schemas.message.Message 型別與 raw dict 雙路徑
- 使用標準 logging 模組
- 更新至 2025/2026 年最新 Claude 定價

4 個快取斷點位置：
  BP1 — System prompt
  BP2 — Tools 定義（作為 API 參數，不在 messages 列表中）
  BP3 — 倒數第三條 assistant 訊息
  BP4 — 倒數第二條 assistant 訊息
"""

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from session_control.schemas.message import Message

logger = logging.getLogger(__name__)

# ── 定價常數（2025/2026，Claude Sonnet 系列，每百萬 token）──────────────────

PRICE_NORMAL_PER_M: float = 3.0        # 一般輸入 token
PRICE_CACHE_WRITE_PER_M: float = 3.75  # 快取寫入（+25%）
PRICE_CACHE_READ_PER_M: float = 0.30   # 快取讀取（-90%）


# ── PromptCacheConfig ─────────────────────────────────────────────────────────


@dataclass
class PromptCacheConfig:
    """Prompt 快取配置

    控制各個快取斷點的啟用狀態與門檻值。
    預設值針對一般對話場景最佳化。
    """

    enabled: bool = True
    """是否啟用 prompt caching"""

    min_cacheable_tokens: int = 1024
    """最小可快取 token 數（低於此值不標記，避免無效快取寫入）"""

    max_breakpoints: int = 4
    """最多使用的快取斷點數（Anthropic 上限為 4）"""

    cache_system_prompt: bool = True
    """BP1：是否快取 system prompt"""

    cache_tools: bool = True
    """BP2：是否快取 tools 定義"""

    cache_history: bool = True
    """BP3 & BP4：是否快取對話歷史中的 assistant 訊息"""

    history_keep_recent: int = 2
    """最近 N 條訊息不加快取標記（保持動態，避免快取污染最新輪次）"""


# ── 核心工具函數 ──────────────────────────────────────────────────────────────


def add_cache_control(
    content: Union[str, list, dict],
    cache_type: str = "ephemeral",
) -> list:
    """為內容加上 cache_control 標記

    支援兩種輸入格式：

    - str：轉換為 Anthropic content block 列表，並在唯一元素上加 cache_control
    - list：deepcopy 後在最後一個元素加 cache_control（支援 Anthropic 格式）
    - dict：視為單一 content block，包裹為列表後加 cache_control

    Args:
        content: 要標記的內容，可為字串、content block 列表或單一 block dict
        cache_type: 快取類型，目前 Anthropic 僅支援 "ephemeral"

    Returns:
        帶有 cache_control 標記的 content block 列表
    """
    cache_marker = {"type": cache_type}

    if isinstance(content, str):
        # 字串路徑：轉為 Anthropic text block 並直接標記
        return [
            {
                "type": "text",
                "text": content,
                "cache_control": cache_marker,
            }
        ]

    if isinstance(content, dict):
        # 單一 block：包裹為列表後標記
        block = copy.deepcopy(content)
        block["cache_control"] = cache_marker
        return [block]

    if isinstance(content, list):
        if not content:
            logger.warning("add_cache_control 收到空列表，略過標記")
            return content

        blocks = copy.deepcopy(content)
        last = blocks[-1]

        # 相容性處理：最後一個元素必須為 dict 才能加 cache_control
        if isinstance(last, dict):
            last["cache_control"] = cache_marker
        else:
            logger.warning(
                "add_cache_control：列表最後一個元素非 dict（%s），略過標記",
                type(last).__name__,
            )

        return blocks

    # 其他型別：降級為字串轉換
    logger.warning(
        "add_cache_control 收到非預期型別 %s，以 str() 降級處理",
        type(content).__name__,
    )
    return add_cache_control(str(content), cache_type)


def add_tools_cache_control(
    tools: List[Dict[str, Any]],
    cache_type: str = "ephemeral",
) -> List[Dict[str, Any]]:
    """為 tools 定義列表的最後一個工具加上 cache_control

    tools 作為 API 參數傳遞（不在 messages 中），因此獨立處理。
    只在最後一個 tool 上標記，對應 BP2 斷點。

    Args:
        tools: Anthropic tool 定義列表
        cache_type: 快取類型

    Returns:
        deepcopy 後已標記的 tools 列表
    """
    if not tools:
        return tools

    tools_copy = copy.deepcopy(tools)
    tools_copy[-1]["cache_control"] = {"type": cache_type}
    logger.debug("BP2 Tools 快取標記：已標記 %d 個工具中的最後一個", len(tools_copy))
    return tools_copy


def mark_cacheable_messages(
    messages: List[Union[Message, Dict[str, Any]]],
    config: PromptCacheConfig,
    estimated_tokens: int = 0,
) -> List[Dict[str, Any]]:
    """依照 4-breakpoint 策略標記可快取的訊息

    將訊息列表轉換為帶有 cache_control 標記的 raw dict 列表（Anthropic API 格式）。

    斷點分配（config.cache_history=True 時）：
      BP3 → 倒數第三條 assistant 訊息
      BP4 → 倒數第二條 assistant 訊息
      （最後一條 assistant 訊息保持動態，不快取）

    注意：
      - BP1 (system prompt) 由呼叫方處理，不在此函數範圍
      - BP2 (tools) 由 add_tools_cache_control() 處理
      - 此函數只處理 messages 列表中的 BP3、BP4

    Args:
        messages: 訊息列表（Message 物件或 raw dict 均可）
        config: 快取配置
        estimated_tokens: 估算的 token 總數（用於 min_cacheable_tokens 檢查）

    Returns:
        轉換後的 raw dict 訊息列表，部分訊息帶有 cache_control
    """
    if not config.enabled or not config.cache_history:
        # 快取未啟用，直接轉換為 dict 格式後回傳
        return [_to_api_dict(m) for m in messages]

    # 找出所有 assistant 訊息的索引（從後往前）
    assistant_indices: List[int] = []
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        role = msg.role if isinstance(msg, Message) else msg.get("role", "")
        if role == "assistant":
            assistant_indices.append(i)

    # 依 history_keep_recent 決定跳過最近幾條
    # keep_recent=2 → 跳過 assistant_indices[0]（最後一條）和 [1]（倒數第二條）
    # BP3 → assistant_indices[2]（倒數第三條），BP4 → assistant_indices[3]（倒數第四條）
    # 但原始 DW 設計是：BP3=倒數第三，BP4=倒數第二（keep_recent=2 表示最後 2 條不快取）
    # 所以 BP4 = assistant_indices[1]，BP3 = assistant_indices[2]
    # 使用的斷點數不超過 (max_breakpoints - 已用於 BP1/BP2 的數量)
    # 保守估算：BP1+BP2 用掉 2 個，剩下 2 個給歷史
    available_bp = max(0, config.max_breakpoints - 2)  # 預留 BP1、BP2

    # 跳過最近 keep_recent 條 assistant 訊息
    skip = config.history_keep_recent
    cacheable_assistant = assistant_indices[skip:]  # 已跳過最近的，從舊的開始

    # 取最多 available_bp 個（從最近可快取的開始）
    to_mark = set(cacheable_assistant[:available_bp])

    if not to_mark:
        logger.debug("mark_cacheable_messages：無可標記的 assistant 訊息")
    else:
        logger.debug(
            "mark_cacheable_messages：標記 %d 個 assistant 訊息（indices: %s）",
            len(to_mark),
            sorted(to_mark),
        )

    # 轉換並標記
    result: List[Dict[str, Any]] = []
    for i, msg in enumerate(messages):
        api_dict = _to_api_dict(msg)

        if i in to_mark:
            # 為此訊息的 content 加上 cache_control
            api_dict = _mark_message_dict(api_dict)

        result.append(api_dict)

    return result


def mark_system_prompt_cache(
    system_prompt: Union[str, List[Dict[str, Any]]],
    config: PromptCacheConfig,
    estimated_tokens: int = 0,
) -> Union[str, List[Dict[str, Any]]]:
    """為 system prompt 加上快取標記（BP1）

    若 estimated_tokens 低於 config.min_cacheable_tokens，略過標記。

    Args:
        system_prompt: system prompt 字串或 content block 列表
        config: 快取配置
        estimated_tokens: 估算的 system prompt token 數

    Returns:
        已標記（或未更動）的 system prompt
    """
    if not config.enabled or not config.cache_system_prompt:
        return system_prompt

    if estimated_tokens > 0 and estimated_tokens < config.min_cacheable_tokens:
        logger.debug(
            "BP1 System prompt 略過：token 數 %d < 最小門檻 %d",
            estimated_tokens,
            config.min_cacheable_tokens,
        )
        return system_prompt

    logger.debug("BP1 System prompt 快取標記套用（estimated_tokens=%d）", estimated_tokens)
    return add_cache_control(system_prompt)


# ── 成本估算 ──────────────────────────────────────────────────────────────────


def estimate_cache_savings(
    total_input_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int = 0,
    price_normal: float = PRICE_NORMAL_PER_M,
    price_cache_write: float = PRICE_CACHE_WRITE_PER_M,
    price_cache_read: float = PRICE_CACHE_READ_PER_M,
) -> Dict[str, float]:
    """估算快取帶來的成本節省

    計算邏輯：
      - 無快取時：所有 token 按正常價計費
      - 有快取時：快取寫入 token 按 write 價，快取命中 token 按 read 價，
        其餘 token 按正常價

    Args:
        total_input_tokens: 本次請求的輸入 token 總數
        cached_tokens: 從快取命中（讀取）的 token 數
        cache_write_tokens: 本次寫入快取的 token 數（若不提供，以 cached_tokens 估算）
        price_normal: 一般輸入定價（$/百萬 token）
        price_cache_write: 快取寫入定價（$/百萬 token）
        price_cache_read: 快取讀取定價（$/百萬 token）

    Returns:
        包含成本估算結果的 dict：
          - cost_without_cache: 無快取時的估算費用（USD）
          - cost_with_cache: 使用快取後的估算費用（USD）
          - savings: 節省金額（USD）
          - savings_pct: 節省百分比（0–100）
    """
    if cache_write_tokens == 0:
        cache_write_tokens = cached_tokens

    normal_tokens = max(0, total_input_tokens - cached_tokens - cache_write_tokens)

    cost_without_cache = total_input_tokens * price_normal / 1_000_000
    cost_with_cache = (
        normal_tokens * price_normal / 1_000_000
        + cache_write_tokens * price_cache_write / 1_000_000
        + cached_tokens * price_cache_read / 1_000_000
    )

    savings = max(0.0, cost_without_cache - cost_with_cache)
    savings_pct = (savings / cost_without_cache * 100) if cost_without_cache > 0 else 0.0

    return {
        "cost_without_cache": round(cost_without_cache, 8),
        "cost_with_cache": round(cost_with_cache, 8),
        "savings": round(savings, 8),
        "savings_pct": round(savings_pct, 2),
    }


# ── CacheStats ────────────────────────────────────────────────────────────────


class CacheStats:
    """快取統計追蹤器

    追蹤整個 session 中的快取命中與寫入情況，
    提供累積的成本節省估算。

    使用方式：
        stats = CacheStats()
        stats.record_cache_creation(tokens=2048)
        stats.record_cache_read(tokens=2048)
        print(stats.get_stats())
    """

    def __init__(self) -> None:
        self.total_cache_creation_tokens: int = 0
        """累積的快取寫入 token 總數"""

        self.total_cache_read_tokens: int = 0
        """累積的快取命中 token 總數"""

        self.cache_creation_count: int = 0
        """快取寫入次數"""

        self.cache_read_count: int = 0
        """快取命中次數"""

        self.total_input_tokens: int = 0
        """累積的輸入 token 總數（含快取）"""

    def record_cache_creation(self, tokens: int) -> None:
        """記錄一次快取寫入事件

        Args:
            tokens: 本次寫入快取的 token 數
        """
        self.total_cache_creation_tokens += tokens
        self.cache_creation_count += 1
        self.total_input_tokens += tokens
        logger.debug("快取寫入 +%d tokens（累積 %d 次）", tokens, self.cache_creation_count)

    def record_cache_read(self, tokens: int) -> None:
        """記錄一次快取命中事件

        Args:
            tokens: 本次從快取讀取的 token 數
        """
        self.total_cache_read_tokens += tokens
        self.cache_read_count += 1
        self.total_input_tokens += tokens
        logger.debug("快取命中 +%d tokens（累積 %d 次）", tokens, self.cache_read_count)

    def record_normal_tokens(self, tokens: int) -> None:
        """記錄非快取的一般輸入 token

        Args:
            tokens: 一般輸入 token 數
        """
        self.total_input_tokens += tokens

    def get_stats(self) -> Dict[str, Any]:
        """取得完整統計報告

        Returns:
            統計 dict，包含 token 計數、次數與成本估算
        """
        savings_info = estimate_cache_savings(
            total_input_tokens=self.total_input_tokens,
            cached_tokens=self.total_cache_read_tokens,
            cache_write_tokens=self.total_cache_creation_tokens,
        )

        return {
            # Token 計數
            "total_input_tokens": self.total_input_tokens,
            "total_cache_creation_tokens": self.total_cache_creation_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            # 次數
            "cache_creation_count": self.cache_creation_count,
            "cache_read_count": self.cache_read_count,
            # 命中率
            "cache_hit_rate": (
                round(self.cache_read_count / (self.cache_creation_count + self.cache_read_count) * 100, 1)
                if (self.cache_creation_count + self.cache_read_count) > 0
                else 0.0
            ),
            # 成本
            **savings_info,
        }

    def reset(self) -> None:
        """重置所有統計數據"""
        self.__init__()
        logger.debug("CacheStats 已重置")

    def __repr__(self) -> str:
        stats = self.get_stats()
        return (
            f"CacheStats("
            f"reads={self.cache_read_count}, "
            f"writes={self.cache_creation_count}, "
            f"savings={stats['savings_pct']:.1f}%)"
        )


# ── 內部輔助函數 ──────────────────────────────────────────────────────────────


def _to_api_dict(msg: Union[Message, Dict[str, Any]]) -> Dict[str, Any]:
    """將 Message 物件或 raw dict 統一轉換為 Anthropic API 格式的 dict

    Args:
        msg: Message 物件或已是 dict 格式的訊息

    Returns:
        {"role": ..., "content": ...} 格式的 dict
    """
    if isinstance(msg, Message):
        return {
            "role": msg.role,
            "content": msg.content,
        }

    # raw dict：直接 shallowcopy 後回傳
    return dict(msg)


def _mark_message_dict(api_dict: Dict[str, Any]) -> Dict[str, Any]:
    """為 API dict 的 content 加上 cache_control

    處理 content 為 str 或 list 的情況。

    Args:
        api_dict: Anthropic 格式訊息 dict

    Returns:
        content 已標記的新 dict（不修改原物件）
    """
    result = dict(api_dict)
    content = result.get("content", "")
    result["content"] = add_cache_control(content)
    return result
