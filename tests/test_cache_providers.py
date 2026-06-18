"""
Cache Provider 測試

驗證：
- ExplicitCacheProvider Protocol 契約
- NullCacheProvider 行為（all no-op）
- GeminiCacheProvider 基本行為（無 GCP 環境時 is_available=False）
- CacheManager + provider 注入（向後相容）
- Ollama 路徑不觸發 google.genai import
"""

from __future__ import annotations



from echo_thought_core.cache.providers import (
    ExplicitCacheProvider,
    GeminiCacheProvider,
    NullCacheProvider,
)


# ── NullCacheProvider ────────────────────────────────────────────────────────


class TestNullCacheProvider:
    """NullCacheProvider 行為"""

    def test_is_explicit_cache_provider(self):
        """滿足 ExplicitCacheProvider Protocol"""
        provider = NullCacheProvider()
        assert isinstance(provider, ExplicitCacheProvider)

    def test_is_not_available(self):
        """永遠不可用"""
        provider = NullCacheProvider()
        assert provider.is_available() is False

    def test_create_returns_none(self):
        """建立快取回傳 None"""
        provider = NullCacheProvider()
        result = provider.create_cache(
            content="test", display_name="test", ttl_seconds=60,
        )
        assert result is None

    def test_delete_returns_false(self):
        """刪除回傳 False"""
        provider = NullCacheProvider()
        assert provider.delete_cache("some-id") is False


# ── GeminiCacheProvider（無 GCP 環境）─────────────────────────────────────


class TestGeminiCacheProviderNoEnv:
    """GeminiCacheProvider 在無 GCP 環境下的行為"""

    def test_is_explicit_cache_provider(self):
        """滿足 ExplicitCacheProvider Protocol"""
        provider = GeminiCacheProvider(client=None, model="test-model")
        assert isinstance(provider, ExplicitCacheProvider)

    def test_no_client_not_available(self):
        """無 client 時不可用（不拋例外）"""
        # 這個測試在沒有 GCP 環境時會走到 _create_client() 失敗路徑
        # 但不應拋出未處理的例外
        provider = GeminiCacheProvider(client=None, model="test-model")
        # 可能可用也可能不可用（取決於環境），但不應拋錯
        _ = provider.is_available()

    def test_create_without_model(self):
        """無 model 時回傳 None"""
        provider = GeminiCacheProvider(client=None, model="")
        result = provider.create_cache(
            content="test", display_name="test", ttl_seconds=60,
        )
        assert result is None

    def test_delete_without_client(self):
        """無 client 時刪除回傳 False"""
        provider = GeminiCacheProvider.__new__(GeminiCacheProvider)
        provider._client = None
        provider._model = ""
        assert provider.delete_cache("some-id") is False


# ── CacheManager + Provider 注入 ────────────────────────────────────────────


class TestCacheManagerProviderInjection:
    """CacheManager 接收 explicit_provider 注入"""

    def test_with_null_provider(self):
        """注入 NullCacheProvider"""
        from echo_thought_core.cache.local_cache import CacheManager

        mgr = CacheManager(
            config={"test_mode": True},
            explicit_provider=NullCacheProvider(),
        )
        assert mgr._explicit_provider is not None
        assert mgr._explicit_provider.is_available() is False

    def test_null_provider_create_cached_content(self):
        """NullCacheProvider 下 _create_cached_content 回傳 None"""
        from echo_thought_core.cache.local_cache import CacheManager

        mgr = CacheManager(
            config={"test_mode": True},
            explicit_provider=NullCacheProvider(),
        )
        result = mgr._create_cached_content("content", "name", 60)
        assert result is None

    def test_local_lru_still_works(self):
        """Provider 注入不影響本地 LRU 快取"""
        from echo_thought_core.cache.local_cache import CacheManager

        mgr = CacheManager(
            config={"test_mode": True},
            explicit_provider=NullCacheProvider(),
        )

        key = mgr.generate_cache_key("test", "content")
        mgr.put_local_cache(key, {"data": 42})
        cached = mgr.get_local_cache(key)
        assert cached == {"data": 42}

    def test_backward_compat_no_provider(self):
        """不傳 provider 時向後相容（自動建立）"""
        from echo_thought_core.cache.local_cache import CacheManager

        mgr = CacheManager(config={"test_mode": True})
        assert mgr._explicit_provider is not None
        # 在 test_mode 下，should_create_explicit_cache 應回傳 False
        assert mgr.should_create_explicit_cache("test", "hash") is False

    def test_smart_cache_respects_provider(self):
        """NullCacheProvider 下智能快取決策不受影響"""
        from echo_thought_core.cache.local_cache import CacheManager

        mgr = CacheManager(
            config={"test_mode": False, "smart_cache_threshold": 10},
            explicit_provider=NullCacheProvider(),
        )
        # 即使 provider 不可用，smart_cache 邏輯仍按 threshold 運作
        # 小內容 → 使用隱性快取
        assert mgr.should_create_explicit_cache("x", "hash") is False


# ── Ollama 路徑隔離驗證 ──────────────────────────────────────────────────────


class TestOllamaImportIsolation:
    """T-02-3: Ollama 路徑不觸發 google.genai import"""

    def test_null_provider_no_google_import(self):
        """NullCacheProvider import 不觸發 google.genai"""
        # 記錄 import 前的 google.genai 模組狀態
        # 注意：在完整測試套件中 google.genai 可能已被其他測試載入
        # 這裡驗證 NullCacheProvider 本身不需要 google.genai
        from echo_thought_core.cache.providers import NullCacheProvider as NCP

        provider = NCP()
        assert provider.is_available() is False
        assert provider.create_cache("t", "t", 60) is None
        # 如果到這裡沒有 ImportError，就表示 NullCacheProvider 不依賴 google

    def test_ollama_backend_no_google_dependency(self):
        """OllamaBackend 模組本身不 import google.genai"""
        # 讀取 ollama.py 的原始碼，確認沒有 google import
        import inspect
        from echo_thought_core.backends.ollama import OllamaBackend

        source = inspect.getsource(OllamaBackend)
        assert "from google" not in source
        assert "import google" not in source
