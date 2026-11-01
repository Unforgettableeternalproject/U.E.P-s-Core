"""T-24 稽核 (B) 在場與輸出：回歸測試（需 PostgreSQL 者經 session_factory；未設時 skip）。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from tests.uep.endpoint_support import SECRET, FakeCore, Mono, StubManager, wall_now
from tests.uep.factories import NOW, make_intent
from tests.uep.presence_support import activate, intent_for, make_rig
from tests.uep.state_support import TOKEN, journal_events, make_stack
from uep.adapters.output import SendResult
from uep.contracts.presence import AcquireResult, ConfirmResult, PresenceStatus
from uep.control.socket_transport import SocketControlClient, SocketControlServer
from uep.endpoint.auth import (
    EndpointAuthError,
    RequestVerifier,
    SignedRequest,
    sign_request,
)
from uep.endpoint.endpoint import CliEndpoint
from uep.endpoint.protocol import AcquireReply, RpcCoreApi
from uep.endpoint.service import EndpointService
from uep.output.delivery import DeliveryEvidence
from uep.output.delivery import DeliveryStatus as S
from uep.output.intent import ControlKind, ControlScope, ControlSignal
from uep.output.manager import DeliveryManager, IntentMismatchError
from uep.presence.epoch import HostEpochStore
from uep.state.coordinator import StateTransitionRequest
from uep.stop import __main__ as stop_cli
from uep.stop.api import PATH_RECOVERY, PATH_STOP, LocalStopServer
from uep.stop.service import GlobalStop, StopAuthError

REPO = Path(__file__).resolve().parents[2]
GOOD_STOP = {"kind": "adapter_stop_report", "playing": False, "buffer_flushed": True}


class _Clocked:
    """只提供 manager／clock 的最小殼，讓 make_stack 的 Stack 也能用 _stuck_recovering。"""

    def __init__(self, manager, clock) -> None:
        self.manager, self.clock = manager, clock


# ---- 1(a) 新行程自證：RECOVERING 的正式出口 ----

def _stuck_recovering(rig, endpoint_id="ep_a"):
    """acquire 後沒有 confirm_activated，超過 activation_timeout -> RECOVERING 且 unquiesced=該端點。"""
    assert rig.manager.acquire(endpoint_id, "desktop").grant is not None
    rig.clock.advance(11)
    view = rig.manager.current()
    assert view.status is PresenceStatus.RECOVERING and view.unquiesced_endpoint_id == endpoint_id


def _in_process_api(rig, endpoint_id):
    service = EndpointService(rig.manager, RequestVerifier({endpoint_id: SECRET}), clock=rig.clock)
    return RpcCoreApi(endpoint_id, SECRET, lambda wire: service.handle(SignedRequest.from_wire(wire)))


def test_fresh_process_endpoint_escapes_recovering_by_self_attestation(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    _stuck_recovering(rig)
    rig.clock.advance(60)  # 卡住的 Lease（約 30 秒）加偏差容忍 30 秒都已過：才可自證（fix2 項目 1）
    ep = CliEndpoint("ep_a", _in_process_api(rig, "ep_a"), clock=Mono(), wall_clock=rig.clock)
    assert ep.start_lease() is AcquireResult.GRANTED
    assert rig.manager.current().status is PresenceStatus.ACTIVE
    ep.close()


def test_fresh_process_does_not_unblock_a_different_endpoint(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    _stuck_recovering(rig, "ep_a")
    ep = CliEndpoint("ep_b", _in_process_api(rig, "ep_b"), clock=Mono(), wall_clock=rig.clock)
    assert ep.start_lease() is AcquireResult.NOT_QUIESCED
    assert rig.manager.current().status is PresenceStatus.RECOVERING
    ep.close()


class _RefusingCore(FakeCore):
    def acquire(self, location):
        self.calls.append("acquire")
        return AcquireReply(AcquireResult.NOT_QUIESCED)


def test_fresh_process_attestation_is_bounded_when_core_keeps_refusing():
    core = _RefusingCore()
    core.unquiesced = True
    ep = CliEndpoint("ep_cli", core, clock=Mono(), wall_clock=wall_now)
    assert ep.start_lease() is AcquireResult.NOT_QUIESCED
    assert 1 <= len(core.quiesced) <= 2 and core.quiesced[0][1]["kind"] == "fresh_process"
    assert core.calls.count("acquire") <= 3


def test_endpoint_that_already_output_does_not_self_attest():
    core = _RefusingCore()
    core.unquiesced = True
    ep = CliEndpoint("ep_cli", core, clock=Mono(), wall_clock=wall_now)
    ep._ever_active = True  # 本行程已輸出過：不是「剛啟動、從未輸出」
    assert ep.start_lease() is AcquireResult.NOT_QUIESCED
    assert core.quiesced == []


# ---- 1(b) operator resolve-recovery（本機 API＋CLI）----

def _http(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_resolve_recovery_api_requires_token_and_records_operator(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    _stuck_recovering(_Clocked(stack.manager, stack.clock))
    server = LocalStopServer(stack.stop, port=0)
    server.start()
    try:
        assert _http("POST", server.url + PATH_RECOVERY, "wrong-token-0123456789", {})[0] == 401
        assert stack.manager.current().status is PresenceStatus.RECOVERING
        status, body = _http("POST", server.url + PATH_RECOVERY, TOKEN, {"operator": "alice"})
        assert (status, body) == (200, {"resolved": True})
        assert stack.manager.current().status is PresenceStatus.NONE
        assert any("alice" in e.actor_id for e in journal_events(session_factory, "presence.recovered"))
        assert _http("POST", server.url + PATH_RECOVERY, TOKEN, {})[1] == {"resolved": False}  # 冪等
    finally:
        server.stop()


def test_resolve_recovery_cli(session_factory, tmp_path, monkeypatch, capsys):
    stack = make_stack(session_factory, tmp_path)
    _stuck_recovering(_Clocked(stack.manager, stack.clock))
    server = LocalStopServer(stack.stop, port=0)
    server.start()
    try:
        monkeypatch.setattr(stop_cli, "get_settings", lambda: SimpleNamespace(stop_token=SecretStr(TOKEN)))
        assert stop_cli.main(["resolve-recovery", "--url", server.url, "--operator", "bob"]) == 0
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"resolved": True}
        assert stack.manager.current().status is PresenceStatus.NONE
    finally:
        server.stop()


# ---- 2. confirm_quiesced 證據驗證 ----

@pytest.mark.parametrize("evidence", [
    {"kind": "adapter_stop_report", "playing": True, "buffer_flushed": True},
    {"kind": "adapter_stop_report", "playing": False, "buffer_flushed": False},
    {"kind": "x", "playing": False, "buffer_flushed": True},
])
def test_confirm_quiesced_rejects_bad_evidence_and_keeps_marker(session_factory, tmp_path, evidence):
    rig = make_rig(session_factory, tmp_path)
    a = activate(rig, "ep_a")
    rig.clock.advance(31)
    rig.manager.expire_due()
    assert rig.manager.confirm_quiesced("ep_a", a.presence_generation, evidence) is ConfirmResult.EVIDENCE_REJECTED
    assert rig.manager.current().unquiesced_endpoint_id == "ep_a"
    assert rig.manager.confirm_quiesced("ep_a", a.presence_generation, GOOD_STOP) is ConfirmResult.ACCEPTED
    assert rig.manager.current().unquiesced_endpoint_id is None


# ---- 3. naive datetime ----

NAIVE = datetime(2026, 10, 26, 12, 0, 30)


def test_naive_datetimes_are_rejected_at_construction():
    with pytest.raises(ValidationError):
        make_intent(valid_until=NAIVE)
    with pytest.raises(ValidationError):
        ControlSignal(control_id="c", kind=ControlKind.STOP, scope=ControlScope.PROCESS,
                      target_fence={"core_epoch": 1}, issued_at=NAIVE)
    with pytest.raises(ValidationError):
        StateTransitionRequest(request_id="r", axis="interaction", proposer="p", expected_version=0,
                               from_state="a", to_state="b", expires_at=NAIVE)
    assert make_intent(valid_until=NOW + timedelta(seconds=5)).valid_until.tzinfo is not None


def test_dispatch_records_rejection_when_gate_raises(session_factory, tmp_path):
    class BoomGate:
        def admit_intent(self, intent):
            raise RuntimeError("gate boom")

    rig = make_rig(session_factory, tmp_path)
    a = activate(rig)
    dm = DeliveryManager(session_factory, _Adapter(), gate=BoomGate())  # type: ignore[arg-type]
    record = dm.dispatch(intent_for(a))
    assert record.status is S.REJECTED and record.failure["code"] == "GATE_ERROR"


# ---- 4. HostEpochStore 跨行程 ----

_WORKER = """
import json, sys
from uep.presence.epoch import HostEpochStore
s = HostEpochStore(sys.argv[1]); out = []
for _ in range(40):
    out.append(s.bump().fencing_epoch)
