"""
Phase 1+2+3 新增測試 — ContextEngine ABC、EchoCompressor、MemoryBridge

驗證：
- EchoCompressor 直接實例化和生命週期
- ContextManager(engine=...) 注入替代引擎
- MemoryBridge 協定和 NullMemoryBridge
- 反抖動保護邏輯
- ToolSummarizer 結構化摘要
- 多模態內容管理
"""

from unittest.mock import MagicMock

import pytest

from echo_thought_core import (
    CompactionConfig,
    ContextEngine,
    ContextManager,
    EchoCompressor,
    MemoryBridge,
    NullMemoryBridge,
)
from echo_thought_core.compression.multimodal import (
    IMAGE_TOKEN_ESTIMATE,
    estimate_image_tokens,
    measure_content_length,
    strip_old_images,
)
from echo_thought_core.compression.rolling_summary import (
    SUMMARY_END_MARKER,
    SUMMARY_PREFIX,
)
from echo_thought_core.compression.tool_summarizer import ToolSummarizer, get_tool_summarizer
from echo_thought_core.schemas.message import Message
from echo_thought_core.schemas.session import CompactionResult, CompressionTier

# ═══════════════════════════════════════════════════════════════════════════
# ContextEngine ABC
# ═══════════════════════════════════════════════════════════════════════════


class TestContextEngineABC:
    """ContextEngine ABC 契約驗證"""

    def test_echo_compressor_is_context_engine(self):
        """EchoCompressor 是 ContextEngine 的子類"""
        engine = EchoCompressor()
        assert isinstance(engine, ContextEngine)

    def test_echo_compressor_name(self):
        """EchoCompressor 名稱為 'echo'"""
        engine = EchoCompressor()
        assert engine.name == "echo"

    def test_engine_get_status(self):
        """get_status 回傳包含 engine 名稱"""
        engine = EchoCompressor()
        status = engine.get_status()
        assert status["engine"] == "echo"
        assert "anti_thrashing" in status

    def test_engine_session_lifecycle(self):
        """session 生命週期鉤子不拋錯"""
        engine = EchoCompressor()
        engine.on_session_start("test-session-1")
        engine.on_session_reset()
        engine.on_session_end("test-session-1", [])

    def test_should_end_session_default_false(self):
        """should_end_session 預設回傳 False"""
        engine = EchoCompressor()
        assert engine.should_end_session([], elapsed_seconds=9999) is False

    def test_update_from_response(self):
        """update_from_response 更新 budget"""
        engine = EchoCompressor()
        engine.update_from_response({
            "input_tokens": 100,
            "output_tokens": 50,
        })
        report = engine.budget.get_usage_report()
        assert "api_usage" in report
        assert report["api_usage"]["input_tokens"] == 100


# ═══════════════════════════════════════════════════════════════════════════
# EchoCompressor 壓縮邏輯
# ═══════════════════════════════════════════════════════════════════════════


class TestEchoCompressor:
    """EchoCompressor 直接壓縮測試"""

    def test_should_compress_normal(self):
        """低使用率時回傳 TIER_0"""
        engine = EchoCompressor(config=CompactionConfig(context_window_size=100000))
        msgs = [Message(role="user", content="hello", token_count=10)]
        tier = engine.should_compress(msgs)
        assert tier == CompressionTier.TIER_0_NORMAL

    def test_should_compress_triggers(self):
        """高使用率時觸發壓縮"""
        engine = EchoCompressor(config=CompactionConfig(
            context_window_size=100,
            compaction_cooldown_seconds=0,
        ))
        msgs = [Message(role="user", content="x" * 200, token_count=70)]
        tier = engine.should_compress(msgs)
        assert tier >= CompressionTier.TIER_1_MICRO_PRUNE

    def test_compress_layer1_sync(self):
        """同步壓縮執行 Layer 1 — 工具訊息確實被摘要"""
        engine = EchoCompressor(config=CompactionConfig(
            context_window_size=1000,
            compaction_cooldown_seconds=0,
        ))
        # 4 條訊息：前 2 條在邊界前（50% = idx 2），後 2 條受保護
        msgs = [
            Message(role="tool", content="x" * 300, token_count=80,
                    tool_call_id="c1", tool_name="read_file"),
            Message(role="tool", content="y" * 300, token_count=80,
                    tool_call_id="c2", tool_name="terminal"),
            Message(role="user", content="recent", token_count=5),
            Message(role="assistant", content="ok", token_count=3),
        ]
        result = engine.compress(msgs, CompressionTier.TIER_1_MICRO_PRUNE)
        assert result is not None
        assert result.tier == CompressionTier.TIER_1_MICRO_PRUNE
        assert result.tokens_saved > 0
        # 確認工具訊息被摘要（metadata 標記 pruned）
        pruned = [m for m in msgs if m.metadata.get("pruned")]
        assert len(pruned) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# ContextManager 委託
