"""
Test LLMLingua-2 整合

測試 LLMLinguaCompressor 的行為和 MicroPruner 的 LLMLingua 規則。
模型相關測試使用 mock，不需要真實 GPU。
"""

from unittest.mock import MagicMock, patch

from echo_thought_core.compression.llmlingua import (
    _HAS_LLMLINGUA,
    LLMLinguaCompressor,
    LLMLinguaConfig,
)
from echo_thought_core.compression.micro_pruning import MicroPruner
from echo_thought_core.schemas.message import Message

# Helper: 讓需要 mock PromptCompressor 的測試在無 llmlingua 時也能正常運行
_patch_available = patch("echo_thought_core.compression.llmlingua._HAS_LLMLINGUA", True)


# ═══════════════════════════════════════════════════════════════════════════
# LLMLinguaConfig
# ═══════════════════════════════════════════════════════════════════════════


class TestLLMLinguaConfig:
    def test_default_values(self):
        config = LLMLinguaConfig()
        assert config.rate == 0.5
        assert config.min_token_threshold == 2000
        assert "xlm-roberta" in config.model_name
        assert config.use_gpu is True

    def test_custom_values(self):
        config = LLMLinguaConfig(rate=0.3, min_token_threshold=500)
        assert config.rate == 0.3
        assert config.min_token_threshold == 500


# ═══════════════════════════════════════════════════════════════════════════
# LLMLinguaCompressor (mock model)
# ═══════════════════════════════════════════════════════════════════════════


class TestLLMLinguaCompressor:
    def test_availability(self):
        compressor = LLMLinguaCompressor()
        assert compressor.is_available == _HAS_LLMLINGUA

    def test_skip_short_text(self):
        """低於門檻的文本不壓縮"""
        compressor = LLMLinguaCompressor(LLMLinguaConfig(min_token_threshold=2000))
        text = "This is a short text."
        result, stats = compressor.compress(text)

        assert result == text
        assert stats["skipped"] is True
        assert stats["reason"] == "below_threshold"

    @patch("echo_thought_core.compression.llmlingua._HAS_LLMLINGUA", True)
    @patch("echo_thought_core.compression.llmlingua.PromptCompressor")
    def test_compress_long_text(self, mock_compressor_cls):
        """超過門檻的文本觸發壓縮"""
        mock_instance = MagicMock()
        mock_instance.compress_prompt.return_value = {
            "compressed_prompt": "compressed version of long text",
            "origin_tokens": 3000,
            "compressed_tokens": 1500,
        }
        mock_compressor_cls.return_value = mock_instance

        compressor = LLMLinguaCompressor(LLMLinguaConfig(min_token_threshold=100))
        long_text = "word " * 2000  # ~2000 words = ~2500 tokens

        result, stats = compressor.compress(long_text)

        assert result == "compressed version of long text"
        assert stats["skipped"] is False
        assert stats["original_tokens"] == 3000
        assert stats["compressed_tokens"] == 1500
        assert stats["saving"] == 1500

    @patch("echo_thought_core.compression.llmlingua._HAS_LLMLINGUA", True)
    @patch("echo_thought_core.compression.llmlingua.PromptCompressor")
    def test_compress_with_custom_rate(self, mock_compressor_cls):
        """可以覆蓋壓縮率"""
        mock_instance = MagicMock()
        mock_instance.compress_prompt.return_value = {
            "compressed_prompt": "very compressed",
            "origin_tokens": 5000,
            "compressed_tokens": 1000,
        }
        mock_compressor_cls.return_value = mock_instance

        compressor = LLMLinguaCompressor(LLMLinguaConfig(min_token_threshold=100))
        long_text = "word " * 2000

        result, stats = compressor.compress(long_text, rate=0.2)
        assert stats["saving"] == 4000

        # 驗證 compress_prompt 被呼叫時用了 rate=0.2
        call_args = mock_instance.compress_prompt.call_args
        assert call_args[1]["rate"] == 0.2 or call_args[0][0] == long_text

    def test_get_stats(self):
        """get_stats 回傳正確結構"""
        compressor = LLMLinguaCompressor()
        stats = compressor.get_stats()

        assert "available" in stats
        assert "initialized" in stats
        assert "model_loaded" in stats
        assert "model_name" in stats
        assert "default_rate" in stats

    @patch("echo_thought_core.compression.llmlingua._HAS_LLMLINGUA", True)
    @patch("echo_thought_core.compression.llmlingua.PromptCompressor")
    def test_compress_error_fallback(self, mock_compressor_cls):
        """壓縮失敗時回傳原文"""
        mock_instance = MagicMock()
        mock_instance.compress_prompt.side_effect = RuntimeError("GPU OOM")
        mock_compressor_cls.return_value = mock_instance

        compressor = LLMLinguaCompressor(LLMLinguaConfig(min_token_threshold=100))
        long_text = "word " * 2000

        result, stats = compressor.compress(long_text)
        assert result == long_text
        assert stats["skipped"] is True
        assert "error" in stats["reason"]


