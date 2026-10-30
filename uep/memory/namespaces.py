"""記憶 namespace 與資料目錄規則（P3接入方案 §3.1、§3.4 階段 X）。

不 import echo_memory：規則照記憶端 ``echo_memory/namespace.py`` 重寫，變更時兩邊須同步
（正規化 regex、``local-default`` 即資料根、``namespaces/<ns>`` 子目錄、``scope-g<guild_id>`` 共享層）。
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_NAMESPACE_ID = "local-default"
SCOPE_NAMESPACE_PREFIX = "scope-g"

_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SCOPE_NAMESPACE_PATTERN = re.compile(r"^scope-g[0-9]{1,48}$")
_OWNER_ID_PATTERN = re.compile(r"[A-Za-z0-9]+")  # owner_id 會進 scope_ref（scope_private_<id>），只收字母數字


def normalize_namespace_id(namespace_id: str | None) -> str:
    value = (namespace_id or DEFAULT_NAMESPACE_ID).strip()
    if not _NAMESPACE_PATTERN.fullmatch(value):
        raise ValueError("namespace_id 必須是 1-64 字元的英數、底線或連字號")
    return value


def is_scope_namespace_id(namespace_id: str) -> bool:
    """共享層 namespace（``scope-g<guild_id>``）；這類 engine 不啟動做夢排程。"""
    return bool(_SCOPE_NAMESPACE_PATTERN.fullmatch(namespace_id or ""))


def validate_owner_id(owner_id: str) -> str:
    if not _OWNER_ID_PATTERN.fullmatch(owner_id or ""):
        raise ValueError(f"owner_id 只能是英數字（會進 scope_ref）：{owner_id!r}")
    return owner_id


def owner_scope_ref(owner_id: str) -> str:
    return f"scope_private_{validate_owner_id(owner_id)}"


def group_scope_ref(guild_id: str) -> str:
    """guild id -> ``scope_group_g<guild_id>``（與共享 namespace ``scope-g<guild_id>`` 一一對應）。"""
    gid = _guild_id(guild_id)
    return f"scope_group_g{gid}"


def group_namespace_id(guild_id: str) -> str:
    return f"{SCOPE_NAMESPACE_PREFIX}{_guild_id(guild_id)}"


def guild_id_of_group_scope(scope_ref: str) -> str | None:
    match = re.fullmatch(r"scope_group_g([0-9]{1,48})", scope_ref or "")
    return match.group(1) if match else None


def _guild_id(guild_id: str) -> str:
    gid = str(guild_id).strip()
    if not re.fullmatch(r"[0-9]{1,48}", gid):
        raise ValueError(f"無效的 guild id：{guild_id!r}")
    return gid


def resolve_namespace_path(storage_root: str | Path, namespace_id: str | None) -> Path:
    """engine 的實際資料目錄：``local-default`` 即根目錄本身，其餘為 ``root/namespaces/<ns>``。"""
    root = Path(storage_root)
    normalized = normalize_namespace_id(namespace_id)
    if normalized == DEFAULT_NAMESPACE_ID:
        return root
    return root / "namespaces" / normalized


def _canonical(path: Path) -> Path:
    return Path(os.path.normcase(str(path.resolve())))


def paths_overlap(a: Path, b: Path) -> bool:
    """兩個目錄相等、或互為祖先／後代（Windows 不分大小寫）。"""
    left, right = _canonical(a), _canonical(b)
    return left == right or left in right.parents or right in left.parents


@dataclass(frozen=True)
class ForeignClaim:
    """其他系統（bot）已占用的記憶目錄。``namespaces`` 為 None 表示整個根目錄都算它的。"""

    root: Path
    namespaces: Sequence[str] | None = None
    label: str = "bot"

    def claimed_paths(self) -> dict[str, Path]:
        if self.namespaces is None:
            return {f"{self.label}:*": Path(self.root)}
        return {
            f"{self.label}:{ns}": resolve_namespace_path(self.root, ns) for ns in self.namespaces
        }


def find_overlaps(core_paths: Mapping[str, Path], foreign: Sequence[ForeignClaim]) -> list[str]:
    """階段 X 核對：Core 每個 namespace 的目錄不得與任何外部占用目錄相交。回傳違規描述（空表示通過）。"""
    problems: list[str] = []
    for ns, core_path in core_paths.items():
        for claim in foreign:
            for name, claimed in claim.claimed_paths().items():
                if paths_overlap(core_path, claimed):
                    problems.append(f"namespace {ns!r}（{core_path}）與 {name}（{claimed}）目錄相交")
    return problems
