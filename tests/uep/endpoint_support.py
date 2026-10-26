"""T-18 端點測試共用替身（不需 DB）。"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from tests.uep.factories import NOW, make_intent
from uep.contracts.presence import (
    AcquireResult,
    AuthorizationTuple,
    ConfirmResult,
    PresenceStatus,
    ReleaseResult,
    RenewResult,
)
from uep.endpoint.protocol import (
    AcquireReply,
    ConfirmReply,
    PresenceSnapshot,
    RenewReply,
)
from uep.ids import new_id
from uep.output.intent import ControlKind, ControlScope, ControlSignal, OutputIntent
from uep.presence.manager import AcquireOutcome, LeaseGrant, PresenceView

SECRET = b"test-secret-0123456789abcdef"
TTL = 30.0


class Mono:
    """可推進的單調時鐘。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def wall_now() -> datetime:
    return NOW


class FakeCore:
    """CoreApi 替身：可模擬斷線（down）、續約結果與停止證據回報結果。"""

    def __init__(self, endpoint_id: str = "ep_cli") -> None:
        self.endpoint_id = endpoint_id
        self.down = False
        self.lease_id = new_id("lease")
        self.generation, self.core_epoch, self.fencing_epoch = 3, 2, 4
        self.owner = False
        self.unquiesced = False
        self.renew_result = RenewResult.RENEWED
        self.confirm_result = ConfirmResult.ACCEPTED
        self.calls: list[str] = []
        self.quiesced: list[tuple[int, dict[str, Any]]] = []
        self.released: list[str] = []

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.down:
            raise ConnectionError("Core 不可達")

    def auth(self) -> AuthorizationTuple:
        return AuthorizationTuple(
            "uep", self.endpoint_id, self.lease_id, self.fencing_epoch, self.core_epoch, self.generation
        )

    def acquire(self, location: str) -> AcquireReply:
        self._check("acquire")
        return AcquireReply(AcquireResult.GRANTED, self.auth(), TTL)

    def confirm_activated(self, lease_id: str, generation: int) -> ConfirmReply:
        self._check("confirm_activated")
        self.owner = True
        return ConfirmReply(ConfirmResult.ACCEPTED, TTL)

    def renew(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> RenewReply:
        self._check("renew")
        return RenewReply(self.renew_result, TTL if self.renew_result is RenewResult.RENEWED else None)

    def release(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> ReleaseResult:
        self._check("release")
        self.released.append(lease_id)
        self.owner = False
        return ReleaseResult.RELEASED

    def confirm_quiesced(self, old_generation: int, evidence: Mapping[str, Any]) -> ConfirmResult:
        self._check("confirm_quiesced")
        self.quiesced.append((old_generation, dict(evidence)))
        return self.confirm_result

    def snapshot(self) -> PresenceSnapshot:
        self._check("snapshot")
        return PresenceSnapshot(
            status="ACTIVE" if self.owner else "NONE", owns=self.owner,
            lease_id=self.lease_id if self.owner else None, generation=self.generation,
            core_epoch=self.core_epoch, fencing_epoch=self.fencing_epoch,
            ttl_seconds=TTL if self.owner else None, unquiesced=self.unquiesced,
        )


def intent_for_auth(auth: AuthorizationTuple, **overrides: Any) -> OutputIntent:
    data: dict[str, Any] = {
        "persona_id": auth.persona_id, "endpoint_id": auth.endpoint_id, "lease_id": auth.lease_id,
        "fencing_epoch": auth.fencing_epoch, "core_epoch": auth.core_epoch,
        "presence_generation": auth.presence_generation,
    }
    data.update(overrides)
    return make_intent(**data)


def signal(
    kind: ControlKind = ControlKind.REVOKE, *, generation: int | None = 3, core_epoch: int = 2,
    scope: ControlScope = ControlScope.PROCESS, control_id: str | None = None, **fence: int,
) -> ControlSignal:
    target = {"core_epoch": core_epoch, **fence}
    if generation is not None:
        target["presence_generation"] = generation
    return ControlSignal(
        control_id=control_id or new_id("ctl"), kind=kind, scope=scope, target_fence=target,
        issued_at=datetime.now(UTC),
    )


class StubManager:
    """PresenceManager 的最小替身（只實作 EndpointService 用到的公開方法）。"""

    def __init__(self, persona_id: str = "uep") -> None:
        self.persona_id = persona_id
        self.generation = 0
        self.lease_id: str | None = None
        self.endpoint_id: str | None = None
        self.status = PresenceStatus.NONE
        self.expires_at: datetime | None = None
        self.quiesced: list[tuple[str, int, dict[str, Any]]] = []
        self.renews = 0

    def current(self) -> PresenceView:
        return PresenceView(
            persona_id=self.persona_id, status=self.status, location="desktop", endpoint_id=self.endpoint_id,
            lease_id=self.lease_id, core_epoch=2, fencing_epoch=4, generation=self.generation,
            expires_at=self.expires_at, row_version=1, transfer_id=None, unquiesced_endpoint_id=None,
            recovery_reason=None,
        )

    def acquire(self, endpoint_id: str, location: str = "desktop") -> AcquireOutcome:
        self.generation += 1
        self.lease_id = new_id("lease")
        self._pending_endpoint = endpoint_id
        expires = datetime.now(UTC) + timedelta(seconds=TTL)
        auth = AuthorizationTuple(self.persona_id, endpoint_id, self.lease_id, 4, 2, self.generation)
        return AcquireOutcome(AcquireResult.GRANTED, LeaseGrant(self.lease_id, new_id("xfer"), expires, auth), 1)

    def confirm_activated(self, lease_id: str, *, generation: int | None = None) -> ConfirmResult:
        if lease_id != self.lease_id:
            return ConfirmResult.STALE
        self.status, self.endpoint_id = PresenceStatus.ACTIVE, self._pending_endpoint
        self.expires_at = datetime.now(UTC) + timedelta(seconds=TTL)
        return ConfirmResult.ACCEPTED

    def renew(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> RenewResult:
        self.renews += 1
        self.expires_at = datetime.now(UTC) + timedelta(seconds=TTL)
        return RenewResult.RENEWED

    def release(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> ReleaseResult:
        self.status, self.endpoint_id, self.lease_id = PresenceStatus.NONE, None, None
        return ReleaseResult.RELEASED

    def confirm_quiesced(self, endpoint_id: str, old_generation: int, evidence: Mapping[str, Any]) -> ConfirmResult:
        self.quiesced.append((endpoint_id, old_generation, dict(evidence)))
        return ConfirmResult.ACCEPTED
