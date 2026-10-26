"""端點身分驗證（R2）：端點對 Core 的每個請求以 per-endpoint secret 做 HMAC-SHA256 簽章。

- 簽章涵蓋 endpoint_id／op／args／ts／nonce；Core 以同一份 secret 重算並以 ``compare_digest`` 比對。
- ``ts`` 超出容許時間差、或 ``(endpoint_id, nonce)`` 重複皆拒絕（防重放）。
- 密鑰不硬編碼：由環境變數載入（``UEP_ENDPOINT_SECRET`` 端點端、``UEP_ENDPOINT_SECRETS`` Core 端，
  格式 ``endpoint_id:secret,endpoint_id:secret``）。
- 本層只證明「請求來自持有 secret 的端點」；回應不簽章（localhost 信任邊界，見 socket_transport）。
"""
from __future__ import annotations

import hashlib
import heapq
import hmac
import json
import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from uep.ids import new_id

SECRET_ENV = "UEP_ENDPOINT_SECRET"
SECRETS_ENV = "UEP_ENDPOINT_SECRETS"
MIN_SECRET_LEN = 16
DEFAULT_SKEW_SECONDS = 30.0


class EndpointAuthError(Exception):
    """身分驗證失敗（簽章錯誤、未知端點、過期或重放）。"""


@dataclass(frozen=True)
class SignedRequest:
    endpoint_id: str
    op: str
    args: dict[str, Any]
    ts: float
    nonce: str
    mac: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id, "op": self.op, "args": self.args,
            "ts": self.ts, "nonce": self.nonce, "mac": self.mac,
        }

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> SignedRequest:
        try:
            args = data["args"]
            if not isinstance(args, dict):
                raise TypeError("args 必須是物件")
            return cls(
                str(data["endpoint_id"]), str(data["op"]), args,
                float(data["ts"]), str(data["nonce"]), str(data["mac"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EndpointAuthError("請求格式不合法") from exc


def _canonical(endpoint_id: str, op: str, args: Mapping[str, Any], ts: float, nonce: str) -> bytes:
    body = {"e": endpoint_id, "o": op, "a": args, "t": repr(float(ts)), "n": nonce}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _check_secret(secret: bytes) -> bytes:
    if len(secret) < MIN_SECRET_LEN:
        raise ValueError(f"端點 secret 至少 {MIN_SECRET_LEN} 個位元組")
    return secret


def sign_request(
    secret: bytes, endpoint_id: str, op: str, args: Mapping[str, Any], *,
    clock: Callable[[], float] = time.time,
) -> SignedRequest:
    _check_secret(secret)
    ts, nonce = clock(), new_id("req")
    clean = dict(args)
    mac = hmac.new(secret, _canonical(endpoint_id, op, clean, ts, nonce), hashlib.sha256).hexdigest()
    return SignedRequest(endpoint_id, op, clean, ts, nonce, mac)


class RequestVerifier:
    """Core 端驗證器；執行緒安全。``secrets`` 為 endpoint_id -> secret（bytes）。"""

    def __init__(
        self, secrets: Mapping[str, bytes], *, clock: Callable[[], float] = time.time,
        skew_seconds: float = DEFAULT_SKEW_SECONDS,
    ) -> None:
        self._secrets = {k: _check_secret(v) for k, v in secrets.items()}
        self._clock = clock
        self._skew = skew_seconds
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str], float] = {}
        self._expiry: list[tuple[float, tuple[str, str]]] = []  # (ts, key) 小頂堆，依時間淘汰

    def verify(self, req: SignedRequest) -> str:
        """通過回傳已驗證的 endpoint_id；否則拋 EndpointAuthError（訊息不洩漏哪一項不符）。"""
        secret = self._secrets.get(req.endpoint_id)
        expected = (
            hmac.new(secret, _canonical(req.endpoint_id, req.op, req.args, req.ts, req.nonce), hashlib.sha256)
            .hexdigest() if secret is not None else ""
        )
        # 以 bytes 比對：非 ASCII 的 mac 不會讓 compare_digest 拋 TypeError
        if secret is None or not hmac.compare_digest(expected.encode("ascii"), req.mac.encode("utf-8")):
            raise EndpointAuthError("身分驗證失敗")
        now = self._clock()
        if not math.isfinite(req.ts) or abs(now - req.ts) > self._skew:
            raise EndpointAuthError("請求時間超出容許範圍")
        key = (req.endpoint_id, req.nonce)
        with self._lock:
            # ts 早於 now - skew 的請求必被時間檢查拒絕，其 nonce 不需再保留
            while self._expiry and self._expiry[0][0] < now - self._skew:
                _ts, old = heapq.heappop(self._expiry)
                self._seen.pop(old, None)
            if key in self._seen:
                raise EndpointAuthError("請求重複（nonce 已使用）")
            self._seen[key] = req.ts
            heapq.heappush(self._expiry, (req.ts, key))
        return req.endpoint_id


def load_secret_from_env(var: str = SECRET_ENV) -> bytes:
    value = os.environ.get(var)
    if not value:
        raise EndpointAuthError(f"未設定環境變數 {var}")
    return _check_secret(value.encode("utf-8"))


def load_secrets_from_env(var: str = SECRETS_ENV) -> dict[str, bytes]:
    raw = os.environ.get(var, "")
    secrets: dict[str, bytes] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        endpoint_id, sep, secret = item.partition(":")
        if not sep or not endpoint_id or not secret:
            raise EndpointAuthError(f"{var} 格式應為 endpoint_id:secret,...")
        secrets[endpoint_id] = _check_secret(secret.encode("utf-8"))
    return secrets
