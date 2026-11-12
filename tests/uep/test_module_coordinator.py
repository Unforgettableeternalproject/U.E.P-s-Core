"""Module Coordinator PR-2：狀態機執行器（fake handle，離線，不需 DB、不碰 GPU）。

時間一律用假時鐘＋手動 ``tick()``（run_scheduler=False），阻塞用 handle 的 gate；測試不 sleep 等時間，
只輪詢「背景 worker 的結果」（有時限）。每個測試結束一定釋放所有 gate 並停 coordinator，避免 worker 卡住。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from tests.uep.module_fakes import FakeClock, FakeHandle, FakeObserver
from uep.lifecycle.coordinator import CoordinatorError, ModuleCoordinator
from uep.lifecycle.descriptor import ModuleDescriptor
from uep.lifecycle.registry import ModuleRegistryError
from uep.lifecycle.states import (
    MODULE_STATE_CHANGED,
    is_legal_publish,
    validate_state_changed_payload,
)


class Recorder:
    """記錄 sink：可選擇丟掉前 N 則（模擬 Bus 丟事件）或拋例外。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.drop_next = 0
        self.fail_next = 0
        self._lock = threading.Lock()

    def __call__(self, event_type: str, payload: Any) -> None:
        assert event_type == MODULE_STATE_CHANGED
        with self._lock:
            if self.fail_next > 0:
                self.fail_next -= 1
                raise RuntimeError("sink down")
            if self.drop_next > 0:
                self.drop_next -= 1
                return
            self.events.append(dict(payload))

    def of(self, module_id: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["module_id"] == module_id]

    def states(self, module_id: str) -> list[str]:
        return [e["state"] for e in self.of(module_id)]


def wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("等待背景工作逾時")
        time.sleep(0.005)


class Rig:
    def __init__(self, **kw: Any) -> None:
        self.rec = Recorder()
        self.clock = FakeClock()
        self.handles: dict[str, Any] = {}
        opts = {"epoch_source": lambda: 1, "clock": self.clock, "run_scheduler": False, "resync_interval_s": 0, **kw}
        self.mc = ModuleCoordinator(self.rec, **opts)

    def add(self, module_id: str, *deps: str, handle: Any = None, **desc: Any) -> Any:
        handle = handle if handle is not None else FakeHandle()
        self.handles[module_id] = handle
        self.mc.register(ModuleDescriptor(module_id, depends_on=tuple(deps), **desc), handle)
        return handle

    def state(self, module_id: str) -> str:
        return self.mc.status(module_id).state

    def until(self, module_id: str, state: str) -> None:
        wait_until(lambda: self.state(module_id) == state)

    def close(self) -> None:
        for h in self.handles.values():
            if hasattr(h, "release_all"):
                h.release_all()
        self.mc.stop(timeout_s=2.0)


