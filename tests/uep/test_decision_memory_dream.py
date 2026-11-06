"""memory.dream 決策點（DL-P1c）：DreamPlanner.decide 收編進 DecisionHub。不需要 DB。"""
from __future__ import annotations

import ast
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from tests.uep.test_dream_loop import BASE, IDLE_FG, OTHER_NS, OWNER_NS, Rig
from uep.decision import DecisionHub
from uep.decision.log import DecisionLogWriter
from uep.decision.memory_dream import (
    KIND_MEMORY_DREAM,
    ON_BUSY_REASON,
    ON_ERROR_REASON,
    ON_INVALID_REASON,
    ON_TIMEOUT_REASON,
    MemoryDreamEngine,
    decider_budget_s,
    engine_budget_s,
    memory_dream_spec,
    register_memory_dream,
)
from uep.memory.dream_planner import (
    DreamLoopConfig,
    DreamPlanner,
    DreamSnapshot,
    NamespaceHistory,
    NamespaceInput,
    NamespaceReadiness,
)

CFG = replace(BASE, min_gap_s=1.0)


def _snap(now=10000.0, *, pending=30, history=None, quiet=1e9, **over) -> DreamSnapshot:
    hist = history or NamespaceHistory(OWNER_NS)
    fg = replace(IDLE_FG, quiet_for_s=quiet)
    base = DreamSnapshot(
        now=now, uptime_s=now, foreground=fg,
        namespaces=(NamespaceInput(hist, NamespaceReadiness(OWNER_NS, pending)),),
    )
    return replace(base, **over)


def _wired(planner: DreamPlanner | None = None, budget_ms: int = 1000):
    planner = planner or DreamPlanner(CFG)
    hub = DecisionHub()
    decider = register_memory_dream(hub, planner, spec=memory_dream_spec(budget_ms), run_id="run1")
    return hub, planner, decider


# ---- 規格 ----

def test_spec_is_async_shadowable_without_native_or_llm():
    spec = memory_dream_spec()
    assert (spec.kind, spec.mode, spec.shadow, spec.allow_native) == (KIND_MEMORY_DREAM, "async", True, False)
    assert spec.outcomes == ("run", "wait")
    assert {spec.on_timeout_reason, spec.on_busy_reason, spec.on_error_reason, spec.on_invalid_reason} == {
        ON_TIMEOUT_REASON, ON_BUSY_REASON, ON_ERROR_REASON, ON_INVALID_REASON}


# ---- 等價性 ----

@pytest.mark.parametrize("snap", [
    _snap(),
    _snap(pending=1),
    _snap(paused=True),
    _snap(uptime_s=10.0),  # startup_grace 以外的 BASE 為 0：改用 in_flight
    _snap(in_flight=True),
    _snap(quiet=100.0),  # quiet_short，帶 not_before
    _snap(history=NamespaceHistory(OWNER_NS, backoff_until=10500.0, consecutive_failures=2)),
    _snap(history=NamespaceHistory(OWNER_NS, last_started_at=9999.5)),  # min_gap 邊界
    _snap(history=NamespaceHistory(OWNER_NS, backoff_until=10000.0)),  # backoff_until == now：不算退避
    _snap(namespaces=()),
])
def test_single_snapshot_matches_direct_decide(snap):
    _hub, planner, decider = _wired()
    assert decider(snap) == planner.decide(snap)


def test_hub_result_is_audit_view_without_namespace_or_text():
    hub, planner, _ = _wired()
    run = hub.submit(KIND_MEMORY_DREAM, _req(_snap()))
    assert (run.outcome, run.reason_code, run.fallback) == ("run", "eligible", False)
    assert run.plan.loop.trigger_kind == planner.decide(_snap()).trigger
    assert OWNER_NS not in repr(run)
    wait = hub.submit(KIND_MEMORY_DREAM, _req(_snap(quiet=100.0), 2))
    assert wait.outcome == "wait" and wait.plan.followups[0].after_ms > 0


def _req(snap, n=1):
    from uep.decision.contracts import DecisionRequest
    return DecisionRequest(request_id=f"t:{n}", kind=KIND_MEMORY_DREAM, trigger="tick", facts={"snapshot": snap})


def _drive(rig: Rig, steps):
    """同一串時間步進：回傳每步決策與最後的歷史／dream 次數，兩條路必須逐步相同。"""
    out = []
    for dt in steps:
        d = rig.tick_after(dt)
        out.append((d.action, d.namespace_id, d.trigger, d.reason, d.not_before))
    status = rig.loop.status()["namespaces"]
    counts = {n: rig.engine(n).dream_count for n in (OWNER_NS, OTHER_NS)}
    return out, status, counts


