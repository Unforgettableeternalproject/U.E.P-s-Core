"""端點與真實 PresenceManager／Output Gate／DeliveryManager 的整合（需 PostgreSQL；未設時 skip）。"""
from __future__ import annotations

import threading

import pytest

from tests.uep.endpoint_support import SECRET, Mono
from tests.uep.presence_support import intent_for, make_rig
from uep.contracts.presence import AcquireResult, GateDecision, PresenceStatus
from uep.control.socket_transport import SocketControlClient, SocketControlServer
from uep.endpoint.auth import RequestVerifier, SignedRequest
from uep.endpoint.endpoint import CliEndpoint
from uep.endpoint.protocol import RpcCoreApi
from uep.endpoint.service import EndpointService
from uep.output.delivery import DeliveryStatus as S
from uep.output.manager import DeliveryManager

WAIT = 3.0
TTL = 30.0


class LateControl:
    """PresenceManager 先建好、傳輸層後建：以轉接物件延後綁定。"""

    def __init__(self) -> None:
        self.target = None

    def send(self, signal, *, endpoint_id=None):
        return self.target.send(signal, endpoint_id=endpoint_id)


def _wait_until(predicate, timeout: float = WAIT) -> bool:
    ticker = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        ticker.wait(0.02)
    return predicate()


@pytest.fixture
def socket_world(session_factory, tmp_path):
    late = LateControl()
    rig = make_rig(session_factory, tmp_path, control=late)
    service = EndpointService(rig.manager, RequestVerifier({"ep_cli": SECRET}), clock=rig.clock)
    server = SocketControlServer(service)
    server.start()
    late.target = server
    holder: dict = {}
    client = SocketControlClient(*server.address, "ep_cli", SECRET, on_control=lambda s: holder["ep"].on_control(s))
    mono = Mono()
    ep = CliEndpoint("ep_cli", client.api, clock=mono, wall_clock=rig.clock)
    holder["ep"] = ep
    client.connect()
    yield rig, ep, mono, session_factory
    ep.close()
    client.close()
    server.close()


def test_full_chain_gate_to_endpoint_playback(socket_world):
    rig, ep, _mono, sf = socket_world
    assert ep.start_lease() is AcquireResult.GRANTED
    auth = rig.manager.authorization()
    assert auth is not None and auth == ep.lease.auth  # 端點本地授權與 Core 權威一致
    dm = DeliveryManager(sf, ep, gate=rig.gate)
    record = dm.dispatch(intent_for(auth))
    assert record.status is S.RENDERED_PLAYED


def test_revoke_over_control_socket_stops_endpoint_and_gate_blocks_later_output(socket_world):
    rig, ep, _mono, sf = socket_world
    ep.start_lease()
    ep.start()
    auth = rig.manager.authorization()
    assert auth is not None
    rig.manager.revoke("global_stop")  # Core 經獨立控制連線通知端點
    assert _wait_until(lambda: ep.lease is None and ep.pending_report_count == 0)
    assert rig.manager.current().status is PresenceStatus.NONE
    adapter_calls = []
    original = ep.send
    ep.send = lambda i, d: adapter_calls.append(d) or original(i, d)  # type: ignore[method-assign]
    record = DeliveryManager(sf, ep, gate=rig.gate).dispatch(intent_for(auth))
    assert record.status is S.REJECTED and record.failure["code"] == GateDecision.REJECT_REVOKED.value
    assert adapter_calls == []  # 被 Gate 擋下，根本沒到端點


@pytest.fixture
def isolated_world(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    service = EndpointService(rig.manager, RequestVerifier({"ep_cli": SECRET, "ep_b": SECRET}), clock=rig.clock)
    down = {"v": False}

    def transport(endpoint_id):
        def call(wire):
            if down["v"] and endpoint_id == "ep_cli":  # 只有 CLI 端點被隔離
                raise ConnectionError("網路隔離")
            return service.handle(SignedRequest.from_wire(wire))
        return call

    mono = Mono()
    ep = CliEndpoint(
        "ep_cli", RpcCoreApi("ep_cli", SECRET, transport("ep_cli")), clock=mono, wall_clock=rig.clock
    )
    other = RpcCoreApi("ep_b", SECRET, transport("ep_b"))
    return rig, ep, other, mono, down


def test_isolated_endpoint_stops_by_itself_then_confirm_quiesced_unblocks_new_endpoint(isolated_world):
    rig, ep, other, mono, down = isolated_world
    assert ep.start_lease() is AcquireResult.GRANTED
    old_generation = ep.lease.auth.presence_generation
    down["v"] = True  # 網路隔離：revoke 與續約都到不了
    mono.advance(TTL + 1)
    rig.clock.advance(TTL + 1)
    ep.tick()
    assert ep.lease is None and ep.pending_report_count == 1  # 端點自行停止，證據待回報
    assert rig.manager.expire_due() is True
    assert other.acquire("discord").result is AcquireResult.NOT_QUIESCED  # 舊端未證實停止：新端不可啟用
    down["v"] = False
    mono.advance(2)  # 越過證據回報的退避
    ep.tick()  # 恢復連線：confirm_quiesced(endpoint_id, old_generation, evidence)
    assert ep.pending_report_count == 0
    view = rig.manager.current()
    assert view.status is PresenceStatus.NONE and view.unquiesced_endpoint_id is None
    assert view.generation == old_generation
    assert other.acquire("discord").result is AcquireResult.GRANTED


def test_local_stop_before_core_expiry_falls_back_to_release(isolated_world):
    rig, ep, other, mono, _down = isolated_world
    ep.start_lease()
    mono.advance(TTL + 1)  # 本地期限已到（偏保守），Core 時鐘尚未到期
    rig.clock.advance(TTL - 5)
    ep.tick()
    assert ep.lease is None and ep.pending_report_count == 0
    assert rig.manager.current().status is PresenceStatus.NONE  # 以 release 結束，不留未證實標記
    assert other.acquire("discord").result is AcquireResult.GRANTED
