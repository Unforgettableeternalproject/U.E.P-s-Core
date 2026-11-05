"""決策中樞 P1：turn.interject 收編（不需資料庫）。

primary 的 tick 行為與 develop 一致（同輸入同結果、同計數鍵）；shadow 比對只記錄、失敗與逾時不影響插話。
"""
from __future__ import annotations

import threading
from collections import Counter

import pytest

from uep.decision import DecisionHub, DecisionPointRegistry, DecisionRequest
from uep.decision.log import DecisionLogWriter
from uep.decision.turn_interject import (
    KIND_TURN_INTERJECT,
    TurnInterjectEngine,
    turn_interject_spec,
)
from uep.decision.turn_reply import TurnReplyEngine, turn_reply_spec
from uep.turn.decision_config import DecisionConfig
from uep.turn.models import Proposal, ReplyMode, SessionContext
from uep.turn.orchestrator import TurnOrchestrator
from uep.turn.proposer import Interjection

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")


def _ij(mode=ReplyMode.RESPOND, reason="interject_context", pid="t.tick") -> Interjection:
    return Interjection(
        speaker_ref="discord:1", text="秘密原文", utterance_refs=("e1", "e2"), speakers=("discord:1",),
        proposal=Proposal(reply_mode=mode, addressed=False, confidence=0.5, reason_code=reason,
                          proposer_id=pid, proposer_version="1"),
    )


def _raise(exc):
    raise exc


class _Ticker:
    """可程式化的 ticking proposer（也實作 propose，讓它可當 shadow）。"""

    def __init__(self, pid, outputs=None, fn=None):
        self.proposer_id = pid
        self.version = "1"
        self.outputs = list(outputs or [])
        self.fn = fn
        self.ticks = 0

    def propose(self, candidate):
        return Proposal(reply_mode=ReplyMode.PASS, proposer_id=self.proposer_id, proposer_version="1")

    def tick(self):
        self.ticks += 1
        if self.fn is not None:
            return self.fn()
        return self.outputs.pop(0) if self.outputs else []


class _Sink:
    def __init__(self):
        self.rows, self.lock = [], threading.Lock()

    def write(self, rows):
        with self.lock:
            self.rows.extend(dict(r) for r in rows)


def _orch(primary, shadows=(), budget_ms=300) -> TurnOrchestrator:
    orch = TurnOrchestrator(
        None, None, auth_provider=lambda: None, proposer=primary, shadow_proposers=shadows,  # type: ignore[arg-type]
        decision=DecisionConfig(proposer_budget_ms=budget_ms),
    )
    orch._ctx = CTX  # 讓 tick 有 session 可用
    return orch


def _summary(results):
    return [(r.reason_code, r.proposal.reason_code if r.proposal else None) for r in results]


# ---- 登記與 plan ----

def test_interject_registered_as_async_shadow_point_without_native():
    orch = _orch(None)
    try:
        spec = orch.hub.registry.get(KIND_TURN_INTERJECT)
        assert (spec.mode, spec.shadow, spec.allow_native) == ("async", True, False)
        assert spec.budget_ms == 300 and spec.on_timeout_reason == "interject_timeout"
        with pytest.raises(ValueError):  # async 不走 decide
            orch.hub.decide(KIND_TURN_INTERJECT, DecisionRequest(request_id="r", kind=KIND_TURN_INTERJECT))
        with pytest.raises(ValueError):  # 不接受 native
            orch.hub.submit(KIND_TURN_INTERJECT, DecisionRequest(request_id="r", kind=KIND_TURN_INTERJECT, native=1))
    finally:
        orch.close()


def test_hub_submit_produces_plan_for_interjection():
    orch = _orch(_Ticker("t.tick"))
    try:
        facts = {"status": "ok", "interjections": (
            {"valid": True, "effective": True, "speaker_ref": "discord:1", "reply_mode": "respond",
             "reason_code": "interject_silence", "confidence": 0.5, "route": "direct_chat", "interaction_to": None,
             "addressed": False, "proposer_id": "t.tick", "proposer_version": "1", "utterance_refs": ("e1",)},)}
        result = orch.hub.submit(KIND_TURN_INTERJECT, DecisionRequest(
            request_id="r1", kind=KIND_TURN_INTERJECT, trigger="tick", facts=facts))
        assert result.decision_id.startswith("dec_") and result.outcome == "respond" and not result.fallback
        assert result.plan is not None and result.plan.reply is not None
        assert (result.plan.route, result.plan.reply.mode, result.plan.reply.addressed) == (
            "direct_chat", "respond", False)
        assert result.reason_code == "interject_silence"
    finally:
        orch.close()


@pytest.mark.parametrize("status,reason", [
    ("timeout", "interject_timeout"), ("busy", "interject_busy"), ("error", "interject_error")])
