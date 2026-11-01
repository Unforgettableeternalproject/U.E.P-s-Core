"""payload_hash 的 per-install 密鑰 HMAC（D42、E-16a）。

無鹽 sha256 讓低熵短文在 blob 刪除後仍可由字典猜測驗證；改為 HMAC-SHA256，密鑰只在本機設定
（``UEP_PAYLOAD_HASH_KEY``）。演算法標記以字串前綴表示，不另開欄位：
- ``hmac-sha256:<kid>:<hex>``：新資料（含 ``payload_blobs.content_hash`` 與 ``events.payload_hash``）。
  ``kid`` = HMAC(key, b"uep-payload-kid") 的前 8 位十六進位，只用來辨識「用哪把密鑰簽的」，不洩漏密鑰；
  金鑰輪替後可分辨「內容真的不同」與「無法驗證」。
- ``hmac-sha256:<hex>``：T-24 之前無 kid 的 HMAC，只能驗證、不再新寫；驗不過時無法判斷是內容不同或金鑰已換。
- 無前綴的 64 位十六進位：T-23 之前寫入的 sha256，**只能驗證、不再新寫**。
密鑰遺失即無法驗證 HMAC 雜湊（A16 接受此代價）；寫入私人 payload 時沒有密鑰則拒絕，不降級成 sha256。
"""
from __future__ import annotations

import enum
import hashlib
import hmac
from dataclasses import dataclass

from pydantic import SecretStr

from uep.config import get_settings

HMAC_PREFIX = "hmac-sha256:"
MIN_KEY_BYTES = 16
KID_LABEL = b"uep-payload-kid"
KID_HEX_LEN = 8


class PayloadHashKeyMissing(RuntimeError):
    """未設定（或過短的）payload_hash 密鑰；拒絕寫入私人 payload。"""


class VerifyOutcome(enum.Enum):
    """驗證結果三態：金鑰輪替時「無法驗證」不等於「內容不符」。"""

    MATCH = "match"
    MISMATCH = "mismatch"  # 可確定內容不同（舊 sha256，或同一把密鑰 kid 相同卻驗不過）
    UNVERIFIABLE = "unverifiable"  # kid 不同，或舊無 kid 的 HMAC 驗不過：可能只是密鑰已換


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
        raw = self.key.get_secret_value()
        if not raw.strip() or len(raw.encode()) < MIN_KEY_BYTES:  # 全空白視同缺失
            raise PayloadHashKeyMissing(f"payload_hash 密鑰至少需 {MIN_KEY_BYTES} 位元組")

    def key_id(self) -> str:
        """密鑰識別碼（HMAC 衍生，不可回推密鑰）。"""
        return hmac.new(self.key.get_secret_value().encode(), KID_LABEL, hashlib.sha256).hexdigest()[:KID_HEX_LEN]

    def _digest(self, data: bytes) -> str:
        return hmac.new(self.key.get_secret_value().encode(), data, hashlib.sha256).hexdigest()

    def hash(self, data: bytes) -> str:
        return f"{HMAC_PREFIX}{self.key_id()}:{self._digest(data)}"

    def check(self, data: bytes, stored: str) -> VerifyOutcome:
        """依已存雜湊的格式驗證並區分三態；比對為常數時間。"""
        if not stored.startswith(HMAC_PREFIX):
            ok = hmac.compare_digest(sha256_hex(data), stored)
            return VerifyOutcome.MATCH if ok else VerifyOutcome.MISMATCH
        body = stored[len(HMAC_PREFIX):]
        kid, sep, digest = body.partition(":")
        if not sep:  # 舊無 kid 的 HMAC
            ok = hmac.compare_digest(self._digest(data), body)
            return VerifyOutcome.MATCH if ok else VerifyOutcome.UNVERIFIABLE
        if hmac.compare_digest(kid, self.key_id()):
            ok = hmac.compare_digest(self._digest(data), digest)
            return VerifyOutcome.MATCH if ok else VerifyOutcome.MISMATCH
        return VerifyOutcome.UNVERIFIABLE

    def verify(self, data: bytes, stored: str) -> bool:
        """是否確定相符（舊 sha256、舊無 kid HMAC、新含 kid 三種格式皆可）。"""
        return self.check(data, stored) is VerifyOutcome.MATCH


def resolve_hasher(hasher: PayloadHasher | None = None) -> PayloadHasher:
    """取得雜湊器：明確傳入優先，否則讀設定；沒有密鑰一律拒絕（fail-closed）。"""
    if hasher is not None:
        return hasher
    key = get_settings().payload_hash_key
    if key is None or not key.get_secret_value().strip():
        raise PayloadHashKeyMissing("未設定 UEP_PAYLOAD_HASH_KEY，拒絕寫入私人 payload")
    return PayloadHasher(key)
