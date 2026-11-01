"""AcquireOutcome 原因碼（T-23）：DENIED 帶 global_stop_engaged／superseded_core，經 RPC 傳到端點。"""
import pytest

from tests.uep.endpoint_support import SECRET, FakeCore, Mono, wall_now
from tests.uep.state_support import make_stack
from uep.contracts.presence import AcquireDenyReason, AcquireResult
from uep.control.socket_transport import SocketControlClient, SocketControlServer
from uep.endpoint.auth import RequestVerifier
from uep.endpoint.endpoint import CliEndpoint
from uep.endpoint.protocol import AcquireReply, acquire_from_wire, acquire_to_wire
from uep.endpoint.service import EndpointService

# ---- wire（不需要資料庫）----

def test_wire_round_trip_with_and_without_reason():
    denied = AcquireReply(AcquireResult.DENIED, reason=AcquireDenyReason.GLOBAL_STOP_ENGAGED)
    assert acquire_from_wire(acquire_to_wire(denied)) == denied
    busy = AcquireReply(AcquireResult.BUSY)
    assert acquire_to_wire(busy)["reason"] is None and acquire_from_wire(acquire_to_wire(busy)).reason is None


def test_unknown_reason_from_newer_core_is_ignored_not_fatal():
    wire = {"result": "DENIED", "auth": None, "ttl_seconds": None, "reason": "something_new"}
    reply = acquire_from_wire(wire)
    assert reply.result is AcquireResult.DENIED and reply.reason is None
    assert acquire_from_wire({"result": "DENIED"}).reason is None  # 舊 Core 沒有此欄位


class _DeniedCore(FakeCore):
    def __init__(self, reason):
        super().__init__()
        self.reason = reason

    def acquire(self, location):
        self._check("acquire")
        return AcquireReply(AcquireResult.DENIED, reason=self.reason)


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [
        (AcquireDenyReason.GLOBAL_STOP_ENGAGED, False),
        (AcquireDenyReason.SUPERSEDED_CORE, True),
        (AcquireDenyReason.GUARD_REJECTED, True),
        (None, True),
    ],
)
def test_endpoint_stops_retrying_only_for_global_stop(reason, retryable):
    ep = CliEndpoint("ep_cli", _DeniedCore(reason), clock=Mono(), wall_clock=wall_now)
    assert ep.start_lease() is AcquireResult.DENIED
    assert ep.last_deny_reason is reason and ep.acquire_retryable is retryable
    assert ep.lease is None


def test_endpoint_reason_cleared_after_success():
    core = FakeCore()
    ep = CliEndpoint("ep_cli", core, clock=Mono(), wall_clock=wall_now)
    ep._deny_reason = AcquireDenyReason.GLOBAL_STOP_ENGAGED
    assert ep.start_lease() is AcquireResult.GRANTED
    assert ep.last_deny_reason is None and ep.acquire_retryable


# ---- 經真實 Manager／Service／socket（需要資料庫）----

@pytest.fixture
def world(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    service = EndpointService(stack.manager, RequestVerifier({"ep_cli": SECRET}), clock=stack.clock)
    server = SocketControlServer(service)
    server.start()
    holder: dict = {}
    client = SocketControlClient(*server.address, "ep_cli", SECRET, on_control=lambda s: holder["ep"].on_control(s))
    ep = CliEndpoint("ep_cli", client.api, clock=Mono(), wall_clock=stack.clock)
    holder["ep"] = ep
    client.connect()
    yield stack, ep
    ep.close()
    client.close()
    server.close()


def test_global_stop_reason_reaches_endpoint_over_rpc(world):
    stack, ep = world
    stack.stop.engage()
    assert ep.start_lease() is AcquireResult.DENIED
    assert ep.last_deny_reason is AcquireDenyReason.GLOBAL_STOP_ENGAGED and not ep.acquire_retryable
    stack.stop.disengage()
    assert ep.start_lease() is AcquireResult.GRANTED  # 解除後（手動重試）可取得
    assert ep.last_deny_reason is None


def test_superseded_core_reason_reaches_endpoint_over_rpc(world):
    stack, ep = world
    stack.new_manager()  # 較新的 Core 取代 stack.manager（service 仍連著舊的）
    assert ep.start_lease() is AcquireResult.DENIED
    assert ep.last_deny_reason is AcquireDenyReason.SUPERSEDED_CORE and ep.acquire_retryable


def test_only_denied_carries_reason_at_manager_level(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    granted = stack.manager.acquire("ep_desktop", "desktop")
    assert granted.result is AcquireResult.GRANTED and granted.reason is None
    stack.manager.confirm_activated(granted.grant.lease_id)
    busy = stack.manager.acquire("ep_discord", "discord")
    assert busy.result is AcquireResult.BUSY and busy.reason is None