@pytest.fixture
def rig():
    made: list[Rig] = []

    def make(**kw: Any) -> Rig:
        r = Rig(**kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


# --- 啟動快照、載入序列、依賴 ---

def test_startup_snapshot_precedes_load_and_autoload_reaches_ready(rig):
    r = rig()
    r.add("lore", autoload=True)
    r.add("idle_mod")  # 不 autoload：停在 unloaded
    r.mc.start()
    assert r.mc.wait_settled(5)
    ev = r.rec.events
    assert [(e["module_id"], e["state"], e["cause"]) for e in ev[:2]] == [
        ("lore", "unloaded", "startup"), ("idle_mod", "unloaded", "startup")]  # 快照在任何載入轉移之前
    assert r.rec.states("lore") == ["unloaded", "loading", "ready"]
    assert r.rec.states("idle_mod") == ["unloaded"]
    assert [e["revision"] for e in r.rec.of("lore")] == [1, 2, 3] and all(e["epoch"] == 1 for e in ev)
    assert r.handles["lore"].calls == ["load"] and r.handles["idle_mod"].calls == []
    for e in ev:
        validate_state_changed_payload(e)


def test_needs_warm_goes_through_warming(rig):
    r = rig()
    r.add("tts", autoload=True, needs_warm=True)
    r.mc.start()
    assert r.mc.wait_settled(5)
    assert r.rec.states("tts") == ["unloaded", "loading", "warming", "ready"]
    assert r.handles["tts"].calls == ["load", "warm"]


def test_dependency_waits_until_ready_and_cycle_missing_dependency_fail_start(rig):
    r = rig()
    gate = r.add("base", autoload=True).block("load")
    r.add("child", "base", autoload=True)
    r.mc.start()
    r.until("base", "loading")
    assert r.state("child") == "unloaded" and r.handles["child"].calls == []  # 依賴未 ready 不啟動
    gate.set()
    assert r.mc.wait_settled(5)
    assert r.state("child") == "ready"

    bad = rig()
    bad.add("a", "ghost")
    with pytest.raises(ModuleRegistryError):
        bad.mc.start()
    cyc = rig()
    cyc.add("x", "y")
    cyc.add("y", "x")
    with pytest.raises(ModuleRegistryError):
        cyc.mc.start()
    assert cyc.rec.events == []  # 驗證失敗不發任何事件


def test_register_rejects_duplicates_and_late_registration(rig):
    r = rig()
    r.add("lore")
    with pytest.raises(ModuleRegistryError):
        r.add("lore")
    r.mc.start()
    with pytest.raises(CoordinatorError):
        r.add("other")
    assert r.mc.known_ids() == {"lore"}


# --- 失敗、退避、重試上限（R12）---

def test_load_failure_backs_off_then_retries_and_recovers(rig):
    r = rig(backoff_s=(5.0, 30.0), max_attempts=3)
    h = r.add("web_search", autoload=True)
    h.fail["load"] = RuntimeError("boom")
    r.mc.start()
    r.until("web_search", "failed")
    st = r.mc.status("web_search")
    assert st.reason_code == "load_error" and st.failures == 1 and not st.retries_exhausted
    r.mc.tick()  # 冷卻中：不重試
    assert h.calls == ["load"] and r.state("web_search") == "failed"
    h.fail.clear()
    r.clock.advance(5.0)
    r.mc.tick()
    r.until("web_search", "ready")
    assert h.calls == ["load", "load"]
    failed_ev = [e for e in r.rec.of("web_search") if e["state"] == "failed"]
    assert failed_ev[0]["attempt"] == 1 and failed_ev[0]["reason_code"] == "load_error"
    assert [e["attempt"] for e in r.rec.of("web_search") if e["state"] == "loading"] == [1, 2]
    assert r.mc.status("web_search").failures == 0  # ready 後歸零


def test_retry_cap_stops_automatic_retries_and_ensure_does_not_reset_or_bypass(rig):
    r = rig(backoff_s=(5.0,), max_attempts=2)
    h = r.add("chatroom", autoload=True)
    h.fail["load"] = RuntimeError("boom")
    r.mc.start()
    r.until("chatroom", "failed")
    # 冷卻中顯式要求：不繞過冷卻，也不另起 load
    res = r.mc.request_load("chatroom")
    assert (res.accepted, res.reason) == (True, "queued_until_cooldown") and h.calls == ["load"]
    r.clock.advance(5.0)
    r.mc.tick()
    wait_until(lambda: h.calls.count("load") == 2 and r.mc.status("chatroom").failures == 2)
    st = r.mc.status("chatroom")
    assert st.state == "failed" and st.retries_exhausted
    r.clock.advance(10_000)
    r.mc.tick()
    assert h.calls.count("load") == 2  # 達上限：不再自動重試
    res = r.mc.request_load("chatroom")  # R12：顯式 ensure 不重設重試上限
    assert (res.accepted, res.reason) == (False, "retries_exhausted")
    assert h.calls.count("load") == 2
    h.fail.clear()
    assert r.mc.reset_retries("chatroom").accepted  # 只有運維／中樞的政策選擇才能重設
    r.until("chatroom", "ready")


# --- 逾時、取消、晚完成 ---

def test_timeout_marks_failed_keeps_occupancy_and_late_completion_cannot_resurrect(rig):
    r = rig(backoff_s=(5.0,), max_attempts=3)
    h = r.add("gpu:indextts", autoload=True, load_timeout_s=10.0)
    gate = h.block("load")
    r.mc.start()
    wait_until(h.entered["load"].is_set)
    r.clock.advance(11.0)
    r.mc.tick()
    st = r.mc.status("gpu:indextts")
    assert st.state == "failed" and st.reason_code == "load_timeout"
    assert st.occupied and st.in_flight  # 保留占用；舊呼叫仍在跑
    r.clock.advance(3.0)  # 冷卻未到
    r.mc.tick()
    gate.set()  # 晚完成：不得推進到 ready，占用仍保留
    wait_until(lambda: not r.mc.status("gpu:indextts").in_flight)
    st = r.mc.status("gpu:indextts")
    assert st.state == "failed" and st.occupied
    assert "ready" not in r.rec.states("gpu:indextts") and h.calls == ["load"]
    assert r.mc.counters["late_completion_ignored"] == 1
    r.clock.advance(5.0)  # 冷卻到期：先清理占用（unload）才重 load，不並行
    r.mc.tick()
    r.until("gpu:indextts", "ready")
    assert h.calls == ["load", "unload", "load"] and h.max_running == 1
    seq = r.rec.states("gpu:indextts")
    assert seq[seq.index("failed"):] == ["failed", "unloading", "unloaded", "loading", "ready"]


def test_retry_due_while_old_call_unfinished_does_not_start_another(rig):
    r = rig(backoff_s=(5.0,), max_attempts=3)
    h = r.add("gpu:indextts", autoload=True, load_timeout_s=10.0)
    gate = h.block("load")
    r.mc.start()
    wait_until(h.entered["load"].is_set)
    r.clock.advance(11.0)
    r.mc.tick()
    r.clock.advance(60.0)  # 退避早已到期，但舊呼叫還沒回來
    r.mc.tick()
    assert h.calls == ["load"] and h.max_running == 1 and r.state("gpu:indextts") == "failed"
    gate.set()  # 舊呼叫回來後才依序清理再重載
    r.until("gpu:indextts", "ready")
    assert h.calls == ["load", "unload", "load"] and h.max_running == 1


def test_cancel_during_loading_records_intent_and_unloads_after_old_call_returns(rig):
    r = rig()
    h = r.add("lore", autoload=True)
    gate = h.block("load")
    r.mc.start()
    wait_until(h.entered["load"].is_set)
    res = r.mc.request_unload("lore")
    assert res.accepted and r.state("lore") == "unloading"
    assert h.calls == ["load"]  # 只登記取消意圖，沒有並行 unload
    gate.set()
    r.until("lore", "unloaded")
    assert h.calls == ["load", "unload"] and h.max_running == 1  # 舊呼叫結束後才清理
    assert r.rec.states("lore") == ["unloaded", "loading", "unloading", "unloaded"]  # 從未 ready
    assert r.mc.status("lore").occupied is False


def test_cancel_during_warming_does_not_promote_to_ready(rig):
    r = rig()
    h = r.add("tts", autoload=True, needs_warm=True)
    gate = h.block("warm")
    r.mc.start()
    wait_until(h.entered["warm"].is_set)
    assert r.state("tts") == "warming"
    assert r.mc.request_unload("tts").accepted
    gate.set()
    r.until("tts", "unloaded")
    assert "ready" not in r.rec.states("tts") and h.calls == ["load", "warm", "unload"]


def test_unload_timeout_marks_failed_and_keeps_occupancy(rig):
    r = rig()
    h = r.add("lore", autoload=True, unload_timeout_s=10.0)
    r.mc.start()
    assert r.mc.wait_settled(5)
    gate = h.block("unload")
    assert r.mc.request_unload("lore").accepted
    wait_until(h.entered["unload"].is_set)
    r.clock.advance(11.0)
    r.mc.tick()
    st = r.mc.status("lore")
    assert st.state == "failed" and st.reason_code == "unload_timeout" and st.occupied
    gate.set()
    wait_until(lambda: not r.mc.status("lore").in_flight)
    assert r.state("lore") == "failed"  # 晚完成的 unload 也不改狀態


def test_failed_with_residue_can_be_cleaned_via_unloading(rig):
    r = rig(backoff_s=(1000.0,))
    h = r.add("lore", autoload=True, load_timeout_s=10.0)
    gate = h.block("load")
    r.mc.start()
    wait_until(h.entered["load"].is_set)
    r.clock.advance(11.0)
    r.mc.tick()  # 逾時：failed＋占用
    gate.set()
    wait_until(lambda: not r.mc.status("lore").in_flight)
    st = r.mc.status("lore")
    assert st.state == "failed" and st.occupied
    assert r.mc.request_unload("lore").accepted  # failed -> unloading（D109）
    r.until("lore", "unloaded")
    assert r.rec.states("lore")[-3:] == ["failed", "unloading", "unloaded"] and h.calls == ["load", "unload"]
    assert r.mc.status("lore").occupied is False


# --- R12：顯式請求不得繞過冷卻（M1）---

def test_unload_then_load_does_not_skip_cooldown(rig):
    r = rig(backoff_s=(5.0,), max_attempts=3)
    h = r.add("chatroom", autoload=True)
    h.fail["load"] = RuntimeError("boom")
    r.mc.start()
    r.until("chatroom", "failed")
    assert r.mc.request_unload("chatroom").accepted  # failed -> unloading -> unloaded
    r.until("chatroom", "unloaded")
    h.fail.clear()
    res = r.mc.request_load("chatroom")
    assert (res.accepted, res.reason) == (True, "queued_until_cooldown")
    r.mc.tick()
    assert r.state("chatroom") == "unloaded" and h.calls == ["load"]  # 冷卻中不另起 load
    r.clock.advance(5.0)
    r.mc.tick()
    r.until("chatroom", "ready")
    assert h.calls == ["load", "load"]


def test_request_load_during_unloading_does_not_skip_cooldown(rig):
    r = rig(backoff_s=(5.0,), max_attempts=3)
    h = r.add("gpu:indextts", autoload=True, load_timeout_s=10.0)
    gate = h.block("load")
    r.mc.start()
    wait_until(h.entered["load"].is_set)
    r.clock.advance(11.0)
    r.mc.tick()  # 逾時：failed＋占用，冷卻 5 秒
    gate.set()
    wait_until(lambda: not r.mc.status("gpu:indextts").in_flight)
    unload_gate = h.block("unload")
    assert r.mc.request_unload("gpu:indextts").accepted
    wait_until(h.entered["unload"].is_set)
    assert r.state("gpu:indextts") == "unloading"
    res = r.mc.request_load("gpu:indextts")
    assert (res.accepted, res.reason) == (True, "queued_until_cooldown")
    unload_gate.set()
    r.until("gpu:indextts", "unloaded")
    r.mc.tick()
    assert h.calls == ["load", "unload"] and r.state("gpu:indextts") == "unloaded"  # 冷卻未到：不載入
    r.clock.advance(5.0)
    r.mc.tick()
    r.until("gpu:indextts", "ready")
    assert h.calls == ["load", "unload", "load"]


def test_restart_reschedules_failed_modules_respecting_cooldown(rig):
    r = rig(backoff_s=(5.0,), max_attempts=3)
    h = r.add("lore", autoload=True)
    gate = h.block("load")
    r.mc.start()
    wait_until(h.entered["load"].is_set)
    r.mc.stop(timeout_s=0.2)  # 收不回來：failed(shutdown_timeout)
    assert r.state("lore") == "failed"
    gate.set()
    wait_until(lambda: not r.mc.status("lore").in_flight)
    r.mc.start()
    r.mc.tick()
    assert r.state("lore") == "failed"  # 冷卻中：不卡死，但也不提早重試
    r.clock.advance(5.0)
    r.mc.tick()
    r.until("lore", "ready")
    assert h.calls == ["load", "unload", "load"]  # 先清理占用再重載


# --- 停機 ---

def test_stop_cancels_in_flight_load_then_final_events_use_shutdown(rig):
    r = rig()
    h = r.add("lore", autoload=True)
    ok = r.add("web_fetch", autoload=True)
    gate = h.block("load")
    r.mc.start()
    wait_until(h.entered["load"].is_set)
    r.until("web_fetch", "ready")
    t = threading.Thread(target=r.mc.stop, kwargs={"timeout_s": 5.0})
    t.start()
    wait_until(lambda: r.state("lore") == "unloading")
    gate.set()
    t.join(10)
    assert not t.is_alive()
    assert r.state("lore") == "unloaded" and r.state("web_fetch") == "unloaded"
    assert r.rec.of("lore")[-1]["cause"] == "shutdown" and r.rec.of("web_fetch")[-1]["cause"] == "shutdown"
    assert h.calls == ["load", "unload"] and ok.calls == ["load", "unload"]
    r.mc.stop()  # 冪等
    assert r.mc.request_load("lore").reason == "not_running"


def test_stop_with_hung_call_marks_failed_and_keeps_occupancy(rig):
    r = rig()
    h = r.add("lore", autoload=True)
    h.block("load")
    r.mc.start()
    wait_until(h.entered["load"].is_set)
    r.mc.stop(timeout_s=0.2)
    st = r.mc.status("lore")
    assert st.state == "failed" and st.reason_code == "shutdown_timeout" and st.occupied


def test_restart_same_epoch_keeps_revision_new_epoch_restarts(rig):
    epoch = [3]
    r = rig(epoch_source=lambda: epoch[0])
    r.add("lore", autoload=True)
    r.mc.start()
    assert r.mc.wait_settled(5)
    first_max = max(e["revision"] for e in r.rec.of("lore"))
    r.mc.stop()
    r.mc.start()
    assert r.mc.wait_settled(5)
    revs = [e["revision"] for e in r.rec.of("lore")]
    assert revs == sorted(revs) and len(set(revs)) == len(revs) and revs[-1] > first_max  # 同 epoch：續號
    r.mc.stop()
    epoch[0] = 4
    r.mc.start()
    assert r.mc.wait_settled(5)
    last = r.rec.of("lore")[-3:]
    assert [e["epoch"] for e in last] == [4, 4, 4] and [e["revision"] for e in last] == [1, 2, 3]
    r.mc.stop()
    epoch[0] = 3
    with pytest.raises(CoordinatorError):
        r.mc.start()  # epoch 不得倒退


# --- resync、丟事件補償 ---

def test_resync_increments_revision_with_same_state_and_survives_publish_failures(rig):
    r = rig()
    r.add("lore", autoload=True)
    r.mc.start()
    assert r.mc.wait_settled(5)
    before = r.rec.of("lore")[-1]
    r.mc.resync()
    snap = r.rec.of("lore")[-1]
    assert snap["cause"] == "resync" and snap["state"] == snap["prev_state"] == "ready"
    assert snap["revision"] == before["revision"] + 1
    r.rec.fail_next = 1  # 發布失敗：不回滾狀態
    r.mc.resync()
    assert r.mc.status("lore").revision == snap["revision"] + 1 and r.mc.status("lore").state == "ready"
    r.mc.resync()
    assert r.rec.of("lore")[-1]["revision"] == snap["revision"] + 2  # 後續 resync 補上


def test_scheduled_resync_via_tick(rig):
    r = rig(resync_interval_s=60.0)
    r.add("lore", autoload=True)
    r.mc.start()
    assert r.mc.wait_settled(5)
    n = len(r.rec.events)
    r.clock.advance(59)
    r.mc.tick()
    assert len(r.rec.events) == n
    r.clock.advance(2)
    r.mc.tick()
    assert r.rec.events[-1]["cause"] == "resync" and len(r.rec.events) == n + 1


def test_published_revisions_are_ordered_under_concurrent_requests(rig):
    r = rig()
    for name in ("m_a", "m_b", "m_c"):
        r.add(name)
    r.mc.start()

    def hammer(name: str) -> None:
        for _ in range(15):
            r.mc.request_load(name)
            r.mc.request_unload(name)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in ("m_a", "m_b", "m_c") for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert r.mc.wait_settled(10)
    for name in ("m_a", "m_b", "m_c"):
        revs = [e["revision"] for e in r.rec.of(name)]
        assert revs == list(range(1, len(revs) + 1))  # 順序與 revision 一致、無缺號
        states = [e["state"] for e in r.rec.of(name)]
        for prev, cur, ev in zip(states, states[1:], r.rec.of(name)[1:], strict=False):
            assert is_legal_publish(prev, cur, ev["cause"]), (prev, cur)
        assert r.handles[name].max_running == 1


# --- 設定閘（probe 成功不得覆蓋）---

def test_closed_gate_is_not_lifted_by_probe_start_or_resync(rig):
    r = rig(probe_interval_s=1.0)
    h = r.add("lore_delete", autoload=True)
    h.probe_result = "ok"
    r.mc.close_gate("lore_delete", "delete_disabled")
    r.mc.start()
    assert r.mc.wait_settled(5)
    for _ in range(3):
        r.clock.advance(1000)
        r.mc.tick()
        r.mc.resync()
    st = r.mc.status("lore_delete")
    assert (st.state, st.reason_code, st.blocked_reason) == ("unloaded", "delete_disabled", "delete_disabled")
    assert h.calls == []  # 沒有 load、沒有 probe
    assert {e["state"] for e in r.rec.of("lore_delete")} == {"unloaded"}
    assert all(e["reason_code"] == "delete_disabled" for e in r.rec.of("lore_delete"))
    assert r.mc.request_load("lore_delete").reason == "blocked:delete_disabled"
    assert r.mc.blocked_ids() == {"lore_delete": "delete_disabled"}


def test_gate_closed_in_descriptor_and_close_gate_after_start_rejected(rig):
    r = rig()
    r.add("chatroom", autoload=True, blocked_reason="not_configured")
    r.mc.start()
    assert r.state("chatroom") == "unloaded"
    with pytest.raises(CoordinatorError):
        r.mc.close_gate("chatroom", "x")


def test_blocked_module_never_auto_retries(rig):
    r = rig(backoff_s=(1.0,))
    h = r.add("web_search", autoload=True, blocked_reason="not_configured")
    r.mc.start()
    for _ in range(3):
        r.clock.advance(100)
        r.mc.tick()
    assert h.calls == [] and r.state("web_search") == "unloaded"


# --- 觀測型 handle 與觀測模式（R3、R10）---

def test_observation_only_handle_state_follows_probe_and_never_loads(rig):
    r = rig(probe_interval_s=1.0, probe_dead_threshold=2)
    obs = FakeObserver("ok")
    r.add("stt", handle=obs)
    r.mc.start()
    assert r.mc.wait_settled(5)
    assert r.state("stt") == "ready" and obs.probes == 1  # 狀態由 probe 推導，沒有 load 呼叫（基底會拋例外）
    obs.result = "degraded"
    r.clock.advance(1)
    r.mc.tick()
    r.until("stt", "degraded")
    obs.result = "ok"
    r.clock.advance(1)
    r.mc.tick()
    r.until("stt", "ready")
    obs.result = "dead"
    for _ in range(2):  # 連續兩次 dead 才 failed
        r.clock.advance(1)
        r.mc.tick()
        wait_until(lambda: not r.mc.status("stt").in_flight)
    r.until("stt", "failed")
    res = r.mc.request_unload("stt")
    assert (res.accepted, res.reason) == (False, "observe_only")


def test_observation_mode_does_not_provision_or_evict(rig):
    r = rig(mode="observe")
    h = r.add("lore", autoload=True)
    h2 = r.add("web_fetch")
    r.mc.start()
    assert r.mc.wait_settled(5)
    assert r.mc.request_load("web_fetch").reason == "observation_mode"
    assert r.mc.request_unload("lore").reason == "observation_mode"
    assert h.calls == ["probe"] and h2.calls == ["probe"]  # 只觀測：從不 load／unload
    assert r.state("lore") == "ready"
    r.mc.stop()
    assert "unload" not in h.calls and "load" not in h.calls
    assert r.state("lore") == "unloaded"


def test_reload_hook_is_kept_but_rejected(rig):
    r = rig()
    h = r.add("lore", autoload=True)
    r.mc.start()
    assert r.mc.wait_settled(5)
    res = r.mc.reload("lore")
    assert (res.accepted, res.reason) == (False, "reload_disabled")
    assert r.state("lore") == "ready" and h.calls == ["load"]


def test_unknown_module_requests_are_rejected(rig):
    r = rig()
    r.add("lore")
    r.mc.start()
    assert r.mc.request_load("ghost").reason == "unknown_module"
    assert r.mc.request_unload("ghost").reason == "unknown_module"


# --- M2：start／wait_settled 返回前事件真的送完 ---

def test_publisher_flush_waits_for_another_threads_drain():
    from uep.lifecycle.publisher import ModuleEventPublisher

    gate, entered, got = threading.Event(), threading.Event(), []

    def sink(event_type, payload):
        entered.set()
        gate.wait(10)
        got.append(payload["revision"])

    pub = ModuleEventPublisher(sink)
    base = {"module_id": "lore", "epoch": 1, "state": "unloaded", "readiness": "unavailable", "reason_code": "x"}
    pub.enqueue(MODULE_STATE_CHANGED, {**base, "revision": 1})
    pub.enqueue(MODULE_STATE_CHANGED, {**base, "revision": 2})
    t = threading.Thread(target=pub.drain)
    t.start()
    assert entered.wait(5)
    assert pub.flush(0.1) is False  # 別的 drainer 還沒送完：flush 不能假裝完成
    gate.set()
    assert pub.flush(5.0) is True and got == [1, 2]
    t.join(5)


def test_start_returns_only_after_snapshots_are_delivered(rig):
    r = rig()
    gate, entered = threading.Event(), threading.Event()
    inner = r.rec

    def slow_sink(event_type, payload):
        entered.set()
        gate.wait(10)
        inner(event_type, payload)

    r.mc._pub._sink = slow_sink
    r.add("lore", autoload=True)
    t = threading.Thread(target=r.mc.start)
    t.start()
    assert entered.wait(5)
    t.join(0.3)
    assert t.is_alive() and r.rec.events == []  # 快照還沒送出，start 不得返回
    gate.set()
    t.join(10)
    assert not t.is_alive() and r.mc.wait_settled(5)
    assert r.rec.of("lore")[0]["cause"] == "startup"
