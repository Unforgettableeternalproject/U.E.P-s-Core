"""
Test Wiring — 端到端串接整合測試

驗證所有串接點：
- BackendFactory 正確建立 LLMClient
- create_context_manager() 一次建立帶 backend 的完整實例
- CacheManager client 注入
- ContextManager → LLMClient 的壓縮管線（mock，不呼叫真實 API）
- Persona loader
- LLMClient 對話歷史管理
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from echo_thought_core import (
    CompactionConfig,
    ContextManager,
    LLMClient,
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
# LLMClient 基本建構 (T-01)
# ═══════════════════════════════════════════════════════════════════════════


class TestLLMClientConstruction:
    """LLMClient 的建構與 AbstractBackend 介面相容性"""

    def test_create_llm_client_default(self):
        """T-01: LLMClient() 建構不拋錯，延遲初始化"""
        client = LLMClient()
        assert client is not None
        assert isinstance(client, AbstractBackend)
        assert hasattr(client, "query")
        assert hasattr(client, "summarize")
        assert hasattr(client, "compact")
        assert hasattr(client, "chat")
        assert hasattr(client, "generate")
        assert hasattr(client, "as_llm_fn")

    def test_create_llm_client_with_config(self):
        """LLMClient 正確傳遞 config dict"""
        client = LLMClient(
            model="gemini-2.5-pro",
            temperature=0.5,
            config={"api_key": "test-key"},
        )
        assert client.model_name == "gemini-2.5-pro"
        assert client.temperature == 0.5

    def test_explicit_params_override_config(self):
        """顯式參數優先於 config dict"""
        client = LLMClient(
            model="gemini-2.5-pro",
            config={"model": "gemini-2.5-flash-lite"},
        )
        assert client.model_name == "gemini-2.5-pro"

    def test_is_abstract_backend(self):
        """LLMClient 是 AbstractBackend 的子類"""
        assert issubclass(LLMClient, AbstractBackend)

    def test_context_window_size(self):
        """get_context_window_size() 回傳正確的值"""
        client = LLMClient(model="gemini-2.5-flash-lite")
        assert client.get_context_window_size() == 1_048_576

    def test_count_tokens_basic(self):
        """count_tokens() 基本計數"""
        client = LLMClient()
        assert client.count_tokens("Hello world") > 0
        assert client.count_tokens("你好世界") > 0

    def test_count_tokens_cjk_higher(self):
        """CJK 字元的 token 計數高於等長 ASCII"""
        client = LLMClient()
        ascii_count = client.count_tokens("abcd")
        cjk_count = client.count_tokens("你好世界")
        # CJK 每字 2/3 token，ASCII 每 4 字元 1 token
        assert cjk_count > ascii_count


# ═══════════════════════════════════════════════════════════════════════════
# BackendFactory 修復驗證 (T-01b)
# ═══════════════════════════════════════════════════════════════════════════


class TestBackendFactory:
    """BackendFactory 的 LLMClient 構造"""

    def test_create_gemini_backend_returns_llm_client(self):
        """T-01b: create_backend("gemini") 回傳 LLMClient"""
        backend = create_backend("gemini", {"model": "gemini-2.5-flash-lite"})
        assert backend is not None
        assert isinstance(backend, LLMClient)
        assert isinstance(backend, AbstractBackend)

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
            assert cm.client is None


# ═══════════════════════════════════════════════════════════════════════════
# LLMClient + CacheManager 整合 (T-05)
# ═══════════════════════════════════════════════════════════════════════════


class TestLLMClientCacheIntegration:
    """LLMClient 自動查詢 CacheManager"""

    def test_client_with_cache_manager(self):
        """T-05: LLMClient 接受 cache_manager 注入"""
        mock_cm = MagicMock(spec=CacheManager)
        mock_cm.caches = {"uep:persona:v1": MagicMock()}

        client = LLMClient(
            config={"model": "gemini-2.5-flash-lite"},
            cache_manager=mock_cm,
        )
        assert client._cache_manager is mock_cm

    def test_client_without_cache_manager(self):
        """不傳 CacheManager 時行為不變"""
        client = LLMClient(config={"model": "gemini-2.5-flash-lite"})
        assert client._cache_manager is None


# ═══════════════════════════════════════════════════════════════════════════
# create_context_manager() (T-03 + T-06)
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateContextManager:
    """高層級 factory：create_context_manager()"""

    @patch("echo_thought_core.llm_client.genai")
    def test_basic_creation(self, mock_genai):
        """T-03: 基本建立帶 backend 的 ContextManager"""
        mgr = create_context_manager(
            backend_config={"model": "gemini-2.5-flash-lite"},
        )
        assert isinstance(mgr, ContextManager)
        assert mgr._backend is not None
        assert isinstance(mgr._backend, LLMClient)
        assert mgr._backend.model_name == "gemini-2.5-flash-lite"

    @patch("echo_thought_core.llm_client.genai")
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

    @patch("echo_thought_core.llm_client.genai")
    def test_with_cache_manager(self, mock_genai):
        """T-06: 帶 CacheManager 建立，共享 client"""
        mock_genai.Client.return_value = MagicMock()

        mgr = create_context_manager(
            backend_config={"model": "gemini-2.5-flash-lite", "api_key": "test"},
            cache_config={"test_mode": True},
        )
        assert mgr._backend is not None
        assert isinstance(mgr._backend, LLMClient)
        assert mgr._backend._cache_manager is not None
        # 驗證共享 client
        assert mgr._backend._cache_manager.client is mgr._backend._get_client()

    @patch("echo_thought_core.llm_client.genai")
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
# LLMClient 歷史管理
# ═══════════════════════════════════════════════════════════════════════════


class TestLLMClientHistory:
    """LLMClient 的對話歷史管理"""

    def test_initial_empty_history(self):
        """新建的 LLMClient 歷史為空"""
        client = LLMClient()
        assert client.turn_count == 0
        assert client.get_history() == []

    def test_clear_history(self):
        """clear_history() 清空所有歷史"""
        client = LLMClient()
        # 模擬加入歷史（直接操作內部 list）
        client._history.append(MagicMock(role="user", parts=[MagicMock(text="hi")]))
        assert len(client._history) == 1
        client.clear_history()
        assert len(client._history) == 0

    def test_as_llm_fn_returns_callable(self):
        """as_llm_fn() 回傳 callable"""
        client = LLMClient()
        fn = client.as_llm_fn()
        assert callable(fn)

    def test_build_prompt_basic(self):
        """build_prompt() 基本拼接"""
        client = LLMClient()
        prompt = client.build_prompt("hello", system_instruction="Be nice")
        assert "hello" in prompt
        assert "Be nice" in prompt

    def test_build_prompt_with_memory(self):
        """build_prompt() 包含記憶脈絡"""
        client = LLMClient()
        prompt = client.build_prompt("hello", memory_context="Past: user likes cats")
        assert "cats" in prompt
        assert "Memory Safety" in prompt


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
            context_window_size=1000,
            thresholds={
                "micro_prune": 0.30,
                "rolling_summary": 0.50,
                "full_compact": 0.90,
            },
            compaction_cooldown_seconds=0,
        )
        mgr = ContextManager(config=config, backend=backend)

        for i in range(30):
            mgr.add_message("user", f"Message {i}: " + "x" * 50)
            mgr.add_message("assistant", f"Reply {i}: " + "y" * 50)

        asyncio.run(mgr.get_messages_async())

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

        for i in range(5):
            mgr.add_message("user", f"Hello {i}")
            mgr.add_message("assistant", f"Hi {i}")

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
        assert len(mgr.history.messages) <= 4

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

    def test_load_nonexistent_config(self):
        """不存在的設定檔回傳空 dict"""
        config = load_llm_config(Path("/nonexistent/path.yaml"))
        assert config == {}

    def test_build_persona_from_config(self):
        """從 config 建立 PersonaContext"""
        persona = build_persona_from_config()
        assert persona.name == "U.E.P"
        assert "cheerful" in persona.traits
        assert persona.custom_instructions
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
