"""端點身分驗證（R2）與 EndpointService（不需 DB）。"""
from __future__ import annotations

import pytest

from tests.uep.endpoint_support import SECRET, StubManager
from uep.contracts.presence import AcquireResult
from uep.endpoint.auth import (
    EndpointAuthError,
    RequestVerifier,
    SignedRequest,
    load_secret_from_env,
    load_secrets_from_env,
    sign_request,
)
from uep.endpoint.protocol import CoreApiError, RpcCoreApi
from uep.endpoint.service import EndpointService

OTHER = b"other-secret-0123456789abcdef"


def _verifier(now: float = 1000.0, **kw) -> RequestVerifier:
    return RequestVerifier({"ep_cli": SECRET}, clock=lambda: now, **kw)


def test_valid_signature_passes():
    req = sign_request(SECRET, "ep_cli", "renew", {"a": 1}, clock=lambda: 1000.0)
    assert _verifier().verify(req) == "ep_cli"


def test_wrong_secret_unknown_endpoint_and_tampering_rejected():
    verifier = _verifier()
    forged = sign_request(OTHER, "ep_cli", "renew", {}, clock=lambda: 1000.0)
    with pytest.raises(EndpointAuthError):
        verifier.verify(forged)
    unknown = sign_request(SECRET, "ep_evil", "renew", {}, clock=lambda: 1000.0)
    with pytest.raises(EndpointAuthError):
        verifier.verify(unknown)
    good = sign_request(SECRET, "ep_cli", "renew", {"generation": 1}, clock=lambda: 1000.0)
    tampered = SignedRequest(good.endpoint_id, good.op, {"generation": 99}, good.ts, good.nonce, good.mac)
    with pytest.raises(EndpointAuthError):
        verifier.verify(tampered)
    swapped_op = SignedRequest(good.endpoint_id, "release", good.args, good.ts, good.nonce, good.mac)
    with pytest.raises(EndpointAuthError):
        verifier.verify(swapped_op)


def test_replay_and_clock_skew_rejected():
    verifier = _verifier()
    req = sign_request(SECRET, "ep_cli", "snapshot", {}, clock=lambda: 1000.0)
    verifier.verify(req)
    with pytest.raises(EndpointAuthError):
        verifier.verify(req)  # 同 nonce 重放
    old = sign_request(SECRET, "ep_cli", "snapshot", {}, clock=lambda: 900.0)
    with pytest.raises(EndpointAuthError):
        verifier.verify(old)  # 超出容許時間差


def test_wire_roundtrip_and_malformed_request():
    req = sign_request(SECRET, "ep_cli", "snapshot", {"x": [1, 2]}, clock=lambda: 1000.0)
    assert SignedRequest.from_wire(req.to_wire()) == req
    with pytest.raises(EndpointAuthError):
        SignedRequest.from_wire({"endpoint_id": "ep_cli"})


def test_secret_must_be_long_enough_and_loaded_from_env(monkeypatch):
    with pytest.raises(ValueError):
        sign_request(b"short", "ep_cli", "x", {})
    monkeypatch.delenv("UEP_ENDPOINT_SECRET", raising=False)
    with pytest.raises(EndpointAuthError):
        load_secret_from_env()
    monkeypatch.setenv("UEP_ENDPOINT_SECRET", SECRET.decode())
    assert load_secret_from_env() == SECRET
    monkeypatch.setenv("UEP_ENDPOINT_SECRETS", f"ep_a:{SECRET.decode()},ep_b:{OTHER.decode()}")
    assert load_secrets_from_env() == {"ep_a": SECRET, "ep_b": OTHER}
    monkeypatch.setenv("UEP_ENDPOINT_SECRETS", "bad-format")
    with pytest.raises(EndpointAuthError):
        load_secrets_from_env()


def _service(manager: StubManager | None = None):
    manager = manager or StubManager()
    verifier = RequestVerifier({"ep_cli": SECRET, "ep_other": OTHER})
    service = EndpointService(manager, verifier)  # type: ignore[arg-type]

    def client(endpoint_id: str, secret: bytes) -> RpcCoreApi:
        return RpcCoreApi(
            endpoint_id, secret, lambda wire: service.handle(SignedRequest.from_wire(wire))
        )

    return manager, service, client


