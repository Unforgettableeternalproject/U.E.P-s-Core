"""
Local Cache — 本地 LRU 快取

從 U.E.P Core 的 cache_manager.py 遷出並精簡。
只保留本地快取邏輯，移除 Gemini 顯性快取（那是 Gemini backend 的事）。
"""

import time
import hashlib
from typing import Optional, Any, Dict
from dataclasses import dataclass, field


@dataclass
class CacheEntry:
    """快取條目"""

    content: Any
    timestamp: float
    hit_count: int = 0
    last_access: float = field(default_factory=time.time)

    def is_expired(self, ttl_seconds: int) -> bool:
        return time.time() - self.timestamp > ttl_seconds


class LocalCache:
    """本地 LRU 快取管理器"""

    def __init__(self, max_entries: int = 100, ttl_seconds: int = 1800):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, CacheEntry] = {}

        # 統計
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @staticmethod
    def make_key(category: str, content: str, **kwargs) -> str:
        """生成快取鍵"""
        parts = [category, content]
        for k, v in sorted(kwargs.items()):
            parts.append(f"{k}:{v}")
        return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[Any]:
        """取得快取內容"""
        if key not in self._cache:
            self.misses += 1
            return None

        entry = self._cache[key]
        if entry.is_expired(self.ttl_seconds):
            del self._cache[key]
            self.misses += 1
            return None

        entry.hit_count += 1
        entry.last_access = time.time()
        self.hits += 1
        return entry.content

    def put(self, key: str, content: Any) -> None:
        """存入快取"""
        if len(self._cache) >= self.max_entries:
            self._evict_lru()

        self._cache[key] = CacheEntry(
            content=content,
            timestamp=time.time(),
        )

    def _evict_lru(self) -> None:
        """淘汰最久未使用的條目"""
        if not self._cache:
            return

        oldest_key = min(
            self._cache.keys(),
            key=lambda k: self._cache[k].last_access,
        )
        del self._cache[oldest_key]
        self.evictions += 1

    def cleanup_expired(self) -> int:
        """清理過期條目"""
        expired = [
            k for k, v in self._cache.items() if v.is_expired(self.ttl_seconds)
        ]
        for k in expired:
            del self._cache[k]
        return len(expired)

    def get_stats(self) -> Dict[str, int]:
        """取得快取統計"""
        total = self.hits + self.misses
        return {
            "entries": len(self._cache),
            "max_entries": self.max_entries,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "evictions": self.evictions,
        }

    def clear(self) -> None:
        """清空快取"""
        self._cache.clear()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
