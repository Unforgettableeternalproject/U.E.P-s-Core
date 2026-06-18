"""
EchoCompressor — 四層漸進式壓縮引擎

ContextEngine 的預設實作，從 ContextManager 萃取的壓縮核心。
提供 Micro-Pruning / Rolling Summary / Full Compaction / Emergency
四層壓縮策略，與反抖動保護。

Usage:
    from echo_thought_core.echo_compressor import EchoCompressor

    engine = EchoCompressor(config=config, persona=persona, backend=client)
    tier = engine.should_compress(messages)
    result = await engine.compress_async(messages, tier)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from echo_thought_core.accounting.budget import TokenBudget
from echo_thought_core.accounting.counter import TokenCounter
from echo_thought_core.compression.emergency import EmergencyCompactor
from echo_thought_core.compression.full_compaction import FullCompactor
from echo_thought_core.compression.micro_pruning import MicroPruner
from echo_thought_core.compression.rolling_summary import RollingSummarizer
from echo_thought_core.compression.sacred import SacredManager
from echo_thought_core.engine_base import ContextEngine
from echo_thought_core.schemas.session import CompactionResult, CompressionTier

if TYPE_CHECKING:
    from echo_thought_core.backends.base import AbstractBackend
    from echo_thought_core.memory_bridge import MemoryBridge
    from echo_thought_core.schemas.config import CompactionConfig, PersonaContext
    from echo_thought_core.schemas.message import Message

logger = logging.getLogger(__name__)


class EchoCompressor(ContextEngine):
    """四層漸進式壓縮引擎 — ContextEngine 的預設實作"""

    @property
    def name(self) -> str:
        return "echo"

    def __init__(
        self,
        config: CompactionConfig | None = None,
        persona: PersonaContext | None = None,
        backend: AbstractBackend | None = None,
        memory_bridge: MemoryBridge | None = None,
    ):
        from echo_thought_core.schemas.config import CompactionConfig

        self.config = config or CompactionConfig()
        self.persona = persona
        self._backend = backend

        # MemoryBridge（可選）
        if memory_bridge is not None:
            self._memory_bridge = memory_bridge
        else:
            from echo_thought_core.memory_bridge import NullMemoryBridge
            self._memory_bridge = NullMemoryBridge()

        # 子系統
        self.counter = TokenCounter(
            backend=self.config.backend,
            model=self.config.model,
        )
        self.budget = TokenBudget(
            context_window=self.config.context_window_size,
            thresholds=self.config.thresholds,
        )
        self.sacred = SacredManager(persona=persona)
        self.pruner = MicroPruner(use_llmlingua=self.config.use_llmlingua)
        self.summarizer = RollingSummarizer(backend=backend)
        self.compactor = FullCompactor(backend=backend)
        self.emergency = EmergencyCompactor(
            max_keep=self.config.max_emergency_messages,
        )

        # 壓縮冷卻
        self._last_compaction_time: float = 0.0

        # 反抖動保護
        self._last_compression_savings_pct: float = 100.0
        self._ineffective_compression_count: int = 0

        # 壓縮事件回調（deprecated，用 MemoryBridge 替代）
        self._on_compaction_callbacks: list[
            Callable[[CompactionResult], Awaitable[None]]
        ] = []

    # -- ContextEngine 核心介面 ---------------------------------------------

    def should_compress(self, messages: list[Message]) -> CompressionTier:
        """根據 token 預算判斷需要的壓縮層級"""
        # 更新 budget
        total = sum(m.token_count or 0 for m in messages)
        self.budget.update(history=total)

        tier = self.budget.check_tier()

        # 冷卻檢查
        if tier > CompressionTier.TIER_0_NORMAL and not self._can_compact():
            return CompressionTier.TIER_0_NORMAL

        # 反抖動：連續 2 次效率 <10% 則跳過（Emergency 除外）
        if (
            self._ineffective_compression_count >= 2
            and tier < CompressionTier.TIER_4_EMERGENCY
        ):
            logger.warning(
                "[EchoCompressor] 反抖動保護：連續 %d 次壓縮效率 <10%%，跳過",
                self._ineffective_compression_count,
            )
            return CompressionTier.TIER_0_NORMAL

        return tier

    def compress(
        self, messages: list[Message], tier: CompressionTier
    ) -> CompactionResult | None:
        """同步壓縮（僅支援 Layer 1）"""
        if tier == CompressionTier.TIER_0_NORMAL:
            return None
        if tier == CompressionTier.TIER_4_EMERGENCY:
            return self._apply_layer4(messages)
        if tier <= CompressionTier.TIER_1_MICRO_PRUNE:
            return self._apply_layer1(messages)
        # Layer 2/3 需要異步
        return None

    async def compress_async(
        self,
        messages: list[Message],
        tier: CompressionTier,
        instructions: str | None = None,
    ) -> CompactionResult | None:
        """異步壓縮（支援所有層級）"""
        if tier == CompressionTier.TIER_0_NORMAL:
            return None
        if tier >= CompressionTier.TIER_3_FULL_COMPACTION:
            return await self._apply_layer3(messages, instructions)
        if tier >= CompressionTier.TIER_2_ROLLING_SUMMARY:
            return await self._apply_layer2(messages)
        return self._apply_layer1(messages)

    # -- Preflight 壓縮 ----------------------------------------------------

    async def preflight_compress(
        self,
        messages: list[Message],
        estimated_request_tokens: int = 0,
        max_passes: int = 3,
    ) -> CompactionResult | None:
        """在 API 呼叫前執行預防性壓縮。

        估算 total + estimated_request_tokens 是否超過 max_input_tokens，
        若超過則自動觸發壓縮。最多執行 max_passes 次。

        Args:
            messages: 當前訊息列表
            estimated_request_tokens: 預估的系統提示 + 工具 schema token 數
            max_passes: 最多壓縮幾次

        Returns:
            最後一次壓縮的結果，或 None（不需要壓縮）
        """
        last_result = None
        for pass_num in range(max_passes):
            total = sum(m.token_count or 0 for m in messages) + estimated_request_tokens
            if total <= self.budget.max_input_tokens:
                break

            tier = self.should_compress(messages)
            if tier == CompressionTier.TIER_0_NORMAL:
                # 可能被冷卻或反抖動阻擋；僅在反抖動情況下強制 Layer 1
                if not self._can_compact():
                    logger.info("[EchoCompressor] Preflight skipped: cooldown active")
                    break
                # 反抖動阻擋——preflight 仍嘗試 Layer 1
                tier = CompressionTier.TIER_1_MICRO_PRUNE

            logger.info(
                "[EchoCompressor] Preflight pass %d: ~%d tokens > %d budget, tier=%s",
                pass_num + 1, total, self.budget.max_input_tokens, tier.name,
            )

            result = await self.compress_async(messages, tier)
            if result:
                last_result = result
            else:
                break  # 無法進一步壓縮

        return last_result

    # -- Session 生命週期 ---------------------------------------------------

    def on_session_reset(self) -> None:
        """重置引擎內部狀態"""
        self.emergency.reset()
        self._last_compaction_time = 0.0
        self._last_compression_savings_pct = 100.0
        self._ineffective_compression_count = 0

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        self._memory_bridge.on_session_start(session_id)

    def on_session_end(
        self, session_id: str, messages: list[Message]
    ) -> None:
        self._memory_bridge.on_session_end(session_id, messages)

    def swap_backend(self, new_backend: AbstractBackend | None) -> None:
        """切換底層 LLM 後端，更新所有子系統的引用。

        更新點：
        - self._backend
        - self.summarizer.backend (RollingSummarizer)
        - self.compactor.backend (FullCompactor)

        不重置壓縮狀態（反抖動、冷卻等保持不變）。

        Args:
            new_backend: 新的 AbstractBackend 實例（None = 離線模式）
        """
        old_name = getattr(self._backend, "model_name", "None")
        new_name = getattr(new_backend, "model_name", "None")

        self._backend = new_backend
        self.summarizer.backend = new_backend
        self.compactor.backend = new_backend

        logger.info(
            "[EchoCompressor] 後端切換: %s → %s",
            old_name, new_name,
        )

    def update_model(self, model: str, context_length: int = 0) -> None:
        """模型切換後重新計算壓縮閾值"""
        if context_length > 0:
            self.budget.context_window = context_length
            # 重新計算各層閾值
            self.config.context_window_size = context_length
        self.counter.model = model
        logger.info(
            "[EchoCompressor] 模型切換: %s, context_length=%d",
            model, context_length,
        )

    def update_from_response(self, usage: dict[str, Any]) -> None:
        """從 API 回應更新 token 使用量"""
        self.budget.update_api_usage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cache_write_tokens=usage.get("cache_write_tokens", 0),
        )

    # -- 狀態報告 -----------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        report = self.budget.get_usage_report()
        report["engine"] = self.name
        report["anti_thrashing"] = {
            "last_savings_pct": round(self._last_compression_savings_pct, 2),
            "ineffective_count": self._ineffective_compression_count,
        }
        return report

    # -- 回調管理（deprecated，保持向後相容）--------------------------------

    def on_compaction(
        self, callback: Callable[[CompactionResult], Awaitable[None]]
    ) -> None:
        """註冊壓縮事件回調（deprecated: 請改用 MemoryBridge）"""
        self._on_compaction_callbacks.append(callback)

    # -- 私有方法 -----------------------------------------------------------

    def _can_compact(self) -> bool:
        """檢查壓縮冷卻"""
        now = time.time()
        return now - self._last_compaction_time >= self.config.compaction_cooldown_seconds

    def _track_efficiency(self, result: CompactionResult) -> None:
        """追蹤壓縮效率（反抖動）"""
        if result.tokens_before > 0:
            pct = result.tokens_saved / result.tokens_before
        else:
            pct = 0.0
        self._last_compression_savings_pct = pct * 100

        if pct < 0.10:
            self._ineffective_compression_count += 1
        else:
            self._ineffective_compression_count = 0

    def _apply_layer1(self, messages: list[Message]) -> CompactionResult | None:
        """執行 Layer 1 微清理"""
        start = time.time()
        tokens_before = sum(m.token_count or 0 for m in messages)
        pruned, saved = self.pruner.prune(messages)

        if saved > 0:
            messages[:] = pruned
            self._last_compaction_time = time.time()

            result = CompactionResult(
                tier=CompressionTier.TIER_1_MICRO_PRUNE,
                tokens_before=tokens_before,
                tokens_after=tokens_before - saved,
                tokens_saved=saved,
                compression_ratio=saved / tokens_before if tokens_before else 0,
                duration_ms=(time.time() - start) * 1000,
            )
            self._track_efficiency(result)
            return result
        return None

    async def _apply_layer2(
        self, messages: list[Message]
    ) -> CompactionResult | None:
        """執行 Layer 2 增量摘要"""
        if not self._backend:
            return self._apply_layer1(messages)

        start = time.time()
        tokens_before = sum(m.token_count or 0 for m in messages)

        # MemoryBridge: 壓縮前提取記憶上下文，注入摘要提示
        memory_context = self._memory_bridge.on_pre_compress(messages)
        extra_instructions = (
            f"\n\nIMPORTANT — preserve these facts from memory:\n{memory_context}"
            if memory_context
            else None
        )

        try:
            updated, summary_text, saved = await self.summarizer.summarize_oldest(
                messages, n_turns=10, instructions=extra_instructions,
            )

            if saved > 0:
                messages[:] = updated
                self._last_compaction_time = time.time()

                result = CompactionResult(
                    tier=CompressionTier.TIER_2_ROLLING_SUMMARY,
                    tokens_before=tokens_before,
                    tokens_after=tokens_before - saved,
                    tokens_saved=saved,
                    compression_ratio=saved / tokens_before if tokens_before else 0,
                    summary_text=summary_text,
                    duration_ms=(time.time() - start) * 1000,
                )
                self._track_efficiency(result)

                # 通知 MemoryBridge + legacy callbacks
                self._memory_bridge.on_post_compress(result)
                for cb in self._on_compaction_callbacks:
                    await cb(result)

                return result

        except Exception as exc:
            logger.warning(
                "[EchoCompressor] Layer 2 摘要失敗，降級為 Layer 1: %s",
                exc, exc_info=True,
            )

        return self._apply_layer1(messages)

    async def _apply_layer3(
        self,
        messages: list[Message],
        instructions: str | None = None,
    ) -> CompactionResult:
        """執行 Layer 3 完整壓縮"""
        if not self._backend:
            result = self._apply_layer1(messages)
            if result:
                return result
            tokens = sum(m.token_count or 0 for m in messages)
            return CompactionResult(
                tier=CompressionTier.TIER_3_FULL_COMPACTION,
                tokens_before=tokens,
                tokens_after=tokens,
                tokens_saved=0,
            )

        start = time.time()
        sacred = self.sacred.get_sacred_content()
        tokens_before = sum(m.token_count or 0 for m in messages)

        # MemoryBridge: 壓縮前提取記憶上下文，注入壓縮提示
        memory_context = self._memory_bridge.on_pre_compress(messages)
        effective_instructions = instructions or ""
        if memory_context:
            effective_instructions += (
                f"\n\nIMPORTANT — preserve these facts from memory:\n{memory_context}"
            )

        try:
            compacted, summary_text, saved = await self.compactor.compact(
                messages, sacred, effective_instructions or None,
            )

            messages[:] = compacted
            # 重新計算壓縮後的 token
            for m in messages:
                if m.token_count is None:
                    self.counter.count_message(m)
            tokens_after = sum(m.token_count or 0 for m in messages)

            self._last_compaction_time = time.time()

            result = CompactionResult(
                tier=CompressionTier.TIER_3_FULL_COMPACTION,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                tokens_saved=max(0, tokens_before - tokens_after),
                compression_ratio=(tokens_before - tokens_after) / tokens_before if tokens_before else 0,
                summary_text=summary_text,
                duration_ms=(time.time() - start) * 1000,
            )
            self._track_efficiency(result)

            # 通知 MemoryBridge + legacy callbacks
            self._memory_bridge.on_post_compress(result)
            for cb in self._on_compaction_callbacks:
                await cb(result)

            return result

        except Exception as exc:
            logger.warning(
                "[EchoCompressor] Layer 3 壓縮失敗，降級為 Layer 1: %s",
                exc, exc_info=True,
            )
            result = self._apply_layer1(messages)
            if result:
                return result
            return CompactionResult(
                tier=CompressionTier.TIER_3_FULL_COMPACTION,
                tokens_before=tokens_before,
                tokens_after=sum(m.token_count or 0 for m in messages),
                tokens_saved=0,
            )

    def _apply_layer4(self, messages: list[Message]) -> CompactionResult:
        """執行 Layer 4 緊急截斷"""
        start = time.time()
        sacred = self.sacred.get_sacred_content()
        tokens_before = sum(m.token_count or 0 for m in messages)

        truncated, saved = self.emergency.compact(messages, sacred)
        messages[:] = truncated

        # 重新計算
        for m in messages:
            if m.token_count is None:
                self.counter.count_message(m)
        tokens_after = sum(m.token_count or 0 for m in messages)

        self._last_compaction_time = time.time()
        actual_saved = max(0, tokens_before - tokens_after)

        result = CompactionResult(
            tier=CompressionTier.TIER_4_EMERGENCY,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_saved=actual_saved,
            compression_ratio=actual_saved / tokens_before if tokens_before else 0,
            duration_ms=(time.time() - start) * 1000,
        )
        # Emergency 不追蹤反抖動
        return result
