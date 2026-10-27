from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar

DEFAULT_NAMESPACE_ID = "local-default"
_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
T = TypeVar("T")


def normalize_namespace_id(namespace_id: str | None) -> str:
    value = (namespace_id or DEFAULT_NAMESPACE_ID).strip()
    if not _NAMESPACE_PATTERN.fullmatch(value):
        raise ValueError(
            "namespace_id must be 1-64 characters using letters, numbers, '_' or '-'"
        )
    return value


def resolve_namespace_path(storage_root: str | Path, namespace_id: str | None) -> Path:
    root = Path(storage_root)
    normalized = normalize_namespace_id(namespace_id)
    if normalized == DEFAULT_NAMESPACE_ID:
        return root
    return root / "namespaces" / normalized


class NamespaceEngineRegistry(Generic[T]):
    def __init__(self, factory: Callable[[str], T]):
        self._factory = factory
        self._instances: dict[str, T] = {}
        self._lock = threading.Lock()

    def get(self, namespace_id: str | None = None) -> T:
        normalized = normalize_namespace_id(namespace_id)
        with self._lock:
            instance = self._instances.get(normalized)
            if instance is None:
                instance = self._factory(normalized)
                self._instances[normalized] = instance
            return instance

    def clear(self) -> None:
        with self._lock:
            self._instances.clear()


def scope_namespace_id(guild_id: str | int) -> str:
    """guild id -> 共享層 namespace id（``scope-g<guild_id>``）；id 無效拋 ValueError。"""
    from echo_memory.schemas.sharing import guild_scope, scope_to_namespace_id

    return normalize_namespace_id(scope_to_namespace_id(guild_scope(guild_id)))
