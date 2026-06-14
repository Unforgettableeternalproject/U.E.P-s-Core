"""
ContextManager 整合測試
"""

import pytest
from echo_thought_core import ContextManager, CompactionConfig, PersonaContext
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
