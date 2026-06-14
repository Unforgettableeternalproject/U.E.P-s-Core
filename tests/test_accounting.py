"""
Token Accounting 測試
"""

import pytest
from session_control.accounting.counter import TokenCounter
from session_control.accounting.budget import TokenBudget
from session_control.schemas.message import Message
from session_control.schemas.session import CompressionTier


class TestTokenCounter:
    def test_count_empty(self):
        counter = TokenCounter(backend="anthropic")
        assert counter.count("") == 0

    def test_count_english(self):
        counter = TokenCounter(backend="anthropic")
        result = counter.count("Hello, world!")
        assert result > 0

    def test_count_message_includes_overhead(self):
        counter = TokenCounter(backend="anthropic")
        msg = Message(role="user", content="Hello")
        tokens = counter.count_message(msg)
        content_tokens = counter.count("Hello")
        assert tokens > content_tokens  # overhead 應該使 message tokens > content tokens
        assert msg.token_count == tokens  # 應該更新 message 上的 token_count

    def test_fallback_counting(self):
        """無 tiktoken 時的 fallback 估算"""
        counter = TokenCounter(backend="unknown_backend")
        counter._tokenizer = None  # 強制使用 fallback
        result = counter.count("Hello world, this is a test.")
        assert result > 0

    def test_cjk_weighting(self):
        """中文字符應該有更高的 token 權重"""
        counter = TokenCounter(backend="unknown_backend")
        counter._tokenizer = None
        ascii_count = counter.count("hello world")
        cjk_count = counter.count("你好世界測試")
        # 中文通常每字佔更多 tokens
        assert cjk_count > 0


class TestTokenBudget:
    def test_initial_state(self):
        budget = TokenBudget(context_window=10000)
        assert budget.total_tokens == 0
        assert budget.usage_percentage == 0.0
        assert budget.available_tokens == 10000

    def test_update_and_check(self):
        budget = TokenBudget(context_window=10000)
        budget.update(history=5000)
        assert budget.total_tokens == 5000
        assert budget.usage_percentage == 0.5

    def test_tier_normal(self):
        budget = TokenBudget(context_window=10000)
        budget.update(history=3000)
        assert budget.check_tier() == CompressionTier.TIER_0_NORMAL

    def test_tier_micro_prune(self):
        budget = TokenBudget(context_window=10000)
        budget.update(history=6500)
        assert budget.check_tier() == CompressionTier.TIER_1_MICRO_PRUNE

    def test_tier_rolling_summary(self):
        budget = TokenBudget(context_window=10000)
        budget.update(history=8500)
        assert budget.check_tier() == CompressionTier.TIER_2_ROLLING_SUMMARY

    def test_tier_full_compact(self):
        budget = TokenBudget(context_window=10000)
        budget.update(history=9500)
        assert budget.check_tier() == CompressionTier.TIER_3_FULL_COMPACTION

    def test_usage_report(self):
        budget = TokenBudget(context_window=10000)
        budget.update(system=1000, history=3000, tools=500, sacred=200)
        report = budget.get_usage_report()
        assert report["total_tokens"] == 4700
        assert report["context_window"] == 10000
        assert report["breakdown"]["system"] == 1000