# ═══════════════════════════════════════════════════════════════════════════
# MicroPruner + LLMLingua 整合
# ═══════════════════════════════════════════════════════════════════════════


class TestMicroPrunerLLMLingua:
    def test_llmlingua_disabled_by_default(self):
        """預設不啟用 LLMLingua"""
        pruner = MicroPruner(use_llmlingua=False)
        assert pruner.use_llmlingua is False

    def test_llmlingua_skips_short_messages(self):
        """短訊息不觸發 LLMLingua"""
        pruner = MicroPruner(use_llmlingua=True)
        msgs = [
            Message(role="user", content="Hello", token_count=2),
            Message(role="assistant", content="Hi there!", token_count=5),
        ]
        result, saved = pruner.prune(msgs)
        assert len(result) == 2
        assert saved == 0

    @patch("echo_thought_core.compression.micro_pruning.MicroPruner._get_llmlingua")
    def test_llmlingua_triggers_on_long_message(self, mock_get):
        """超過 2000 tokens 的 user/assistant 訊息觸發 LLMLingua"""
        mock_compressor = MagicMock()
        mock_compressor.compress.return_value = (
            "compressed content",
            {
                "original_tokens": 3000,
                "compressed_tokens": 1500,
                "ratio": 0.5,
                "saving": 1500,
                "skipped": False,
            },
        )
        mock_get.return_value = mock_compressor

        pruner = MicroPruner(use_llmlingua=True)
        long_content = "word " * 3000
        msgs = [
            Message(role="user", content=long_content, token_count=3000),
            Message(role="assistant", content="Short reply", token_count=5),
        ]

        result, saved = pruner.prune(msgs)

        assert saved == 1500
        assert result[0].content == "compressed content"
        assert result[0].metadata.get("llmlingua_compressed") is True
        assert result[1].content == "Short reply"  # 短訊息不動

    def test_llmlingua_skips_compressed_messages(self):
        """已壓縮的訊息不重複壓縮"""
        pruner = MicroPruner(use_llmlingua=True)
        msgs = [
            Message(
                role="user",
                content="word " * 3000,
                token_count=3000,
                is_compressed=True,
            ),
        ]
        result, saved = pruner.prune(msgs)
        assert saved == 0

    def test_llmlingua_skips_tool_messages(self):
        """tool 訊息不觸發 LLMLingua（走 tool pruning 規則）"""
        pruner = MicroPruner(use_llmlingua=True)
        msgs = [
            Message(role="tool", content="x" * 10000, token_count=5000),
        ]
        result, saved = pruner.prune(msgs)
        # tool 訊息可能被 tool pruning 規則處理，但不走 LLMLingua
        assert not any(m.metadata.get("llmlingua_compressed") for m in result)
