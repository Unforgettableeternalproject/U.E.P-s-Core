"""ES 的端點：沿用 ``CliEndpoint`` 的 Lease 生命週期（acquire／續約／watchdog／控制訊號），改成不自己播放。

ES 的 sink 才是真的出聲處：``send`` 只做端點的二次接納（P-14）並回報 ``transport_sent``（O-16：send 在
sent 時返回，之後的播放證據由 sink 包裝以 ``DeliveryManager.report_evidence`` 回報）。

``admit_output`` 是 sink 包裝每個 chunk 前呼叫的本地接納：只讀記憶體（lease、stopped_turn、本地 TTL），
**不碰 DB**，可在 ES loop 上同步呼叫。Lease 被撤銷、續約失敗、watchdog 到期或收到 stop 控制訊號時，
經 ``on_stop`` 通知宿主（在端點的本地執行緒上、鎖外呼叫；宿主只能設旗標／排程，不能阻塞）。
"""
from __future__ import annotations

import logging
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from uep.adapters.output import SendResult
from uep.contracts.presence import AuthorizationTuple, GateDecision, ReleaseResult
from uep.endpoint.endpoint import CliEndpoint, EndpointRejected, LocalLease, _Dropped
from uep.endpoint.protocol import (
    AcquireReply,
    ConfirmReply,
    CoreApi,
    PresenceSnapshot,
    RenewReply,
)
from uep.output.delivery import DeliveryEvidence
from uep.output.intent import ControlKind, ControlScope, ControlSignal, OutputIntent

logger = logging.getLogger("uep.echo.endpoint")

_RESULT_LIMIT = 1024
REASON_PRESENCE_LOST = "presence_lost"
REASON_GLOBAL_STOP = "global_stop"
REASON_CONTROL_STOP = "control_stop"


@dataclass(frozen=True)
class StopNotice:
    """端點要求停止輸出的通知。``turn_generation`` 只有 scope=turn 的 stop 才有。"""

    reason: str
    turn_generation: int | None = None


@dataclass(frozen=True)
class _Probe:
    """``CliEndpoint._admit`` 只讀這幾個欄位；用輕量物件避免每個 chunk 建一個 OutputIntent。"""

    persona_id: str
    endpoint_id: str
    lease_id: str
    fencing_epoch: int
    core_epoch: int
    presence_generation: int
    turn_generation: int | None
    valid_until: datetime


class DeferredCoreApi:
    """組裝期先建端點、之後才有 PresenceManager：實際的 ``CoreApi`` 在 ``attach`` 時才接上。"""

    def __init__(self) -> None:
        self._target: CoreApi | None = None

    def attach(self, target: CoreApi) -> None:
        self._target = target

    def _api(self) -> CoreApi:
        if self._target is None:
            raise ConnectionError("CoreApi 尚未接上（EchoPipelineHost 尚未 bind）")
        return self._target

    def acquire(self, location: str) -> AcquireReply:
        return self._api().acquire(location)

    def confirm_activated(self, lease_id: str, generation: int) -> ConfirmReply:
        return self._api().confirm_activated(lease_id, generation)

    def renew(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> RenewReply:
        return self._api().renew(lease_id, core_epoch, generation, fencing_epoch)

    def release(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> ReleaseResult:
        return self._api().release(lease_id, core_epoch, generation, fencing_epoch)

    def confirm_quiesced(self, old_generation: int, evidence: Any) -> Any:
        return self._api().confirm_quiesced(old_generation, evidence)

    def snapshot(self) -> PresenceSnapshot:
        return self._api().snapshot()


class EchoEndpoint(CliEndpoint):
    def __init__(
        self, endpoint_id: str, core: CoreApi, *, on_stop: Callable[[StopNotice], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(endpoint_id, core, **kwargs)
        self._on_stop = on_stop
        self._notices: deque[StopNotice] = deque()
        self._sent: OrderedDict[str, SendResult] = OrderedDict()

    def set_core(self, core: CoreApi) -> None:
        self._core = core

    def set_on_stop(self, on_stop: Callable[[StopNotice], None] | None) -> None:
        self._on_stop = on_stop

    # ---- 觀測 ----

    def lease_auth(self) -> AuthorizationTuple | None:
        lease: LocalLease | None = self.lease
        return lease.auth if lease is not None else None

    # ---- 二次接納（本地、不碰 DB）----

    def admit_output(
        self, auth: AuthorizationTuple, turn_generation: int | None, valid_until: datetime
    ) -> GateDecision:
        probe = _Probe(
            auth.persona_id, auth.endpoint_id, auth.lease_id, auth.fencing_epoch, auth.core_epoch,
            auth.presence_generation, turn_generation, valid_until,
        )
        with self._lock:
            return self._admit(probe)  # type: ignore[arg-type]

    # ---- OutputAdapter：送入即返回，播放證據之後由 sink 回報（O-16）----

    def send(self, intent: OutputIntent, dispatch_id: str) -> SendResult:
        with self._lock:
            previous = self._sent.get(dispatch_id)
            if previous is not None:
                return previous  # 同一 dispatch_id 不重複效果
            decision = self._admit(intent)
            if decision is not GateDecision.ADMIT:
                raise EndpointRejected(decision)
            result = SendResult(dispatch_id, DeliveryEvidence.TRANSPORT_SENT, self._wall())
            self._sent[dispatch_id] = result
            self._results[dispatch_id] = result  # lookup 同源
            while len(self._sent) > _RESULT_LIMIT:
                self._sent.popitem(last=False)
        return result

    # ---- 停止通知 ----

    def _drop_locked(self, reason: str, code: GateDecision) -> _Dropped | None:
        dropped = super()._drop_locked(reason, code)
        if dropped is not None:
            self._notices.append(StopNotice(REASON_PRESENCE_LOST))
        return dropped

    def _cancel_for(self, signal: ControlSignal) -> None:
        super()._cancel_for(signal)
        if signal.kind not in (ControlKind.STOP, ControlKind.ABORT):
            return
        if signal.scope is ControlScope.TURN:
            self._notices.append(StopNotice(REASON_CONTROL_STOP, signal.target_fence.get("turn_generation")))
        else:
            self._notices.append(StopNotice(REASON_GLOBAL_STOP))

    def _fire_notices(self) -> None:
        with self._lock:
            notices = list(self._notices)
            self._notices.clear()
        callback = self._on_stop
        if callback is None:
            return
        for notice in notices:
            try:
                callback(notice)
            except Exception:
                logger.exception("端點停止通知處理失敗")

    def local_tick(self) -> None:
        super().local_tick()
        self._fire_notices()

    def net_tick(self) -> None:
        super().net_tick()
        self._fire_notices()
