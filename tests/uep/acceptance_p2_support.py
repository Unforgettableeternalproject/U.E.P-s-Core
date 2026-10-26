"""V11／V12／V15 驗收測試共用 helper（需 PostgreSQL；由 test_acceptance_v11/v12/v15.py 匯入）。

端點以 ``CliEndpoint`` 模擬（Desktop／Discord／Website 僅 endpoint_id 與 location 不同）。
兩種傳輸：
- in-process：``RpcCoreApi`` 簽章後直接呼叫 ``EndpointService.handle``，可逐端點注入斷線（``down``）。
- 真實 loopback socket：``SocketControlServer``／``SocketControlClient``（含 HMAC 與控制推送）。
時間一律用可注入時鐘（``FakeClock``／``Mono``），不做真時間 sleep；只有等執行緒的 ``wait_until`` 會短暫輪詢。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from tests.uep.endpoint_support import Mono
from tests.uep.presence_support import Rig, make_rig
from uep.control.channel import InProcessControlChannel
from uep.control.socket_transport import SocketControlClient, SocketControlServer
from uep.endpoint.auth import RequestVerifier, SignedRequest
from uep.endpoint.endpoint import CliEndpoint
from uep.endpoint.protocol import RpcCoreApi
from uep.endpoint.service import EndpointService
from uep.events.journal import row_to_event
from uep.events.models import EventRow
from uep.presence.gate import ALLOW_ALL, OutputGate
from uep.presence.manager import PresenceManager
from uep.presence.models import (
    PresenceCurrentRow,
    PresenceLeaseRow,
    PresenceTransferRow,
)

TTL = 30.0
HEARTBEAT = 5.0
WAIT = 3.0

# 測試專用 per-endpoint secret（非真實密鑰）
SECRETS: dict[str, bytes] = {
    "ep_desktop": b"desktop-secret-0123456789abcdef",
    "ep_discord": b"discord-secret-0123456789abcdef",
    "ep_website": b"website-secret-0123456789abcdef",
}
LOCATIONS: dict[str, str] = {"ep_desktop": "desktop", "ep_discord": "discord", "ep_website": "website"}


def wait_until(predicate, timeout: float = WAIT) -> bool:
    """短輪詢等背景執行緒（不 sleep 真時間以外的任何事）。"""
    ticker = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        ticker.wait(0.02)
    return predicate()


def settle(endpoint: CliEndpoint, timeout: float = WAIT) -> bool:
    """反覆處理已到達的控制訊號，直到端點放掉 Lease（控制訊號經 socket／信箱非同步到達）。"""

    def done() -> bool:
        endpoint.process_controls()
        return endpoint.lease is None

    return wait_until(done, timeout)


class LateControl:
    """PresenceManager 先建好、控制通道後建：以轉接物件延後綁定。"""

    def __init__(self) -> None:
        self.target: Any = None

    def send(self, signal, *, endpoint_id=None):
        return self.target.send(signal, endpoint_id=endpoint_id)


class SpyApi:
    """包住 CoreApi，記錄 ``confirm_quiesced`` 的呼叫（驗證停止證據內容）。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.quiesced: list[tuple[int, dict[str, Any]]] = []  # 成功送達 Core 的停止證據
        self.quiesce_attempts = 0  # 含失敗（隔離中）的嘗試次數
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            self.calls.append(name)
            return target(*args, **kwargs)

        return call

    def confirm_quiesced(self, old_generation, evidence):
        self.calls.append("confirm_quiesced")
        self.quiesce_attempts += 1
        result = self._inner.confirm_quiesced(old_generation, evidence)  # 隔離時在此拋例外，不記為送達
        self.quiesced.append((old_generation, dict(evidence)))
        return result


@dataclass
class World:
    rig: Rig
    service: EndpointService
    channel: InProcessControlChannel | None = None
    server: SocketControlServer | None = None
    mono: Mono = field(default_factory=Mono)
    down: dict[str, bool] = field(default_factory=dict)  # in-process 傳輸：endpoint_id -> 已隔離
    endpoints: dict[str, CliEndpoint] = field(default_factory=dict)
    clients: dict[str, SocketControlClient] = field(default_factory=dict)
    apis: dict[str, SpyApi] = field(default_factory=dict)

    def api(self, endpoint_id: str) -> SpyApi:
        return self.apis[endpoint_id]

    def endpoint(self, endpoint_id: str) -> CliEndpoint:
        return self.endpoints[endpoint_id]

    def close(self) -> None:
        for ep in self.endpoints.values():
            ep.close()
        for client in self.clients.values():
            client.close()
        if self.server is not None:
            self.server.close()


