"""
Context Manager — 會話控制主入口

統合 ContextEngine、Token Accounting、對話歷史管理。
提供簡單的 API 介面供外部使用。

內部委託 EchoCompressor（或任意 ContextEngine 實作）執行壓縮。
公開 API 保持向後相容。

整合點標記（🔌 INTEGRATION POINT）：
  標記為 🔌 的位置是設計給其他子系統整合的介面。
  目前有獨立可用的預設實作，未來接入外部模組時替換。
  - MemoryBridge → Echo Memory 系統（跨會話記憶持久化）
  - update_from_response → LLM API 回應後精確 token 追蹤
  - preflight_compress → API 呼叫前自動壓縮
  - update_model → 動態模型切換
  - should_end_session → 動態會話邊界判斷

Usage:
    manager = ContextManager(config=config, persona=persona)
    manager.add_message(role="user", content="Hello!")
    messages = manager.get_messages()
    usage = manager.get_usage()
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

from echo_thought_core.backends.base import AbstractBackend
from echo_thought_core.engine_base import ContextEngine
from echo_thought_core.schemas.config import CompactionConfig, PersonaContext
from echo_thought_core.schemas.message import ConversationHistory, Message
from echo_thought_core.schemas.session import (
    CompactionResult,
    CompressionTier,
    SessionState,
)

if TYPE_CHECKING:
    from echo_thought_core.accounting.budget import TokenBudget
    from echo_thought_core.accounting.counter import TokenCounter
    from echo_thought_core.compression.emergency import EmergencyCompactor
    from echo_thought_core.compression.full_compaction import FullCompactor
    from echo_thought_core.compression.micro_pruning import MicroPruner
    from echo_thought_core.compression.rolling_summary import RollingSummarizer
    from echo_thought_core.compression.sacred import SacredManager
    from echo_thought_core.memory_bridge import MemoryBridge


logger = logging.getLogger(__name__)


class ContextManager:
    """會話控制主入口 — 委託 ContextEngine 執行壓縮"""

    def __init__(
        self,
        config: CompactionConfig | None = None,
        persona: PersonaContext | None = None,
        backend: AbstractBackend | None = None,
        engine: ContextEngine | None = None,
        memory_bridge: Optional["MemoryBridge"] = None,
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

        # 🔌 INTEGRATION POINT: Session lifecycle — on_session_start
        # 通知引擎（及 MemoryBridge）會話已開始
        self._engine.on_session_start(self.state.session_id)

    # -- 公開 API（向後相容）------------------------------------------------

    def add_message(
        self,
        role: str,
        content: str,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        is_reproducible: bool = True,
        metadata: dict[str, Any] | None = None,
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

    def get_messages(self) -> list[dict[str, str]]:
        """取得供 API 使用的訊息列表（同步版本只做 Layer 1）"""
        # 🔌 INTEGRATION POINT: multimodal 圖片清理
        # strip_old_images 在壓縮判斷前執行，防止圖片累積
        self._strip_old_images_if_needed()

        tier = self._engine.should_compress(self.history.messages)

        if tier >= CompressionTier.TIER_1_MICRO_PRUNE:
            result = self._engine.compress(self.history.messages, tier)
            if result:
                self._after_compression(result)

        return [
            {"role": m.role, "content": m.content}
            for m in self.history.messages
        ]

    async def get_messages_async(self) -> list[dict[str, str]]:
        """取得供 API 使用的訊息列表（異步版本，支援 Layer 2/3）"""
        self._strip_old_images_if_needed()

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
        self, instructions: str | None = None
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

    def handle_api_error(self, error_code: int) -> CompactionResult | None:
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

    def get_usage(self) -> dict[str, object]:
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
        old_session_id = self.state.session_id

        # 🔌 INTEGRATION POINT: Session lifecycle — on_session_end
        # 通知引擎（及 MemoryBridge）舊會話結束
        self._engine.on_session_end(old_session_id, self.history.messages)

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

        # 同步清空 LLMClient 歷史
        if self._backend is not None and hasattr(self._backend, "clear_history"):
            self._backend.clear_history()

        # 🔌 INTEGRATION POINT: Session lifecycle — on_session_start (new session)
        self._engine.on_session_start(self.state.session_id)

    # -- 🔌 INTEGRATION POINT: 新增 API（供外部整合使用）---------------------

    def update_from_response(self, usage: dict[str, Any]) -> None:
        """從 API 回應更新實際 token 使用量。

        🔌 INTEGRATION POINT: LLM API 回應後精確追蹤
        整合方式：在每次 LLM API 呼叫後，將 usage dict 傳入。
        未來接入 U.E.P Core 的 LLM Module 時，由 event bus 觸發。

        Args:
            usage: API 回應的 usage 資訊，可包含：
                   input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens
        """
        self._engine.update_from_response(usage)

    async def preflight_compress(
        self,
        estimated_request_tokens: int = 0,
        max_passes: int = 3,
    ) -> CompactionResult | None:
        """在 API 呼叫前執行預防性壓縮。

        🔌 INTEGRATION POINT: API 呼叫前自動壓縮
        整合方式：在送出 API 請求前呼叫，傳入預估的
        system prompt + tools schema token 數。

        Args:
            estimated_request_tokens: 預估的固定開銷 token 數
            max_passes: 最多壓縮幾次（預設 3）

        Returns:
            最後一次壓縮的結果，或 None（不需要壓縮）
        """
        result = await self._engine.preflight_compress(
            self.history.messages,
            estimated_request_tokens=estimated_request_tokens,
            max_passes=max_passes,
        )
        if result:
            self._after_compression(result)
        return result

    def switch_backend(
        self,
        backend_type: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Runtime 切換 LLM 後端（provider hot-swap）。

        🔌 INTEGRATION POINT: 動態後端切換
        整合方式：當使用者或系統需要切換 LLM 服務時呼叫。

        切換流程：
        1. 從 BackendRegistry 建立新後端實例
        2. 將當前對話歷史同步到新後端
        3. 更新引擎所有子系統的後端引用
        4. 更新 config 中的 backend/model 設定
        5. 若新後端有不同的 context_window，更新預算

        Args:
            backend_type: 新後端名稱（如 "ollama", "gemini"）
            config:       後端設定 dict
        """
        from echo_thought_core.backends.registry import BackendRegistry

        registry = BackendRegistry.default()
        new_backend = registry.create(backend_type, config or {})

        old_backend_name = self.config.backend
        new_model = getattr(new_backend, "model_name", "unknown")

        # Step 1: 同步歷史到新後端
        if self.history.messages:
            messages_for_sync = [
                {"role": m.role, "content": m.content}
                for m in self.history.messages
            ]
            if hasattr(new_backend, "sync_history_from_messages"):
                new_backend.sync_history_from_messages(messages_for_sync)

        # Step 2: 更新引擎的所有後端引用
        self._engine.swap_backend(new_backend)
        self._backend = new_backend

        # Step 3: 更新 config
        self.config.backend = backend_type
        self.config.model = new_model

        # Step 4: 若 context_window 不同，更新預算
        new_ctx = new_backend.get_context_window_size()
        if new_ctx != self.config.context_window_size:
            self._engine.update_model(new_model, new_ctx)
            self.config.context_window_size = new_ctx
            self.state.max_tokens = new_ctx

        logger.info(
            "[ContextManager] 後端切換: %s → %s (model=%s, ctx=%d)",
            old_backend_name, backend_type, new_model, new_ctx,
        )

    def update_model(self, model: str, context_length: int = 0) -> None:
        """動態切換模型，重新計算壓縮閾值。

        🔌 INTEGRATION POINT: 動態模型切換
        整合方式：當使用者或系統切換 LLM 模型時呼叫。

        Args:
            model: 新模型名稱
            context_length: 新模型的 context window 大小（0 = 不變）
        """
        self._engine.update_model(model, context_length)
        if context_length > 0:
            self.config.context_window_size = context_length
            self.state.max_tokens = context_length

    def prefetch_memory(self, query: str) -> str:
        """預抓取記憶上下文。

        🔌 INTEGRATION POINT: Echo Memory 每輪預抓取
        整合方式：在每輪對話前呼叫，傳入使用者訊息。
        回傳的字串可注入 system prompt 或 user context。
        需要實作 MemoryBridge 的 prefetch() 方法。

        Args:
            query: 使用者訊息或搜尋關鍵字

        Returns:
            記憶上下文字串（空字串表示無相關記憶）
        """
        return self._engine._memory_bridge.prefetch(query)

    def should_end_session(self, elapsed_seconds: float = 0.0, **signals: Any) -> bool:
        """詢問引擎是否應結束當前 session。

        🔌 INTEGRATION POINT: 動態會話邊界判斷
        整合方式：由 DC bot 或 U.E.P Core 定期呼叫。
        需要子類覆寫 ContextEngine.should_end_session()。
        目前預設回傳 False（外部控制邊界）。

        Args:
            elapsed_seconds: 當前 session 已持續的秒數
            **signals: 額外信號（topic_drift_score, emotional_shift 等）

        Returns:
            True 表示系統建議結束當前 session
        """
        return self._engine.should_end_session(
            self.history.messages,
            elapsed_seconds=elapsed_seconds,
            **signals,
        )

    # -- 便利屬性（供 CLI 等存取引擎子系統）---------------------------------

    @property
    def counter(self) -> "TokenCounter":
        """TokenCounter（供外部存取）"""
        return self._engine.counter

    @property
    def budget(self) -> "TokenBudget":
        """TokenBudget（供外部存取）"""
        return self._engine.budget

    @property
    def sacred(self) -> "SacredManager":
        """SacredManager（供外部存取）"""
        return self._engine.sacred

    @property
    def pruner(self) -> "MicroPruner":
        """MicroPruner（供外部存取）"""
        return self._engine.pruner

    @property
    def summarizer(self) -> "RollingSummarizer":
        """RollingSummarizer（供外部存取）"""
        return self._engine.summarizer

    @property
    def compactor(self) -> "FullCompactor":
        """FullCompactor（供外部存取）"""
        return self._engine.compactor

    @property
    def emergency(self) -> "EmergencyCompactor":
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
        """壓縮完成後同步所有狀態（含 LLMClient 歷史同步）"""
        total = sum(m.token_count or 0 for m in self.history.messages)
        self.history.total_tokens = total
        self.history.compression_count += 1
        self._engine.budget.update(history=total)
        self.state.current_tokens = self._engine.budget.total_tokens
        self.state.message_count = len(self.history.messages)
        self.state.compression_count += 1
        self.state.last_compaction_at = time.time()
        self.state.current_tier = self._engine.budget.check_tier()

        # 同步壓縮後的歷史到 LLMClient
        if self._backend is not None and hasattr(self._backend, "sync_history_from_messages"):
            compressed_messages = [
                {"role": m.role, "content": m.content}
                for m in self.history.messages
            ]
            self._backend.sync_history_from_messages(compressed_messages)

    def _strip_old_images_if_needed(self) -> None:
        """在壓縮判斷前清理舊的 base64 圖片。

        🔌 INTEGRATION POINT: multimodal 圖片管理
        在 get_messages() 前自動執行，防止圖片佔用 context window。
        """
        from echo_thought_core.compression.multimodal import strip_old_images

        updated, saved = strip_old_images(self.history.messages, keep_last_n=3)
        if saved > 0:
            self.history.messages = updated
            self._sync_state()
            logger.info(
                "[ContextManager] 清理舊圖片，節省 ~%d tokens", saved,
            )
