"""
Backend Factory — 根據設定建立 LLM 後端或完整的 ContextManager

提供兩個層級的 API：
- create_backend(): 低層級，建立 AbstractBackend 實例
- create_context_manager(): 高層級，一次建立帶有 Backend + CacheManager 的完整 ContextManager

支援的後端：
- "gemini": Google Gemini / Vertex AI（需要 google-genai）
- "ollama": 本地 Ollama 服務（需要 requests + 本地 Ollama 執行中）
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from echo_thought_core.backends.base import AbstractBackend
from echo_thought_core.defaults import DEFAULT_MODEL

if TYPE_CHECKING:
    from echo_thought_core.context_manager import ContextManager
    from echo_thought_core.schemas.config import CompactionConfig, PersonaContext

logger = logging.getLogger(__name__)

# 已註冊的後端類型
SUPPORTED_BACKENDS = ("gemini", "ollama")


def create_backend(
    backend_type: str = "gemini",
    config: dict[str, Any] | None = None,
) -> AbstractBackend:
    """根據後端類型建立對應的實例

    Args:
        backend_type: "gemini" | "ollama"
        config: 後端特定設定

    Returns:
        AbstractBackend 實例
    """
    config = config or {}

    if backend_type == "gemini":
        from echo_thought_core.llm_client import LLMClient

        return LLMClient(config=config)

    elif backend_type == "ollama":
        from echo_thought_core.backends.ollama import OllamaBackend

        return OllamaBackend(config=config)

    else:
        supported = ", ".join(f"'{b}'" for b in SUPPORTED_BACKENDS)
        raise ValueError(
            f"Unknown backend type: {backend_type!r}. "
            f"Supported backends: {supported}"
        )


def create_context_manager(
    backend_type: str = "gemini",
    backend_config: dict[str, Any] | None = None,
    compaction_config: CompactionConfig | None = None,
    persona: PersonaContext | None = None,
    cache_config: dict[str, Any] | None = None,
    memory_bridge: Any | None = None,
) -> ContextManager:
    """一次建立帶有 LLMClient + CacheManager 的完整 ContextManager

    這是推薦的高層級 API。內部處理所有串接邏輯：
    - 建立 LLMClient（實作 AbstractBackend）
    - 建立 CacheManager（可選，與 LLMClient 共享 Gemini client）
    - 將 CacheManager 注入 LLMClient
    - 將 LLMClient 傳入 ContextManager

    Args:
        backend_type: 後端類型，預設 "gemini"
        backend_config: 後端設定 dict（傳給 LLMClient）
        compaction_config: 壓縮設定（CompactionConfig 實例）
        persona: 人設上下文（PersonaContext 實例）
        cache_config: CacheManager 設定 dict（None 表示不啟用顯性快取）

    Returns:
        完整串接的 ContextManager 實例

    Example:
        >>> from echo_thought_core.backends.factory import create_context_manager
        >>> manager = create_context_manager(
        ...     backend_config={"model": "gemini-2.5-flash-lite"},
        ...     persona=PersonaContext(name="U.E.P"),
        ... )
        >>> manager.add_message("user", "Hello!")
    """
    from echo_thought_core.context_manager import ContextManager
    from echo_thought_core.schemas.config import CompactionConfig

    backend_config = backend_config or {}

    # Step 1: 建立 Backend
    if backend_type == "gemini":
        from echo_thought_core.llm_client import LLMClient

        client = LLMClient(config=backend_config)

        # Step 2: 建立 CacheManager（可選），共享 LLMClient 的 Gemini client
        if cache_config is not None:
            from echo_thought_core.cache.local_cache import CacheManager

            shared_client = client._get_client()
            cache_mgr = CacheManager(config=cache_config, client=shared_client)
            client._cache_manager = cache_mgr

        backend: AbstractBackend = client
    elif backend_type == "ollama":
        from echo_thought_core.backends.ollama import OllamaBackend

        backend = OllamaBackend(config=backend_config)
        if cache_config is not None:
            logger.warning(
                "[Factory] cache_config 僅支援 gemini 後端，ollama 後端的 cache_config 被忽略",
            )
    else:
        backend = create_backend(backend_type, backend_config)
        if cache_config is not None:
            logger.warning(
                "[Factory] cache_config 僅支援 gemini 後端，%s 後端的 cache_config 被忽略",
                backend_type,
            )

    # Step 3: 建立 ContextManager，注入 Backend
    config = compaction_config or CompactionConfig(
        backend=backend_type,
        model=backend_config.get("model", DEFAULT_MODEL),
    )

    return ContextManager(
        config=config,
        persona=persona,
        backend=backend,
        memory_bridge=memory_bridge,
    )
