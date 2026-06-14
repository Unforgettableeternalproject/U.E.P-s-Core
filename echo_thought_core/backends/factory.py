"""
Backend Factory — 根據設定建立對應的後端實例，或一次建立完整的 ContextManager

提供兩個層級的 API：
- create_backend(): 低層級，只建立 LLM 後端
- create_context_manager(): 高層級，一次建立帶有 Backend + CacheManager 的完整 ContextManager
"""

from typing import Optional, Dict, Any
from echo_thought_core.backends.base import AbstractBackend


def create_backend(
    backend_type: str = "gemini",
    config: Optional[Dict[str, Any]] = None,
) -> AbstractBackend:
    """根據後端類型建立對應的實例

    Args:
        backend_type: "gemini" | "local"
        config: 後端特定設定

    Returns:
        AbstractBackend 實例
    """
    config = config or {}

    if backend_type == "gemini":
        from echo_thought_core.backends.gemini import GeminiBackend

        return GeminiBackend(config=config)

    elif backend_type == "local":
        raise NotImplementedError(
            "Local model backend (Qwen2.5) is planned for Phase 3"
        )

    else:
        raise ValueError(f"Unknown backend type: {backend_type}")


def create_context_manager(
    backend_type: str = "gemini",
    backend_config: Optional[Dict[str, Any]] = None,
    compaction_config: Optional["CompactionConfig"] = None,
    persona: Optional["PersonaContext"] = None,
    cache_config: Optional[Dict[str, Any]] = None,
) -> "ContextManager":
    """一次建立帶有 Backend + CacheManager 的完整 ContextManager

    這是推薦的高層級 API。內部處理所有串接邏輯：
    - 建立 LLM Backend
    - 建立 CacheManager（可選，與 Backend 共享 Gemini client）
    - 將 CacheManager 注入 Backend
    - 將 Backend 傳入 ContextManager

    Args:
        backend_type: 後端類型，預設 "gemini"
        backend_config: 後端設定 dict（傳給 create_backend）
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
    from echo_thought_core.schemas.config import CompactionConfig, PersonaContext

    backend_config = backend_config or {}

    # Step 1: 建立 Backend
    if backend_type == "gemini":
        from echo_thought_core.backends.gemini import GeminiBackend

        # 先建立不帶 CacheManager 的 Backend（需要先取得 client）
        backend = GeminiBackend(config=backend_config)

        # Step 2: 建立 CacheManager（可選），共享 Backend 的 Gemini client
        if cache_config is not None:
            from echo_thought_core.cache.local_cache import CacheManager

            # 共享 Gemini client，避免重複建立連線
            shared_client = backend._get_client()
            cache_mgr = CacheManager(config=cache_config, client=shared_client)
            backend._cache_manager = cache_mgr
    else:
        backend = create_backend(backend_type, backend_config)

    # Step 3: 建立 ContextManager，注入 Backend
    config = compaction_config or CompactionConfig(
        backend=backend_type,
        model=backend_config.get("model", "gemini-2.5-flash-lite"),
    )

    return ContextManager(
        config=config,
        persona=persona,
        backend=backend,
    )