def test_loop_sequence_with_error_backoff_matches_direct_path(tmp_path):
    # 含 error（指數退避）、min_gap、not_before 邊界（backoff 結束前 1 秒／剛好／之後）
    steps = [0, 2, 70, 70, 70, 400, 55, 1, 0.5, 2, 1300, 1300, 1300, 2000, 70, 70, 2400, 2400, 2400, 2400]

    def run(use_hub: bool):
        rig = Rig(tmp_path / ("hub" if use_hub else "direct"), names=(OWNER_NS, OTHER_NS), cfg=CFG)
        rig.engine(OWNER_NS).dream_result = {"error": "boom"}
        hub = None
        if use_hub:
            hub = DecisionHub()
            rig.loop.set_decider(register_memory_dream(hub, rig.loop.planner))
        return _drive(rig, steps), hub

    (direct, _) = run(False)
    (via_hub, _hub) = run(True)
    assert via_hub == direct
    assert direct[2][OWNER_NS] >= 2  # 確實有多次 error 與退避演進
    assert direct[1][OWNER_NS]["consecutive_failures"] >= 2


def test_no_decider_never_touches_hub_and_calls_planner_directly(tmp_path):
    rig = Rig(tmp_path, cfg=CFG)
    calls = []
    real = rig.loop.planner.decide
    rig.loop.planner.decide = lambda s: calls.append(1) or real(s)  # type: ignore[method-assign]
    assert rig.loop.tick().action == "run" and calls == [1]
    assert rig.loop._decider is None


# ---- 失敗、逾時、例外 ----

def test_planner_exception_waits_with_reason_and_does_not_retry(tmp_path):
    rig = Rig(tmp_path, cfg=CFG)
    hub = DecisionHub()
    rig.loop.set_decider(register_memory_dream(hub, rig.loop.planner))
    calls = []

    def boom(_s):
        calls.append(1)
        raise RuntimeError("secret detail")

    rig.loop.planner.decide = boom  # type: ignore[method-assign]
    d = rig.loop.tick()
    assert (d.action, d.reason, d.namespace_id) == ("wait", ON_ERROR_REASON, None)
    assert calls == [1]  # 只呼叫一次，沒有 fallback 再呼叫
    assert rig.engine().dream_count == 0
    del rig.loop.planner.decide  # 還原
    assert rig.tick_after(1).action == "run"  # 迴圈繼續 tick


def test_hub_level_exception_waits(tmp_path):
    rig = Rig(tmp_path, cfg=CFG)
    hub = DecisionHub()
    rig.loop.set_decider(register_memory_dream(hub, rig.loop.planner))
    calls = []
    real = rig.loop.planner.decide
    rig.loop.planner.decide = lambda s: calls.append(1) or real(s)  # type: ignore[method-assign]

    def bad_observer(*_a):
        raise RuntimeError("x")
    hub.add_observer(bad_observer)  # 觀察者失敗不影響結果
    assert rig.loop.tick().action == "run" and calls == [1]
    hub._engines[KIND_MEMORY_DREAM] = None  # type: ignore[assignment]  # engine 壞掉 -> submit 例外
    d = rig.tick_after(5)
    assert (d.action, d.reason) == ("wait", ON_ERROR_REASON) and calls == [1]


def test_unregistered_kind_waits():
    hub = DecisionHub()
    planner = DreamPlanner(CFG)
    decider = register_memory_dream(hub, planner)
    hub.registry._specs.clear()
    d = decider(_snap())
    assert (d.action, d.reason) == ("wait", ON_ERROR_REASON)


def test_timeout_then_busy_then_recovers_without_second_decide():
    planner = DreamPlanner(CFG)
    gate, entered = threading.Event(), threading.Event()
    calls = []
    real = planner.decide

    def slow(s):
        calls.append(1)
        entered.set()
        gate.wait(5)
        return real(s)

    planner.decide = slow  # type: ignore[method-assign]
    _hub, _p, decider = _wired(planner, budget_ms=50)
    d1 = decider(_snap())
    assert (d1.action, d1.reason) == ("wait", ON_TIMEOUT_REASON) and entered.is_set()
    d2 = decider(_snap())
    assert (d2.action, d2.reason) == ("wait", ON_BUSY_REASON)
    assert calls == [1]  # busy 時不再呼叫 decide
    gate.set()
    for _ in range(200):
        if decider._pending is not None and decider._pending.done():
            break
        threading.Event().wait(0.01)
    d3 = decider(_snap())
    assert d3 == real(_snap()) and calls == [1, 1]  # 遲到結果不被誤用，之後恢復正常


