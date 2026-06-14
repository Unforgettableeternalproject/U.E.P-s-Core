"""
Prompt Caching 測試套件

涵蓋 PromptCacheConfig、add_cache_control、mark_cacheable_messages、
add_tools_cache_control、estimate_cache_savings、CacheStats。
純單元測試，零外部 API 呼叫。
"""

from __future__ import annotations

import pytest
from session_control.schemas.message import Message
from session_control.prompt.caching import (
    PromptCacheConfig,
    add_cache_control,
    add_tools_cache_control,
    mark_cacheable_messages,
    estimate_cache_savings,
    CacheStats,
    PRICE_NORMAL_PER_M,
    PRICE_CACHE_WRITE_PER_M,
    PRICE_CACHE_READ_PER_M,
)


# ── 輔助 ──────────────────────────────────────────────────────────────────────


def _make_msg(role: str, content: str = "hello", token_count: int = 100) -> Message:
    return Message(role=role, content=content, token_count=token_count)  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════════════
# PromptCacheConfig 預設值
# ══════════════════════════════════════════════════════════════════════════════


class TestPromptCacheConfigDefaults:
    def test_enabled_true_by_default(self):
        cfg = PromptCacheConfig()
        assert cfg.enabled is True

    def test_min_cacheable_tokens_default(self):
        cfg = PromptCacheConfig()
        assert cfg.min_cacheable_tokens == 1024

    def test_max_breakpoints_default(self):
        cfg = PromptCacheConfig()
        assert cfg.max_breakpoints == 4

    def test_cache_system_prompt_true_by_default(self):
        cfg = PromptCacheConfig()
        assert cfg.cache_system_prompt is True

    def test_cache_tools_true_by_default(self):
        cfg = PromptCacheConfig()
        assert cfg.cache_tools is True

    def test_cache_history_true_by_default(self):
        cfg = PromptCacheConfig()
        assert cfg.cache_history is True

    def test_history_keep_recent_default(self):
        cfg = PromptCacheConfig()
        assert cfg.history_keep_recent == 2


# ══════════════════════════════════════════════════════════════════════════════
# add_cache_control
# ══════════════════════════════════════════════════════════════════════════════


class TestAddCacheControl:
    def test_string_input_returns_list(self):
        result = add_cache_control("some text")
        assert isinstance(result, list)
        assert len(result) == 1

    def test_string_input_produces_text_block(self):
        result = add_cache_control("hello")
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "hello"

    def test_string_input_has_cache_control(self):
        result = add_cache_control("hello")
        assert "cache_control" in result[0]
        assert result[0]["cache_control"]["type"] == "ephemeral"

    def test_list_input_marks_last_element(self):
        blocks = [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
        ]
        result = add_cache_control(blocks)
        assert "cache_control" in result[-1]
        assert "cache_control" not in result[0]

    def test_list_input_does_not_modify_original(self):
        blocks = [{"type": "text", "text": "x"}]
        add_cache_control(blocks)
        assert "cache_control" not in blocks[0]

    def test_empty_list_returns_as_is(self):
        result = add_cache_control([])
        assert result == []

    def test_dict_input_wrapped_in_list(self):
        block = {"type": "text", "text": "hello"}
        result = add_cache_control(block)
        assert isinstance(result, list)
        assert len(result) == 1
        assert "cache_control" in result[0]

    def test_dict_input_does_not_modify_original(self):
        block = {"type": "text", "text": "hello"}
        add_cache_control(block)
        assert "cache_control" not in block

    def test_custom_cache_type_propagates(self):
        result = add_cache_control("text", cache_type="persistent")
        assert result[0]["cache_control"]["type"] == "persistent"


# ══════════════════════════════════════════════════════════════════════════════
# add_tools_cache_control
# ══════════════════════════════════════════════════════════════════════════════


class TestAddToolsCacheControl:
    def test_marks_last_tool_only(self):
        tools = [
            {"name": "tool_a", "description": "A"},
            {"name": "tool_b", "description": "B"},
        ]
        result = add_tools_cache_control(tools)
        assert "cache_control" in result[-1]
        assert "cache_control" not in result[0]

    def test_empty_tools_returns_empty(self):
        assert add_tools_cache_control([]) == []

    def test_does_not_modify_original(self):
        tools = [{"name": "tool_x"}]
        add_tools_cache_control(tools)
        assert "cache_control" not in tools[0]

    def test_single_tool_is_marked(self):
        tools = [{"name": "only_tool"}]
        result = add_tools_cache_control(tools)
        assert "cache_control" in result[0]
        assert result[0]["cache_control"]["type"] == "ephemeral"


