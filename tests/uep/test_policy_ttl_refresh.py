"""政策快照 ttl 由 Host tick 確認後刷新（不需資料庫、假 clock）。"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from uep.decision import DecisionHub
from uep.decision.log import DecisionLogWriter
from uep.decision.policy_publish import KIND_POLICY_SNAPSHOT, register_policy_publish
from uep.echo.host import EchoPipelineHost
from uep.echo.policy import CAUSE_TTL_REFRESH, HostPolicySource
from uep.turn.engagement import ChannelMode

TTL = 10.0


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class StopAfter:
    """假的 _tick_stop：wait 前 n 次回 False（繼續），之後回 True（停止）。"""

    def __init__(self, n: int) -> None:
        self.n = n

    def wait(self, _interval: float) -> bool:
        if self.n <= 0:
            return True
        self.n -= 1
        return False


class _Ctx:
    guild_id = None


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr("uep.echo.policy.time", SimpleNamespace(perf_counter=c))
    return c


def make_host(clock, *, ttl=TTL, ticks=1, on_tick=None, on_sync=None):
    """不經 __init__ 的最小 Host：只放 _tick_loop／_refresh_policy_if_due 用到的欄位。"""
    host = object.__new__(EchoPipelineHost)
    host.policy = HostPolicySource("web", ttl_s=ttl)
    host._session = _Ctx()
    host._tick_stop = StopAfter(ticks)
    host._decision = SimpleNamespace(tick_interval_s=0.05)
    host.calls = []

    def submit_tick():
        host.calls.append("tick")
        if on_tick:
            on_tick(host)
        return []

    def sync(_ctx):
        host.calls.append("sync")
        if on_sync:
            on_sync(host)

    host.submit_tick = submit_tick
    host._sync_participants = sync
    return host


def stale(host, clock) -> bool:
    return host.policy.core_current().is_stale(now=clock())


def test_refresh_after_half_ttl_resets_age_and_bumps_version(clock):
    host = make_host(clock)
    seen = []
    host.policy.add_publish_listener(seen.append)
    clock.t += TTL * 0.6
    host._tick_loop()
    assert [(p.cause, p.version, p.prev_version) for p in seen] == [(CAUSE_TTL_REFRESH, 2, 1)]
    assert host.policy.version == 2 and host.policy.core_current().version == 2
    assert host.policy.age_s() == 0.0
    clock.t += TTL * 0.9
    assert not stale(host, clock)
    assert host.calls == ["tick", "sync"]


def test_ttl_none_never_refreshes(clock):
    host = make_host(clock, ttl=None, ticks=3)
    clock.t += 10_000
    host._tick_loop()
    assert host.policy.version == 1 and host.calls == ["tick"] * 3  # 不重讀 roster


def test_age_below_half_ttl_does_not_refresh(clock):
    host = make_host(clock)
    clock.t += TTL * 0.49
    host._tick_loop()
    assert host.policy.version == 1 and host.calls == ["tick"]


def test_tick_failure_skips_refresh_and_snapshot_expires(clock, caplog):
    def boom(_h):
        raise RuntimeError("tick 壞了")

    host = make_host(clock, on_tick=boom, ticks=2)
    clock.t += TTL * 0.6
    with caplog.at_level(logging.ERROR):
        host._tick_loop()
    assert host.policy.version == 1 and host.calls == ["tick", "tick"]
    clock.t += TTL
    assert stale(host, clock)


def test_sync_failure_skips_publish_and_snapshot_expires(clock):
    def boom(_h):
        raise RuntimeError("roster 讀不到")

    host = make_host(clock, on_sync=boom)
    clock.t += TTL * 0.6
    host._tick_loop()  # 例外不外洩
    assert host.policy.version == 1 and host.calls == ["tick", "sync"]
    clock.t += TTL
    assert stale(host, clock)


def test_no_tick_means_snapshot_expires(clock):
    host = make_host(clock, ticks=0)
    clock.t += TTL + 1
    host._tick_loop()
    assert host.calls == [] and stale(host, clock)


def test_sync_that_already_published_is_not_followed_by_ttl_refresh(clock):
    seen = []

    def flip(h):  # 重讀人數 → 模式翻轉監聽器 publish
        h.policy.publish_channel_mode(ChannelMode.ACTIVE)

    host = make_host(clock, on_sync=flip)
    host.policy.add_publish_listener(seen.append)
    clock.t += TTL * 0.6
    host._tick_loop()
    assert [(p.cause, p.version) for p in seen] == [("channel_mode", 2)]
    assert host.policy.version == 2


def test_publish_rejects_illegal_cause():
    src = HostPolicySource("web")
    for bad in ("participants", "channel_mode", "initial", "bogus", ""):
        with pytest.raises(ValueError):
            src.publish(cause=bad)
    with pytest.raises(ValueError):
        src.publish(cause=CAUSE_TTL_REFRESH, channel_mode=ChannelMode.ACTIVE)
    assert src.version == 1  # 失敗不換版


def test_age_s_and_ttl_s_properties(clock):
    src = HostPolicySource("web", ttl_s=3.0)
    assert src.ttl_s == 3.0 and HostPolicySource("web").ttl_s is None
    assert src.age_s() == 0.0
    clock.t += 2.5
    assert src.age_s() == 2.5 and src.age_s(now=clock.t + 1) == 3.5


class _ListSink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, rows) -> None:
        self.rows.extend(rows)


def test_audit_records_ttl_refresh_with_contiguous_versions(clock):
    hub = DecisionHub()
    source = HostPolicySource("web", ttl_s=TTL)
    sink = _ListSink()
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(hub)
    register_policy_publish(hub, source, run_id="run1", record_initial=False)
    source.publish()
    source.publish(cause=CAUSE_TTL_REFRESH)
    source.publish(cause=CAUSE_TTL_REFRESH)
    assert writer.flush()
    rows = [r for r in sink.rows if r["kind"] == KIND_POLICY_SNAPSHOT]
    assert [(r["reason_code"], r["request_id"]) for r in rows] == [
        ("participants", "run1:policy:2"), ("ttl_refresh", "run1:policy:3"), ("ttl_refresh", "run1:policy:4"),
    ]
    assert all(r["outcome"] == "published" for r in rows)
    writer.close()
