"""
Context Manager — 會話控制主入口

統合 Token Accounting、四層壓縮引擎、Backend 抽象、神聖段落保護。
提供簡單的 API 介面供外部使用。

Usage:
    manager = ContextManager(config=config, persona=persona)
    manager.add_message(role="user", content="Hello!")
    messages = manager.get_messages()
    usage = manager.get_usage()
"""

import time
import uuid
from typing import List, Optional, Dict, Any, Callable, Awaitable

from session_control.schemas.config import CompactionConfig, PersonaContext
from session_control.schemas.message import Message, ConversationHistory
from session_control.schemas.session import (
    SessionState,
    CompactionResult,
    CompressionTier,
)
from session_control.accounting.counter import TokenCounter
from session_control.accounting.budget import TokenBudget
from session_control.compression.sacred import SacredManager
from session_control.compression.micro_pruning import MicroPruner
from session_control.compression.rolling_summary import RollingSummarizer
from session_control.compression.full_compaction import FullCompactor
from session_control.compression.emergency import EmergencyCompactor
from session_control.backends.base import AbstractBackend


class ContextManager:
    """會話控制主入口"""

    def __init__(
        self,
        config: Optional[CompactionConfig] = None,
        persona: Optional[PersonaContext] = None,
        backend: Optional[AbstractBackend] = None,
    ):
        self.config = config or CompactionConfig()
        self.persona = persona

        # Session 狀態
        self.state = SessionState(
            session_id=str(uuid.uuid4())[:8],
            max_tokens=self.config.context_window_size,
        )

        # 後端（可選，Layer 2/3 需要）
        self._backend = backend

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

        # 對話歷史
        self.history = ConversationHistory()

        # 壓縮事件回調
        self._on_compaction_callbacks: List[Callable[[CompactionResult], Awaitable[None]]] = []

        # 壓縮冷卻
        self._last_compaction_time: float = 0.0

    def add_message(
        self,
        role: str,
        content: str,
        tool_call_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        is_reproducible: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Message:
        """新增訊息到對話歷史"""
        msg = Message(
            role=role,  # type: ignore[arg-type]
            content=content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            is_reproducible=is_reproducible,
            metadata=metadata or {},
        )

        # 計算 token 數
        self.counter.count_message(msg)

        # 加入歷史
        self.history.add(msg)

        # 更新預算
        self.budget.update(history=self.history.total_tokens)

        # 更新狀態
        self.state.current_tokens = self.budget.total_tokens
        self.state.message_count = len(self.history.messages)
        self.state.current_tier = self.budget.check_tier()

        return msg

    def get_messages(self) -> List[Dict[str, str]]:
        """取得供 API 使用的訊息列表

        自動檢查是否需要觸發壓縮（同步版本只做 Layer 1）。
        """
        tier = self.budget.check_tier()

        if tier >= CompressionTier.TIER_1_MICRO_PRUNE and self._can_compact():
            self._apply_layer1()

        return [
            {"role": m.role, "content": m.content}
            for m in self.history.messages
        ]

    async def get_messages_async(self) -> List[Dict[str, str]]:
        """取得供 API 使用的訊息列表（異步版本，支援 Layer 2/3）"""
        tier = self.budget.check_tier()

        if tier >= CompressionTier.TIER_1_MICRO_PRUNE and self._can_compact():
            if tier >= CompressionTier.TIER_3_FULL_COMPACTION:
                await self._apply_layer3()
            elif tier >= CompressionTier.TIER_2_ROLLING_SUMMARY:
                await self._apply_layer2()
            else:
                self._apply_layer1()

        return [
            {"role": m.role, "content": m.content}
            for m in self.history.messages
        ]

    async def compact(self, instructions: Optional[str] = None) -> CompactionResult:
        """手動觸發壓縮（對應 /compact 命令）"""
        # 強制執行 Layer 3
        return await self._apply_layer3(instructions=instructions)

    def handle_api_error(self, error_code: int) -> Optional[CompactionResult]:
        """處理 API 錯誤（413 等）觸發 Layer 4"""
        if error_code == 413:
            return self._apply_layer4()
        return None

    def get_usage(self) -> Dict[str, object]:
        """取得 token 使用量報告"""
        report = self.budget.get_usage_report()
        report["session_id"] = self.state.session_id
        report["message_count"] = self.state.message_count
        report["compression_count"] = self.state.compression_count
        return report

    def on_compaction(self, callback: Callable[[CompactionResult], Awaitable[None]]) -> None:
        """註冊壓縮事件回調（供 Echo Memory 整合）"""
        self._on_compaction_callbacks.append(callback)

    # === 私有方法 ===

    def _can_compact(self) -> bool:
        """檢查壓縮冷卻"""
        now = time.time()
        if now - self._last_compaction_time < self.config.compaction_cooldown_seconds:
            return False
        return True

    def _apply_layer1(self) -> Optional[CompactionResult]:
        """執行 Layer 1 微清理"""
        start = time.time()
        pruned, saved = self.pruner.prune(self.history.messages)

        if saved > 0:
            self.history.messages = pruned
            self.history.total_tokens -= saved
            self.budget.update(history=self.history.total_tokens)
            self.state.current_tokens = self.budget.total_tokens
            self.state.compression_count += 1
            self._last_compaction_time = time.time()

            return CompactionResult(
                tier=CompressionTier.TIER_1_MICRO_PRUNE,
                tokens_before=self.history.total_tokens + saved,
                tokens_after=self.history.total_tokens,
                tokens_saved=saved,
                compression_ratio=saved / (self.history.total_tokens + saved) if saved else 0,
                duration_ms=(time.time() - start) * 1000,
            )
        return None

    async def _apply_layer2(self) -> Optional[CompactionResult]:
        """執行 Layer 2 增量摘要"""
        if not self._backend:
            # 無後端，降級為 Layer 1
            return self._apply_layer1()

        start = time.time()
        try:
            updated, summary_text, saved = await self.summarizer.summarize_oldest(
                self.history.messages, n_turns=10
            )

            if saved > 0:
                self.history.messages = updated
                self.history.total_tokens -= saved
                self.history.compression_count += 1
                self.budget.update(history=self.history.total_tokens)
                self.state.current_tokens = self.budget.total_tokens
                self.state.compression_count += 1
                self.state.last_compaction_at = time.time()
                self._last_compaction_time = time.time()

                result = CompactionResult(
                    tier=CompressionTier.TIER_2_ROLLING_SUMMARY,
                    tokens_before=self.history.total_tokens + saved,
                    tokens_after=self.history.total_tokens,
                    tokens_saved=saved,
                    compression_ratio=saved / (self.history.total_tokens + saved),
                    summary_text=summary_text,
                    duration_ms=(time.time() - start) * 1000,
                )

                # 通知回調（Echo Memory 整合點）
                for cb in self._on_compaction_callbacks:
                    await cb(result)

                return result

        except Exception:
            # 摘要失敗，降級為 Layer 1
            pass

        return self._apply_layer1()

    async def _apply_layer3(self, instructions: Optional[str] = None) -> CompactionResult:
        """執行 Layer 3 完整壓縮"""
        if not self._backend:
            # 無後端，降級為 Layer 1
            result = self._apply_layer1()
            if result:
                return result
            return CompactionResult(
                tier=CompressionTier.TIER_3_FULL_COMPACTION,
                tokens_before=self.history.total_tokens,
                tokens_after=self.history.total_tokens,
                tokens_saved=0,
            )

        start = time.time()
        sacred = self.sacred.get_sacred_content()
        tokens_before = self.history.total_tokens

        try:
            compacted, summary_text, saved = await self.compactor.compact(
                self.history.messages, sacred, instructions
            )

            self.history.messages = compacted
            self.history.total_tokens = sum(m.token_count or 0 for m in compacted)
            # 重新計算壓縮後訊息的 token
            for m in compacted:
                if m.token_count is None:
                    self.counter.count_message(m)
            self.history.total_tokens = sum(m.token_count or 0 for m in compacted)
            self.history.compression_count += 1
            self.budget.update(history=self.history.total_tokens)
            self.state.current_tokens = self.budget.total_tokens
            self.state.compression_count += 1
            self.state.last_compaction_at = time.time()
            self._last_compaction_time = time.time()

            result = CompactionResult(
                tier=CompressionTier.TIER_3_FULL_COMPACTION,
                tokens_before=tokens_before,
                tokens_after=self.history.total_tokens,
                tokens_saved=max(0, tokens_before - self.history.total_tokens),
                compression_ratio=(tokens_before - self.history.total_tokens) / tokens_before if tokens_before else 0,
                summary_text=summary_text,
                duration_ms=(time.time() - start) * 1000,
            )

            # 通知回調（Echo Memory 整合點）
            for cb in self._on_compaction_callbacks:
                await cb(result)

            return result

        except Exception:
            # 壓縮失敗，降級為 Layer 1
            result = self._apply_layer1()
            if result:
                return result
            return CompactionResult(
                tier=CompressionTier.TIER_3_FULL_COMPACTION,
                tokens_before=tokens_before,
                tokens_after=self.history.total_tokens,
                tokens_saved=0,
            )

    def _apply_layer4(self) -> CompactionResult:
        """執行 Layer 4 緊急截斷"""
        start = time.time()
        sacred = self.sacred.get_sacred_content()
        truncated, saved = self.emergency.compact(
            self.history.messages, sacred
        )

        tokens_before = self.history.total_tokens
        self.history.messages = truncated
        self.history.total_tokens = sum(m.token_count or 0 for m in truncated)
        self.budget.update(history=self.history.total_tokens)
        self.state.current_tokens = self.budget.total_tokens
        self.state.compression_count += 1
        self.state.last_compaction_at = time.time()
        self._last_compaction_time = time.time()

        return CompactionResult(
            tier=CompressionTier.TIER_4_EMERGENCY,
            tokens_before=tokens_before,
            tokens_after=self.history.total_tokens,
            tokens_saved=saved,
            compression_ratio=saved / tokens_before if tokens_before else 0,
            duration_ms=(time.time() - start) * 1000,
        )
