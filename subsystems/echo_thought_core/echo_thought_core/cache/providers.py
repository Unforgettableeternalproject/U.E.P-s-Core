"""
Explicit Cache Providers — 顯性快取的可插拔後端

將 CacheManager 中 provider-specific 的顯性快取邏輯分離為
獨立的 Provider 實作。CacheManager 透過 ExplicitCacheProvider
Protocol 操作，不再直接耦合 Gemini/Vertex AI。

Providers:
  - GeminiCacheProvider: Gemini/Vertex AI CachedContent API
  - NullCacheProvider:   No-op（Ollama 等不支援顯性快取的後端）

Usage:
    from echo_thought_core.cache.providers import NullCacheProvider
    from echo_thought_core.cache.local_cache import CacheManager

    cache_mgr = CacheManager(explicit_provider=NullCacheProvider())
"""

from __future__ import annotations

import logging
import os
from typing import Any

try:
    from typing import Protocol, runtime_checkable
except ImportError:
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]

logger = logging.getLogger(__name__)


@runtime_checkable
class ExplicitCacheProvider(Protocol):
    """顯性快取提供者協定。

    負責與特定 LLM 平台的 CachedContent API 互動。
    CacheManager 透過此介面建立和取用顯性快取，
    本地 LRU 快取邏輯不經過此介面。
    """

    def is_available(self) -> bool:
        """此 provider 是否可用（client 已初始化、服務可達）。"""
        ...

    def create_cache(
        self,
        content: str,
        display_name: str,
        ttl_seconds: int,
        model: str = "",
    ) -> str | None:
        """在遠端建立顯性快取。

        Args:
            content:      要快取的內容（如 system instruction）
            display_name: 快取的顯示名稱
            ttl_seconds:  存活時間（秒）
            model:        關聯的模型名稱

        Returns:
            快取 ID（如 Gemini 的 cache.name），失敗時回傳 None
        """
        ...

    def delete_cache(self, cache_id: str) -> bool:
        """刪除遠端快取。

        Args:
            cache_id: 快取 ID

        Returns:
            是否成功刪除
        """
        ...


# ═══════════════════════════════════════════════════════════════════════════════
# GeminiCacheProvider
# ═══════════════════════════════════════════════════════════════════════════════


class GeminiCacheProvider:
    """Gemini/Vertex AI 顯性快取提供者。

    封裝 google.genai.Client 的 CachedContent API。
    所有 google.genai import 僅在此類內部發生。

    認證優先順序：
    1. 外部注入 client（與 LLMClient 共享）
    2. GOOGLE_API_KEY 環境變數
    3. GCP_PROJECT_ID + GCP_LOCATION（Vertex AI）
    """

    def __init__(
        self,
        client: Any | None = None,
        model: str = "",
    ):
        """初始化 GeminiCacheProvider。

        Args:
            client: 外部注入的 genai.Client（可選）
            model:  預設模型名稱（用於建立快取時的 model 參數）
        """
        self._client = client
        self._model = model

        if self._client is None:
            self._client = self._create_client()

    def _create_client(self) -> Any | None:
        """嘗試建立 Vertex AI / Gemini API client。"""
        try:
            from google import genai  # type: ignore[import-untyped]
        except ImportError:
            logger.warning(
                "[GeminiCacheProvider] google-genai 未安裝，顯性快取不可用"
            )
            return None

        # 優先用 API Key
        api_key = os.getenv("GOOGLE_API_KEY")
        if api_key:
            try:
                client = genai.Client(api_key=api_key)
                logger.info("[GeminiCacheProvider] 使用 GOOGLE_API_KEY 初始化")
                return client
            except Exception as exc:
                logger.error(
                    "[GeminiCacheProvider] API Key 初始化失敗: %s", exc
                )

        # Fallback: Vertex AI
        project_id = os.getenv("GCP_PROJECT_ID")
        location = os.getenv("GCP_LOCATION")
        if project_id:
            try:
                client = genai.Client(
                    vertexai=True,
                    project=project_id,
                    location=location,
                )
                logger.info(
                    "[GeminiCacheProvider] Vertex AI 初始化成功 (project=%s)",
                    project_id,
                )
                return client
            except Exception as exc:
                logger.error(
                    "[GeminiCacheProvider] Vertex AI 初始化失敗: %s", exc
                )

        logger.warning(
            "[GeminiCacheProvider] 無可用認證，顯性快取不可用"
        )
        return None

    def is_available(self) -> bool:
        return self._client is not None

    def create_cache(
        self,
        content: str,
        display_name: str,
        ttl_seconds: int,
        model: str = "",
    ) -> str | None:
        if not self._client:
            return None

        effective_model = model or self._model
        if not effective_model:
            logger.warning("[GeminiCacheProvider] 未指定模型，無法建立快取")
            return None

        try:
            from google.genai import types  # type: ignore[import-untyped]

            cache = self._client.caches.create(
                model=effective_model,
                config=types.CreateCachedContentConfig(
                    display_name=display_name,
                    system_instruction=content,
                    contents=[],
                    ttl=f"{ttl_seconds}s",
                ),
            )

            cache_id = getattr(cache, "name", None)
            logger.info(
                "[GeminiCacheProvider] 快取建立: %s (TTL=%ds)",
                cache_id, ttl_seconds,
            )
            return cache_id

        except Exception as exc:
            logger.error("[GeminiCacheProvider] 快取建立失敗: %s", exc)
            return None

    def delete_cache(self, cache_id: str) -> bool:
        if not self._client:
            return False

        try:
            self._client.caches.delete(name=cache_id)
            logger.info("[GeminiCacheProvider] 快取刪除: %s", cache_id)
            return True
        except Exception as exc:
            logger.error("[GeminiCacheProvider] 快取刪除失敗: %s", exc)
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# NullCacheProvider
# ═══════════════════════════════════════════════════════════════════════════════


class NullCacheProvider:
    """無操作的快取提供者 — 用於不支援顯性快取的後端（如 Ollama）。

    所有操作皆 no-op，is_available() 永遠回傳 False。
    """

    def is_available(self) -> bool:
        return False

    def create_cache(
        self,
        content: str,
        display_name: str,
        ttl_seconds: int,
        model: str = "",
    ) -> str | None:
        return None

    def delete_cache(self, cache_id: str) -> bool:
        return False