def test_invalid_snapshot_is_fallback():
    hub, _p, _d = _wired()
    from uep.decision.contracts import DecisionRequest
    res = hub.submit(KIND_MEMORY_DREAM, DecisionRequest(request_id="x", kind=KIND_MEMORY_DREAM, facts={}))
    assert (res.fallback, res.outcome, res.reason_code) == (True, "wait", ON_INVALID_REASON)


def test_late_result_for_other_request_is_not_taken():
    engine = MemoryDreamEngine(DreamPlanner(CFG))
    engine.decide(_req(_snap(), 1))
    assert engine.take("t:2") is None
    assert engine.take("t:1") is not None and engine.take("t:1") is None


# ---- 日誌 ----

class _Sink:
    def __init__(self):
        self.rows = []

    def write(self, rows):
        self.rows.extend(rows)


def test_decision_log_attaches_memory_dream_without_raw_data(tmp_path):
    rig = Rig(tmp_path, cfg=CFG)
    hub = DecisionHub()
    rig.loop.set_decider(register_memory_dream(hub, rig.loop.planner))
    sink = _Sink()
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(hub)
    try:
        rig.loop.tick()  # run
        rig.tick_after(0.2)  # wait（min_gap）
        assert writer.flush()
    finally:
        writer.close()
    rows = [r for r in sink.rows if r["kind"] == KIND_MEMORY_DREAM]
    assert [(r["role"], r["outcome"], r["status"]) for r in rows] == [("primary", "run", "ok"), ("primary", "wait", "ok")]
    assert rows[1]["reason_code"] == "min_gap"
    blob = json.dumps(rows, default=str)
    assert OWNER_NS not in blob and "snapshot" not in blob


def test_decision_log_suppresses_repeated_quiet_waits(tmp_path):
    rig = Rig(tmp_path, cfg=replace(CFG, min_gap_s=1000.0))
    hub = DecisionHub()
    rig.loop.set_decider(register_memory_dream(hub, rig.loop.planner))
    sink = _Sink()
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(hub)
    try:
        rig.loop.tick()  # run：記
        for _ in range(3):
            rig.tick_after(0.2)  # min_gap wait：第一筆記，其餘靜默
        rig.engine().readiness["pending"] = 0
        rig.tick_after(70)  # 原因改變（not_enough_pending）：記
        rig.tick_after(70)  # 同原因：靜默
        assert writer.flush()
    finally:
        writer.close()
    rows = [r for r in sink.rows if r["kind"] == KIND_MEMORY_DREAM]
    assert [(r["outcome"], r["reason_code"]) for r in rows] == [
        ("run", "eligible"), ("wait", "min_gap"), ("wait", "not_enough_pending")]
    assert writer.metrics["memory.dream.quiet"] == 3


def test_fallback_is_always_logged_and_resets_quiet_state():
    hub, _p, _d = _wired()
    engine = hub.engine(KIND_MEMORY_DREAM)
    sink = _Sink()
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(hub)
    try:
        wait = _snap(quiet=100.0)
        hub.submit(KIND_MEMORY_DREAM, _req(wait, 1))
        hub.submit(KIND_MEMORY_DREAM, _req(wait, 2))  # 靜默
        from uep.decision.contracts import DecisionRequest
        for n in (3, 4):  # fallback 每次都記
            hub.submit(KIND_MEMORY_DREAM, DecisionRequest(request_id=f"b{n}", kind=KIND_MEMORY_DREAM, facts={}))
        hub.submit(KIND_MEMORY_DREAM, _req(wait, 5))  # fallback 之後同原因 wait 再記一次
        assert writer.flush()
    finally:
        writer.close()
    assert engine is not None
    assert [(r["outcome"], r["status"]) for r in sink.rows] == [
        ("wait", "ok"), ("wait", "invalid"), ("wait", "invalid"), ("wait", "ok")]


# ---- 預算在 engine 內：fallback 進日誌、遲到結果丟棄 ----

def _logged_wired(planner, budget_ms=50):
    hub, _p, decider = _wired(planner, budget_ms=budget_ms)
    sink = _Sink()
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(hub)
    seen = []
    hub.add_observer(lambda *a: seen.append(a))
    return hub, decider, writer, sink, seen