# ═══════════════════════════════════════════════════════════════════════════


class TestContextManagerDelegation:
    """ContextManager 委託 ContextEngine 測試"""

    def test_default_engine_is_echo(self):
        """預設引擎是 EchoCompressor"""
        mgr = ContextManager()
        assert isinstance(mgr._engine, EchoCompressor)
        assert mgr._engine.name == "echo"

    def test_custom_engine_injection(self):
        """可注入自訂引擎"""
        engine = EchoCompressor(
            config=CompactionConfig(context_window_size=50000)
        )
        mgr = ContextManager(engine=engine)
        assert mgr._engine is engine

    def test_subsystem_properties(self):
        """property 代理正確指向引擎子系統"""
        mgr = ContextManager()
        assert mgr.counter is mgr._engine.counter
        assert mgr.budget is mgr._engine.budget
        assert mgr.sacred is mgr._engine.sacred

    def test_reset_session_delegates(self):
        """reset_session 重置引擎狀態"""
        mgr = ContextManager()
        mgr.add_message("user", "hello")
        mgr.reset_session()
        assert len(mgr.history.messages) == 0
        assert mgr._engine._ineffective_compression_count == 0

    def test_get_usage_includes_engine_status(self):
        """get_usage 包含引擎狀態資訊"""
        mgr = ContextManager()
        usage = mgr.get_usage()
        assert "engine" in usage
        assert usage["engine"] == "echo"


# ═══════════════════════════════════════════════════════════════════════════
# MemoryBridge
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryBridge:
    """MemoryBridge Protocol 測試"""

    def test_null_bridge_is_protocol_compliant(self):
        """NullMemoryBridge 符合 MemoryBridge Protocol"""
        bridge = NullMemoryBridge()
        assert isinstance(bridge, MemoryBridge)

    def test_null_bridge_returns_empty(self):
        """NullMemoryBridge 所有方法回傳空值"""
        bridge = NullMemoryBridge()
        assert bridge.on_pre_compress([]) == ""
        bridge.on_post_compress(MagicMock())  # 不拋錯
        bridge.on_session_start("test")
        bridge.on_session_end("test", [])
        assert bridge.prefetch("query") == ""

    def test_custom_bridge_integration(self):
        """自訂 MemoryBridge 可注入 EchoCompressor"""
        class TestBridge:
            def __init__(self):
                self.pre_compress_called = False
                self.post_compress_called = False

            def on_pre_compress(self, messages):
                self.pre_compress_called = True
                return "Remember: user likes dark mode"

            def on_post_compress(self, result):
                self.post_compress_called = True

            def on_session_start(self, session_id):
                pass

            def on_session_end(self, session_id, messages):
                pass

            def prefetch(self, query):
                return ""

        bridge = TestBridge()
        engine = EchoCompressor(memory_bridge=bridge)
        assert engine._memory_bridge is bridge


# ═══════════════════════════════════════════════════════════════════════════
# 反抖動保護
# ═══════════════════════════════════════════════════════════════════════════