print(json.dumps(out))
"""


def test_epoch_bump_is_unique_and_monotonic_across_processes(tmp_path):
    path = tmp_path / "e.json"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    procs = [subprocess.Popen([sys.executable, "-c", _WORKER, str(path)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, cwd=REPO) for _ in range(4)]
    seqs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()[-500:]
        seqs.append(json.loads(out))
    allv = [v for seq in seqs for v in seq]
    assert len(allv) == len(set(allv)) == 160  # 沒有重複核發
    assert all(seq == sorted(seq) for seq in seqs)  # 每個行程看到的值遞增
    assert HostEpochStore(path).read().fencing_epoch == 160  # 沒有回退


# ---- 5. RequestVerifier 重啟後不接受重啟前的請求 ----

def test_fresh_verifier_rejects_requests_signed_before_it_was_created():
    req = sign_request(SECRET, "ep", "renew", {"lease_id": "x"}, clock=lambda: 990.0)
    now = [985.0]
    old = RequestVerifier({"ep": SECRET}, clock=lambda: now[0])  # 重啟前就存在的實例
    now[0] = 995.0
    old.verify(req)
    with pytest.raises(EndpointAuthError):
        RequestVerifier({"ep": SECRET}, clock=lambda: 1000.0).verify(req)  # 重啟後重放
    fresh = sign_request(SECRET, "ep", "renew", {"lease_id": "x"}, clock=lambda: 1000.5)
    assert RequestVerifier({"ep": SECRET}, clock=lambda: 1000.0).verify(fresh) == "ep"


# ---- 6. dispatch 比對已持久化 intent ----

class _Adapter:
    lookup_authoritative = False

    def __init__(self):
        self.sent = []

    def send(self, intent, dispatch_id):
        self.sent.append(intent.content_ref)
        return SendResult(dispatch_id, DeliveryEvidence.TRANSPORT_SENT, NOW)

    def control(self, signal):
        pass

    def lookup(self, dispatch_id):
        raise AssertionError


def test_dispatch_rejects_intent_that_differs_from_persisted(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    a = activate(rig)
    adapter = _Adapter()
    dm = DeliveryManager(session_factory, adapter, gate=rig.gate)
    original = intent_for(a, output_id="out_x", content_ref="blob_A")
    dm.prepare(original)
    for changed in ({"content_ref": "blob_B"}, {"endpoint_id": "ep_other"}, {"audience_scope": "scope_public"}):
        with pytest.raises(IntentMismatchError):
            dm.dispatch(original.model_copy(update=changed))
    assert adapter.sent == []
    assert dm.dispatch(original).status is S.SENT
    assert adapter.sent == ["blob_A"]


# ---- 7. 控制通道伺服器身分 ----

def _rogue_server(reply: bytes) -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(reply)
        threading.Event().wait(0.5)
        conn.close()
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1]


@pytest.mark.parametrize("reply", [b'{"type":"hello_ok"}\n', b'{"type":"hello_ok","proof":"deadbeef"}\n'])
def test_client_rejects_server_that_cannot_prove_secret(reply):
    port = _rogue_server(reply)
    client = SocketControlClient("127.0.0.1", port, "ep_cli", SECRET, on_control=lambda s: None)
    with pytest.raises(EndpointAuthError):
        client.connect()
    assert not client.connected


def test_client_trusts_real_server_and_server_does_not_register_before_hello_ok(monkeypatch):
    service = EndpointService(StubManager(), RequestVerifier({"ep_cli": SECRET}))  # type: ignore[arg-type]
    server = SocketControlServer(service)
    server.start()
    try:
        client = SocketControlClient(*server.address, "ep_cli", SECRET, on_control=lambda s: None)
        client.connect()
        assert client.connected and server.connected("ep_cli")
        client.close()

        def boom(*_args):
            raise EndpointAuthError("x")

        monkeypatch.setattr(service, "prove", boom)
        second = SocketControlClient(*server.address, "ep_cli", SECRET, on_control=lambda s: None)
        with pytest.raises((EndpointAuthError, ConnectionError)):
            second.connect()
        assert not server.connected("ep_cli")
    finally:
        server.close()


# ---- 8. UEP_STOP_TOKEN 最小長度 ----

def test_short_stop_token_is_treated_as_unset(session_factory, tmp_path, monkeypatch):
    stack = make_stack(session_factory, tmp_path, token="short-token")  # 11 位元組
    with pytest.raises(StopAuthError):
        stack.stop.authenticate("short-token")
    server = LocalStopServer(stack.stop, port=0)
    server.start()
    try:
        assert _http("POST", server.url + PATH_STOP, "short-token", {})[0] == 503
    finally:
        server.stop()
    monkeypatch.setattr(stop_cli, "get_settings", lambda: SimpleNamespace(stop_token=SecretStr("short-token")))
    assert stop_cli.main(["stop", "--url", "http://127.0.0.1:1"]) == 1
    GlobalStop(session_factory, stack.manager, stack.control, token=SecretStr("x" * 16)).authenticate("x" * 16)


# ---- 9. revoke 後他端 acquire 的放行條件（A20／D43）----

def test_revoke_marks_old_endpoint_and_blocks_others_until_stop_evidence(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    a = activate(rig, "ep_a")
    out = rig.manager.revoke("global_stop")
    assert out.generation == a.presence_generation  # revoke 不使 generation +1
    assert rig.manager.current().unquiesced_endpoint_id == "ep_a"
    blocked = rig.manager.acquire("ep_b", "discord")
    assert blocked.result is AcquireResult.NOT_QUIESCED and blocked.grant is None
    assert rig.manager.current().status is PresenceStatus.NONE
    assert rig.manager.confirm_quiesced("ep_a", a.presence_generation, GOOD_STOP) is ConfirmResult.ACCEPTED
    assert rig.manager.acquire("ep_b", "discord").result is AcquireResult.GRANTED


def test_revoke_marker_clears_when_old_lease_expires_without_evidence(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    activate(rig, "ep_a")
    rig.manager.revoke("global_stop")
    rig.clock.advance(29)
    assert rig.manager.acquire("ep_b", "discord").result is AcquireResult.NOT_QUIESCED
    rig.clock.advance(2)  # 舊 Lease 的 expires_at 已過
    assert rig.manager.acquire("ep_b", "discord").result is AcquireResult.GRANTED
    assert rig.manager.current().unquiesced_endpoint_id is None


def test_revoked_endpoint_itself_may_reacquire_immediately(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    activate(rig, "ep_a")
    rig.manager.revoke("global_stop")
    assert rig.manager.acquire("ep_a", "desktop").result is AcquireResult.GRANTED


def test_lease_expiry_marker_still_requires_evidence_even_after_expiry(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    activate(rig, "ep_a")
    rig.clock.advance(31)
    assert rig.manager.acquire("ep_b", "discord").result is AcquireResult.NOT_QUIESCED
    rig.clock.advance(100)
    assert rig.manager.acquire("ep_b", "discord").result is AcquireResult.NOT_QUIESCED  # 過期路徑維持 INV-08
