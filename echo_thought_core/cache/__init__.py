"""Cache — 快取層（從 U.E.P Core 遷出改造）

包含：
- CacheManager: 顯性/隱性/本地 LRU 三層快取管理
- ExplicitCacheProvider: 顯性快取的可插拔後端 Protocol
- GeminiCacheProvider: Gemini/Vertex AI 實作
- NullCacheProvider: 無操作實作（Ollama 等後端）
"""

from echo_thought_core.cache.providers import (
    ExplicitCacheProvider,
    GeminiCacheProvider,
    NullCacheProvider,
)

__all__ = [
    "ExplicitCacheProvider",
    "GeminiCacheProvider",
    "NullCacheProvider",
]
