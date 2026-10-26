"""端點對 Core 的 API 契約（端點視角）與可序列化回覆。

``CoreApi`` 是端點唯一依賴的 Core 介面；實作有：
- ``RpcCoreApi``（本檔）：簽章後交給可注入的 transport（in-process 或 socket 控制傳輸層）。
- 測試替身。
ttl 一律以「Core 回覆當下的剩餘秒數」表示，端點自行換算成本地單調時鐘期限
（不信任跨機器的絕對時間，VC-4-4）。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from uep.contracts.presence import (
    AcquireDenyReason,
    AcquireResult,
    AuthorizationTuple,
    ConfirmResult,
    ReleaseResult,
    RenewResult,
)
from uep.endpoint.auth import EndpointAuthError, sign_request


class CoreApiError(Exception):
    """Core 回報請求錯誤（非 CC-4 結果碼）。"""


@dataclass(frozen=True)
class AcquireReply:
    result: AcquireResult
    auth: AuthorizationTuple | None = None
    ttl_seconds: float | None = None
    reason: AcquireDenyReason | None = None  # DENIED 的原因碼（T-23）


@dataclass(frozen=True)
class ConfirmReply:
    result: ConfirmResult
    ttl_seconds: float | None = None


@dataclass(frozen=True)
class RenewReply:
    result: RenewResult
    ttl_seconds: float | None = None


@dataclass(frozen=True)
class PresenceSnapshot:
    """權威快照（只有 owner 才會拿到 lease_id）。"""

    status: str
    owns: bool
    lease_id: str | None
    generation: int
    core_epoch: int
    fencing_epoch: int
    ttl_seconds: float | None
    unquiesced: bool


class CoreApi(Protocol):
    def acquire(self, location: str) -> AcquireReply: ...

    def confirm_activated(self, lease_id: str, generation: int) -> ConfirmReply: ...

    def renew(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> RenewReply: ...

    def release(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> ReleaseResult: ...

    def confirm_quiesced(self, old_generation: int, evidence: Mapping[str, Any]) -> ConfirmResult: ...

    def snapshot(self) -> PresenceSnapshot: ...


def _auth_to_wire(auth: AuthorizationTuple | None) -> dict[str, Any] | None:
    if auth is None:
        return None
    return dict(vars(auth))


def acquire_to_wire(reply: AcquireReply) -> dict[str, Any]:
    return {
        "result": reply.result.value, "auth": _auth_to_wire(reply.auth), "ttl_seconds": reply.ttl_seconds,
        "reason": reply.reason.value if reply.reason is not None else None,
    }


def acquire_from_wire(data: Mapping[str, Any]) -> AcquireReply:
    raw = data.get("auth")
    auth = AuthorizationTuple(**raw) if raw else None
    raw_reason = data.get("reason")
    try:
        reason = AcquireDenyReason(raw_reason) if raw_reason else None
    except ValueError:  # 較新的 Core 帶了未知原因碼：視為無原因，結果碼仍照處理
        reason = None
    return AcquireReply(AcquireResult(data["result"]), auth, data.get("ttl_seconds"), reason)


def snapshot_to_wire(snap: PresenceSnapshot) -> dict[str, Any]:
    return dict(vars(snap))


def snapshot_from_wire(data: Mapping[str, Any]) -> PresenceSnapshot:
    return PresenceSnapshot(**dict(data))


# transport：接收簽章後的請求（wire dict），回傳 Core 的回應 {"ok": bool, "result"|"error": ...}
RpcTransport = Callable[[dict[str, Any]], dict[str, Any]]


class RpcCoreApi:
    """以 HMAC 簽章請求、經注入的 transport 呼叫 Core；連線失敗由 transport 拋 ConnectionError。"""

    def __init__(
        self, endpoint_id: str, secret: bytes, transport: RpcTransport, *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._endpoint_id = endpoint_id
        self._secret = secret
        self._transport = transport
        self._clock = clock

    def _call(self, op: str, **args: Any) -> Any:
        request = sign_request(self._secret, self._endpoint_id, op, args, clock=self._clock)
        response = self._transport(request.to_wire())
        if response.get("ok"):
            return response["result"]
        code = str(response.get("error") or "UNKNOWN")
        if code == "AUTH_FAILED":
            raise EndpointAuthError("Core 拒絕端點身分")
        raise CoreApiError(code)

    def acquire(self, location: str) -> AcquireReply:
        return acquire_from_wire(self._call("acquire", location=location))

    def confirm_activated(self, lease_id: str, generation: int) -> ConfirmReply:
        data = self._call("confirm_activated", lease_id=lease_id, generation=generation)
        return ConfirmReply(ConfirmResult(data["result"]), data.get("ttl_seconds"))

    def renew(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> RenewReply:
        data = self._call(
            "renew", lease_id=lease_id, core_epoch=core_epoch, generation=generation,
            fencing_epoch=fencing_epoch,
        )
        return RenewReply(RenewResult(data["result"]), data.get("ttl_seconds"))

    def release(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> ReleaseResult:
        data = self._call(
            "release", lease_id=lease_id, core_epoch=core_epoch, generation=generation,
            fencing_epoch=fencing_epoch,
        )
        return ReleaseResult(data["result"])

    def confirm_quiesced(self, old_generation: int, evidence: Mapping[str, Any]) -> ConfirmResult:
        data = self._call("confirm_quiesced", old_generation=old_generation, evidence=dict(evidence))
        return ConfirmResult(data["result"])

    def snapshot(self) -> PresenceSnapshot:
        return snapshot_from_wire(self._call("snapshot"))
