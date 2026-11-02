"""
Backend Registry — LLM 後端提供者註冊表

集中管理所有可用的 LLM 後端，支援：
- 內建後端自動註冊（gemini, ollama）
- 自訂後端動態註冊
- 按名稱建立實例
- 列出可用提供者

設計理念：
  Registry 管理「怎麼建」（factory function），
  不持有已建立的實例。實例由 ContextManager 持有。

Usage:
    from echo_thought_core.backends.registry import BackendRegistry

    # 取得全域 registry（自動註冊內建後端）
    registry = BackendRegistry.default()

    # 建立後端
    backend = registry.create("ollama", {"model": "gemma4:e4b-it-q4_K_M"})

    # 註冊自訂後端
    registry.register("my_custom", MyCustomBackend)

    # 列出可用
    print(registry.list_providers())  # ['gemini', 'ollama', 'my_custom']
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from echo_thought_core.backends.base import AbstractBackend

logger = logging.getLogger(__name__)

# 後端工廠函式的型別：(config: dict) -> AbstractBackend
BackendFactory = Callable[[dict[str, Any]], AbstractBackend]


class BackendRegistry:
    """LLM 後端提供者註冊表

    每個提供者用一個 factory function 註冊：
    - factory(config: dict) -> AbstractBackend
    - config 由使用者傳入，factory 負責解析
    """

    def __init__(self):
        self._providers: dict[str, BackendFactory] = {}

    # -- 單例 ---------------------------------------------------------------

    _default_instance: BackendRegistry | None = None

    @classmethod
    def default(cls) -> BackendRegistry:
        """取得全域 registry（懶初始化，自動註冊內建後端）"""
        if cls._default_instance is None:
            registry = cls()
            registry._register_builtins()
            cls._default_instance = registry
        return cls._default_instance

    @classmethod
    def reset_default(cls) -> None:
        """重置全域 registry（測試用）"""
        cls._default_instance = None

    # -- 註冊 ---------------------------------------------------------------

    def register(
        self,
        name: str,
        factory: BackendFactory | type,
        *,
        override: bool = False,
    ) -> None:
        """註冊一個後端提供者

        Args:
            name:     提供者名稱（如 "ollama", "my_custom"）
            factory:  工廠函式 (dict) -> AbstractBackend，
                      或 AbstractBackend 子類（會自動包裝）
            override: 是否允許覆蓋已存在的提供者
        """
        if name in self._providers and not override:
            raise ValueError(
                f"Provider {name!r} already registered. "
                f"Use override=True to replace."
            )

        # 如果傳入的是 class 而非 factory function，自動包裝
        if isinstance(factory, type) and issubclass(factory, AbstractBackend):
            cls_ref = factory
            factory = lambda config, _cls=cls_ref: _cls(config=config)  # noqa: E731

        self._providers[name] = factory
        logger.debug("[Registry] Registered provider: %s", name)

    def unregister(self, name: str) -> bool:
        """移除一個提供者

        Returns:
            True 如果成功移除，False 如果不存在
        """
        if name in self._providers:
            del self._providers[name]
            return True
        return False

    # -- 建立 ---------------------------------------------------------------

    def create(self, name: str, config: dict[str, Any] | None = None) -> AbstractBackend:
        """建立一個後端實例

        Args:
            name:   提供者名稱
            config: 後端設定 dict

        Returns:
            AbstractBackend 實例

        Raises:
            ValueError: 未知的提供者名稱
        """
        if name not in self._providers:
            available = ", ".join(f"'{p}'" for p in sorted(self._providers))
            raise ValueError(
                f"Unknown backend provider: {name!r}. "
                f"Available: {available or '(none)'}"
            )

        factory = self._providers[name]
        return factory(config or {})

    # -- 查詢 ---------------------------------------------------------------

    def list_providers(self) -> list[str]:
        """列出所有已註冊的提供者名稱"""
        return sorted(self._providers.keys())

    def has(self, name: str) -> bool:
        """檢查提供者是否已註冊"""
        return name in self._providers

    def __contains__(self, name: str) -> bool:
        return self.has(name)

    def __len__(self) -> int:
        return len(self._providers)

    def __repr__(self) -> str:
        providers = ", ".join(self.list_providers())
        return f"BackendRegistry([{providers}])"

    # -- 內建後端 -----------------------------------------------------------

    def _register_builtins(self) -> None:
        """註冊內建後端（延遲 import 避免載入依賴）"""

        def _gemini_factory(config: dict) -> AbstractBackend:
            from echo_thought_core.llm_client import LLMClient
            return LLMClient(config=config)

        def _ollama_factory(config: dict) -> AbstractBackend:
            from echo_thought_core.backends.ollama import OllamaBackend
            return OllamaBackend(config=config)

        def _openai_factory(config: dict) -> AbstractBackend:
            from echo_thought_core.backends.openai import OpenAIBackend
            return OpenAIBackend(config=config)

        self.register("gemini", _gemini_factory)
        self.register("ollama", _ollama_factory)
        self.register("openai", _openai_factory)
