"""
WorkingMemory - 工作記憶（短期緩存）

使用 dict + TTL 實現簡單的短期記憶。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("echo_memory.working")


class WorkingMemory:
    """工作記憶 - 短期緩存"""

    def __init__(self, ttl_seconds: int = 300):
        """
        Args:
            ttl_seconds: 緩存有效期（秒）
        """
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[Any, float]] = {}  # key -> (value, expire_time)

        logger.info(f"[WorkingMemory] 初始化完成 - TTL: {ttl_seconds}s")

    def set(self, key: str, value: Any):
        """設置緩存"""
        expire_at = time.time() + self.ttl
        self._cache[key] = (value, expire_at)

    def get(self, key: str) -> Any | None:
        """獲取緩存（自動清理過期）"""
        if key not in self._cache:
            return None

        value, expire_at = self._cache[key]
        if time.time() >= expire_at:
            del self._cache[key]
            return None

        return value

    def clear(self):
        """清空所有緩存"""
        self._cache.clear()
