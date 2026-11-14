"""確認／拒絕話語詞庫（契約 §3A.3）：整句 fullmatch（去除空白與標點），刻意保守。

- 確認與拒絕以外的任何句子（含「好但是……」「不過……」、含疑問、過長）都是 ``other``：不解決 proposal，只計入 ``max_intervening``。
- 確定性通道：``確認 <nonce>`` ／ ``confirm <nonce>``；不含正確 nonce 的此句型不算確認（由服務比對 nonce）。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Literal

Utterance = Literal["confirm", "decline", "other"]

MAX_LEN = 24  # 超過視為一般發言

CONFIRM_WORDS = frozenset({
    "是", "是的", "對", "沒錯", "好", "好的", "好啊", "可以", "確認", "確定", "嗯對",
    "yes", "yep", "ok", "okay", "confirm", "sure",
})
DECLINE_WORDS = frozenset({
    "不", "不是", "不要", "不用", "算了", "取消", "先不要", "no", "nope", "cancel", "nevermind",
})

_QUESTION_MARK = re.compile(r"[?？]")
_NONCE_ZH = re.compile(r"^確認\s*([a-z0-9]{8,32})$")
_NONCE_EN = re.compile(r"^confirm\s+([a-z0-9]{8,32})$")


def strip_noise(text: str) -> str:
    """去除空白與標點／符號（與歸級詞庫同一規則）。"""
    return "".join(ch for ch in text if not ch.isspace() and unicodedata.category(ch)[0] not in ("P", "S"))


def _normalize(text: str) -> str:
    return strip_noise(text).lower()


def parse_nonce(text: object) -> str | None:
    """確定性通道：``確認 <nonce>``／``confirm <nonce>`` 回 nonce；不符句型回 None。"""
    if not isinstance(text, str):
        return None
    stripped = text.strip().lower()
    if not stripped or len(stripped) > MAX_LEN + 24 or _QUESTION_MARK.search(stripped):
        return None
    match = _NONCE_ZH.fullmatch(stripped) or _NONCE_EN.fullmatch(stripped)
    return match.group(1) if match else None


def classify(text: object) -> Utterance:
    """口語分類：整句 fullmatch；其餘一律 other。"""
    if not isinstance(text, str):
        return "other"
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_LEN or _QUESTION_MARK.search(stripped):
        return "other"
    norm = _normalize(stripped)
    if norm in CONFIRM_WORDS:
        return "confirm"
    if norm in DECLINE_WORDS:
        return "decline"
    return "other"
