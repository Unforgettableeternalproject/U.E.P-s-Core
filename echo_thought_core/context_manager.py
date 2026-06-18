"""
Context Manager — 會話控制主入口

統合 ContextEngine、Token Accounting、對話歷史管理。
提供簡單的 API 介面供外部使用。

內部委託 EchoCompressor（或任意 ContextEngine 實作）執行壓縮。
公開 API 保持向後相容。

Usage:
    manager = ContextManager(config=config, persona=persona)
    manager.add_message(role="user", content="Hello!")
    messages = manager.get_messages()
    usage = manager.get_usage()
"""

import logging
import time
import uuid
from typing import Any, Callable, Awaitable, Dict, List, Optional

from echo_thought_core.schemas.config import CompactionConfig, PersonaContext
from echo_thought_core.schemas.message import Message, ConversationHistory
from echo_thought_core.schemas.session import (
    SessionState,
    CompactionResult,
    CompressionTier,
)
from echo_thought_core.engine_base import ContextEngine
from echo_thought_core.backends.base import AbstractBackend


logger = logging.getLogger(__name__)


class ContextManager:
    """會話控制主入口 — 委託 ContextEngine 執行壓縮"""

    def __init__(
        self,
        config: Optional[CompactionConfig] = None,
        persona: Optional[PersonaContext] = None,
        backend: Optional[AbstractBackend] = None,
        engine: Optional[ContextEngine] = None,
        memory_bridge: Optional[Any] = None,
    ):
        self.config = config or CompactionConfig()
        self.persona = persona

        # Session 狀態
        self.state = SessionState(
            session_id=str(uuid.uuid4())[:8],
            max_tokens=self.config.context_window_size,
        )

        # 後端引用（供外部存取，如 CLI）
        self._backend = backend

        # 壓縮引擎（預設為 EchoCompressor）
        if engine is not None:
            self._engine = engine
        else:
            from echo_thought_core.echo_compressor import EchoCompressor

            self._engine = EchoCompressor(
                config=self.config,
                persona=persona,
                backend=backend,
                memory_bridge=memory_bridge,
            )

        # 對話歷史
        self.history = ConversationHistory()

    # -- 公開 API（向後相容）------------------------------------------------

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
        self._engine.counter.count_message(msg)

        # 加入歷史
        self.history.add(msg)

        # 更新狀態
        self._sync_state()

        return msg

    def get_messages(self) -> List[Dict[str, str]]:
        """取得供 API 使用的訊息列表（同步版本只做 Layer 1）"""
        tier = self._engine.should_compress(self.history.messages)

        if tier >= CompressionTier.TIER_1_MICRO_PRUNE:
            result = self._engine.compress(self.history.messages, tier)
            if result:
                self._after_compression(result)

        return [
            {"role": m.role, "content": m.content}
            for m in self.history.messages
        ]

    async def get_messages_async(self) -> List[Dict[str, str]]:
        """取得供 API 使用的訊息列表（異步版本，支援 Layer 2/3）"""
        tier = self._engine.should_compress(self.history.messages)

        if tier >= CompressionTier.TIER_1_MICRO_PRUNE:
            result = await self._engine.compress_async(
                self.history.messages, tier
            )
            if result:
                self._after_compression(result)

        return [
            {"role": m.role, "content": m.content}
            for m in self.history.messages
        ]

    async def compact(
        self, instructions: Optional[str] = None
    ) -> CompactionResult:
        """手動觸發壓縮（對應 /compact 命令）"""
        result = await self._engine.compress_async(
            self.history.messages,
            CompressionTier.TIER_3_FULL_COMPACTION,
            instructions=instructions,
        )
        if result:
            self._after_compression(result)
            return result

        tokens = self.history.total_tokens
        return CompactionResult(
            tier=CompressionTier.TIER_3_FULL_COMPACTION,
            tokens_before=tokens,
            tokens_after=tokens,
            tokens_saved=0,
        )

    def handle_api_error(self, error_code: int) -> Optional[CompactionResult]:
        """處理 API 錯誤（413 等）觸發 Layer 4"""
        if error_code == 413:
            result = self._engine.compress(
                self.history.messages,
                CompressionTier.TIER_4_EMERGENCY,
            )
            if result:
                self._after_compression(result)
            return result
        return None

    def get_usage(self) -> Dict[str, object]:
        """取得 token 使用量報告"""
        report = self._engine.get_status()
        report["session_id"] = self.state.session_id
        report["message_count"] = self.state.message_count
        report["compression_count"] = self.state.compression_count
        return report

    def on_compaction(
        self, callback: Callable[[CompactionResult], Awaitable[None]]
    ) -> None:
        """註冊壓縮事件回調（deprecated: 請改用 MemoryBridge）"""
        self._engine.on_compaction(callback)

    def reset_session(self) -> None:
        """完全重置會話（對應 /clear 命令）"""
        # 清空歷史
        self.history.messages.clear()
        self.history.total_tokens = 0
        self.history.compression_count = 0

        # 重置 session state
        self.state.session_id = str(uuid.uuid4())[:8]
        self.state.current_tokens = 0
        self.state.message_count = 0
        self.state.compression_count = 0
        self.state.current_tier = CompressionTier.TIER_0_NORMAL
        self.state.last_compaction_at = None
        self.state.created_at = time.time()

        # 重置引擎
        self._engine.on_session_reset()

    # -- 便利屬性（供 CLI 等存取引擎子系統）---------------------------------

    @property
    def counter(self) -> Any:
        """TokenCounter（供外部存取）"""
        return self._engine.counter

    @property
    def budget(self) -> Any:
        """TokenBudget（供外部存取）"""
        return self._engine.budget

    @property
    def sacred(self) -> Any:
        """SacredManager（供外部存取）"""
        return self._engine.sacred

    @property
    def pruner(self) -> Any:
        """MicroPruner（供外部存取）"""
        return self._engine.pruner

    @property
    def summarizer(self) -> Any:
        """RollingSummarizer（供外部存取）"""
        return self._engine.summarizer

    @property
    def compactor(self) -> Any:
        """FullCompactor（供外部存取）"""
        return self._engine.compactor

    @property
    def emergency(self) -> Any:
        """EmergencyCompactor（供外部存取）"""
        return self._engine.emergency

    # -- 私有方法 -----------------------------------------------------------

    def _sync_state(self) -> None:
        """同步引擎 budget 與 session state"""
        total = sum(m.token_count or 0 for m in self.history.messages)
        self.history.total_tokens = total
        self._engine.budget.update(history=total)
        self.state.current_tokens = self._engine.budget.total_tokens
        self.state.message_count = len(self.history.messages)
        self.state.current_tier = self._engine.budget.check_tier()

    def _after_compression(self, result: CompactionResult) -> None:
        """壓縮完成後同步所有狀態"""
        # 重新計算 token（messages 已被引擎原地修改）
        total = sum(m.token_count or 0 for m in self.history.messages)
        self.history.total_tokens = total
        self.history.compression_count += 1
        self._engine.budget.update(history=total)
        self.state.current_tokens = self._engine.budget.total_tokens
        self.state.message_count = len(self.history.messages)
        self.state.compression_count += 1
        self.state.last_compaction_at = time.time()
        self.state.current_tier = self._engine.budget.check_tier()
