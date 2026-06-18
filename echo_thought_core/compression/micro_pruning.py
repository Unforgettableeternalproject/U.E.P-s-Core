"""
Layer 1: Micro-Pruning — 規則式輕量清理

觸發條件：token 使用率 > 60%
策略：
- 清理舊工具輸出（大於 200 chars 的 tool_result，邊界之前）
- 壓縮連續相似的確認訊息
- 移除空白/無意義的訊息
- 可選：LLMLingua-2 對長訊息做 token 刪減

零 LLM 呼叫，規則式處理。

v2 整合 Destiny-Weavers context_compressor.py Phase 1 設計：
- boundary-aware 剪裁（只剪 protect_tail_tokens 邊界之前的輸出）
- 統一使用 PRUNED_TOOL_PLACEHOLDER 佔位字串
"""

from __future__ import annotations

import logging

from echo_thought_core.compression.tool_pairs import PRUNED_TOOL_PLACEHOLDER
from echo_thought_core.compression.tool_summarizer import get_tool_summarizer
from echo_thought_core.defaults import LLMLINGUA_MIN_TOKEN_THRESHOLD
from echo_thought_core.schemas.message import Message

logger = logging.getLogger(__name__)

# 工具輸出被剪裁後的佔位字串（從 tool_pairs 模組集中管理）
# 重新匯出以維持此模組的對外介面不變
_PRUNED_TOOL_PLACEHOLDER = PRUNED_TOOL_PLACEHOLDER

# 工具輸出長度門檻：超過此值才值得替換
_PRUNE_MIN_CHARS = 200

# 估算佔位字串本身的 token 數（用於計算節省量）
_PLACEHOLDER_TOKEN_ESTIMATE = 12


