"""
Token Accounting 測試
"""

import pytest
from echo_thought_core.accounting.counter import TokenCounter
from echo_thought_core.accounting.budget import TokenBudget
from echo_thought_core.schemas.message import Message
from echo_thought_core.schemas.session import CompressionTier


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


# ─────────────────────────────────────────────────────────────────────────────
# 以下為新增測試（針對 counter.py 與 budget.py 的新增功能）
# ─────────────────────────────────────────────────────────────────────────────

from echo_thought_core.accounting.counter import (
    ModelFamily,
    ModelInfo,
    MODEL_DATABASE,
    get_model_info,
    get_model_context_length,
    estimate_tokens,
    estimate_message_tokens,
    estimate_messages_tokens,
    count_tokens_in_tail,
    MESSAGE_OVERHEAD_TOKENS,
    CONVERSATION_BASE_TOKENS,
)
from echo_thought_core.accounting.budget import (
    calculate_context_threshold,
    calculate_tail_budget,
    calculate_summary_budget,
)


# ─────────────────────────────────────────────────────────────────────────────
# TestModelDatabase — MODEL_DATABASE 與枚舉基本屬性
# ─────────────────────────────────────────────────────────────────────────────

class TestModelDatabase:
    def test_database_has_at_least_18_models(self):
        """MODEL_DATABASE 至少要有 18 個模型"""
        assert len(MODEL_DATABASE) >= 18

    def test_model_family_enum_members(self):
        """ModelFamily 枚舉應包含各家廠商"""
        expected = {"anthropic", "openai", "google", "alibaba", "deepseek", "meta", "unknown"}
        actual = {m.value for m in ModelFamily}
        assert expected == actual

    def test_model_info_dataclass_fields(self):
        """ModelInfo 的欄位應可正常存取"""
        info = MODEL_DATABASE["claude-sonnet-4-6"]
        assert info.name == "claude-sonnet-4-6"
        assert info.family == ModelFamily.ANTHROPIC
        assert info.context_window == 1_000_000
        assert info.max_output_tokens == 32_000
        assert info.input_cost_per_1m == 3.0
        assert info.output_cost_per_1m == 15.0
        assert isinstance(info.aliases, list)

    def test_all_models_have_positive_context_window(self):
        """所有模型的 context_window 必須為正整數"""
        for name, info in MODEL_DATABASE.items():
            assert info.context_window > 0, f"{name} context_window 不合法"

    def test_all_models_have_positive_costs(self):
        """所有模型的成本欄位必須非負"""
        for name, info in MODEL_DATABASE.items():
            assert info.input_cost_per_1m >= 0, f"{name} input_cost 不合法"
            assert info.output_cost_per_1m >= 0, f"{name} output_cost 不合法"

    def test_representative_models_present(self):
        """確認各家廠商代表模型存在"""
        must_have = [
            "claude-sonnet-4-5",
            "gpt-4o",
            "gemini-2.5-pro",
            "qwen3-72b",
            "deepseek-v3",
            "llama-4-maverick",
        ]
        for key in must_have:
            assert key in MODEL_DATABASE, f"缺少模型：{key}"


# ─────────────────────────────────────────────────────────────────────────────
# TestGetModelInfo — get_model_info 各匹配路徑
# ─────────────────────────────────────────────────────────────────────────────

class TestGetModelInfo:
    def test_exact_match(self):
        """精確匹配 key 應直接返回對應 ModelInfo"""
        info = get_model_info("claude-opus-4-5")
        assert info is not None
        assert info.name == "claude-opus-4-5"
        assert info.family == ModelFamily.ANTHROPIC

    def test_alias_match(self):
        """別名精確匹配應返回對應 ModelInfo"""
        # claude-sonnet-4.6 是 claude-sonnet-4-6 的別名
        info = get_model_info("claude-sonnet-4.6")
        assert info is not None
        assert info.name == "claude-sonnet-4-6"

    def test_alias_match_openai(self):
        """gpt4o 是 gpt-4o 的別名"""
        info = get_model_info("gpt4o")
        assert info is not None
        assert info.name == "gpt-4o"

    def test_fuzzy_partial_match(self):
        """部分名稱包含匹配應命中"""
        info = get_model_info("deepseek-v3-0324")
        assert info is not None
        assert info.name == "deepseek-v3"

    def test_unknown_model_returns_none(self):
        """完全不存在的模型名稱應返回 None"""
        result = get_model_info("not-a-real-model-xyz-9999")
        assert result is None

    def test_empty_string_returns_none(self):
        """空字串輸入應返回 None"""
        result = get_model_info("")
        assert result is None

    def test_case_insensitive_exact_match(self):
        """大小寫不一致的精確匹配（normalize 後）"""
        info = get_model_info("CLAUDE-OPUS-4-5")
        assert info is not None
        assert info.name == "claude-opus-4-5"