def make_world(
    session_factory, tmp_path: Path, *, endpoint_ids=("ep_desktop", "ep_discord", "ep_website"),
    socket: bool = False, attach_control: bool = True, renew_fraction: float = HEARTBEAT / TTL,
    **endpoint_kw: Any,
) -> World:
    """建立 Core（Manager／Gate／Service）與每個 endpoint_id 一個 ``CliEndpoint``。

    ``socket=True`` 走真實 loopback socket（控制訊號經 socket 推送）；否則走 in-process 傳輸與
    ``InProcessControlChannel``。``attach_control=False`` 時端點收不到 revoke（模擬漏收）。
    """
    late = LateControl()
    channel = None if socket else InProcessControlChannel()
    rig = make_rig(session_factory, tmp_path, control=late)
    service = EndpointService(rig.manager, RequestVerifier({e: SECRETS[e] for e in endpoint_ids}), clock=rig.clock)
    world = World(rig, service, channel=channel)
    if socket:
        world.server = SocketControlServer(service)
        world.server.start()
        late.target = world.server
    else:
        late.target = channel
    for endpoint_id in endpoint_ids:
        holder: dict[str, CliEndpoint] = {}
        if socket:
            assert world.server is not None
            client = SocketControlClient(
                *world.server.address, endpoint_id, SECRETS[endpoint_id],
                on_control=(lambda s, h=holder: h["ep"].on_control(s)) if attach_control else (lambda s: None),
            )
            world.clients[endpoint_id] = client
            api = SpyApi(client.api)
        else:
            world.down[endpoint_id] = False
            api = SpyApi(RpcCoreApi(endpoint_id, SECRETS[endpoint_id], _transport(world, endpoint_id)))
        world.apis[endpoint_id] = api
        ep = CliEndpoint(
            endpoint_id, api, location=LOCATIONS[endpoint_id], clock=world.mono, wall_clock=rig.clock,
            renew_fraction=renew_fraction, **endpoint_kw,
        )
        holder["ep"] = ep
        world.endpoints[endpoint_id] = ep
        if socket:
            world.clients[endpoint_id].connect()
        elif attach_control:
            assert channel is not None
            ep.attach_control(channel)
    return world


def _transport(world: World, endpoint_id: str):
    def call(wire):
        if world.down.get(endpoint_id):
            raise ConnectionError("網路隔離")
        return world.service.handle(SignedRequest.from_wire(wire))

    return call


def restart_core(world: World) -> tuple[PresenceManager, OutputGate]:
    """模擬 Core 重啟：同一 DB 與 Host epoch 檔，全新 Manager（已 start）、Gate 與 EndpointService。

    舊 Manager／Gate 保留在原 ``rig`` 內供「舊 Core 的殘存行程」斷言；in-process 傳輸改指向新 Service。
    """
    old = world.rig
    late = LateControl()
    late.target = world.channel
    manager = PresenceManager(
        old.session_factory, epoch_store=old.store, publisher=old.publisher, control=late, clock=old.clock
    )
    manager.start()
    gate = OutputGate(
        old.session_factory, manager, clock=old.clock, publisher=old.publisher,
        turn_generation_source=ALLOW_ALL, presence_audience=ALLOW_ALL,
    )
    world.service = EndpointService(manager, RequestVerifier({e: SECRETS[e] for e in world.endpoints}), clock=old.clock)
    return manager, gate


# ---- DB 觀測 ----

def live_lease_count(sf) -> int:
    """ISSUED／ACTIVE 的 Lease 數（任一時刻不得 > 1）。"""
    with sf() as s:
        return s.scalar(
            select(func.count()).select_from(PresenceLeaseRow).where(PresenceLeaseRow.state.in_(("ISSUED", "ACTIVE")))
        ) or 0


def open_transfer_count(sf) -> int:
    with sf() as s:
        return s.scalar(
            select(func.count()).select_from(PresenceTransferRow).where(
                PresenceTransferRow.phase.in_(("PREPARING", "QUIESCING", "CHECKPOINTED", "ACTIVATING"))
            )
        ) or 0


def presence_row_count(sf) -> int:
    with sf() as s:
        return s.scalar(select(func.count()).select_from(PresenceCurrentRow)) or 0


def lease_total(sf) -> int:
    with sf() as s:
        return s.scalar(select(func.count()).select_from(PresenceLeaseRow)) or 0


def invariant_snapshot(sf) -> tuple[int, int, int]:
    """單一語句（同一快照）取 (ACTIVE row 數, 存活 Lease 數, 開啟中 transfer 數)，供並行取樣。"""
    with sf() as s:
        row = s.execute(text(
            "SELECT (SELECT count(*) FROM presence_current WHERE status = 'ACTIVE'),"
            " (SELECT count(*) FROM presence_leases WHERE state IN ('ISSUED','ACTIVE')),"
            " (SELECT count(*) FROM presence_transfers WHERE phase IN ('PREPARING','QUIESCING','CHECKPOINTED','ACTIVATING'))"
        )).one()
        return int(row[0]), int(row[1]), int(row[2])


def journal_payloads(sf, event_type: str) -> list[dict[str, Any]]:
    with sf() as s:
        rows = s.scalars(select(EventRow).where(EventRow.event_type == event_type).order_by(EventRow.journal_seq))
        return [dict(row_to_event(r).payload) for r in rows]