def test_engine_fallback_reasons_come_from_spec(status, reason):
    orch = _orch(_Ticker("t.tick"))
    try:
        result = orch.hub.submit(KIND_TURN_INTERJECT, DecisionRequest(
            request_id="r", kind=KIND_TURN_INTERJECT, facts={"status": status}))
        assert result.fallback and result.reason_code == reason and result.outcome == "pass"
        assert result.engine.tier == "fallback"
    finally:
        orch.close()


# ---- primary 行為零變化 ----

def _run_ticks(primary_factory, shadows=(), n=3, boom_engine=False):
    orch = _orch(primary_factory(), shadows)
    if boom_engine:
        orch._interject_engine.on_tick = lambda *a, **k: _raise(RuntimeError("engine down"))  # type: ignore[method-assign]
    try:
        out = [_summary(orch.tick()) for _ in range(n)]
        orch.wait_shadow(2)
        old = {k: v for k, v in orch.metrics.items() if k.startswith(("proposer.", "decision."))}
        return out, old, orch.metrics
    finally:
        orch.close()


def test_primary_interjections_identical_with_and_without_shadow_and_engine_failure():
    def mk():
        return _Ticker("t.tick", outputs=[[_ij()], [], [_ij(ReplyMode.PASS, "x"), _ij(ReplyMode.PASSIVE, "p")]])

    base, base_metrics, _ = _run_ticks(mk)
    shadow = _Ticker("s.tick", outputs=[[_ij(pid="s.tick")]])
    with_shadow, sh_metrics, _ = _run_ticks(mk, (shadow,))
    broken, broken_metrics, m = _run_ticks(mk, (), boom_engine=True)
    assert base == with_shadow == broken and len(base[0]) == 1 and len(base[2]) == 2
    assert base_metrics == sh_metrics == broken_metrics
    assert m["interject.engine_error"] == 3


def test_primary_tick_timeout_and_error_keep_old_counters():
    gate = threading.Event()
    orch = _orch(_Ticker("t.tick", fn=lambda: gate.wait(2) and []), budget_ms=30)
    try:
        assert orch.tick() == [] and orch.metrics["proposer.tick_timeout"] == 1
        assert orch.tick() == [] and orch.metrics["proposer.tick_busy"] == 1
    finally:
        gate.set()
        orch.close()
    orch = _orch(_Ticker("t.tick", fn=lambda: _raise(RuntimeError("x"))))
    try:
        assert orch.tick() == [] and orch.metrics["proposer.tick_error"] == 1
    finally:
        orch.close()


# ---- shadow 比對與日誌 ----

def _logged(orch, sink):
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(orch.hub)
    return writer


def test_shadow_agree_disagree_and_quiet_are_logged_without_text():
    sink = _Sink()
    primary = _Ticker("t.tick", outputs=[[_ij()], [], []])
    shadow = _Ticker("s.tick", outputs=[
        [_ij(pid="s.tick")], [_ij(ReplyMode.PASSIVE, "interject_silence", "s.tick")], []])
    orch = _orch(primary, (shadow,))
    writer = _logged(orch, sink)
    try:
        for _ in range(3):
            orch.tick()
            assert orch.wait_shadow(2)
        assert writer.flush()
    finally:
        orch.close()
        writer.close()
    shadows = [r for r in sink.rows if r["role"] == "shadow"]
    primaries = {r["decision_id"]: r for r in sink.rows if r["role"] == "primary"}
    assert [(r["agree"], r["outcome"]) for r in shadows] == [(True, "respond"), (False, "passive")]
    assert all(r["kind"] == KIND_TURN_INTERJECT and r["status"] == "ok" for r in shadows)
    assert len(primaries) == 2 and all(r["primary_decision_id"] in primaries for r in shadows)
    assert primaries[shadows[0]["primary_decision_id"]]["outcome"] == "respond"
    assert primaries[shadows[1]["primary_decision_id"]]["outcome"] == "pass"  # primary 靜默、shadow 會插話
    assert orch.metrics["interject.quiet"] == 2 and orch.metrics["interject.shadow_quiet"] == 1
    assert orch.metrics["shadow.interject_agree"] == 1 and orch.metrics["shadow.interject_disagree"] == 1
    assert "秘密原文" not in repr(sink.rows)


def test_shadow_tick_error_and_late_recorded_primary_unaffected():
    primary = _Ticker("t.tick", outputs=[[_ij()]])
    shadow = _Ticker("s.tick", fn=lambda: _raise(RuntimeError("x")))
    orch = _orch(primary, (shadow,))
    try:
        results = orch.tick()
        assert orch.wait_shadow(2)
        assert len(results) == 1
        (rec,) = list(orch._interject_engine.shadow_log)
        assert (rec.status, rec.shadow_mode, rec.agree) == ("error", None, False)
        assert orch.metrics["shadow.interject_error"] == 1
    finally:
        orch.close()

    shadow2 = _Ticker("s2", fn=lambda: (threading.Event().wait(0.2), [_ij(pid="s2")])[1])
    orch = _orch(_Ticker("t.tick", outputs=[[_ij()]]), (shadow2,), budget_ms=30)
    try:
        orch.tick()
        assert orch.wait_shadow(2)
        (rec,) = list(orch._interject_engine.shadow_log)
        assert rec.status == "late" and rec.agree
    finally:
        orch.close()


