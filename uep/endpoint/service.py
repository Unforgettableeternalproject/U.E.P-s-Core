"""Core 端的端點服務：驗證端點身分後，把請求轉給 PresenceManager。

- 端點身分取自已驗證的 ``endpoint_id``，``args`` 不能覆寫；端點只能操作自己的 Lease。
- 本服務不修改 PresenceManager，僅使用其公開 API。
- ``clock`` 須與 PresenceManager 使用同一個時鐘，ttl 才會一致。
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from uep.contracts.presence import (
    AcquireResult,
    ConfirmResult,
    PresenceStatus,
    RenewResult,
)
from uep.endpoint.auth import EndpointAuthError, RequestVerifier, SignedRequest
from uep.endpoint.protocol import (
    AcquireReply,
    PresenceSnapshot,
    acquire_to_wire,
    snapshot_to_wire,
)
from uep.presence.manager import PresenceManager

logger = logging.getLogger("uep.endpoint.service")

_GRANT_LIMIT = 256
_STALE = "STALE"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EndpointService:
    def __init__(
        self, manager: PresenceManager, verifier: RequestVerifier, *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._manager = manager
        self._verifier = verifier
        self._clock = clock
        self._lock = threading.Lock()
        self._grants: OrderedDict[str, str] = OrderedDict()  # lease_id -> 被核發的 endpoint_id

    def authenticate(self, request: SignedRequest) -> str:
        return self._verifier.verify(request)

    def handle(self, request: SignedRequest) -> dict[str, Any]:
        """驗證並分派；回 ``{"ok": True, "result": ...}`` 或 ``{"ok": False, "error": 碼}``。"""
        try:
            endpoint_id = self._verifier.verify(request)
        except EndpointAuthError:
            return {"ok": False, "error": "AUTH_FAILED"}
        handler = None if request.op.startswith("_") else getattr(self, f"_op_{request.op}", None)
        if handler is None:
            return {"ok": False, "error": "UNKNOWN_OP"}
        try:
            return {"ok": True, "result": handler(endpoint_id, request.args)}
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "BAD_ARGS"}
        except Exception:
            logger.exception("端點請求處理失敗（%s）", request.op)
            return {"ok": False, "error": "INTERNAL"}

    # ---- 操作 ----

    def _ttl(self, expires_at: datetime | None) -> float | None:
        if expires_at is None:
            return None
        return max(0.0, (expires_at - self._clock()).total_seconds())

    def _owns(self, endpoint_id: str, lease_id: str) -> bool:
        view = self._manager.current()
        return view.status is PresenceStatus.ACTIVE and view.endpoint_id == endpoint_id and view.lease_id == lease_id

    def _op_acquire(self, endpoint_id: str, args: dict[str, Any]) -> dict[str, Any]:
        outcome = self._manager.acquire(endpoint_id, str(args["location"]))
        if outcome.grant is None:
            return acquire_to_wire(AcquireReply(outcome.result, reason=outcome.reason))
        with self._lock:
            self._grants[outcome.grant.lease_id] = endpoint_id
            if len(self._grants) > _GRANT_LIMIT:
                self._grants.popitem(last=False)
        return acquire_to_wire(AcquireReply(
            AcquireResult.GRANTED, outcome.grant.authorization, self._ttl(outcome.grant.expires_at)
        ))

    def _op_confirm_activated(self, endpoint_id: str, args: dict[str, Any]) -> dict[str, Any]:
        lease_id = str(args["lease_id"])
        with self._lock:
            granted_to = self._grants.get(lease_id)
        if granted_to != endpoint_id:
            return {"result": _STALE, "ttl_seconds": None}  # 不是核發給此端點的 Lease
        result = self._manager.confirm_activated(lease_id, generation=int(args["generation"]))
        ttl = self._ttl(self._manager.current().expires_at) if result is ConfirmResult.ACCEPTED else None
        return {"result": result.value, "ttl_seconds": ttl}

    def _op_renew(self, endpoint_id: str, args: dict[str, Any]) -> dict[str, Any]:
        lease_id = str(args["lease_id"])
        if not self._owns(endpoint_id, lease_id):
            return {"result": _STALE, "ttl_seconds": None}
        result = self._manager.renew(
            lease_id, int(args["core_epoch"]), int(args["generation"]), int(args["fencing_epoch"])
        )
        ttl = self._ttl(self._manager.current().expires_at) if result is RenewResult.RENEWED else None
        return {"result": result.value, "ttl_seconds": ttl}

    def _op_release(self, endpoint_id: str, args: dict[str, Any]) -> dict[str, Any]:
        lease_id = str(args["lease_id"])
        if not self._owns(endpoint_id, lease_id):
            return {"result": _STALE}
        result = self._manager.release(
            lease_id, int(args["core_epoch"]), int(args["generation"]), int(args["fencing_epoch"])
        )
        return {"result": result.value}

    def _op_confirm_quiesced(self, endpoint_id: str, args: dict[str, Any]) -> dict[str, Any]:
        evidence = args.get("evidence") or {}
        if not isinstance(evidence, dict):
            raise TypeError("evidence 必須是物件")
        result = self._manager.confirm_quiesced(endpoint_id, int(args["old_generation"]), evidence)
        return {"result": result.value}

    def _op_snapshot(self, endpoint_id: str, args: dict[str, Any]) -> dict[str, Any]:
        view = self._manager.current()
        owns = view.status is PresenceStatus.ACTIVE and view.endpoint_id == endpoint_id
        snap = PresenceSnapshot(
            status=view.status.value, owns=owns, lease_id=view.lease_id if owns else None,
            generation=view.generation, core_epoch=view.core_epoch, fencing_epoch=view.fencing_epoch,
            ttl_seconds=self._ttl(view.expires_at) if owns else None,
            unquiesced=view.unquiesced_endpoint_id == endpoint_id,
        )
        return snapshot_to_wire(snap)
