"""DL-P1d：policy.snapshot／channel.mode 收編（publish 模式，不需資料庫）。"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from uep.decision import DecisionHub, DecisionPointSpec, DecisionRequest
from uep.decision.log import DecisionLogWriter
from uep.decision.policy_publish import (
    KIND_CHANNEL_MODE,
    KIND_POLICY_SNAPSHOT,
    ChannelModeEngine,
    PolicySnapshotEngine,
    register_policy_publish,
)
from uep.echo.policy import HostPolicySource
from uep.turn.engagement import ChannelMode


class _ListSink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, rows) -> None:
        self.rows.extend(rows)


def _wired(*, run_id="run1", record_initial=False):
    hub = DecisionHub()
    source = HostPolicySource("website")
    sink = _ListSink()
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(hub)
    register_policy_publish(hub, source, run_id=run_id, record_initial=record_initial)
    return hub, source, writer, sink


def _script(source: HostPolicySource):
    out = [
        source.publish(), source.publish_channel_mode(ChannelMode.ACTIVE),
        source.publish_channel_mode(ChannelMode.ACTIVE), source.publish(platform="discord"),
        source.publish_channel_mode(ChannelMode.PASSIVE), source.publish(),
    ]
    return (
        [(s.version, s.platform) for s in out], source.current().version, source.core_current().channel_mode,
        source.core_current().version,
    )


def _no_listener_source():
    return SimpleNamespace(add_publish_listener=lambda cb: None)


def test_listener_does_not_change_versions_or_returns():
    bare = HostPolicySource("website")
    _, wired, writer, _ = _wired()
    assert _script(bare) == _script(wired)
    assert wired.current().platform == bare.current().platform
    writer.close()


def test_listener_sees_published_snapshot_and_publication_fields():
    source = HostPolicySource("website", ttl_s=5.0)
    seen = []
    source.add_publish_listener(lambda pub: seen.append((pub, source.current().version, source.core_current().version)))
    source.publish_channel_mode(ChannelMode.ACTIVE)
    source.publish()
    source.publish(platform="discord")
    pub0, es_v, core_v = seen[0]
    assert (pub0.version, es_v, core_v) == (2, 2, 2)
    assert (pub0.prev_version, pub0.cause, pub0.ttl_s) == (1, "channel_mode", 5.0)
    assert (pub0.prev_channel_mode, pub0.channel_mode) == (None, ChannelMode.ACTIVE)
    assert [p.cause for p, _, _ in seen] == ["channel_mode", "participants", "platform"]
    assert seen[2][0].platform == "discord" and seen[2][0].channel_mode is ChannelMode.ACTIVE


def test_listener_exception_does_not_break_publish():
    source = HostPolicySource("website")
    calls = []

    def boom(_pub):
        calls.append(1)
        raise RuntimeError("x")

    source.add_publish_listener(boom)
    source.add_publish_listener(lambda pub: calls.append(pub.version))
    snap = source.publish()
    assert snap is source.current() and snap.version == 2
    assert calls == [1, 2]


def test_flip_writes_two_rows_participants_one_row():
    _, source, writer, sink = _wired()
    source.publish_channel_mode(ChannelMode.ACTIVE)
    assert writer.flush()
    rows = {r["kind"]: r for r in sink.rows}
    assert set(rows) == {KIND_POLICY_SNAPSHOT, KIND_CHANNEL_MODE} and len(sink.rows) == 2
    assert rows[KIND_CHANNEL_MODE]["outcome"] == "active" and rows[KIND_CHANNEL_MODE]["reason_code"] == "mode_flip"
    assert rows[KIND_POLICY_SNAPSHOT]["outcome"] == "published"
    assert rows[KIND_POLICY_SNAPSHOT]["reason_code"] == "channel_mode"
    assert rows[KIND_CHANNEL_MODE]["request_id"] == rows[KIND_POLICY_SNAPSHOT]["request_id"] == "run1:policy:2"
    sink.rows.clear()
    source.publish_channel_mode(ChannelMode.ACTIVE)  # 沒有翻轉：只記快照
    source.publish()  # 參與者變動
    assert writer.flush()
    assert [(r["kind"], r["reason_code"], r["request_id"]) for r in sink.rows] == [
        (KIND_POLICY_SNAPSHOT, "channel_mode", "run1:policy:3"), (KIND_POLICY_SNAPSHOT, "participants", "run1:policy:4"),
    ]
    sink.rows.clear()
    source.publish_channel_mode(ChannelMode.PASSIVE)
    assert writer.flush()
    assert sorted((r["kind"], r["outcome"]) for r in sink.rows) == [
        (KIND_CHANNEL_MODE, "passive"), (KIND_POLICY_SNAPSHOT, "published"),
    ]
    assert all(r["status"] == "ok" and r["fallback"] is False for r in sink.rows)
    assert all(r["candidate_id"] is None and r["utterance_refs"] is None for r in sink.rows)
    writer.close()


def test_disabled_log_and_no_hub_are_free():
    source = HostPolicySource("website")
    assert source.publish().version == 2  # 無監聽者
    hub = DecisionHub()
    writer = DecisionLogWriter(None)  # 停用
    writer.attach(hub)
    register_policy_publish(hub, source)
    source.publish_channel_mode(ChannelMode.ACTIVE)
    assert source.version == 3 and not writer.enabled and writer._thread is None


def _pub(**kw):
    base = {
        "version": 5, "prev_version": 4, "platform": "website", "channel_mode": ChannelMode.ACTIVE,
        "prev_channel_mode": None, "cause": "channel_mode", "ttl_s": None, "es_version": 5, "core_version": 5,
        "core_channel_mode": ChannelMode.ACTIVE,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"prev_version": 3}, "version_gap"),
        ({"core_version": 4}, "split_version"),
        ({"core_channel_mode": ChannelMode.PASSIVE}, "mode_mismatch"),
        ({}, "channel_mode"),
    ],
)
def test_invariant_audit_reason_codes(override, reason):
    hub = DecisionHub()
    sink = _ListSink()
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(hub)
    recorder = register_policy_publish(hub, _no_listener_source())
    recorder(_pub(**override))
    assert writer.flush()
    snap = next(r for r in sink.rows if r["kind"] == KIND_POLICY_SNAPSHOT)
    assert (snap["outcome"], snap["reason_code"], snap["status"], snap["fallback"]) == ("published", reason, "ok", False)
    writer.close()


def test_invalid_facts_fall_back():
    hub = DecisionHub()
    recorder = register_policy_publish(hub, _no_listener_source())
    results = []
    hub.add_observer(lambda req, res, spec, ms: results.append(res))
    recorder(_pub(version="5"))
    recorder(_pub(cause="bogus", prev_channel_mode=ChannelMode.PASSIVE))
    assert results and all(r.fallback and r.reason_code == "policy_publish_invalid" for r in results)
    for engine in (PolicySnapshotEngine(), ChannelModeEngine()):
        res = engine.decide(DecisionRequest(request_id="r", kind="k", facts={}))
        assert res.fallback and res.reason_code == "policy_publish_invalid"


def test_modes_are_mutually_exclusive():
    hub = DecisionHub()
    register_policy_publish(hub, _no_listener_source())
    hub.register(DecisionPointSpec(kind="s.sync", mode="sync"), PolicySnapshotEngine())
    hub.register(DecisionPointSpec(kind="s.async", mode="async"), PolicySnapshotEngine())

    def req(kind, **kw):
        return DecisionRequest(request_id="r", kind=kind, **kw)

    with pytest.raises(ValueError):
        hub.decide(KIND_POLICY_SNAPSHOT, req(KIND_POLICY_SNAPSHOT))
    with pytest.raises(ValueError):
        hub.submit(KIND_POLICY_SNAPSHOT, req(KIND_POLICY_SNAPSHOT))
    with pytest.raises(ValueError):
        hub.publish("s.sync", req("s.sync"))
    with pytest.raises(ValueError):
        hub.publish("s.async", req("s.async"))
    with pytest.raises(ValueError):
        hub.decide("s.async", req("s.async"))
    with pytest.raises(ValueError):
        hub.submit("s.sync", req("s.sync"))
    with pytest.raises(ValueError):
        hub.publish(KIND_POLICY_SNAPSHOT, req(KIND_CHANNEL_MODE))
    with pytest.raises(ValueError):
        hub.publish(KIND_POLICY_SNAPSHOT, req(KIND_POLICY_SNAPSHOT, native=object()))


def test_blocked_sink_does_not_delay_publish():
    hub = DecisionHub()
    source = HostPolicySource("website")
    gate = threading.Event()

    class _Blocked:
        def write(self, rows) -> None:
            gate.wait(10)

    writer = DecisionLogWriter(_Blocked(), flush_interval=0.01, max_queue=4)
    writer.attach(hub)
    register_policy_publish(hub, source)
    started = time.perf_counter()
    for i in range(50):  # 超過佇列容量也不阻塞：滿了只計數丟棄
        source.publish_channel_mode(ChannelMode.ACTIVE if i % 2 == 0 else ChannelMode.PASSIVE)
    elapsed = time.perf_counter() - started
    dropped = writer.metrics["decision_log.dropped"]
    gate.set()
    writer.close(timeout=2)
    assert source.version == 51 and source.core_current().version == 51
    assert elapsed < 1.0
    assert dropped > 0


def test_blocked_listener_does_not_block_readers_or_other_publishes():
    """listener 在鎖外執行：卡住的 listener 只卡住發布者自己的執行緒。

    讀端（current／core_current）讀到已換入的新版；其他執行緒的 publish 也不會被擋（鎖在 listener 前已釋放）。
    """
    source = HostPolicySource("website")
    entered, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def listener(pub):
        calls.append(pub.version)
        if pub.version == 2:  # 只卡第一次發布
            entered.set()
            release.wait(10)

    source.add_publish_listener(listener)
    result: list[int] = []
    thread_a = threading.Thread(target=lambda: result.append(source.publish().version), daemon=True)
    thread_a.start()
    try:
        assert entered.wait(2)
        started = time.perf_counter()
        assert source.current().version == 2 and source.core_current().version == 2
        assert source.publish_channel_mode(ChannelMode.ACTIVE).version == 3  # 其他發布不被擋
        assert time.perf_counter() - started < 1.0
        assert thread_a.is_alive() and not result  # A 仍卡在 listener
    finally:
        release.set()
    thread_a.join(2)
    assert result == [2] and not thread_a.is_alive()
    assert calls == [2, 3]


def _rows(sink):
    return [(r["kind"], r["request_id"], r["outcome"], r["reason_code"]) for r in sink.rows]


def test_initial_record_then_v2_without_gap():
    _, source, writer, sink = _wired(record_initial=True)
    assert writer.flush()
    snap = [r for r in sink.rows if r["kind"] == KIND_POLICY_SNAPSHOT]
    assert [(r["request_id"], r["reason_code"]) for r in snap] == [("run1:policy:1", "initial")]
    mode_rows = [r for r in sink.rows if r["kind"] == KIND_CHANNEL_MODE]
    assert len(mode_rows) == (1 if source.core_current().channel_mode is not None else 0)
    source.publish()
    assert writer.flush()
    snap = [r for r in sink.rows if r["kind"] == KIND_POLICY_SNAPSHOT]
    assert [(r["request_id"], r["reason_code"]) for r in snap] == [
        ("run1:policy:1", "initial"), ("run1:policy:2", "participants"),
    ]
    writer.close()


def test_record_initial_false_has_no_v1_record():
    _, source, writer, sink = _wired(record_initial=False)
    source.publish()
    assert writer.flush()
    assert [(r["kind"], r["request_id"]) for r in sink.rows] == [(KIND_POLICY_SNAPSHOT, "run1:policy:2")]
    writer.close()


def test_source_without_baseline_method_skips_initial():
    hub = DecisionHub()
    attached = []
    fake = SimpleNamespace(add_publish_listener=attached.append)
    recorder = register_policy_publish(hub, fake)  # 預設 record_initial=True 也不拋錯
    assert attached == [recorder]


def _facts(**kw):
    base = {
        "version": 1, "prev_version": None, "platform": "website", "cause": "initial", "ttl_s": None,
        "es_version": 1, "core_version": 1, "channel_mode": None, "prev_channel_mode": None,
        "core_channel_mode": None,
    }
    base.update(kw)
    return DecisionRequest(request_id="r", kind=KIND_POLICY_SNAPSHOT, facts=base)


def test_initial_must_be_first_for_audit():
    engine = PolicySnapshotEngine()
    assert engine.decide(_facts()).reason_code == "initial"
    assert engine.decide(_facts(version=2, prev_version=1, cause="participants", es_version=2, core_version=2)).reason_code == "participants"
    assert engine.decide(_facts()).reason_code == "initial_not_first"
    assert engine.decide(_facts(cause="participants")).reason_code == "initial_not_first"
    res = engine.decide(_facts(cause="initial", prev_version=3))
    assert res.fallback and res.reason_code == "policy_publish_invalid"
    # 第一筆就不是 initial：其後的 initial 同樣不合法
    other = PolicySnapshotEngine()
    other.decide(_facts(version=2, prev_version=1, cause="participants", es_version=2, core_version=2))
    res = other.decide(_facts())
    assert (res.outcome, res.reason_code) == ("published", "initial_not_first")


class _RaceSource:
    """模擬：listener 已掛上、基準尚未回傳時，另一執行緒的 v2 先到。"""

    def __init__(self, attached: threading.Event, go: threading.Event) -> None:
        self.listeners = []
        self._attached, self._go = attached, go

    def add_publish_listener_with_baseline(self, listener):
        self.listeners.append(listener)
        self._attached.set()
        assert self._go.wait(5)
        return _pub(version=1, prev_version=None, prev_channel_mode=None, cause="initial", es_version=1, core_version=1)


def test_initial_is_published_before_concurrent_v2():
    hub = DecisionHub()
    order = []
    hub.add_observer(lambda req, res, spec, ms: order.append((req.kind, req.facts["version"])))
    attached, go = threading.Event(), threading.Event()
    source = _RaceSource(attached, go)
    reg = threading.Thread(target=lambda: register_policy_publish(hub, source), daemon=True)
    reg.start()
    assert attached.wait(5)
    v2 = threading.Thread(target=lambda: source.listeners[0](_pub(version=2, prev_version=1, cause="participants", es_version=2, core_version=2)), daemon=True)
    v2.start()
    v2.join(2)
    assert not v2.is_alive()  # 只入列，不等待
    assert order == []
    go.set()
    reg.join(5)
    assert not reg.is_alive()
    versions = [v for k, v in order if k == KIND_POLICY_SNAPSHOT]
    assert versions == [1, 2]


def test_blocked_hub_does_not_block_other_publishers_and_order_is_kept():
    hub = DecisionHub()
    source = HostPolicySource("website")
    seen: list[int] = []
    gate, entered = threading.Event(), threading.Event()

    def observer(req, res, spec, ms):
        if spec.kind == KIND_POLICY_SNAPSHOT:
            seen.append(req.facts["version"])
            if req.facts["version"] == 1:
                entered.set()
                gate.wait(10)

    hub.add_observer(observer)
    reg = threading.Thread(target=lambda: register_policy_publish(hub, source), daemon=True)
    reg.start()
    try:
        assert entered.wait(5)  # 基準的 hub.publish 卡住
        done: list[int] = []
        t = threading.Thread(target=lambda: done.append(source.publish().version), daemon=True)
        started = time.perf_counter()
        t.start()
        t.join(2)
        assert done == [2] and time.perf_counter() - started < 1.0
        assert source.publish_channel_mode(ChannelMode.ACTIVE).version == 3
        assert seen == [1]  # 其餘尚在佇列
    finally:
        gate.set()
    reg.join(5)
    assert not reg.is_alive()
    assert seen == [1, 2, 3]
