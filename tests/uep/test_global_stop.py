"""全域停止（D36、T-10；T-16 審查轉入 (1)(2)；需 PostgreSQL）。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from tests.uep.state_support import (
    TOKEN,
    Collector,
    activate_endpoint,
    journal_events,
    make_stack,
)
from uep.contracts.presence import AcquireResult, ConfirmResult, PresenceStatus
from uep.events.models import OutboxRow
from uep.output.intent import ControlKind
from uep.stop import __main__ as stop_cli
from uep.stop.api import PATH_RELEASE, PATH_STOP, LocalStopServer
from uep.stop.models import GlobalStopFlagRow
from uep.stop.service import MAX_GENERATION, GlobalStop, StopAuthError

pytestmark = pytest.mark.usefixtures("session_factory")


def flag(session_factory) -> bool:
    with session_factory() as s:
        row = s.get(GlobalStopFlagRow, "uep")
        return bool(row and row.engaged)


# ---- 認證 ----

def test_wrong_or_missing_token_is_rejected_and_changes_nothing(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    activate_endpoint(stack.manager)
    for bad in (None, "", "wrong"):
        with pytest.raises(StopAuthError):
            stack.stop.trigger(bad)
    assert not flag(session_factory) and stack.manager.current().status is PresenceStatus.ACTIVE


def test_unconfigured_token_rejects_everything_fail_closed(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path, token="")
    with pytest.raises(StopAuthError):
        stack.stop.trigger("")
    with pytest.raises(StopAuthError):
        stack.stop.trigger("anything")
    assert not flag(session_factory)


def test_token_falls_back_to_settings_not_hardcoded(session_factory, tmp_path, monkeypatch):
    stack = make_stack(session_factory, tmp_path)
    service = GlobalStop(session_factory, stack.manager, stack.control)  # 不傳 token -> 讀 settings
    monkeypatch.setattr("uep.stop.service.get_settings", lambda: SimpleNamespace(stop_token=SecretStr("from-env-0123456789")))
    service.trigger("from-env-0123456789")
    assert flag(session_factory)
    monkeypatch.setattr("uep.stop.service.get_settings", lambda: SimpleNamespace(stop_token=None))
    with pytest.raises(StopAuthError):
        service.trigger("from-env-0123456789")


# ---- 效果 ----

def test_trigger_revokes_lease_sets_flag_and_reaches_endpoint(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    grant = activate_endpoint(stack.manager)
    got = Collector()
    stack.control.subscribe("ep_desktop", got)
    outbox_before = count_outbox(session_factory)
    result = stack.stop.trigger(TOKEN, operator="tester")
    assert result.flag_engaged and result.presence_revoked and not result.errors
    assert "ep_desktop" in result.delivered_to
    # manager.revoke 對舊端的定向 revoke 與全域廣播各一，同一 handler 都會收到 revoke
    signals = [got.get(), got.get()]
    assert {s.kind for s in signals} == {ControlKind.REVOKE}
    assert all(s.target_fence["presence_generation"] >= grant.authorization.presence_generation for s in signals)
    assert stack.manager.current().status is PresenceStatus.NONE
    assert flag(session_factory)
    assert [e.payload["reason_code"] for e in journal_events(session_factory, "core.global_stop_engaged")] == ["global_stop"]
    # 控制訊號不經 outbox：outbox 只多了 presence.revoked 的一列
    assert count_outbox(session_factory) - outbox_before == 1


def count_outbox(session_factory) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(OutboxRow)) or 0


@pytest.mark.parametrize("scenario", ["no_row", "none", "recovering", "active"])
def test_broadcast_does_not_depend_on_presence_row_state(session_factory, tmp_path, scenario):
    """T-16 審查 (1)：NONE／RECOVERING／連 row 都沒有，也要通知所有已訂閱端點。"""
    stack = make_stack(session_factory, tmp_path, start=(scenario != "no_row"))
    manager = stack.manager
    if scenario == "recovering":
        activate_endpoint(manager)
        manager = stack.new_manager()  # Core 重啟 -> RECOVERING
        assert manager.current().status is PresenceStatus.RECOVERING
        stack.stop = GlobalStop(session_factory, manager, stack.control, token=SecretStr(TOKEN))
    elif scenario == "active":
        activate_endpoint(manager)
    elif scenario == "none":
        assert manager.current().status is PresenceStatus.NONE
    collectors = {name: Collector() for name in ("ep_a", "ep_b")}
    for name, collector in collectors.items():
        stack.control.subscribe(name, collector)
    result = stack.stop.trigger(TOKEN)
    assert set(result.delivered_to) == {"ep_a", "ep_b"}
    for collector in collectors.values():
        assert collector.get().kind is ControlKind.REVOKE
    assert flag(session_factory)


def test_broadcast_still_happens_when_presence_revoke_fails(session_factory, tmp_path, monkeypatch):
    stack = make_stack(session_factory, tmp_path)
    activate_endpoint(stack.manager)
    got = Collector()
    stack.control.subscribe("ep_desktop", got)

    def boom(reason: str = "x"):
        raise RuntimeError("db down")

    monkeypatch.setattr(stack.manager, "revoke", boom)
    result = stack.stop.trigger(TOKEN)
    assert "revoke:RuntimeError" in result.errors and result.flag_engaged
    assert got.get().kind is ControlKind.REVOKE


def test_broadcast_uses_never_late_generation_when_fence_unreadable(session_factory, tmp_path, monkeypatch):
    stack = make_stack(session_factory, tmp_path)
    got = Collector()
    stack.control.subscribe("ep_desktop", got)

    real = stack.stop._sf
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] >= 2:  # 旗標（第 1 次）之後才壞：只影響 _fence 讀取
            raise RuntimeError("db down")
        return real()

    monkeypatch.setattr(stack.stop, "_sf", flaky)
    monkeypatch.setattr(stack.manager, "revoke", lambda reason="x": SimpleNamespace(lease_id=None))
    result = stack.stop.engage()
    assert any(e.startswith("fence:") for e in result.errors)
    assert got.get().target_fence["presence_generation"] == MAX_GENERATION


# ---- T-16 審查 (2)：暫停旗標阻止重 acquire ----

def test_flag_blocks_reacquire_until_explicit_release(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    activate_endpoint(stack.manager)
    stack.stop.trigger(TOKEN)
    for endpoint in ("ep_desktop", "ep_other"):
        assert stack.manager.acquire(endpoint, "desktop").result is AcquireResult.DENIED
    # 重啟後旗標仍在（存 DB）
    restarted = stack.new_manager()
    assert restarted.acquire("ep_desktop", "desktop").result is AcquireResult.DENIED
    with pytest.raises(StopAuthError):
        stack.stop.release("wrong")
    assert stack.stop.release(TOKEN, operator="tester") is True
    assert stack.stop.release(TOKEN) is False  # 冪等
    outcome = restarted.acquire("ep_desktop", "desktop")
    assert outcome.result is AcquireResult.GRANTED
    assert journal_events(session_factory, "core.global_stop_released")


def test_stop_revokes_issued_but_unconfirmed_lease(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    outcome = stack.manager.acquire("ep_desktop", "desktop")
    assert outcome.grant is not None
    stack.stop.trigger(TOKEN)
    assert stack.manager.confirm_activated(outcome.grant.lease_id) is ConfirmResult.STALE
    assert stack.manager.current().status is PresenceStatus.NONE


def test_trigger_twice_is_idempotent(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    got = Collector()
    stack.control.subscribe("ep_desktop", got)
    stack.stop.trigger(TOKEN)
    stack.stop.trigger(TOKEN)
    assert flag(session_factory)
    assert len(journal_events(session_factory, "core.global_stop_engaged")) == 1  # 旗標事件只記一次
    assert got.get().kind is ControlKind.REVOKE
    assert got.get().kind is ControlKind.REVOKE  # 但每次觸發都會再廣播（新 control_id）


@pytest.mark.parametrize("round_no", range(10))
def test_concurrent_acquire_and_stop_never_leaves_authority(session_factory, tmp_path, round_no):
    """旗標先於撤銷：acquire 與 stop 競態後，不可能還有有效授權，之後的 acquire 一律 DENIED。"""
    stack = make_stack(session_factory, tmp_path)
    barrier = threading.Barrier(2)

    def acquirer() -> None:
        barrier.wait()
        outcome = stack.manager.acquire("ep_desktop", "desktop")
        if outcome.grant is not None:
            stack.manager.confirm_activated(outcome.grant.lease_id)

    def stopper() -> None:
        barrier.wait()
        stack.stop.trigger(TOKEN)

    threads = [threading.Thread(target=acquirer), threading.Thread(target=stopper)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert stack.manager.current().status is not PresenceStatus.ACTIVE
    assert stack.manager.authorization() is None
    assert stack.manager.acquire("ep_desktop", "desktop").result is AcquireResult.DENIED


# ---- 本機 API 與 CLI ----

def http(method: str, url: str, token: str | None = TOKEN, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def api(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    server = LocalStopServer(stack.stop, port=0)
    server.start()
    yield stack, server
    server.stop()


def test_api_requires_bearer_token(api, session_factory):
    _stack, server = api
    assert http("POST", server.url + PATH_STOP, token=None, body={})[0] == 401
    assert http("POST", server.url + PATH_STOP, token="wrong", body={})[0] == 401
    assert http("GET", server.url + PATH_STOP, token="wrong")[0] == 401
    assert not flag(session_factory)


def test_api_stop_status_and_release(api, session_factory):
    stack, server = api
    activate_endpoint(stack.manager)
    got = Collector()
    stack.control.subscribe("ep_desktop", got)
    status, body = http("POST", server.url + PATH_STOP, body={"reason": "panic", "operator": "cli"})
    assert status == 200 and body["flag_engaged"] and body["presence_revoked"] and "ep_desktop" in body["delivered_to"]
    assert got.get().kind is ControlKind.REVOKE
    assert http("GET", server.url + PATH_STOP) == (200, {"engaged": True})
    assert http("POST", server.url + PATH_RELEASE, body={})[1] == {"released": True}
    assert http("GET", server.url + PATH_STOP) == (200, {"engaged": False})
    assert http("GET", server.url + "/nope")[0] == 404


def test_api_unconfigured_token_is_503_and_nonloopback_is_refused(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path, token="")
    server = LocalStopServer(stack.stop, port=0)
    server.start()
    try:
        assert http("POST", server.url + PATH_STOP, token="x", body={})[0] == 503
    finally:
        server.stop()
    with pytest.raises(ValueError):
        LocalStopServer(stack.stop, host="0.0.0.0", port=0)


def test_api_rejects_oversized_body(api):
    _stack, server = api
    status, _ = http("POST", server.url + PATH_STOP, body={"reason": "x" * 10000})
    assert status == 400


def test_cli_stop_and_release_use_env_token(api, session_factory, monkeypatch, capsys):
    _stack, server = api
    monkeypatch.setattr(stop_cli, "get_settings", lambda: SimpleNamespace(stop_token=SecretStr(TOKEN)))
    assert stop_cli.main(["stop", "--url", server.url, "--reason", "drill"]) == 0
    assert flag(session_factory)
    assert '"flag_engaged": true' in capsys.readouterr().out
    assert stop_cli.main(["status", "--url", server.url]) == 0
    assert stop_cli.main(["release", "--url", server.url]) == 0
    assert not flag(session_factory)
    monkeypatch.setattr(stop_cli, "get_settings", lambda: SimpleNamespace(stop_token=SecretStr("bad")))
    assert stop_cli.main(["stop", "--url", server.url]) == 1  # 被拒
    monkeypatch.setattr(stop_cli, "get_settings", lambda: SimpleNamespace(stop_token=None))
    assert stop_cli.main(["stop", "--url", server.url]) == 1  # 本機未設定 token
    assert not flag(session_factory)


# ---- 審查修正：失敗要讓呼叫者知道 ----

def test_flag_write_failure_is_reported_but_broadcast_still_happens(session_factory, tmp_path, monkeypatch):
    stack = make_stack(session_factory, tmp_path)
    got = Collector()
    stack.control.subscribe("ep_desktop", got)

    def boom(reason, operator):
        raise RuntimeError("db down")

    monkeypatch.setattr(stack.stop, "_engage_flag", boom)
    result = stack.stop.engage()
    assert not result.flag_engaged and "flag:RuntimeError" in result.errors
    assert got.get().kind is ControlKind.REVOKE
    assert not flag(session_factory)


def test_api_returns_500_when_flag_fails_and_207_on_partial_failure(api, session_factory, monkeypatch):
    stack, server = api

    def boom(reason, operator):
        raise RuntimeError("db down")

    monkeypatch.setattr(stack.stop, "_engage_flag", boom)
    status, body = http("POST", server.url + PATH_STOP, body={})
    assert status == 500 and body["flag_engaged"] is False and body["errors"]
    monkeypatch.undo()

    def revoke_fails(reason="x"):
        raise RuntimeError("x")

    monkeypatch.setattr(stack.manager, "revoke", revoke_fails)
    status, body = http("POST", server.url + PATH_STOP, body={})
    assert status == 207 and body["flag_engaged"] is True and "revoke:RuntimeError" in body["errors"]


def test_cli_exits_3_with_stderr_hint_on_incomplete_stop(api, monkeypatch, capsys):
    stack, server = api
    monkeypatch.setattr(stop_cli, "get_settings", lambda: SimpleNamespace(stop_token=SecretStr(TOKEN)))

    def revoke_fails(reason="x"):
        raise RuntimeError("x")

    monkeypatch.setattr(stack.manager, "revoke", revoke_fails)
    assert stop_cli.main(["stop", "--url", server.url]) == 3
    assert "未完全生效" in capsys.readouterr().err


def test_api_server_binds_exclusively_and_has_connection_timeout(api):
    _stack, server = api
    from uep.stop import api as api_module
    assert api_module._ExclusiveHTTPServer.allow_reuse_address is False
    handler = server._httpd.RequestHandlerClass
    assert handler.timeout == api_module._CONN_TIMEOUT