class MicroPruner:
    """Layer 1 微清理器"""

    def __init__(self, use_llmlingua: bool = False, llmlingua_config=None):
        self.use_llmlingua = use_llmlingua
        self._llmlingua_compressor = None
        self._llmlingua_config = llmlingua_config

    def prune(
        self,
        messages: list[Message],
        protect_tail_tokens: int | None = None,
    ) -> tuple[list[Message], int]:
        """執行微清理

        整合 DW Phase 1 的 boundary-aware 剪裁邏輯：
        只剪裁位於 protect_tail_tokens 邊界之前的舊工具輸出。
        若未指定 protect_tail_tokens，則以訊息數量的後半段作為保護區。

        Args:
            messages: 輸入訊息列表
            protect_tail_tokens: 從尾部保護的 token 數量上限。
                                 在此預算內的訊息不會被剪裁。
                                 None 表示保護後 50% 的訊息。

        Returns:
            (清理後的訊息列表, 節省的 token 數)
        """
        tokens_saved = 0
        result: list[Message] = []

        # 計算保護邊界索引（boundary_idx 及之後的訊息不做 tool output 剪裁）
        boundary_idx = self._compute_protect_boundary(messages, protect_tail_tokens)
        logger.debug(
            "MicroPruner.prune: 共 %d 條訊息，保護邊界 index=%d",
            len(messages),
            boundary_idx,
        )

        for i, msg in enumerate(messages):
            # 規則 1：剪裁舊工具輸出（boundary 之前、大於門檻、可重現）
            if (
                i < boundary_idx
                and msg.role == "tool"
                and len(msg.content) > _PRUNE_MIN_CHARS
                and not msg.metadata.get("pruned", False)
            ):
                saved = self._prune_tool_result(msg, result)
                tokens_saved += saved
                continue

            # 規則 2：壓縮連續的短確認訊息（assistant 角色，token 數 < 15）
            if (
                msg.role == "assistant"
                and msg.token_count
                and msg.token_count < 15
                and result
                and result[-1].role == "assistant"
                and result[-1].token_count
                and result[-1].token_count < 15
            ):
                merged_content = f"{result[-1].content} | {msg.content}"
                result[-1] = Message(
                    role=result[-1].role,
                    content=merged_content,
                    timestamp=result[-1].timestamp,
                    token_count=(result[-1].token_count or 0) + (msg.token_count or 0) - 4,
                    tool_call_id=result[-1].tool_call_id,
                    tool_name=result[-1].tool_name,
                    is_reproducible=result[-1].is_reproducible,
                    metadata={**result[-1].metadata, "merged": True},
                )
                tokens_saved += 4  # 省下一組 role overhead
                logger.debug("MicroPruner.prune: 合併短確認訊息 at index %d", i)
                continue

            # 規則 3：LLMLingua-2 對長訊息做 token 級刪減
            if (
                self.use_llmlingua
                and msg.role in ("user", "assistant")
                and msg.token_count
                and msg.token_count >= LLMLINGUA_MIN_TOKEN_THRESHOLD
                and not msg.is_compressed
                and not msg.metadata.get("llmlingua_compressed", False)
            ):
                compressed_msg, saved = self._apply_llmlingua(msg)
                result.append(compressed_msg)
                tokens_saved += saved
                continue

            result.append(msg)

        return result, tokens_saved

    # ── 內部輔助 ──────────────────────────────────────────────────────────────

    def _compute_protect_boundary(
        self,
        messages: list[Message],
        protect_tail_tokens: int | None,
    ) -> int:
        """計算保護邊界的索引位置。

        保護邊界之後（含）的訊息不會被工具輸出剪裁規則觸碰。

        若 protect_tail_tokens 有指定，從尾部往前累計 token 數，
        直到超出預算為止，該位置即為邊界。
        若未指定，預設保護後半段訊息（索引 >= len/2）。

        Returns:
            邊界索引（此索引及之後的訊息都受保護）
        """
        n = len(messages)
        if n == 0:
            return 0

        if protect_tail_tokens is None:
            # 預設：保護後 50% 的訊息
            return n // 2

        # 從尾部往前累計，找到 token 預算耗盡的位置
        accumulated = 0
        for i in range(n - 1, -1, -1):
            token_count = messages[i].token_count or 0
            if accumulated + token_count > protect_tail_tokens:
                # 下一條（i+1）就是邊界
                return i + 1
            accumulated += token_count

        # 所有訊息都在保護範圍內
        return 0

    def _get_llmlingua(self):
        """Lazy init LLMLinguaCompressor"""
        if self._llmlingua_compressor is None:
            from echo_thought_core.compression.llmlingua import (
                LLMLinguaCompressor,
                LLMLinguaConfig,
            )

            config = self._llmlingua_config or LLMLinguaConfig()
            self._llmlingua_compressor = LLMLinguaCompressor(config=config)

        return self._llmlingua_compressor

    def _apply_llmlingua(self, msg: Message) -> tuple[Message, int]:
        """對單一長訊息做 LLMLingua-2 壓縮

        Returns:
            (壓縮後的 Message, 節省的 token 數)
        """
        compressor = self._get_llmlingua()
        compressed_text, stats = compressor.compress(msg.content)

        if stats.get("skipped", True):
            return msg, 0

        saving = stats.get("saving", 0)

        compressed_msg = Message(
            role=msg.role,
            content=compressed_text,
            timestamp=msg.timestamp,
            token_count=stats.get("compressed_tokens"),
            tool_call_id=msg.tool_call_id,
            tool_name=msg.tool_name,
            is_reproducible=msg.is_reproducible,
            metadata={
                **msg.metadata,
                "llmlingua_compressed": True,
                "llmlingua_original_tokens": stats.get("original_tokens"),
                "llmlingua_ratio": stats.get("ratio"),
            },
        )

        logger.info(
            "MicroPruner: LLMLingua 壓縮 %s 訊息，%d → %d tokens (節省 %d)",
            msg.role,
            stats.get("original_tokens", 0),
            stats.get("compressed_tokens", 0),
            saving,
        )

        return compressed_msg, saving

    def _prune_tool_result(
        self,
        msg: Message,
        result: list[Message],
    ) -> int:
        """將一條工具輸出替換為結構化摘要，回傳節省的 token 數。

        使用 ToolSummarizer 產生有資訊量的單行摘要，
        而非簡單的佔位字串。

        優先使用 token_count（精確），回退到 len(content)//4（粗估）。
        """
        original_tokens = msg.token_count or (len(msg.content) // 4)

        # 使用 ToolSummarizer 產生結構化摘要
        summarizer = get_tool_summarizer()
        summary = summarizer.summarize(
            tool_name=msg.tool_name,
            content=msg.content,
            metadata=msg.metadata,
        )

        # 估算摘要的 token 數
        summary_tokens = max(_PLACEHOLDER_TOKEN_ESTIMATE, len(summary) // 4)

        placeholder_msg = Message(
            role=msg.role,
            content=summary,
            timestamp=msg.timestamp,
            token_count=summary_tokens,
            tool_call_id=msg.tool_call_id,
            tool_name=msg.tool_name,
            is_reproducible=msg.is_reproducible,
            metadata={
                **msg.metadata,
                "pruned": True,
                "original_tokens": original_tokens,
                "original_char_count": len(msg.content),
            },
        )
        result.append(placeholder_msg)

        saved = max(0, original_tokens - summary_tokens)
        logger.debug(
            "MicroPruner: 剪裁 tool_result (tool=%s)，節省約 %d tokens",
            msg.tool_name or "unknown",
            saved,
        )
        return saved
