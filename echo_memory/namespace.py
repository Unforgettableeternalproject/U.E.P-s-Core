from __future__ import annotations

import re
from pathlib import Path

DEFAULT_NAMESPACE_ID = "local-default"
_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


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

