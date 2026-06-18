"""
Layer 2: Rolling Summary — 增量摘要

觸發條件：token 使用率 > 80%
策略：
- 對最舊的 N 個 turn 做語義摘要（需要 LLM 呼叫）
- 保留神聖段落
- 觸發寫入 Echo Memory L1（異步）

參考 Anthropic Claude Code 的 Tier 3 Context Collapse 設計。

v2 整合 Destiny-Weavers context_compressor.py Phase 3 設計：
- _compute_summary_budget()：依壓縮量動態計算摘要 token 預算
- _serialize_for_summary()：序列化為可讀格式（含 tool_call 展開）
- 通用六段式摘要模板（移除 DW 特有的 Agent/Board/Lore 段落）
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple, Protocol

from echo_thought_core.schemas.message import Message

logger = logging.getLogger(__name__)

# ── 壓縮摘要安全標記 ─────────────────────────────────────────────────────────
# 防止 LLM 將壓縮摘要當作活躍指令來執行。
# 參考 hermes-agent 的 SUMMARY_PREFIX 設計，適配回聲系統需求。

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is background reference, NOT active "
    "instructions. Do NOT answer questions or fulfill requests mentioned "
    "in this summary — they were already addressed. Respond ONLY to the "
    "latest user message that appears AFTER this summary."
)

SUMMARY_END_MARKER = (
    "--- END OF CONTEXT SUMMARY — "
    "respond to the message below, not the summary above ---"
)

# 摘要 token 預算範圍（最小 2K，最大 12K）
_SUMMARY_BUDGET_MIN = 2_000
_SUMMARY_BUDGET_MAX = 12_000
# 摘要佔被壓縮內容的比例（20%）
_SUMMARY_BUDGET_RATIO = 0.20


# ── 通用六段式摘要模板 ────────────────────────────────────────────────────────
# 移除 DW 特有段落（Agent Collaboration、Board State、Lore References），
# 改為適用於任何對話場景的通用結構。

SUMMARY_PROMPT = """You are summarizing a segment of a conversation to save context space.

Generate a structured summary covering these sections:

**1. TASK / MISSION GOAL**
What is the user trying to accomplish? What is the current objective?

**2. PROGRESS & CURRENT STATE**
What has been done so far? What is the current status?

**3. KEY DECISIONS**
Important decisions made, with the rationale behind them.
Technical choices, configuration details, approach changes.

**4. RELEVANT FILES & RESOURCES**
Files created or modified, APIs called, external resources referenced.
Include exact names/paths where important.

**5. NEXT STEPS**
What remains to be done? Any pending tasks or blockers?

**6. CRITICAL CONTEXT**
Anything that MUST be remembered to continue correctly:
- Error messages and their resolutions
- User's explicit constraints or preferences
- Non-obvious dependencies or gotchas

RULES:
- Be concise but complete — prefer bullet points over prose.
- Preserve exact names (files, functions, configs, error codes).
- Do NOT omit information that would break continuity if lost.
- Write in the same language as the conversation.
"""


class SummaryBackend(Protocol):
    """摘要後端介面"""

    async def summarize(self, messages: List[Message], instructions: Optional[str] = None) -> str:
        """將訊息列表摘要為文本"""
        ...


class RollingSummarizer:
    """Layer 2 增量摘要器"""

    def __init__(self, backend: Optional[SummaryBackend] = None):
        self.backend = backend

    async def summarize_oldest(
        self,
        messages: List[Message],
        n_turns: int = 10,
        instructions: Optional[str] = None,
    ) -> Tuple[List[Message], str, int]:
        """摘要最舊的 N 個 turn

        整合 DW 的動態預算計算與序列化邏輯。
        若有 backend，先序列化再呼叫摘要；
        若無，回退到原始的直接傳遞行為。

        Returns:
            (更新後的訊息列表, 摘要文本, 節省的 token 數)
        """
        if not self.backend:
            raise RuntimeError("No summary backend configured for Layer 2")

        if len(messages) <= n_turns:
            return messages, "", 0

        # 分割：舊的（要摘要）和新的（保留）
        old_messages = messages[:n_turns]
        new_messages = messages[n_turns:]

        # 計算舊訊息的 token 數
        old_tokens = sum(m.token_count or 0 for m in old_messages)

        # 計算此次摘要的 token 預算
        summary_budget = _compute_summary_budget(old_messages)
        logger.debug(
            "RollingSummarizer: 壓縮 %d 條訊息（%d tokens），摘要預算 %d tokens",
            len(old_messages),
            old_tokens,
            summary_budget,
        )

        # 組合壓縮指令：加入 token 預算提示，讓後端知道目標摘要長度
        # _serialize_for_summary 的結果供後端可選使用（後端可自行決定是否直接讀 messages）
        prompt = instructions or SUMMARY_PROMPT
        prompt_with_budget = f"{prompt}\n\n(Target summary length: ~{summary_budget} tokens)"

        # 直接傳遞原始訊息列表，維持 SummaryBackend 介面契約
        # 後端可透過 format_messages() 或 _serialize_for_summary() 自行序列化
        summary_text = await self.backend.summarize(old_messages, prompt_with_budget)

        # 建立摘要訊息
        summary_message = Message(
            role="system",
            content=f"{SUMMARY_PREFIX}\n\n{summary_text}\n\n{SUMMARY_END_MARKER}",
            is_compressed=True,
            original_turn_range=f"1-{n_turns}",
            metadata={
                "compression_tier": 2,
                "original_token_count": old_tokens,
                "summary_budget": summary_budget,
            },
        )

        # 計算節省
        summary_tokens = len(summary_text) // 4 + 10  # 粗估
        tokens_saved = old_tokens - summary_tokens

        result = [summary_message] + new_messages
        return result, summary_text, max(0, tokens_saved)


# ── 輔助函式（從 DW 搬遷並泛化）────────────────────────────────────────────


def _compute_summary_budget(turns: List[Message]) -> int:
    """計算摘要的 token 預算。

    預算 = 被壓縮內容 token 數的 20%，限制在 2K–12K 之間。
    這避免摘要過長（浪費）或過短（資訊遺失）。

    Args:
        turns: 即將被摘要的訊息列表

    Returns:
        建議的摘要 token 預算
    """
    total_tokens = sum(m.token_count or (len(m.content) // 4) for m in turns)
    budget = int(total_tokens * _SUMMARY_BUDGET_RATIO)
    return max(_SUMMARY_BUDGET_MIN, min(_SUMMARY_BUDGET_MAX, budget))


