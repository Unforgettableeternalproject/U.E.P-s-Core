"""跨行程控制傳輸層（localhost socket，與資料通道分開；不需 DB）。"""
from __future__ import annotations

import queue
import socket
import threading

import pytest

from tests.uep.endpoint_support import (
    SECRET,
    Mono,
    StubManager,
    intent_for_auth,
    signal,
    wall_now,
)
from uep.contracts.presence import AcquireResult
from uep.control.socket_transport import SocketControlClient, SocketControlServer
from uep.endpoint.auth import EndpointAuthError, RequestVerifier
from uep.endpoint.endpoint import CliEndpoint
from uep.endpoint.service import EndpointService
from uep.output.intent import ControlKind

WAIT = 3.0
OTHER = b"other-secret-0123456789abcdef"


@pytest.fixture
def server():
    manager = StubManager()
    service = EndpointService(manager, RequestVerifier({"ep_cli": SECRET}))  # type: ignore[arg-type]
    srv = SocketControlServer(service)
    srv.start()
    srv.manager = manager  # type: ignore[attr-defined]
    yield srv
    srv.close()


def _client(server, secret=SECRET, endpoint_id="ep_cli", on_control=None):
    host, port = server.address
    q: queue.Queue = queue.Queue()
    client = SocketControlClient(host, port, endpoint_id, secret, on_control=on_control or q.put)
    return client, q


def test_server_refuses_non_loopback_binding():
    with pytest.raises(ValueError):
        SocketControlServer(None, host="0.0.0.0")  # type: ignore[arg-type]


def test_wrong_secret_is_rejected_at_handshake(server):
    client, _q = _client(server, secret=OTHER)
    with pytest.raises(EndpointAuthError):
        client.connect()
    assert not client.connected and not server.connected("ep_cli")


def test_unknown_endpoint_is_rejected_at_handshake(server):
    client, _q = _client(server, endpoint_id="ep_evil")
    with pytest.raises(EndpointAuthError):
        client.connect()


def test_unauthenticated_raw_connection_gets_no_signals(server):
    host, port = server.address
    with socket.create_connection((host, port), timeout=2) as raw:
        raw.sendall(b'{"type":"hello","req":{"endpoint_id":"ep_cli","op":"hello","args":{},"ts":1,"nonce":"n","mac":"x"}}\n')
        assert b"AUTH_FAILED" in raw.recv(4096)
    assert not server.connected("ep_cli")
    result = server.send(signal(ControlKind.REVOKE))
    assert result.delivered_to == ()


def test_rpc_roundtrip_over_socket(server):
    client, _q = _client(server)
    client.connect()
    try:
        reply = client.api.acquire("desktop")
        assert reply.result is AcquireResult.GRANTED and reply.auth is not None
        assert client.api.confirm_activated(reply.auth.lease_id, reply.auth.presence_generation).ttl_seconds
        assert client.api.snapshot().owns
    finally:
        client.close()


def test_control_signal_is_pushed_to_authenticated_endpoint_only(server):
    client, q = _client(server)
    client.connect()
    try:
        sig = signal(ControlKind.REVOKE)
        assert server.send(sig, endpoint_id="ep_cli").delivered_to == ("ep_cli",)
        got = q.get(timeout=WAIT)
        assert got.control_id == sig.control_id and got.kind is ControlKind.REVOKE
        assert server.send(sig, endpoint_id="ep_other").delivered_to == ()
    finally:
        client.close()