# ══════════════════════════════════════════════════════════════════════════════
# mark_cacheable_messages — 4-breakpoint 標記
# ══════════════════════════════════════════════════════════════════════════════


class TestMarkCacheableMessages:
    def _cfg(self, **kwargs) -> PromptCacheConfig:
        return PromptCacheConfig(**kwargs)

    def test_disabled_config_returns_no_cache_marks(self):
        cfg = self._cfg(enabled=False)
        msgs = [_make_msg("assistant") for _ in range(5)]
        result = mark_cacheable_messages(msgs, cfg)
        for m in result:
            content = m.get("content", "")
            if isinstance(content, list):
                assert not any("cache_control" in b for b in content)

    def test_cache_history_false_returns_no_marks(self):
        cfg = self._cfg(cache_history=False)
        msgs = [_make_msg("assistant") for _ in range(5)]
        result = mark_cacheable_messages(msgs, cfg)
        for m in result:
            content = m.get("content", "")
            if isinstance(content, list):
                assert not any("cache_control" in b for b in content)

    def test_skips_most_recent_assistant_messages(self):
        # 5 條 assistant，keep_recent=2 → 最後 2 條不標記
        cfg = self._cfg(history_keep_recent=2)
        msgs = [_make_msg("assistant", content=f"msg{i}") for i in range(5)]
        result = mark_cacheable_messages(msgs, cfg)

        def has_cache(m):
            content = m.get("content", "")
            if isinstance(content, list):
                return any("cache_control" in b for b in content)
            return False

        # 後 2 條（index 3, 4）不應有快取標記
        assert not has_cache(result[3])
        assert not has_cache(result[4])

    def test_marks_third_from_last_assistant(self):
        # 足夠多的 assistant，第 3 條（倒數）應被標記
        cfg = self._cfg(history_keep_recent=2, max_breakpoints=4)
        msgs = [_make_msg("assistant", content=f"a{i}") for i in range(5)]
        result = mark_cacheable_messages(msgs, cfg)

        def has_cache(m):
            content = m.get("content", "")
            if isinstance(content, list):
                return any("cache_control" in b for b in content)
            return False

        # 倒數第三條 = index 2，應標記
        assert has_cache(result[2])

    def test_output_is_list_of_dicts(self):
        cfg = self._cfg()
        msgs = [_make_msg("user"), _make_msg("assistant")]
        result = mark_cacheable_messages(msgs, cfg)
        assert isinstance(result, list)
        for m in result:
            assert isinstance(m, dict)
            assert "role" in m
            assert "content" in m

    def test_mixed_message_objects_and_dicts(self):
        cfg = self._cfg(cache_history=False)
        msg_obj = _make_msg("user")
        msg_dict = {"role": "assistant", "content": "hello"}
        result = mark_cacheable_messages([msg_obj, msg_dict], cfg)
        assert len(result) == 2


# ══════════════════════════════════════════════════════════════════════════════
# estimate_cache_savings
# ══════════════════════════════════════════════════════════════════════════════