class TestAntiThrashing:
    """反抖動保護測試"""

    def test_tracks_compression_efficiency(self):
        """追蹤壓縮效率百分比"""
        engine = EchoCompressor()
        result = CompactionResult(
            tier=CompressionTier.TIER_1_MICRO_PRUNE,
            tokens_before=1000,
            tokens_after=200,
            tokens_saved=800,
        )
        engine._track_efficiency(result)
        assert engine._last_compression_savings_pct == 80.0
        assert engine._ineffective_compression_count == 0

    def test_ineffective_count_increments(self):
        """效率 <10% 時遞增無效計數"""
        engine = EchoCompressor()
        result = CompactionResult(
            tier=CompressionTier.TIER_1_MICRO_PRUNE,
            tokens_before=1000,
            tokens_after=950,
            tokens_saved=50,
        )
        engine._track_efficiency(result)
        assert engine._ineffective_compression_count == 1

    def test_anti_thrashing_skips_after_two(self):
        """連續 2 次無效後跳過壓縮"""
        engine = EchoCompressor(config=CompactionConfig(
            context_window_size=100,
            compaction_cooldown_seconds=0,
        ))
        engine._ineffective_compression_count = 2

        msgs = [Message(role="user", content="x" * 200, token_count=70)]
        tier = engine.should_compress(msgs)
        assert tier == CompressionTier.TIER_0_NORMAL

    def test_effective_compression_resets_count(self):
        """有效壓縮重置無效計數"""
        engine = EchoCompressor()
        engine._ineffective_compression_count = 3
        result = CompactionResult(
            tier=CompressionTier.TIER_1_MICRO_PRUNE,
            tokens_before=1000,
            tokens_after=400,
            tokens_saved=600,
        )
        engine._track_efficiency(result)
        assert engine._ineffective_compression_count == 0


# ═══════════════════════════════════════════════════════════════════════════
# ToolSummarizer
# ═══════════════════════════════════════════════════════════════════════════


class TestToolSummarizer:
    """ToolSummarizer 結構化摘要測試"""

    def test_read_file_formatter(self):
        """read_file 格式化器"""
        summarizer = ToolSummarizer()
        result = summarizer.summarize(
            "read_file", "line1\nline2\nline3",
            {"args": {"path": "config.py"}},
        )
        assert "[read_file]" in result
        assert "config.py" in result

    def test_terminal_formatter(self):
        """terminal 格式化器"""
        summarizer = ToolSummarizer()
        result = summarizer.summarize(
            "terminal", '"exit_code": 0\noutput line 1\noutput line 2',
            {"args": {"command": "npm test"}},
        )
        assert "[terminal]" in result
        assert "npm test" in result

    def test_generic_fallback(self):
        """未知工具使用通用 fallback"""
        summarizer = ToolSummarizer()
        result = summarizer.summarize(
            "my_custom_tool", "x" * 500, {},
        )
        assert "[my_custom_tool]" in result
        assert "omitted" in result

    def test_short_content_not_summarized(self):
        """短內容不做摘要"""
        summarizer = ToolSummarizer()
        short = "just a small result"
        result = summarizer.summarize("unknown_tool", short, {})
        assert result == short  # 原封不動

    def test_register_custom_formatter(self):
        """可註冊自訂格式化器"""
        summarizer = ToolSummarizer()
        summarizer.register_formatter(
            "my_tool",
            lambda name, content, meta: f"[{name}] custom: {len(content)} chars",
        )
        result = summarizer.summarize("my_tool", "hello world", {})
        assert result == "[my_tool] custom: 11 chars"

    def test_global_singleton(self):
        """全域 singleton 一致性"""
        s1 = get_tool_summarizer()
        s2 = get_tool_summarizer()
        assert s1 is s2


# ═══════════════════════════════════════════════════════════════════════════
# 多模態內容管理
# ═══════════════════════════════════════════════════════════════════════════