def test_primary_failure_is_logged_as_fallback_row():
    sink = _Sink()
    orch = _orch(_Ticker("t.tick", fn=lambda: _raise(RuntimeError("x"))))
    writer = _logged(orch, sink)
    try:
        assert orch.tick() == []
        assert writer.flush()
    finally:
        orch.close()
        writer.close()
    (row,) = sink.rows
    assert (row["kind"], row["role"], row["status"], row["fallback"], row["reason_code"]) == (
        KIND_TURN_INTERJECT, "primary", "error", True, "interject_error")


def test_non_ticking_primary_with_ticking_shadow_records_disagreement():
    # 預設主 proposer 不會 tick：shadow 若會插話就是不一致（primary 靜默列一併記錄）
    shadow = _Ticker("s.tick", outputs=[[_ij(pid="s.tick")]])
    orch = _orch(None, (shadow,))
    try:
        assert orch.tick() == []
        assert orch.wait_shadow(2)
        (rec,) = list(orch._interject_engine.shadow_log)
        assert (rec.primary_mode, rec.shadow_mode, rec.agree) == ("pass", "respond", False)
    finally:
        orch.close()


def test_shadow_busy_skips_without_blocking():
    gate = threading.Event()
    shadow = _Ticker("s.tick", fn=lambda: (gate.wait(2), [])[1])
    orch = _orch(_Ticker("t.tick"), (shadow,))
    try:
        orch.tick()
        orch.tick()  # 上一個 shadow tick 還在跑：跳過
        assert orch.metrics["shadow.tick_skipped_busy"] == 1
    finally:
        gate.set()
        orch.wait_shadow(2)
        orch.close()


def test_engine_standalone_registration_matches_orchestrator_wiring():
    registry = DecisionPointRegistry()
    hub = DecisionHub(registry)
    metrics: Counter[str] = Counter()
    reply = TurnReplyEngine(_Ticker("p"), registry=registry, metrics=metrics)
    hub.register(turn_reply_spec(300), reply)
    engine = TurnInterjectEngine(reply, registry=registry, metrics=metrics, submit=hub.submit, run_id="r")
    hub.register(turn_interject_spec(250), engine)
    assert engine.budget_s == 0.25
    engine.budget_s = None
    assert registry.get(KIND_TURN_INTERJECT).budget_ms == 0 and engine.budget_s is None
    reply.close()


def test_stuck_shadow_does_not_slow_primary_tick():
    import time

    gate = threading.Event()
    shadow = _Ticker("s.tick", fn=lambda: (gate.wait(5), [])[1])  # 卡住的 shadow tick
    orch = _orch(_Ticker("t.tick", outputs=[[_ij()], [_ij()], [_ij()]]), (shadow,))
    try:
        durations, counts = [], []
        for _ in range(3):
            start = time.perf_counter()
            counts.append(len(orch.tick()))
            durations.append(time.perf_counter() - start)
        assert counts == [1, 1, 1]  # primary 結果不變
        assert max(durations) < 0.25  # 遠小於 shadow 卡住的時間：不等 shadow
        assert orch.metrics["shadow.tick_skipped_busy"] == 2 and not orch.wait_shadow(0.05)
        assert orch.metrics["proposer.tick_busy"] == 0 and orch.metrics["proposer.tick_timeout"] == 0
    finally:
        gate.set()
        orch.wait_shadow(2)
        orch.close()


def test_log_sink_failure_does_not_affect_primary_and_only_warns(caplog):
    class _BadSink:
        def write(self, rows):
            raise OSError("disk full")

    import logging

    orch = _orch(_Ticker("t.tick", outputs=[[_ij()]]), (_Ticker("s.tick", outputs=[[_ij(pid="s.tick")]]),))
    writer = DecisionLogWriter(_BadSink(), flush_interval=0.01)
    writer.attach(orch.hub)
    try:
        with caplog.at_level(logging.WARNING):
            results = orch.tick()
            assert orch.wait_shadow(2)
            writer.flush()
        assert len(results) == 1  # primary 插話結果不受影響
        assert writer.metrics["decision_log.dropped"] >= 1
        assert any(r.levelno == logging.WARNING and "決策日誌寫入失敗" in r.getMessage() for r in caplog.records)
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    finally:
        orch.close()
        writer.close()