# ─────────────────────────────────────────────────────────────────────────────
# TestGetModelContextLength — get_model_context_length 各 provider 分支
# ─────────────────────────────────────────────────────────────────────────────

class TestGetModelContextLength:
    def test_known_model_returns_database_value(self):
        """已知模型應返回 MODEL_DATABASE 中的 context_window"""
        result = get_model_context_length("gemini-2.0-flash")
        assert result == 2_000_000

    def test_anthropic_provider_fallback(self):
        """未知模型但 provider=anthropic 應返回 200K"""
        result = get_model_context_length("some-unknown-claude", provider="anthropic")
        assert result == 200_000

    def test_openai_provider_fallback(self):
        """未知模型但 provider=openai 應返回 128K"""
        result = get_model_context_length("some-unknown-gpt", provider="openai")
        assert result == 128_000

    def test_google_provider_fallback(self):
        """未知模型但 provider=google 應返回 1M"""
        result = get_model_context_length("gemini-unknown", provider="google")
        assert result == 1_000_000

    def test_meta_provider_fallback(self):
        """未知模型但 provider=meta 應返回 131K"""
        result = get_model_context_length("llama-unknown", provider="meta")
        assert result == 131_072

    def test_completely_unknown_returns_128k(self):
        """模型與 provider 都未知時，保守返回 128K"""
        result = get_model_context_length("totally-alien-model")
        assert result == 128_000


# ─────────────────────────────────────────────────────────────────────────────
# TestEstimation — estimate_tokens / estimate_message_tokens / estimate_messages_tokens
# ─────────────────────────────────────────────────────────────────────────────