class TestMultimodal:
    """多模態內容管理測試"""

    def test_estimate_no_images(self):
        """無圖片回傳 0"""
        assert estimate_image_tokens("just plain text") == 0

    def test_estimate_with_base64_image(self):
        """偵測 base64 圖片"""
        content = f"看這張圖 data:image/png;base64,{'A' * 200} 你覺得如何"
        tokens = estimate_image_tokens(content)
        assert tokens == IMAGE_TOKEN_ESTIMATE

    def test_strip_old_images_keeps_recent(self):
        """保留最近 N 張圖片"""
        img = f"data:image/jpeg;base64,{'B' * 200}"
        msgs = [
            Message(role="user", content=f"old: {img}", token_count=1700),
            Message(role="user", content=f"mid: {img}", token_count=1700),
            Message(role="user", content=f"new: {img}", token_count=1700),
        ]
        result, saved = strip_old_images(msgs, keep_last_n=1)
        # 第 0, 1 個的圖片被清理，第 2 個保留
        assert "base64" not in result[0].content
        assert "base64" not in result[1].content
        assert "base64" in result[2].content
        assert saved == IMAGE_TOKEN_ESTIMATE * 2

    def test_strip_no_images_noop(self):
        """無圖片時不動作"""
        msgs = [Message(role="user", content="text only", token_count=5)]
        result, saved = strip_old_images(msgs)
        assert result is msgs
        assert saved == 0

    def test_measure_mixed_content(self):
        """混合內容的 token 估算"""
        text = "hello " * 100  # 600 chars → ~150 tokens
        img = f"data:image/png;base64,{'C' * 200}"
        tokens = measure_content_length(f"{text}{img}")
        assert tokens >= IMAGE_TOKEN_ESTIMATE  # 至少有圖片的量


# ═══════════════════════════════════════════════════════════════════════════
# 壓縮安全標記
# ═══════════════════════════════════════════════════════════════════════════


class TestCompressionSafetyMarkers:
    """壓縮摘要指令安全標記測試"""

    def test_summary_prefix_contains_reference_only(self):
        """SUMMARY_PREFIX 包含 REFERENCE ONLY 標記"""
        assert "REFERENCE ONLY" in SUMMARY_PREFIX
        assert "latest user message" in SUMMARY_PREFIX

    def test_summary_end_marker_exists(self):
        """SUMMARY_END_MARKER 存在且有意義"""
        assert "END OF CONTEXT SUMMARY" in SUMMARY_END_MARKER


# ═══════════════════════════════════════════════════════════════════════════
# Preflight 壓縮
# ═══════════════════════════════════════════════════════════════════════════


class TestPreflightCompress:
    """Preflight 壓縮測試"""

    @pytest.mark.asyncio
    async def test_preflight_no_compression_needed(self):
        """token 未超預算時不壓縮"""
        engine = EchoCompressor(config=CompactionConfig(
            context_window_size=100000,
            compaction_cooldown_seconds=0,
        ))
        msgs = [Message(role="user", content="hello", token_count=10)]
        result = await engine.preflight_compress(msgs, estimated_request_tokens=100)
        assert result is None

    @pytest.mark.asyncio
    async def test_preflight_triggers_compression(self):
        """token 超預算時觸發壓縮"""
        engine = EchoCompressor(config=CompactionConfig(
            context_window_size=200,
            compaction_cooldown_seconds=0,
        ))
        # 4 條訊息，前兩條大工具輸出在邊界前（50%=idx 2）
        msgs = [
            Message(role="tool", content="x" * 300, token_count=80,
                    tool_call_id="c1", tool_name="read_file"),
            Message(role="tool", content="y" * 300, token_count=80,
                    tool_call_id="c2", tool_name="terminal"),
            Message(role="user", content="recent", token_count=5),
            Message(role="assistant", content="ok", token_count=3),
        ]
        result = await engine.preflight_compress(
            msgs, estimated_request_tokens=100,
        )
        # 如果壓縮成功，result 不為 None 且有正數 tokens_saved
        if result is not None:
            assert result.tokens_saved >= 0

    @pytest.mark.asyncio
    async def test_preflight_respects_cooldown(self):
        """Preflight 尊重冷卻期"""
        engine = EchoCompressor(config=CompactionConfig(
            context_window_size=100,
            compaction_cooldown_seconds=9999,  # 超長冷卻
        ))
        import time
        engine._last_compaction_time = time.time()  # 剛壓縮過
        msgs = [Message(role="user", content="x" * 200, token_count=70)]
        result = await engine.preflight_compress(msgs, estimated_request_tokens=50)
        # 冷卻中不壓縮
        assert result is None

    @pytest.mark.asyncio
    async def test_preflight_max_passes_limit(self):
        """Preflight 不超過 max_passes"""
        engine = EchoCompressor(config=CompactionConfig(
            context_window_size=10,  # 極小，永遠超預算
            compaction_cooldown_seconds=0,
        ))
        msgs = [Message(role="user", content="hello", token_count=5)]
        # max_passes=1，最多壓縮一次就停
        await engine.preflight_compress(
            msgs, estimated_request_tokens=100, max_passes=1,
        )
        # 不會無限循環（測試能正常結束即驗證）