def _blocked_planner():
    planner = DreamPlanner(CFG)
    gate, entered = threading.Event(), threading.Event()
    real = planner.decide

    def slow(s):
        entered.set()
        gate.wait(5)
        return real(s)

    planner.decide = slow  # type: ignore[method-assign]
    return planner, gate, entered


def test_engine_budget_is_strictly_below_decider_budget():
    for ms in (50, 1000, 5000):
        spec = memory_dream_spec(ms)
        assert engine_budget_s(spec) == ms / 1000
        assert engine_budget_s(spec) < decider_budget_s(spec)
    assert engine_budget_s(memory_dream_spec(0)) is None and decider_budget_s(memory_dream_spec(0)) is None


def test_stuck_planner_logs_timeout_fallback_and_drops_late_result():
    planner, gate, entered = _blocked_planner()
    hub, decider, writer, sink, seen = _logged_wired(planner)
    engine = hub.engine(KIND_MEMORY_DREAM)
    try:
        d = decider(_snap())
        assert entered.is_set() and (d.action, d.reason) == ("wait", ON_TIMEOUT_REASON)
        assert writer.flush()
        rows = [r for r in sink.rows if r["kind"] == KIND_MEMORY_DREAM]
        assert [(r["outcome"], r["reason_code"], r["fallback"]) for r in rows] == [("wait", ON_TIMEOUT_REASON, True)]
        assert len(seen) == 1
        results = [a[1] for a in seen]
        assert results[0].fallback and results[0].reason_code == ON_TIMEOUT_REASON
        request_id = results[0].request_id
        gate.set()
        for _ in range(500):  # 等 guard 的 worker 跑完（遲到結果產生）
            if not engine._guard.busy:
                break
            threading.Event().wait(0.01)
        assert not engine._guard.busy
        assert writer.flush()
        assert len([r for r in sink.rows if r["kind"] == KIND_MEMORY_DREAM]) == 1  # 觀察者沒有再收到
        assert len(seen) == 1
        assert engine.take(request_id) is None  # 暫存裡也沒有
    finally:
        gate.set()
        writer.close()


def test_busy_fallback_is_logged():
    planner, gate, _entered = _blocked_planner()
    hub, decider, writer, sink, _seen = _logged_wired(planner)
    try:
        assert decider(_snap()).reason == ON_TIMEOUT_REASON
        engine = hub.engine(KIND_MEMORY_DREAM)
        res = hub.submit(KIND_MEMORY_DREAM, _req(_snap(), 9))  # 上一次還在跑：engine 內回 busy
        assert (res.fallback, res.outcome, res.reason_code) == (True, "wait", ON_BUSY_REASON)
        assert engine.take("t:9") is None
        assert decider(_snap()).reason == ON_BUSY_REASON
        assert writer.flush()
        reasons = [r["reason_code"] for r in sink.rows if r["kind"] == KIND_MEMORY_DREAM]
        assert reasons == [ON_TIMEOUT_REASON, ON_BUSY_REASON, ON_BUSY_REASON]
    finally:
        gate.set()
        writer.close()


def test_planner_exception_is_logged_as_error_fallback():
    planner = DreamPlanner(CFG)

    def boom(_s):
        raise RuntimeError("secret detail")

    planner.decide = boom  # type: ignore[method-assign]
    _hub, decider, writer, sink, _seen = _logged_wired(planner, budget_ms=1000)
    try:
        d = decider(_snap())
        assert (d.action, d.reason) == ("wait", ON_ERROR_REASON)
        assert writer.flush()
        rows = [r for r in sink.rows if r["kind"] == KIND_MEMORY_DREAM]
        assert [(r["outcome"], r["reason_code"]) for r in rows] == [("wait", ON_ERROR_REASON)]
        assert "secret detail" not in json.dumps(rows, default=str)
    finally:
        writer.close()


def test_normal_path_through_engine_budget_is_unchanged():
    planner = DreamPlanner(CFG)
    _hub, decider, writer, sink, _seen = _logged_wired(planner, budget_ms=1000)
    try:
        assert decider(_snap()) == planner.decide(_snap())
        assert writer.flush()
        rows = [r for r in sink.rows if r["kind"] == KIND_MEMORY_DREAM]
        assert [(r["outcome"], r["status"]) for r in rows] == [("run", "ok")]
    finally:
        writer.close()


# ---- import 邊界 ----

def test_memory_package_does_not_import_decision():
    root = Path(__file__).resolve().parents[2] / "uep" / "memory"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n == "uep.decision" or n.startswith("uep.decision.") for n in names), path.name


def test_config_type_is_untouched():
    assert DreamLoopConfig().shadow is True  # D100：不改 shadow 預設
