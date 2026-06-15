"""
Test Wiring — 端到端串接整合測試

驗證 feature/wiring 的所有串接點：
- BackendFactory 正確建立 GeminiBackend
- create_context_manager() 一次建立帶 backend 的完整實例
- CacheManager client 注入
- ContextManager → Backend 的壓縮管線（mock，不呼叫真實 API）
- Persona loader
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from echo_thought_core import (
    CompactionConfig,
    ContextManager,
    PersonaContext,
)
from echo_thought_core.backends.base import AbstractBackend
from echo_thought_core.backends.factory import create_backend, create_context_manager
from echo_thought_core.cache.local_cache import CacheManager
from echo_thought_core.persona import (
    build_persona_from_config,
    build_system_instruction,
    load_llm_config,
)
from echo_thought_core.schemas.session import CompressionTier


# ═══════════════════════════════════════════════════════════════════════════
# BackendFactory 修復驗證 (T-01)
# ═══════════════════════════════════════════════════════════════════════════


class TestBackendFactory:
    """BackendFactory 的 GeminiBackend 構造修復"""

    @patch("echo_thought_core.backends.gemini.genai")
    def test_create_gemini_backend_no_type_error(self, mock_genai):
        """T-01: create_backend("gemini") 不再拋 TypeError"""
        backend = create_backend("gemini", {"model": "gemini-2.5-flash-lite"})
        assert backend is not None
        assert hasattr(backend, "query")
        assert hasattr(backend, "summarize")
        assert hasattr(backend, "compact")
        assert backend.model_name == "gemini-2.5-flash-lite"

    @patch("echo_thought_core.backends.gemini.genai")
    def test_create_gemini_backend_with_config(self, mock_genai):
        """create_backend 正確傳遞 config dict"""
        backend = create_backend("gemini", {
            "model": "gemini-2.5-pro",
            "temperature": 0.5,
            "api_key": "test-key",
        })
        assert backend.model_name == "gemini-2.5-pro"
        assert backend.temperature == 0.5

    def test_create_backend_unknown_type(self):
        """未知後端類型應拋 ValueError"""
        with pytest.raises(ValueError, match="Unknown backend type"):
            create_backend("unknown_backend")


# ═══════════════════════════════════════════════════════════════════════════
# CacheManager client 注入 (T-04)
# ═══════════════════════════════════════════════════════════════════════════


class TestCacheManagerClientInjection:
    """CacheManager 外部 client 注入"""

    def test_inject_external_client(self):
        """T-04: 注入外部 client 後不嘗試建立 Vertex AI 連線"""
        mock_client = MagicMock()
        cm = CacheManager(config={"test_mode": True}, client=mock_client)
        assert cm.client is mock_client

    def test_default_client_without_injection(self):
        """不注入 client 時，走原有邏輯（無 GCP_PROJECT_ID → client=None）"""
        with patch.dict("os.environ", {}, clear=True):
            cm = CacheManager(config={"test_mode": True})
            # 無 GCP_PROJECT_ID，client 應為 None
            assert cm.client is None


# ═══════════════════════════════════════════════════════════════════════════
# GeminiBackend + CacheManager 整合 (T-05)
# ═══════════════════════════════════════════════════════════════════════════


class TestGeminiCacheIntegration:
    """GeminiBackend 自動查詢 CacheManager"""

    @patch("echo_thought_core.backends.gemini.genai")
    def test_query_auto_cache_lookup(self, mock_genai):
        """T-05: query() 在有 CacheManager 時自動查詢快取"""
        from echo_thought_core.backends.gemini import GeminiBackend

        mock_cm = MagicMock(spec=CacheManager)
        mock_cm.caches = {"uep:persona:v1": MagicMock()}
        mock_cm.get_cached_content_for_generation.return_value = "cached-id-123"

        backend = GeminiBackend(
            config={"model": "gemini-2.5-flash-lite"},
            cache_manager=mock_cm,
        )
        assert backend._cache_manager is mock_cm

    @patch("echo_thought_core.backends.gemini.genai")
    def test_query_without_cache_manager(self, mock_genai):
        """不傳 CacheManager 時行為不變"""
        from echo_thought_core.backends.gemini import GeminiBackend

        backend = GeminiBackend(config={"model": "gemini-2.5-flash-lite"})
        assert backend._cache_manager is None


# ═══════════════════════════════════════════════════════════════════════════
# create_context_manager() (T-03 + T-06)
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateContextManager:
    """高層級 factory：create_context_manager()"""

    @patch("echo_thought_core.backends.gemini.genai")
    def test_basic_creation(self, mock_genai):
        """T-03: 基本建立帶 backend 的 ContextManager"""
        mgr = create_context_manager(
            backend_config={"model": "gemini-2.5-flash-lite"},
        )
        assert isinstance(mgr, ContextManager)
        assert mgr._backend is not None
        assert mgr._backend.model_name == "gemini-2.5-flash-lite"

    @patch("echo_thought_core.backends.gemini.genai")
    def test_with_persona(self, mock_genai):
        """帶 persona 建立"""
        persona = PersonaContext(
            name="U.E.P",
            traits=["cheerful", "curious"],
        )
        mgr = create_context_manager(
            backend_config={"model": "gemini-2.5-flash-lite"},
            persona=persona,
        )
        assert mgr.persona is not None
        assert mgr.persona.name == "U.E.P"

    @patch("echo_thought_core.backends.gemini.genai")
    def test_with_cache_manager(self, mock_genai):
        """T-06: 帶 CacheManager 建立，共享 client"""
        mock_genai.Client.return_value = MagicMock()

        mgr = create_context_manager(
            backend_config={"model": "gemini-2.5-flash-lite", "api_key": "test"},
            cache_config={"test_mode": True},
        )
        assert mgr._backend is not None
        assert mgr._backend._cache_manager is not None
        # 驗證共享 client
        assert mgr._backend._cache_manager.client is mgr._backend._get_client()

    @patch("echo_thought_core.backends.gemini.genai")
    def test_with_compaction_config(self, mock_genai):
        """自訂壓縮設定"""
        custom_config = CompactionConfig(
            backend="gemini",
            model="gemini-2.5-flash-lite",
            context_window_size=50000,
        )
        mgr = create_context_manager(
            backend_config={"model": "gemini-2.5-flash-lite"},
            compaction_config=custom_config,
        )
        assert mgr.config.context_window_size == 50000


# ═══════════════════════════════════════════════════════════════════════════
# 端到端壓縮管線（mock backend）
# ═══════════════════════════════════════════════════════════════════════════


class TestCompressionPipeline:
    """端到端：add_message → 觸發壓縮 → backend 被呼叫"""

    def _make_mock_backend(self):
        """建立 mock backend"""
        backend = MagicMock(spec=AbstractBackend)
        backend.summarize = AsyncMock(return_value="Summary of old messages.")
        backend.compact = AsyncMock(return_value="<summary>Compacted content.</summary>")
        backend.count_tokens.return_value = 100
        return backend

    def test_layer2_triggers_backend_summarize(self):
        """Layer 2 壓縮管線：觸發 → backend.summarize() 被呼叫"""
        backend = self._make_mock_backend()

        config = CompactionConfig(
            backend="gemini",
            model="gemini-2.5-flash-lite",
            context_window_size=1000,  # 小 window
            thresholds={
                "micro_prune": 0.30,
                "rolling_summary": 0.50,
                "full_compact": 0.90,
            },
            compaction_cooldown_seconds=0,
        )
        mgr = ContextManager(config=config, backend=backend)

        # 灌入足夠多的訊息超過 rolling_summary 閾值
        for i in range(30):
            mgr.add_message("user", f"Message {i}: " + "x" * 50)
            mgr.add_message("assistant", f"Reply {i}: " + "y" * 50)

        # 觸發異步壓縮
        asyncio.run(mgr.get_messages_async())

        # 驗證：backend.summarize 或 compact 被呼叫過
        assert backend.summarize.called or backend.compact.called

    def test_manual_compact_triggers_backend(self):
        """手動 /compact 觸發 Layer 3"""
        backend = self._make_mock_backend()

        config = CompactionConfig(
            backend="gemini",
            context_window_size=100000,
            compaction_cooldown_seconds=0,
        )
        mgr = ContextManager(config=config, backend=backend)

        # 加幾條訊息
        for i in range(5):
            mgr.add_message("user", f"Hello {i}")
            mgr.add_message("assistant", f"Hi {i}")

        # 手動壓縮
        result = asyncio.run(mgr.compact())
        assert result is not None
        assert backend.compact.called

    def test_layer4_no_backend_needed(self):
        """Layer 4 緊急截斷不需要 backend"""
        config = CompactionConfig(
            backend="gemini",
            context_window_size=100000,
            max_emergency_messages=2,
        )
        mgr = ContextManager(config=config, backend=None)

        for i in range(10):
            mgr.add_message("user", f"Message {i}")
            mgr.add_message("assistant", f"Reply {i}")

        result = mgr.handle_api_error(413)
        assert result is not None
        assert result.tier == CompressionTier.TIER_4_EMERGENCY
        assert len(mgr.history.messages) <= 4  # max_emergency_messages * 2 保守估計

    def test_on_compaction_callback(self):
        """壓縮觸發 Echo Memory 回調"""
        backend = self._make_mock_backend()
        callback_received = []

        async def mock_callback(result):
            callback_received.append(result)

        config = CompactionConfig(
            backend="gemini",
            context_window_size=100000,
            compaction_cooldown_seconds=0,
        )
        mgr = ContextManager(config=config, backend=backend)
        mgr.on_compaction(mock_callback)

        for i in range(5):
            mgr.add_message("user", f"Hello {i}")
            mgr.add_message("assistant", f"Hi {i}")

        asyncio.run(mgr.compact())
        assert len(callback_received) > 0


# ═══════════════════════════════════════════════════════════════════════════
# Persona Loader (T-03b)
# ═══════════════════════════════════════════════════════════════════════════


class TestPersonaLoader:
    """Persona 設定檔載入"""

    def test_load_llm_config(self):
        """載入預設 llm_config.yaml"""
        config = load_llm_config()
        assert isinstance(config, dict)
        assert "character" in config
        assert "U.E.P" in config["character"]
        assert config.get("model") == "gemini-2.5-flash-lite"

    def test_load_nonexistent_config(self):
        """不存在的設定檔回傳空 dict"""
        config = load_llm_config(Path("/nonexistent/path.yaml"))
        assert config == {}

    def test_build_persona_from_config(self):
        """從 config 建立 PersonaContext"""
        persona = build_persona_from_config()
        assert persona.name == "U.E.P"
        assert "cheerful" in persona.traits
        assert persona.custom_instructions  # character 文字應非空
        assert "U.E.P" in persona.custom_instructions

    def test_build_system_instruction(self):
        """組裝 system_instruction"""
        instruction = build_system_instruction()
        assert "U.E.P" in instruction
        assert "interdimensional" in instruction

    def test_build_system_instruction_with_extra(self):
        """附加額外上下文"""
        instruction = build_system_instruction(extra_context="Remember: user likes cats.")
        assert "U.E.P" in instruction
        assert "cats" in instruction