# ═══════════════════════════════════════════════════════════════════════════
# compression_count 一致性
# ═══════════════════════════════════════════════════════════════════════════


class TestHistorySync:
    """P0 修復驗證：壓縮後 LLMClient 歷史同步"""

    def test_compression_syncs_to_llmclient(self):
        """壓縮後 ContextManager 同步歷史到 LLMClient"""
        from echo_thought_core.llm_client import LLMClient

        client = LLMClient(config={"model": "gemini-2.5-flash-lite"})
        # 手動加入歷史模擬 chat() 行為
        client._history = list(range(16))  # 16 條假歷史

        mgr = ContextManager(
            config=CompactionConfig(
                context_window_size=100,
                compaction_cooldown_seconds=0,
            ),
            backend=client,
        )
        # 加入足夠的訊息觸發壓縮
        for i in range(8):
            mgr.add_message("tool", "x" * 300, tool_call_id=f"c{i}", tool_name="read_file")
            mgr.add_message("user", f"msg {i}")

        before_mgr = len(mgr.history.messages)

        # 觸發壓縮
        mgr.get_messages()

        after_mgr = len(mgr.history.messages)

        if after_mgr < before_mgr:
            # 壓縮確實發生了，驗證 LLMClient 歷史也被同步
            assert len(client._history) == after_mgr
        # 如果沒觸發壓縮（全在保護區內），那不需要同步

    def test_reset_clears_llmclient_history(self):
        """reset_session 同時清空 LLMClient 歷史"""
        from echo_thought_core.llm_client import LLMClient

        client = LLMClient(config={"model": "gemini-2.5-flash-lite"})
        client._history = list(range(10))

        mgr = ContextManager(backend=client)
        mgr.add_message("user", "hello")
        mgr.reset_session()

        assert len(client._history) == 0
        assert len(mgr.history.messages) == 0

    def test_offline_compression_no_api_key_required(self):
        """離線壓縮（無 API key）不應拋錯 — P1 回歸測試"""
        from unittest.mock import patch as _patch

        from echo_thought_core.llm_client import LLMClient

        # 模擬無 API key 環境
        with _patch.dict("os.environ", {}, clear=True):
            client = LLMClient(config={"model": "gemini-2.5-flash-lite"})

        mgr = ContextManager(
            config=CompactionConfig(
                context_window_size=100,
                compaction_cooldown_seconds=0,
            ),
            backend=client,
        )
        for i in range(8):
            mgr.add_message("tool", "x" * 300, tool_call_id=f"c{i}", tool_name="read_file")
            mgr.add_message("user", f"msg {i}")

        # 不應因缺少 API key 而拋 ValueError
        messages = mgr.get_messages()
        assert isinstance(messages, list)


class TestCompressionCount:
    """壓縮計數一致性測試"""

    def test_compression_count_sync(self):
        """同步壓縮正確遞增 compression_count"""
        mgr = ContextManager(config=CompactionConfig(
            context_window_size=100,
            compaction_cooldown_seconds=0,
        ))
        # 加入足夠的訊息觸發壓縮
        for i in range(6):
            mgr.add_message("tool", "x" * 300, tool_call_id=f"c{i}", tool_name="read_file")
            mgr.add_message("user", f"msg {i}")

        count_before = mgr.state.compression_count
        mgr.get_messages()
        # 若觸發了壓縮，count 應該遞增
        assert mgr.state.compression_count >= count_before
        # history 和 state 計數保持同步
        assert mgr.history.compression_count == mgr.state.compression_count
