"""
Layer 3: Full Compaction — 完整壓縮

觸發條件：token 使用率 > 92%
策略：
- 結構化九段式摘要（參考 Anthropic Claude Code Tier 4）
- 強制寫入 Echo Memory L2
- 神聖段落完整保留

壓縮比目標：6:1 ~ 8:1

v2 整合 Destiny-Weavers context_compressor.py Phase 2 設計：
- _find_tail_cut_by_tokens()：基於 token 預算從尾部反向掃描，確定壓縮邊界
- 1.5x 軟性上限避免切割超大訊息
- 硬性最小保護：至少保護 3 條訊息
- 更新壓縮模板為通用九段式（移除 DW 特有段落）
"""

from __future__ import annotations

import logging
from typing import Protocol

from echo_thought_core.compression.rolling_summary import (
    SUMMARY_END_MARKER,
    SUMMARY_PREFIX,
)
from echo_thought_core.compression.tool_pairs import (
    align_boundary_backward,
    align_boundary_forward,
    sanitize_tool_pairs,
)
from echo_thought_core.schemas.message import Message
from echo_thought_core.schemas.session import SacredContent

logger = logging.getLogger(__name__)

# 硬性最小保護條數：tail 至少保護末尾這幾條訊息
_MIN_TAIL_PROTECT = 3
# 軟性上限係數：若單條訊息超過 budget * 1.5，仍納入 tail 而非強行切割
_SOFT_LIMIT_FACTOR = 1.5


# ── 通用九段式壓縮模板 ────────────────────────────────────────────────────────
# 移除 DW 特有段落（Agent Collaboration、Board State、Lore References、Character Persona），
# 第 9 段改為通用的 Identity & Operational Context，由神聖段落填充。

COMPACTION_PROMPT = """You are compressing a full conversation into a structured summary.

First, reason through what must be preserved inside <analysis> tags (this will be discarded).
Then produce the final summary inside <summary> tags using exactly these sections:

<analysis>
(Your working notes — which information is critical vs. safe to omit)
</analysis>

<summary>
1. PRIMARY REQUEST & INTENT
What is the user's core goal? What problem are they solving?

2. KEY TECHNICAL CONCEPTS
Architecture decisions, design patterns, configurations, constraints.
Include exact names, versions, and values where relevant.

3. FILES & CODE SECTIONS
Every file created or modified. Key function names, class names, module paths.
Short but exact — enough to locate and understand what changed.

4. ERRORS & RESOLUTIONS
Error messages encountered, root causes identified, fixes applied.
Include what was tried and what worked.

5. PROBLEM-SOLVING APPROACHES
Strategies attempted. What worked, what failed, and why.
Dead ends worth remembering to avoid repeating.

6. ALL USER MESSAGES VERBATIM
[SACRED — copy every user message exactly as written, do NOT paraphrase or omit any]

7. PENDING TASKS
Unfinished items, known issues, deferred decisions.

8. CURRENT WORK IN PROGRESS
What was actively being worked on when this compaction triggered.
The exact state of the work (e.g., "editing function X in file Y, line Z").

9. IDENTITY & OPERATIONAL CONTEXT
[SACRED — preserved exactly from sacred content, do NOT modify]
{sacred_content}
</summary>

RULES:
- Section 6 is SACRED: every user message must appear verbatim, in order.
- Section 9 is SACRED: copy the provided sacred content without changes.
- Sections 1–5, 7–8: be concise. Bullet points preferred over prose.
- Preserve ALL exact identifiers: file paths, function names, error codes, config keys.
- Write in the same language as the conversation.
"""


class CompactionBackend(Protocol):
    """壓縮後端介面"""

    async def compact(
        self,
        messages: list[Message],
        sacred: SacredContent,
        instructions: str | None = None,
    ) -> str:
        """將完整對話壓縮為結構化摘要"""
        ...


