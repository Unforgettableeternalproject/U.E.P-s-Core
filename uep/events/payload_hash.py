"""payload_hash 的 per-install 密鑰 HMAC（D42、E-16a）。

無鹽 sha256 讓低熵短文在 blob 刪除後仍可由字典猜測驗證；改為 HMAC-SHA256，密鑰只在本機設定
（``UEP_PAYLOAD_HASH_KEY``）。演算法標記以字串前綴表示，不另開欄位：
- ``hmac-sha256:<hex>``：新資料（含 ``payload_blobs.content_hash`` 與 ``events.payload_hash``）。
- 無前綴的 64 位十六進位：T-23 之前寫入的 sha256，**只能驗證、不再新寫**。
密鑰遺失即無法驗證 HMAC 雜湊（A16 接受此代價）；寫入私人 payload 時沒有密鑰則拒絕，不降級成 sha256。
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from pydantic import SecretStr

from uep.config import get_settings

HMAC_PREFIX = "hmac-sha256:"
MIN_KEY_BYTES = 16


class PayloadHashKeyMissing(RuntimeError):
    """未設定（或過短的）payload_hash 密鑰；拒絕寫入私人 payload。"""


def sha256_hex(data: bytes) -> str:
    """舊版無鹽 sha256；只用於相容驗證歷史資料，不再用於新寫入。"""
    return hashlib.sha256(data).hexdigest()


def algorithm_of(stored: str) -> str:
    """回傳已存雜湊的演算法標記：``hmac-sha256`` 或 ``sha256``（舊資料）。"""
    return "hmac-sha256" if stored.startswith(HMAC_PREFIX) else "sha256"


@dataclass(frozen=True)
class PayloadHasher:
    key: SecretStr

    def __post_init__(self) -> None:
        if len(self.key.get_secret_value().encode()) < MIN_KEY_BYTES:
            raise PayloadHashKeyMissing(f"payload_hash 密鑰至少需 {MIN_KEY_BYTES} 位元組")

    def hash(self, data: bytes) -> str:
        digest = hmac.new(self.key.get_secret_value().encode(), data, hashlib.sha256).hexdigest()
        return HMAC_PREFIX + digest

    def verify(self, data: bytes, stored: str) -> bool:
        """依已存雜湊的演算法驗證：HMAC 用密鑰、舊資料用 sha256；比對為常數時間。"""
        if stored.startswith(HMAC_PREFIX):
            return hmac.compare_digest(self.hash(data), stored)
        return hmac.compare_digest(sha256_hex(data), stored)


def resolve_hasher(hasher: PayloadHasher | None = None) -> PayloadHasher:
    """取得雜湊器：明確傳入優先，否則讀設定；沒有密鑰一律拒絕（fail-closed）。"""
    if hasher is not None:
        return hasher
    key = get_settings().payload_hash_key
    if key is None or not key.get_secret_value():
        raise PayloadHashKeyMissing("未設定 UEP_PAYLOAD_HASH_KEY，拒絕寫入私人 payload")
    return PayloadHasher(key)