def test_control_is_independent_of_blocked_data_path(server):
    """資料通道（send）卡住時，控制通道仍能立即讓端點停止。"""
    gate = threading.Event()
    stopped = threading.Event()
    ep_holder = {}
    client = SocketControlClient(
        *server.address, "ep_cli", SECRET, on_control=lambda s: ep_holder["ep"].on_control(s)
    )
    clock = Mono()

    def on_chunk(_intent, chunk):
        if chunk == 0:
            gate.set()  # 資料面進入播放；等控制面把它停掉
            assert stopped.wait(WAIT)

    ep = CliEndpoint(
        "ep_cli", client.api, clock=clock, wall_clock=wall_now, on_chunk=on_chunk,
        chunk_count=lambda _i: 3, sink=lambda t: stopped.set() if "已停止" in t else None,
    )
    ep_holder["ep"] = ep
    client.connect()
    ep.start()
    try:
        assert ep.start_lease() is AcquireResult.GRANTED
        intent = intent_for_auth(ep.lease.auth)  # type: ignore[union-attr]
        results: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: results.put(_safe_send(ep, intent)), daemon=True).start()
        assert gate.wait(WAIT)
        server.send(signal(ControlKind.REVOKE, generation=ep.lease.auth.presence_generation), endpoint_id="ep_cli")  # type: ignore[union-attr]
        outcome = results.get(timeout=WAIT)
        assert ep.lease is None and (outcome == "partial" or outcome == "rejected")
        assert _wait_until(lambda: bool(server.manager.quiesced))
        assert server.manager.quiesced[0][0] == "ep_cli"  # 證據經控制連線回到 Core
    finally:
        ep.close()
        client.close()


def _wait_until(predicate, timeout: float = WAIT) -> bool:
    ticker = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        ticker.wait(0.02)
    return predicate()


def _safe_send(ep, intent):
    from uep.adapters.output import SendFailed
    try:
        result = ep.send(intent, "dsp_1")
        return "partial" if result.played_range["complete"] is False else "complete"
    except SendFailed:
        return "rejected"


def test_reconnect_replaces_old_connection_and_resumes(server):
    ep_holder = {}
    client = SocketControlClient(
        *server.address, "ep_cli", SECRET, on_control=lambda s: ep_holder["ep"].on_control(s)
    )
    ep = CliEndpoint("ep_cli", client.api, clock=Mono(), wall_clock=wall_now)
    ep_holder["ep"] = ep
    client.connect()
    try:
        assert ep.start_lease() is AcquireResult.GRANTED
        client.connect()  # 重連（新連線取代舊連線）
        outcome = ep.reconnect()
        assert outcome.value == "RESUMED"
        assert server.manager.renews == 1
        sig = signal(ControlKind.REVOKE, generation=ep.lease.auth.presence_generation)  # type: ignore[union-attr]
        server.send(sig, endpoint_id="ep_cli")
        assert _wait_until(lambda: (ep.process_controls(), ep.lease is None)[1])
    finally:
        client.close()


def test_request_while_disconnected_raises_connection_error(server):
    client, _q = _client(server)
    with pytest.raises(ConnectionError):
        client.api.snapshot()


def test_client_refuses_non_loopback_host():
    with pytest.raises(ValueError):
        SocketControlClient("192.0.2.1", 1, "ep_cli", SECRET, on_control=lambda s: None)


def test_connection_loss_only_wakes_requests_of_that_wire(server):
    """舊連線中斷時，不得誤傷重連後新連線上的等待請求（N8）。"""
    client, _q = _client(server)
    client.connect()
    old_wire = client._wire
    client.connect()  # 重連：新 wire
    new_wire = client._wire
    event, slot = threading.Event(), []
    with new_wire.waiting_lock:
        new_wire.waiting[99] = (event, slot)
    old_wire.close()
    assert _wait_until(lambda: old_wire.closed)
    assert not event.is_set() and 99 in new_wire.waiting
    client.close()


def test_server_uses_exclusive_addr_use_when_available(server):
    import socket as _socket
    if hasattr(_socket, "SO_EXCLUSIVEADDRUSE"):
        host, port = server.address
        probe = _socket.socket()
        probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_EXCLUSIVEADDRUSE, 1)
        with pytest.raises(OSError):
            probe.bind((host, port))
        probe.close()
