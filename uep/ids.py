"""穩定 ID 產生（契約 §0.3、D32）：格式為 ``前綴_ULID``。"""
from __future__ import annotations

import re

from ulid import ULID

# 契約 §0.3 列出的前綴，加上實作所需的 obx（outbox）、blob、trc（trace）、rcpt（receipt）、att（attempt）、ctl（control）
PREFIXES = frozenset(
    {
        "evt", "actv", "task", "run", "step", "act", "turn", "seg", "conv",
        "lease", "xfer", "out", "dsp", "dlv", "snap", "appr", "req",
        "obx", "blob", "trc", "rcpt", "att", "ctl",
    }
)

_ID_PATTERN = re.compile(r"^([a-z]+)_([0-9A-HJKMNP-TV-Z]{26})$")


def new_id(prefix: str) -> str:
    """產生 ``<prefix>_<ULID>``；前綴必須在已登記的集合內。"""
    if prefix not in PREFIXES:
        raise ValueError(f"未登記的 ID 前綴：{prefix!r}")
    return f"{prefix}_{ULID()}"


def is_valid_id(value: str, prefix: str | None = None) -> bool:
    """檢查字串是否為合法的 ``前綴_ULID``；可指定必須的前綴。"""
    match = _ID_PATTERN.match(value or "")
    if not match or match.group(1) not in PREFIXES:
        return False
    return prefix is None or match.group(1) == prefix
