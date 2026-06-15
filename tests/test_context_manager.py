"""
ContextManager 整合測試
"""

import pytest
from echo_thought_core import ContextManager, CompactionConfig
from echo_thought_core.schemas.session import CompressionTier


class TestContextManager:
    def test_create_manager(self, manager):
        assert manager is not None
        assert manager.state.session_id is not None
        assert manager.state.current_tokens == 0

    def test_add_message(self, manager):
        msg = manager.add_message(role="user", content="Hello!")
        assert msg.role == "user"
        assert msg.content == "Hello!"
        assert msg.token_count is not None
        assert msg.token_count > 0
        assert manager.state.message_count == 1
        assert manager.state.current_tokens > 0

    def test_add_multiple_messages(self, manager):
        manager.add_message(role="user", content="Hello!")
        manager.add_message(role="assistant", content="Hi there!")
        manager.add_message(role="user", content="How are you?")
        assert manager.state.message_count == 3

    def test_get_messages(self, manager):
        manager.add_message(role="user", content="Hello!")
        manager.add_message(role="assistant", content="Hi!")
        messages = manager.get_messages()
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    def test_get_usage(self, manager):
        manager.add_message(role="user", content="Hello!")
        usage = manager.get_usage()
        assert "total_tokens" in usage
        assert "context_window" in usage
        assert "usage_percentage" in usage
        assert "session_id" in usage
        assert usage["total_tokens"] > 0

    def test_tier_escalation(self):
        """測試 token 累積觸發壓縮層級升級"""
        config = CompactionConfig(
            context_window_size=100,  # 極小的 window
            thresholds={
                "micro_prune": 0.50,
                "rolling_summary": 0.70,
                "full_compact": 0.90,
            },
        )
        manager = ContextManager(config=config)

        # 低使用量
        manager.add_message(role="user", content="Hi")
        assert manager.state.current_tier == CompressionTier.TIER_0_NORMAL

    def test_sacred_content_preserved(self, manager):
        """測試神聖段落是否正確設定"""
        sacred = manager.sacred.get_sacred_text()
        assert "TestBot" in sacred

    def test_handle_api_error_413(self, manager):
        """測試 413 錯誤觸發 Layer 4"""
        # 加入一些訊息
        for i in range(10):
            manager.add_message(role="user", content=f"Message {i} " * 20)
            manager.add_message(role="assistant", content=f"Response {i} " * 20)

        result = manager.handle_api_error(413)
        assert result is not None
        assert result.tier == CompressionTier.TIER_4_EMERGENCY
        assert result.tokens_saved > 0

    def test_handle_api_error_other(self, manager):
        """非 413 錯誤不觸發壓縮"""
        result = manager.handle_api_error(500)
        assert result is None

    def test_emergency_circuit_breaker(self, manager):
        """測試緊急壓縮的斷路器"""
        for i in range(5):
            manager.add_message(role="user", content=f"Message {i} " * 20)

        # 第一次 413 應該成功
        result = manager.handle_api_error(413)
        assert result is not None

        # 第二次 413 應該觸發斷路器
        with pytest.raises(RuntimeError, match="Emergency compaction already attempted"):
            manager.handle_api_error(413)

    def test_layer4_token_recount(self):
        """Layer 4 emergency compaction 必須重算所有 token_count=None 的訊息

        回歸測試：emergency 新建的 sacred Message 的 token_count=None，
        若不重算會被當成 0，導致 total_tokens 嚴重低報。
        """
        from echo_thought_core.schemas.config import PersonaContext

        persona = PersonaContext(
            name="TestBot",
            traits=["friendly"],
            custom_instructions="This is a test persona with some meaningful content.",
        )
        config = CompactionConfig(
            context_window_size=100000,
            max_emergency_messages=2,
        )
        mgr = ContextManager(config=config, persona=persona, backend=None)

        # 加入足夠多的訊息
        for i in range(20):
            mgr.add_message("user", f"Message {i}: " + "test content " * 20)
            mgr.add_message("assistant", f"Reply {i}: " + "response data " * 20)

        tokens_before = mgr.history.total_tokens
        assert tokens_before > 0

        result = mgr.handle_api_error(413)
        assert result is not None
        assert result.tier == CompressionTier.TIER_4_EMERGENCY

        # 驗證重算：history.total_tokens 必須等於逐一加總（無 None 被當 0）
        actual_recount = sum(m.token_count for m in mgr.history.messages)
        assert mgr.history.total_tokens == actual_recount
        assert mgr.history.total_tokens > 0

        # 確認沒有任何訊息的 token_count 仍為 None
        for m in mgr.history.messages:
            assert m.token_count is not None, (
                f"Message '{m.content[:30]}...' still has token_count=None after Layer 4"
            )

        # tokens_after 與 result 一致
        assert result.tokens_after == mgr.history.total_tokens
        assert result.tokens_saved == tokens_before - mgr.history.total_tokens

    def test_reset_session(self, manager):
        """reset_session() 完全重置所有狀態"""
        # 累積一些狀態
        for i in range(5):
            manager.add_message("user", f"Message {i}")
        old_session_id = manager.state.session_id

        manager.reset_session()

        assert manager.state.current_tokens == 0
        assert manager.state.message_count == 0
        assert manager.state.compression_count == 0
        assert manager.state.current_tier == CompressionTier.TIER_0_NORMAL
        assert len(manager.history.messages) == 0
        assert manager.history.total_tokens == 0
        assert manager.state.session_id != old_session_id