def test_service_rejects_bad_secret_through_rpc():
    _manager, _svc, client = _service()
    with pytest.raises(EndpointAuthError):
        client("ep_cli", OTHER).acquire("desktop")


def test_service_acquire_activate_renew_and_snapshot_owner_only():
    manager, _s, client = _service()
    cli, other = client("ep_cli", SECRET), client("ep_other", OTHER)
    reply = cli.acquire("desktop")
    assert reply.result is AcquireResult.GRANTED and reply.auth is not None and reply.auth.endpoint_id == "ep_cli"
    assert cli.confirm_activated(reply.auth.lease_id, reply.auth.presence_generation).ttl_seconds
    assert cli.renew(reply.auth.lease_id, 2, reply.auth.presence_generation, 4).ttl_seconds
    mine, theirs = cli.snapshot(), other.snapshot()
    assert mine.owns and mine.lease_id == reply.auth.lease_id
    assert not theirs.owns and theirs.lease_id is None  # 非 owner 拿不到 lease_id
    assert manager.renews == 1


def test_service_blocks_other_endpoint_from_touching_lease():
    manager, _s, client = _service()
    cli, other = client("ep_cli", SECRET), client("ep_other", OTHER)
    auth = cli.acquire("desktop").auth
    assert auth is not None
    assert other.confirm_activated(auth.lease_id, auth.presence_generation).result.value == "STALE"
    cli.confirm_activated(auth.lease_id, auth.presence_generation)
    assert other.renew(auth.lease_id, 2, auth.presence_generation, 4).result.value == "STALE"
    assert other.release(auth.lease_id, 2, auth.presence_generation, 4).value == "STALE"
    assert manager.renews == 0 and manager.status.value == "ACTIVE"


def test_service_confirm_quiesced_uses_authenticated_identity_not_args():
    manager, _s, client = _service()
    client("ep_other", OTHER).confirm_quiesced(3, {"kind": "adapter_stop_report"})
    assert manager.quiesced[0][0] == "ep_other"  # 以驗證過的身分為準


def test_service_unknown_op_and_bad_args():
    _manager, service, _client = _service()
    from uep.endpoint.auth import sign_request as sign
    resp = service.handle(SignedRequest.from_wire(sign(SECRET, "ep_cli", "_op_acquire", {}).to_wire()))
    assert resp == {"ok": False, "error": "UNKNOWN_OP"}
    resp = service.handle(SignedRequest.from_wire(sign(SECRET, "ep_cli", "renew", {}).to_wire()))
    assert resp == {"ok": False, "error": "BAD_ARGS"}
    api = RpcCoreApi("ep_cli", SECRET, lambda wire: service.handle(SignedRequest.from_wire(wire)))
    with pytest.raises(CoreApiError):
        api._call("nope")


def test_non_ascii_mac_is_auth_error_not_type_error():
    req = sign_request(SECRET, "ep_cli", "snapshot", {}, clock=lambda: 1000.0)
    bad = SignedRequest(req.endpoint_id, req.op, req.args, req.ts, req.nonce, "中文" * 16)
    with pytest.raises(EndpointAuthError):
        _verifier().verify(bad)


@pytest.mark.parametrize("ts", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timestamp_rejected(ts):
    req = sign_request(SECRET, "ep_cli", "snapshot", {}, clock=lambda: ts)
    with pytest.raises(EndpointAuthError):
        _verifier().verify(req)


def test_nonce_cache_evicts_by_time_not_count():
    now = {"t": 1000.0}
    verifier = RequestVerifier({"ep_cli": SECRET}, clock=lambda: now["t"], skew_seconds=30.0)
    first = sign_request(SECRET, "ep_cli", "snapshot", {}, clock=lambda: 1000.0)
    verifier.verify(first)
    for i in range(5000):  # 大量其他請求不會把仍在時效內的 nonce 擠掉
        verifier.verify(sign_request(SECRET, "ep_cli", "snapshot", {"i": i}, clock=lambda: 1000.0))
    with pytest.raises(EndpointAuthError):
        verifier.verify(first)  # 仍在時效內：重放被擋
    now["t"] = 1100.0  # 超過 skew：舊 nonce 淘汰（再重放也會被時間檢查擋下）
    verifier.verify(sign_request(SECRET, "ep_cli", "snapshot", {}, clock=lambda: 1100.0))
    assert len(verifier._seen) == 1