class FullCompactor:
    """Layer 3 完整壓縮器"""

    def __init__(self, backend: CompactionBackend | None = None):
        self.backend = backend

    async def compact(
        self,
        messages: list[Message],
        sacred: SacredContent,
        instructions: str | None = None,
        tail_token_budget: int | None = None,
    ) -> tuple[list[Message], str, int]:
        """執行完整壓縮

        整合 DW 的 _find_tail_cut_by_tokens() 邊界計算邏輯：
        若指定 tail_token_budget，先用 token 預算計算 tail 邊界，
        對齊 tool 組，再對 head 部分做結構化摘要，最後組裝並修復 tool pairs。

        Args:
            messages: 輸入訊息列表
            sacred: 神聖段落（人設、使用者指令等）
            instructions: 可選的自訂壓縮指令（覆蓋預設 COMPACTION_PROMPT）
            tail_token_budget: tail 側保留的 token 預算。
                               None 表示壓縮全部訊息（傳統模式）。

        Returns:
            (壓縮後的訊息列表, 摘要文本, 節省的 token 數)
        """
        if not self.backend:
            raise RuntimeError("No compaction backend configured for Layer 3")

        # 計算壓縮前的 token 數
        total_before = sum(m.token_count or 0 for m in messages)

        if tail_token_budget is not None:
            # DW 模式：計算 head/tail 邊界，只摘要 head 部分
            result, summary_text, tokens_saved = await self._compact_with_boundary(
                messages, sacred, instructions, tail_token_budget, total_before
            )
        else:
            # 傳統模式：壓縮全部訊息
            result, summary_text, tokens_saved = await self._compact_all(
                messages, sacred, instructions, total_before
            )

        return result, summary_text, tokens_saved

    # ── 內部壓縮策略 ──────────────────────────────────────────────────────────

    async def _compact_all(
        self,
        messages: list[Message],
        sacred: SacredContent,
        instructions: str | None,
        total_before: int,
    ) -> tuple[list[Message], str, int]:
        """傳統模式：壓縮全部訊息為單一結構化摘要。"""
        summary_text = await self.backend.compact(messages, sacred, instructions)

        compacted_message = Message(
            role="system",
            content=f"{SUMMARY_PREFIX}\n\n{summary_text}\n\n{SUMMARY_END_MARKER}",
            is_compressed=True,
            original_turn_range=f"1-{len(messages)}",
            metadata={
                "compression_tier": 3,
                "original_token_count": total_before,
                "original_message_count": len(messages),
            },
        )

        summary_tokens = len(summary_text) // 4 + 10
        tokens_saved = total_before - summary_tokens

        return [compacted_message], summary_text, max(0, tokens_saved)

    async def _compact_with_boundary(
        self,
        messages: list[Message],
        sacred: SacredContent,
        instructions: str | None,
        tail_token_budget: int,
        total_before: int,
    ) -> tuple[list[Message], str, int]:
        """邊界模式：保留 tail，只摘要 head 部分。"""
        # Step 1: 找出 tail 起始索引（head_end = tail 起點）
        head_end = _find_tail_cut_by_tokens(messages, tail_token_budget)
        logger.debug(
            "FullCompactor: 邊界計算結果 head_end=%d（共 %d 條訊息）",
            head_end,
            len(messages),
        )

        # Step 2: 對齊邊界，避免切割 tool 組
        head_end = align_boundary_forward(messages, head_end)
        head_end = align_boundary_backward(messages, head_end)
        logger.debug("FullCompactor: 對齊後 head_end=%d", head_end)

        # Step 3: 分割 head / tail
        head_messages = messages[:head_end]
        tail_messages = messages[head_end:]

        if not head_messages:
            # 沒有東西可以壓縮，直接返回原始訊息
            logger.warning("FullCompactor: head 為空，跳過壓縮")
            return messages, "", 0

        # Step 4: 對 head 執行結構化摘要
        summary_text = await self.backend.compact(head_messages, sacred, instructions)
        head_tokens = sum(m.token_count or 0 for m in head_messages)

        summary_message = Message(
            role="system",
            content=f"{SUMMARY_PREFIX}\n\n{summary_text}\n\n{SUMMARY_END_MARKER}",
            is_compressed=True,
            original_turn_range=f"1-{head_end}",
            metadata={
                "compression_tier": 3,
                "original_token_count": head_tokens,
                "original_message_count": head_end,
            },
        )

        # Step 5: 組裝 summary + tail，然後修復 tool pairs
        assembled = [summary_message] + tail_messages
        assembled = sanitize_tool_pairs(assembled)

        summary_tokens = len(summary_text) // 4 + 10
        tokens_saved = head_tokens - summary_tokens

        return assembled, summary_text, max(0, tokens_saved)


# ── 輔助函式（從 DW 搬遷並泛化）────────────────────────────────────────────


def _find_tail_cut_by_tokens(
    messages: list[Message],
    tail_token_budget: int,
    head_end_hint: int = 0,
) -> int:
    """從尾部往前掃描，找出在 token 預算內可以保留的最早起始索引。

    硬性規則：至少保護末尾 _MIN_TAIL_PROTECT 條訊息。
    軟性規則：若某條訊息超過 budget * 1.5，仍納入 tail（避免切割大訊息）。

    Args:
        messages: 完整訊息列表
        tail_token_budget: tail 側允許的最大 token 數
        head_end_hint: head 的最小結束位置（tail 不能往前延伸超過此值）

    Returns:
        tail 的起始索引（即 head_end），此索引之後的訊息為 tail。
    """
    n = len(messages)
    if n == 0:
        return 0

    # 硬性：tail 最多往前延伸到 n - _MIN_TAIL_PROTECT
    tail_start_max = n - _MIN_TAIL_PROTECT
    if tail_start_max <= head_end_hint:
        # 訊息太少，無法安全切割
        return max(head_end_hint, n - _MIN_TAIL_PROTECT)

    # 從尾部往前累計 token
    accumulated = 0
    tail_start = n  # 預設：tail 從最末尾開始（即不保留任何 tail）

    for i in range(n - 1, head_end_hint - 1, -1):
        msg_tokens = messages[i].token_count or (len(messages[i].content) // 4)
        soft_limit = tail_token_budget * _SOFT_LIMIT_FACTOR

        if accumulated + msg_tokens > tail_token_budget:
            # 超出硬性預算
            if accumulated + msg_tokens <= soft_limit:
                # 在軟性上限內：納入以避免切割大訊息
                accumulated += msg_tokens
                tail_start = i
                logger.debug(
                    "_find_tail_cut_by_tokens: 軟性上限接受 index=%d (msg_tokens=%d)",
                    i,
                    msg_tokens,
                )
            else:
                # 真的超出，停在這裡
                break
        else:
            accumulated += msg_tokens
            tail_start = i

    # 確保不違反硬性最小保護
    return min(tail_start, tail_start_max)
