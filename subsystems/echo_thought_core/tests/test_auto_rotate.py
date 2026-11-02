"""
Auto Session Rotate 測試

驗證：
- auto_rotate_session() 完整流程（壓縮→換 session→摘要注入）
- get_messages_async() 自動觸發 rotate
- MemoryBridge 通知正確（on_session_end / on_session_start 各一次）
- last_rotate_result 屬性行為（讀取後清空）
- 壓縮失敗時不 rotate
- CompactionResult.session_rotated 欄位
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from echo_thought_core import CompactionConfig, ContextManager
from echo_thought_core.memory_bridge import NullMemoryBridge
from echo_thought_core.schemas.message import Message
from echo_thought_core.schemas.session import CompactionResult, CompressionTier


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def small_config():
    """極小 context window，方便觸發壓縮"""
    return CompactionConfig(
        context_window_size=500,
        thresholds={
            "micro_prune": 0.50,
            "rolling_summary": 0.70,
            "full_compact": 0.90,
        },
        compaction_cooldown_seconds=0,
    )


@pytest.fixture
def mock_memory_bridge():
    """追蹤呼叫的 MemoryBridge mock"""
    bridge = MagicMock(spec=NullMemoryBridge)
    bridge.on_pre_compress.return_value = ""
    bridge.on_post_compress.return_value = None
    bridge.on_session_start.return_value = None
    bridge.on_session_end.return_value = None
    bridge.prefetch.return_value = ""
    return bridge


def _fill_messages(manager: ContextManager, n: int = 20) -> None:
    """灌入 n 組 user/assistant 訊息"""
    for i in range(n):
        manager.add_message("user", f"Test message #{i}: " + "lorem ipsum " * 8)
        manager.add_message("assistant", f"Response #{i}: " + "dolor sit amet " * 8)


# ── CompactionResult 新欄位 ─────────────────────────────────────────────────


class TestCompactionResultFields:
    """T-01-1: CompactionResult 新增欄位"""

    def test_default_values(self):
        """新欄位有正確的預設值"""
        result = CompactionResult(
            tier=CompressionTier.TIER_1_MICRO_PRUNE,
            tokens_before=100,
            tokens_after=80,
        )
        assert result.session_rotated is False
        assert result.previous_session_id is None
        assert result.new_session_id is None

    def test_backward_compat(self):
        """不傳新欄位時向後相容"""
        result = CompactionResult(
            tier=CompressionTier.TIER_3_FULL_COMPACTION,
            tokens_before=1000,
            tokens_after=200,
            tokens_saved=800,
        )
        assert result.savings_percentage == 0.8
        assert result.session_rotated is False

    def test_with_rotate_fields(self):
        """可以設定 rotate 欄位"""
        result = CompactionResult(
            tier=CompressionTier.TIER_3_FULL_COMPACTION,
            tokens_before=1000,
            tokens_after=200,
            tokens_saved=800,
            session_rotated=True,
            previous_session_id="abc123",
            new_session_id="def456",
        )
        assert result.session_rotated is True
        assert result.previous_session_id == "abc123"
        assert result.new_session_id == "def456"


# ── Auto Rotate Session ────────────────────────────────────────────────────


class TestAutoRotateSession:
    """T-01-2: auto_rotate_session() 方法"""

    @pytest.mark.asyncio
    async def test_rotate_changes_session_id(self, small_config):
        """rotate 後 session_id 改變"""
        mgr = ContextManager(config=small_config, backend=None)
        old_session_id = mgr.state.session_id
        _fill_messages(mgr, n=5)

        # 手動 mock 引擎的壓縮方法，讓它回傳成功結果
        async def fake_compress(messages, tier, instructions=None):
            saved = sum(m.token_count or 0 for m in messages) // 2
            summary_msg = Message(
                role="system",
                content="[COMPRESSED SUMMARY] test summary",
                is_compressed=True,
                token_count=20,
            )
            messages.clear()
            messages.append(summary_msg)
            return CompactionResult(
                tier=tier,
                tokens_before=saved * 2,
                tokens_after=20,
                tokens_saved=saved,
                summary_text="test summary",
                metadata={"original_message_count": 10},
            )

        mgr._engine.compress_async = fake_compress

        result = await mgr.auto_rotate_session()
        assert result is not None
        assert result.session_rotated is True
        assert result.previous_session_id == old_session_id
        assert result.new_session_id == mgr.state.session_id
        assert mgr.state.session_id != old_session_id

    @pytest.mark.asyncio
    async def test_rotate_preserves_summary(self, small_config):
        """rotate 後歷史只剩壓縮摘要"""
        mgr = ContextManager(config=small_config, backend=None)
        _fill_messages(mgr, n=5)

        async def fake_compress(messages, tier, instructions=None):
            summary_msg = Message(
                role="system",
                content="[COMPRESSED] summary content here",
                is_compressed=True,
                token_count=15,
            )
            messages.clear()
            messages.append(summary_msg)
            return CompactionResult(
                tier=tier,
                tokens_before=500,
                tokens_after=15,
                tokens_saved=485,
                metadata={"original_message_count": 10},
            )

        mgr._engine.compress_async = fake_compress

        result = await mgr.auto_rotate_session()
        assert result is not None
        assert len(mgr.history.messages) == 1
        assert mgr.history.messages[0].is_compressed is True
        assert "summary content here" in mgr.history.messages[0].content

    @pytest.mark.asyncio
    async def test_rotate_notifies_memory_bridge_once(self, small_config, mock_memory_bridge):
        """rotate 過程中 MemoryBridge 的 on_session_end/start 各只呼叫一次"""
        mgr = ContextManager(
            config=small_config,
            backend=None,
            memory_bridge=mock_memory_bridge,
        )
        # init 會呼叫 on_session_start 一次，先 reset mock
        mock_memory_bridge.reset_mock()

        _fill_messages(mgr, n=5)
        old_session = mgr.state.session_id

        async def fake_compress(messages, tier, instructions=None):
            summary_msg = Message(
                role="system", content="summary", is_compressed=True, token_count=10,
            )
            messages.clear()
            messages.append(summary_msg)
            return CompactionResult(
                tier=tier, tokens_before=300, tokens_after=10, tokens_saved=290,
                metadata={},
            )

        mgr._engine.compress_async = fake_compress

        result = await mgr.auto_rotate_session()
        assert result is not None

        # on_session_end 和 on_session_start 各只呼叫一次
        end_calls = [c for c in mock_memory_bridge.on_session_end.call_args_list]
        start_calls = [c for c in mock_memory_bridge.on_session_start.call_args_list]
        assert len(end_calls) == 1, f"on_session_end called {len(end_calls)} times"
        assert len(start_calls) == 1, f"on_session_start called {len(start_calls)} times"

        # 確認舊/新 session ID 正確
        assert end_calls[0][0][0] == old_session
        assert start_calls[0][0][0] == mgr.state.session_id

    @pytest.mark.asyncio
    async def test_rotate_skipped_when_compression_fails(self, small_config):
        """壓縮失敗（saved=0）時不 rotate"""
        mgr = ContextManager(config=small_config, backend=None)
        old_session = mgr.state.session_id
        _fill_messages(mgr, n=3)

        async def fake_no_savings(messages, tier, instructions=None):
            return CompactionResult(
                tier=tier, tokens_before=100, tokens_after=100, tokens_saved=0,
            )

        mgr._engine.compress_async = fake_no_savings

        result = await mgr.auto_rotate_session()
        assert result is None
        assert mgr.state.session_id == old_session  # 沒有換

    @pytest.mark.asyncio
    async def test_rotate_syncs_backend(self, small_config):
        """rotate 後同步 backend 歷史"""
        mock_backend = MagicMock()
        mock_backend.get_context_window_size.return_value = 500
        mock_backend.sync_history_from_messages = MagicMock()

        mgr = ContextManager(config=small_config, backend=mock_backend)
        _fill_messages(mgr, n=3)

        async def fake_compress(messages, tier, instructions=None):
            summary_msg = Message(
                role="system", content="summary", is_compressed=True, token_count=10,
            )
            messages.clear()
            messages.append(summary_msg)
            return CompactionResult(
                tier=tier, tokens_before=300, tokens_after=10, tokens_saved=290,
                metadata={},
            )

        mgr._engine.compress_async = fake_compress

        await mgr.auto_rotate_session()

        # 驗證 sync_history_from_messages 被呼叫
        mock_backend.sync_history_from_messages.assert_called()
        synced = mock_backend.sync_history_from_messages.call_args[0][0]
        assert len(synced) == 1
        assert synced[0]["content"] == "summary"

    @pytest.mark.asyncio
    async def test_rotate_resets_engine_state(self, small_config):
        """rotate 後引擎內部狀態（反抖動等）被重置"""
        mgr = ContextManager(config=small_config, backend=None)
        # 人為設定反抖動計數器
        mgr._engine._ineffective_compression_count = 5
        mgr._engine._last_compression_savings_pct = 2.0

        _fill_messages(mgr, n=3)

        async def fake_compress(messages, tier, instructions=None):
            summary_msg = Message(
                role="system", content="s", is_compressed=True, token_count=5,
            )
            messages.clear()
            messages.append(summary_msg)
            return CompactionResult(
                tier=tier, tokens_before=200, tokens_after=5, tokens_saved=195,
                metadata={},
            )

        mgr._engine.compress_async = fake_compress

        await mgr.auto_rotate_session()

        assert mgr._engine._ineffective_compression_count == 0
        assert mgr._engine._last_compression_savings_pct == 100.0


# ── last_rotate_result 屬性 ────────────────────────────────────────────────


class TestLastRotateResult:
    """T-01-3: last_rotate_result 屬性"""

    def test_initially_none(self):
        """初始為 None"""
        mgr = ContextManager(config=CompactionConfig(context_window_size=1000))
        assert mgr.last_rotate_result is None

    def test_read_clears(self):
        """讀取後自動清空"""
        mgr = ContextManager(config=CompactionConfig(context_window_size=1000))
        fake_result = CompactionResult(
            tier=CompressionTier.TIER_3_FULL_COMPACTION,
            tokens_before=100, tokens_after=20, tokens_saved=80,
            session_rotated=True,
        )
        mgr._last_rotate_result = fake_result

        # 第一次讀取
        got = mgr.last_rotate_result
        assert got is not None
        assert got.session_rotated is True

        # 第二次讀取應為 None
        assert mgr.last_rotate_result is None