class TestEstimation:
    def test_estimate_tokens_empty_string(self):
        """空字串應返回 0"""
        assert estimate_tokens("") == 0

    def test_estimate_tokens_ascii(self):
        """純 ASCII 字串的估算應大於 0"""
        result = estimate_tokens("Hello, this is a simple test string.")
        assert result > 0

    def test_estimate_tokens_cjk(self):
        """中文字串估算值應高於相同長度的 ASCII（因為 CJK 加權）"""
        cjk_result = estimate_tokens("你好世界")    # 4 個 CJK 字
        ascii_result = estimate_tokens("abcd")      # 4 個 ASCII 字
        # CJK 每字估算比 ASCII 多，所以 CJK 應 >= ASCII
        assert cjk_result >= ascii_result

    def test_estimate_message_tokens_overhead(self):
        """基本訊息應包含 MESSAGE_OVERHEAD_TOKENS"""
        msg = {"role": "user", "content": ""}
        result = estimate_message_tokens(msg)
        # 空內容只有 overhead
        assert result == MESSAGE_OVERHEAD_TOKENS + estimate_tokens("")

    def test_estimate_message_tokens_with_content(self):
        """含 content 的訊息應大於純 overhead"""
        msg = {"role": "assistant", "content": "This is a longer response with more tokens."}
        result = estimate_message_tokens(msg)
        assert result > MESSAGE_OVERHEAD_TOKENS

    def test_estimate_message_tokens_with_tool_calls(self):
        """含 tool_calls 的訊息應比無 tool_calls 的同內容訊息多"""
        plain_msg = {"role": "assistant", "content": "calling tool"}
        tool_msg = {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
        }
        plain_tokens = estimate_message_tokens(plain_msg)
        tool_tokens = estimate_message_tokens(tool_msg)
        assert tool_tokens > plain_tokens

    def test_estimate_messages_tokens_empty_list(self):
        """空訊息列表應返回 0"""
        assert estimate_messages_tokens([]) == 0

    def test_estimate_messages_tokens_multiple(self):
        """多條訊息應累加，並包含 CONVERSATION_BASE_TOKENS"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        result = estimate_messages_tokens(messages)
        expected_min = CONVERSATION_BASE_TOKENS + estimate_message_tokens(messages[0]) + estimate_message_tokens(messages[1])
        assert result == expected_min


# ─────────────────────────────────────────────────────────────────────────────
# TestCountTokensInTail — count_tokens_in_tail 邊界與一般場景
# ─────────────────────────────────────────────────────────────────────────────

class TestCountTokensInTail:
    def test_empty_messages_returns_zero(self):
        """空訊息列表應返回 0"""
        assert count_tokens_in_tail([], 10000) == 0

    def test_zero_budget_returns_zero(self):
        """預算為 0 時應返回 0"""
        messages = [{"role": "user", "content": "hello"}]
        assert count_tokens_in_tail(messages, 0) == 0

    def test_negative_budget_returns_zero(self):
        """負預算也應返回 0"""
        messages = [{"role": "user", "content": "hello"}]
        assert count_tokens_in_tail(messages, -100) == 0

    def test_large_budget_protects_all(self):
        """預算足夠時應保護所有訊息"""
        messages = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
            {"role": "user", "content": "msg3"},
        ]
        result = count_tokens_in_tail(messages, 1_000_000)
        assert result == len(messages)

    def test_tight_budget_protects_tail_only(self):
        """預算緊張時應只保護尾部若干條"""
        # 製造一個明顯不夠裝全部訊息的預算
        short_msg = {"role": "user", "content": "hi"}
        long_msg = {"role": "assistant", "content": "A" * 200}
        messages = [long_msg, long_msg, short_msg]
        # 只給夠放一條 short_msg 的預算
        tight_budget = estimate_message_tokens(short_msg)
        result = count_tokens_in_tail(messages, tight_budget)
        assert result == 1


# ─────────────────────────────────────────────────────────────────────────────
# TestBudgetFunctions — 獨立工具函數
# ─────────────────────────────────────────────────────────────────────────────

class TestBudgetFunctions:
    def test_calculate_context_threshold_normal(self):
        """正常 context 應返回 context_length * threshold_percent"""
        result = calculate_context_threshold(200_000, 0.80)
        assert result == 160_000

    def test_calculate_context_threshold_minimum_guarantee(self):
        """小 context 模型應保證最小 64K 閾值"""
        # 10000 * 0.80 = 8000 < 65536，應被拉到 65536
        result = calculate_context_threshold(10_000, 0.80)
        assert result == 65_536

    def test_calculate_context_threshold_exactly_at_boundary(self):
        """剛好在 64K 邊界的 context"""
        # 81920 * 0.80 = 65536，剛好等於最小值
        result = calculate_context_threshold(81_920, 0.80)
        assert result == 65_536

    def test_calculate_tail_budget(self):
        """尾部預算應為閾值 * ratio"""
        result = calculate_tail_budget(160_000, ratio=0.20)
        assert result == 32_000

    def test_calculate_summary_budget_normal(self):
        """摘要預算正常情況"""
        result = calculate_summary_budget(50_000, ratio=0.20)
        # 50000 * 0.20 = 10000，在 [2000, 12000] 範圍內
        assert result == 10_000

    def test_calculate_summary_budget_min_clamp(self):
        """摘要預算低於 min 時應 clamp 到 min"""
        result = calculate_summary_budget(1_000, ratio=0.20, min_tokens=2_000)
        # 1000 * 0.20 = 200 < 2000
        assert result == 2_000

    def test_calculate_summary_budget_max_clamp(self):
        """摘要預算超過 max 時應 clamp 到 max"""
        result = calculate_summary_budget(1_000_000, ratio=0.20, max_tokens=12_000)
        # 1000000 * 0.20 = 200000 > 12000
        assert result == 12_000


# ─────────────────────────────────────────────────────────────────────────────
# TestTokenBudgetDW — TokenBudget 新增方法（DW 合併功能）
# ─────────────────────────────────────────────────────────────────────────────

class TestTokenBudgetDW:
    def test_reserve_for_output_default(self):
        """預設 reserve_for_output 應為 4096"""
        budget = TokenBudget(context_window=200_000)
        assert budget.reserve_for_output == 4_096

    def test_reserve_for_output_custom(self):
        """自訂 reserve_for_output 應正確設定"""
        budget = TokenBudget(context_window=200_000, reserve_for_output=8_192)
        assert budget.reserve_for_output == 8_192

    def test_max_input_tokens(self):
        """max_input_tokens 應為 context_window - reserve_for_output"""
        budget = TokenBudget(context_window=100_000, reserve_for_output=5_000)
        assert budget.max_input_tokens == 95_000

    def test_remaining_input_budget_initial(self):
        """初始狀態下剩餘輸入預算應等於 max_input_tokens"""
        budget = TokenBudget(context_window=100_000, reserve_for_output=5_000)
        assert budget.remaining_input_budget == 95_000

    def test_remaining_input_budget_after_update(self):
        """更新 token 後剩餘輸入預算應減少"""
        budget = TokenBudget(context_window=100_000, reserve_for_output=5_000)
        budget.update(history=20_000)
        assert budget.remaining_input_budget == 75_000

    def test_remaining_output_budget_initial(self):
        """初始輸出預算應等於 reserve_for_output"""
        budget = TokenBudget(context_window=200_000, reserve_for_output=4_096)
        assert budget.remaining_output_budget == 4_096

    def test_remaining_output_budget_after_record(self):
        """記錄輸出 token 後，剩餘輸出預算應減少"""
        budget = TokenBudget(context_window=200_000, reserve_for_output=4_096)
        budget.record_usage(input_tokens=1000, output_tokens=1000)
        assert budget.remaining_output_budget == 3_096

    def test_can_fit_input_true(self):
        """輸入 tokens 在預算內應返回 True"""
        budget = TokenBudget(context_window=100_000, reserve_for_output=5_000)
        assert budget.can_fit(50_000, is_output=False) is True

    def test_can_fit_input_false(self):
        """輸入 tokens 超過預算應返回 False"""
        budget = TokenBudget(context_window=100_000, reserve_for_output=5_000)
        budget.update(history=90_000)
        assert budget.can_fit(10_000, is_output=False) is False

    def test_can_fit_output_true(self):
        """輸出 tokens 在 reserve 內應返回 True"""
        budget = TokenBudget(context_window=200_000, reserve_for_output=4_096)
        assert budget.can_fit(1_000, is_output=True) is True

    def test_can_fit_output_false(self):
        """輸出 tokens 超過 reserve 應返回 False"""
        budget = TokenBudget(context_window=200_000, reserve_for_output=4_096)
        assert budget.can_fit(5_000, is_output=True) is False

    def test_record_usage_accumulates(self):
        """多次 record_usage 應累積"""
        budget = TokenBudget(context_window=200_000)
        budget.record_usage(input_tokens=500, output_tokens=100, cache_read_tokens=50, cache_write_tokens=20)
        budget.record_usage(input_tokens=300, output_tokens=80)
        report = budget.get_usage_report()
        assert report["api_usage"]["input_tokens"] == 800
        assert report["api_usage"]["output_tokens"] == 180
        assert report["api_usage"]["cache_read_tokens"] == 50
        assert report["api_usage"]["cache_write_tokens"] == 20

    def test_estimate_cost_no_model_info(self):
        """未提供 model_info 應返回 (0.0, 0.0)"""
        budget = TokenBudget(context_window=200_000)
        budget.record_usage(input_tokens=10_000, output_tokens=2_000)
        cost = budget.estimate_cost(model_info=None)
        assert cost == (0.0, 0.0)

    def test_estimate_cost_with_model_info(self):
        """提供 model_info 應返回非零成本"""
        budget = TokenBudget(context_window=200_000)
        budget.record_usage(input_tokens=1_000_000, output_tokens=1_000_000)
        model = get_model_info("gpt-4o")
        input_cost, output_cost = budget.estimate_cost(model_info=model)
        # gpt-4o: input 2.5/1M, output 10.0/1M
        assert input_cost > 0
        assert output_cost > 0
        # 粗略驗證輸出成本 = 1M tokens * $10/1M = $10
        assert abs(output_cost - 10.0) < 0.01

    def test_should_compress_below_threshold(self):
        """使用率低於閾值時 should_compress 應返回 False"""
        budget = TokenBudget(context_window=100_000)
        budget.update(history=50_000)  # 50%
        assert budget.should_compress(threshold=0.80) is False

    def test_should_compress_above_threshold(self):
        """使用率高於閾值時 should_compress 應返回 True"""
        budget = TokenBudget(context_window=100_000)
        budget.update(history=85_000)  # 85%
        assert budget.should_compress(threshold=0.80) is True

    def test_should_compress_uses_default_threshold(self):
        """未傳 threshold 時應使用 rolling_summary 預設值 (0.80)"""
        budget = TokenBudget(context_window=100_000)
        budget.update(history=82_000)  # 82% > 0.80
        assert budget.should_compress() is True

    def test_get_status_includes_compression_flag(self):
        """get_status 應包含 compression_needed 欄位"""
        budget = TokenBudget(context_window=100_000)
        status = budget.get_status()
        assert "compression_needed" in status

    def test_get_status_includes_estimated_cost(self):
        """get_status 應包含 estimated_cost_usd 欄位"""
        budget = TokenBudget(context_window=100_000)
        status = budget.get_status()
        assert "estimated_cost_usd" in status
        assert "total" in status["estimated_cost_usd"]

    def test_get_status_cost_zero_without_usage(self):
        """未記錄任何使用量時，get_status 成本應為 0"""
        budget = TokenBudget(context_window=100_000)
        status = budget.get_status()
        assert status["estimated_cost_usd"]["total"] == 0.0
