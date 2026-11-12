"""AR-3b：PresenceSinkProxy 與 Host 的條件式建立（輕量 Host，不碰 DB）。"""
import threading
from types import SimpleNamespace

from uep.config import get_settings
from uep.echo.host import EchoPipelineHost
from uep.echo.schedule_wiring import attach_presence_arbiter
from uep.schedule.sink_proxy import PresenceSinkProxy
from uep.turn.models import SessionContext

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")
REQUEST_KW = {"request_id": "r-1", "actor_ref": "discord:1", "actor_authenticated": True, "is_owner": True,
              "source_platform": "discord"}


class Target:
    def __init__(self, fail=False):
        self.abandons, self.requests, self.fail = [], [], fail

    def on_abandon_signal(self, signal, *, is_owner=None):
        if self.fail:
            raise RuntimeError("boom")
        self.abandons.append((signal, is_owner))

    def on_presence_request(self, proposal, **kw):
        if self.fail:
            raise RuntimeError("boom")
        self.requests.append((proposal, kw))


def test_unbound_calls_are_dropped_and_counted():
    counts = {}
    proxy = PresenceSinkProxy(metrics_inc=lambda n: counts.__setitem__(n, counts.get(n, 0) + 1))
    proxy.on_abandon_signal("sig", is_owner=True)
    proxy.on_presence_request("prop", **REQUEST_KW)
    assert proxy.dropped == 2 and not proxy.bound
    assert counts == {"presence_sink.dropped.abandon_signal": 1, "presence_sink.dropped.presence_request": 1}


def test_bound_calls_are_forwarded_with_the_same_signature_and_unbind_stops_it():
    proxy, target = PresenceSinkProxy(), Target()
    proxy.bind(target)
    proxy.on_abandon_signal("sig", is_owner=True)
    proxy.on_abandon_signal("sig2")  # is_owner 預設 None
    proxy.on_presence_request("prop", **REQUEST_KW)
    assert target.abandons == [("sig", True), ("sig2", None)]
    assert target.requests == [("prop", REQUEST_KW)] and proxy.bound
    proxy.unbind()
    proxy.on_abandon_signal("late", is_owner=True)
    assert len(target.abandons) == 2 and proxy.dropped == 1 and not proxy.bound


def test_forwarding_errors_do_not_escape():
    counts = {}
    proxy = PresenceSinkProxy(metrics_inc=lambda n: counts.__setitem__(n, counts.get(n, 0) + 1))
    proxy.bind(Target(fail=True))
    proxy.on_abandon_signal("sig", is_owner=True)
    proxy.on_presence_request("prop", **REQUEST_KW)
    assert counts == {"presence_sink.error.abandon_signal": 1, "presence_sink.error.presence_request": 1}


def test_bind_unbind_and_calls_are_thread_safe():
    proxy, target = PresenceSinkProxy(), Target()
    stop = threading.Event()
    errors = []

    def toggler():
        while not stop.is_set():
            proxy.bind(target)
            proxy.unbind()

    def caller():
        try:
            for _ in range(2000):
                proxy.on_abandon_signal("s", is_owner=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=toggler)] + [threading.Thread(target=caller) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads[1:]:
        t.join(10)
    stop.set()
    threads[0].join(5)
    assert errors == [] and len(target.abandons) + proxy.dropped == 6000


# ---- Host：旗標 off 時不建 proxy、行為不變 ----

def make_host():
    return EchoPipelineHost(publisher=None, think=object(), speak=object(), sink_factory=lambda *a: "x",
                            session=CTX)  # type: ignore[arg-type]


def test_host_does_not_create_the_proxy_when_the_flag_is_off():
    assert get_settings().presence_arbiter == "off"
    host = make_host()
    assert host.presence_sinks is None
    assert host.orchestrator._on_abandon_signal is None and host.orchestrator._on_presence_request is None


def test_host_creates_and_wires_the_proxy_when_the_flag_is_not_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "presence_arbiter", "shadow")
    host = make_host()
    proxy = host.presence_sinks
    assert isinstance(proxy, PresenceSinkProxy)
    assert host.orchestrator._on_abandon_signal == proxy.on_abandon_signal
    assert host.orchestrator._on_presence_request == proxy.on_presence_request
    host.orchestrator._on_abandon_signal("sig", is_owner=True)  # 尚未綁定：丟棄並計入 host.metrics
    assert host.metrics["presence_sink.dropped.abandon_signal"] == 1


def test_attach_binds_the_hosts_proxy_and_stop_unbinds(monkeypatch):
    monkeypatch.setattr(get_settings(), "presence_arbiter", "shadow")
    host = make_host()
    view = SimpleNamespace(status=SimpleNamespace(value="NONE"), location=None, transfer_id=None, expires_at=None)
    bus = SimpleNamespace(subscribe=lambda *a: None, unsubscribe=lambda *a: None)
    core = SimpleNamespace(
        echo=host, bus=bus, state=SimpleNamespace(current=lambda axis: SimpleNamespace(state="IDLE", version=1)),
        presence=SimpleNamespace(peek=lambda: view), memory=None, tasks=None, persona_id="uep", publisher=None)
    reader = SimpleNamespace(resolve=lambda at=None: None, tz=lambda: "Asia/Taipei")
    arbiter = attach_presence_arbiter(SimpleNamespace(presence_arbiter="shadow"), core, session_factory=None,
                                      reader=reader, start=False)
    assert host.presence_sinks.bound
    arbiter.stop()
    assert not host.presence_sinks.bound