class TestEstimateCacheSavings:
    def test_no_cached_tokens_means_no_savings(self):
        result = estimate_cache_savings(
            total_input_tokens=1000,
            cached_tokens=0,
            cache_write_tokens=0,
        )
        assert result["savings"] == 0.0
        assert result["savings_pct"] == 0.0

    def test_cost_without_cache_calculation(self):
        result = estimate_cache_savings(
            total_input_tokens=1_000_000,
            cached_tokens=0,
            cache_write_tokens=0,
        )
        expected = PRICE_NORMAL_PER_M
        assert abs(result["cost_without_cache"] - expected) < 1e-6

    def test_cache_read_is_cheaper_than_normal(self):
        # 模擬第二次以後的請求：cached_tokens=1M 全部命中 cache read
        # 注意：cache_write_tokens=0 時函數會把 cached_tokens 當作首次寫入（更貴）
        # 因此需要明確設定 cache_write_tokens 為非零值表示「已建立快取」
        result = estimate_cache_savings(
            total_input_tokens=1_000_000,
            cached_tokens=1_000_000,
            cache_write_tokens=1,  # 非零，避免觸發 fallback 邏輯
        )
        assert result["cost_with_cache"] < result["cost_without_cache"]
        assert result["savings"] > 0

    def test_savings_pct_correct_range(self):
        result = estimate_cache_savings(
            total_input_tokens=1_000_000,
            cached_tokens=500_000,
            cache_write_tokens=500_000,
        )
        assert 0 <= result["savings_pct"] <= 100

    def test_zero_input_tokens_no_division_error(self):
        result = estimate_cache_savings(
            total_input_tokens=0,
            cached_tokens=0,
        )
        assert result["savings_pct"] == 0.0

    def test_returns_required_keys(self):
        result = estimate_cache_savings(total_input_tokens=100, cached_tokens=50)
        assert "cost_without_cache" in result
        assert "cost_with_cache" in result
        assert "savings" in result
        assert "savings_pct" in result

    def test_write_tokens_default_to_cached_tokens(self):
        # cache_write_tokens 預設 = cached_tokens
        r1 = estimate_cache_savings(total_input_tokens=1000, cached_tokens=200, cache_write_tokens=0)
        r2 = estimate_cache_savings(total_input_tokens=1000, cached_tokens=200, cache_write_tokens=200)
        assert r1 == r2


# ══════════════════════════════════════════════════════════════════════════════
# CacheStats
# ══════════════════════════════════════════════════════════════════════════════


class TestCacheStats:
    def test_initial_state_all_zeros(self):
        stats = CacheStats()
        assert stats.total_cache_creation_tokens == 0
        assert stats.total_cache_read_tokens == 0
        assert stats.cache_creation_count == 0
        assert stats.cache_read_count == 0
        assert stats.total_input_tokens == 0

    def test_record_cache_creation(self):
        stats = CacheStats()
        stats.record_cache_creation(2048)
        assert stats.total_cache_creation_tokens == 2048
        assert stats.cache_creation_count == 1
        assert stats.total_input_tokens == 2048

    def test_record_cache_read(self):
        stats = CacheStats()
        stats.record_cache_read(1024)
        assert stats.total_cache_read_tokens == 1024
        assert stats.cache_read_count == 1
        assert stats.total_input_tokens == 1024

    def test_accumulates_multiple_records(self):
        stats = CacheStats()
        stats.record_cache_creation(1000)
        stats.record_cache_creation(500)
        assert stats.total_cache_creation_tokens == 1500
        assert stats.cache_creation_count == 2

    def test_get_stats_returns_required_keys(self):
        stats = CacheStats()
        stats.record_cache_creation(1000)
        stats.record_cache_read(1000)
        result = stats.get_stats()
        required_keys = [
            "total_input_tokens",
            "total_cache_creation_tokens",
            "total_cache_read_tokens",
            "cache_creation_count",
            "cache_read_count",
            "cache_hit_rate",
            "cost_without_cache",
            "cost_with_cache",
            "savings",
            "savings_pct",
        ]
        for key in required_keys:
            assert key in result, f"get_stats() 缺少鍵值：{key}"

    def test_cache_hit_rate_calculation(self):
        stats = CacheStats()
        stats.record_cache_creation(100)   # 1 write
        stats.record_cache_read(100)       # 1 read
        result = stats.get_stats()
        # hit_rate = 1/(1+1)*100 = 50.0
        assert result["cache_hit_rate"] == 50.0

    def test_reset_clears_all_counters(self):
        stats = CacheStats()
        stats.record_cache_creation(999)
        stats.record_cache_read(999)
        stats.reset()
        assert stats.total_cache_creation_tokens == 0
        assert stats.total_cache_read_tokens == 0
        assert stats.cache_creation_count == 0
        assert stats.cache_read_count == 0
        assert stats.total_input_tokens == 0

    def test_hit_rate_zero_when_no_records(self):
        stats = CacheStats()
        result = stats.get_stats()
        assert result["cache_hit_rate"] == 0.0
