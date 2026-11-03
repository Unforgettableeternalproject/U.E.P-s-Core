"""穩定 ID 產生（契約 §0.3、D32）：格式為 ``前綴_ULID``。"""
from __future__ import annotations

import hashlib
import re

from ulid import ULID

# 契約 §0.3 列出的前綴，加上實作所需的 obx（outbox）、blob、trc（trace）、rcpt（receipt）、att（attempt）、ctl（control）、dec（決策中樞 decision）
PREFIXES = frozenset(
    {
        "evt", "actv", "task", "run", "step", "act", "turn", "seg", "conv",
        "lease", "xfer", "out", "dsp", "dlv", "snap", "appr", "req",
        "obx", "blob", "trc", "rcpt", "att", "ctl", "dec",
    }
)

_ID_PATTERN = re.compile(r"([a-z]+)_([0-9A-HJKMNP-TV-Z]{26})")


def new_id(prefix: str) -> str:
    """產生 ``<prefix>_<ULID>``；前綴必須在已登記的集合內。"""
    if prefix not in PREFIXES:
        raise ValueError(f"未登記的 ID 前綴：{prefix!r}")
    return f"{prefix}_{ULID()}"


def is_valid_id(value: str, prefix: str | None = None) -> bool:
    """檢查字串是否為合法的 ``前綴_ULID``；可指定必須的前綴。"""
    match = _ID_PATTERN.fullmatch(value or "")
    if not match or match.group(1) not in PREFIXES:
        return False
    return prefix is None or match.group(1) == prefix


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def derived_id(prefix: str, *parts: str) -> str:
    """由輸入確定性導出的 ``<prefix>_<26 字 Crockford>``（與 ``new_id`` 同格式與值域）。

    同一組輸入永遠得到同一個 id；用於「一對一且可重放」的對應（例如 turn_id -> 回合 Task／Run）。
    """
    if prefix not in PREFIXES:
        raise ValueError(f"未登記的 ID 前綴：{prefix!r}")
    digest = hashlib.sha256(("\x1f".join((prefix, *parts))).encode()).digest()
    number = int.from_bytes(digest[:16], "big")  # 128 位元：首字元 <= 7，與 ULID 同值域
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[number & 31])
        number >>= 5
    return f"{prefix}_" + "".join(reversed(chars))
