"""決策中樞 P0 包裝（DL-1）：turn.reply 在包裝前後行為零變化（不需資料庫）。

``_legacy_propose`` 逐行複製包裝前 ``TurnOrchestrator._propose`` 的決策邏輯（不含 shadow），作為對照基準。
"""
from __future__ import annotations

import ast
import dataclasses
import re
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from uep.decision import (
    DecisionHub,
    DecisionPointRegistry,
    DecisionPointSpec,
    DecisionRequest,
)
from uep.decision.turn_reply import (
    KIND_TURN_REPLY,
    TurnReplyEngine,
    proposal_to_result,
    result_to_proposal,
    turn_reply_spec,
)
from uep.turn.decision_config import DecisionConfig
from uep.turn.models import Proposal, ReplyMode, SessionContext, TurnCandidate
from uep.turn.orchestrator import ShadowRecord, TurnOrchestrator
from uep.turn.proposer import (
    ProposerGuard,
    RespondProposer,
    fallback_proposal,
    validate_proposal,
)

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")


def _cand(cid="cand1", *, addressed=False, count=1) -> TurnCandidate:
    return TurnCandidate(
        candidate_id=cid, utterance_refs=("e1",), speakers=("discord:1",), ctx=CTX, interaction_state="IDLE",
        interaction_version=1, presence_active=True, text="hi", participant_count=count, addressed_hint=addressed,
    )


class _P:
    def __init__(self, fn, pid="t.p"):
        self.fn, self.proposer_id = fn, pid

    def propose(self, candidate):
        return self.fn(candidate)


def _good(candidate):
    return Proposal(reply_mode=ReplyMode.PASS, addressed=False, confidence=0.4, route="agent_loop",
                    interaction_to="LISTENING", reason_code="ctx", proposer_id="t.p", proposer_version="9")


def _legacy_propose(proposer, candidate, budget_s, metrics: Counter, guard: ProposerGuard) -> Proposal:
    proposal, status = guard.call(proposer.propose, candidate, budget_s)
    if status == "busy":
        metrics["proposer.busy"] += 1
        return fallback_proposal(candidate, "proposer_busy")
    if status == "timeout":
        metrics["proposer.timeout"] += 1
        return fallback_proposal(candidate, "proposer_timeout")
    if status != "ok":
        metrics["proposer.error"] += 1
        return fallback_proposal(candidate, "proposer_error")
    if validate_proposal(proposal) is None:
        return proposal
    metrics["proposer.invalid"] += 1
    return fallback_proposal(candidate, "proposal_invalid")


def _orch(proposer=None, *, budget_ms=300, shadows=(), on_shadow=None) -> TurnOrchestrator:
    return TurnOrchestrator(
        None, None, auth_provider=lambda: None, proposer=proposer, shadow_proposers=shadows,  # type: ignore[arg-type]
        on_shadow=on_shadow, decision=DecisionConfig(proposer_budget_ms=budget_ms),
    )


def _boom(_c):
    raise RuntimeError("boom")


def _slow(c):
    threading.Event().wait(0.5)
    return _good(c)


def _bad_mode(_c):
    return Proposal(reply_mode="nope", reason_code="x")  # type: ignore[arg-type]


def _bad_route(_c):
    return Proposal(reply_mode=ReplyMode.RESPOND, route="evil")


def _bad_code(_c):
    return Proposal(reply_mode=ReplyMode.RESPOND, reason_code="bad code!")


def _bad_interaction(_c):
    return Proposal(reply_mode=ReplyMode.RESPOND, interaction_to="WORK")


def _not_a_proposal(_c):
    return "not a proposal"


@pytest.mark.parametrize("fn,budget_ms,key", [
    (_good, 300, None),
    (_boom, 300, "proposer.error"),
    (_slow, 30, "proposer.timeout"),
    (_bad_mode, 300, "proposer.invalid"),
    (_bad_route, 300, "proposer.invalid"),
    (_bad_code, 300, "proposer.invalid"),
    (_bad_interaction, 300, "proposer.invalid"),
    (_not_a_proposal, 300, "proposer.invalid"),
])
@pytest.mark.parametrize("addressed,count", [(False, 1), (True, 3), (False, 3)])
def test_propose_identical_before_and_after_wrapping(fn, budget_ms, key, addressed, count):
    cand = _cand(addressed=addressed, count=count)
    orch = _orch(_P(fn), budget_ms=budget_ms)
    legacy_metrics: Counter[str] = Counter()
    legacy_guard = ProposerGuard("legacy")
    try:
        expected = _legacy_propose(_P(fn), cand, budget_ms / 1000.0, legacy_metrics, legacy_guard)
        actual = orch._propose(cand)
    finally:
        legacy_guard.close()
        orch.close()
    assert actual == expected
    assert (actual.reason_code, actual.reply_mode) == (expected.reason_code, expected.reply_mode)
    assert dict(orch.metrics) == dict(legacy_metrics)
    if key:
        assert orch.metrics[key] == 1 and sum(orch.metrics.values()) == 1


def test_busy_fallback_counts_and_reason():
    release = threading.Event()
    started = threading.Event()

    def _stuck(c):
        started.set()
        release.wait(2)
        return _good(c)

    orch = _orch(_P(_stuck), budget_ms=30)
    try:
        first = orch._propose(_cand("a"))  # 逾時；執行緒仍卡住
        assert started.wait(1) and first.reason_code == "proposer_timeout"
        second = orch._propose(_cand("b", addressed=True, count=3))
        assert (second.reason_code, second.reply_mode) == ("proposer_busy", ReplyMode.RESPOND)
        assert orch.metrics["proposer.timeout"] == 1 and orch.metrics["proposer.busy"] == 1
    finally:
        release.set()
        orch.close()


def test_default_proposer_and_hub_result_shape():
    orch = _orch()
    try:
        assert orch.proposer.proposer_id == RespondProposer.proposer_id
        assert KIND_TURN_REPLY in orch.hub.registry.kinds()
        req = DecisionRequest(request_id="r1", kind=KIND_TURN_REPLY, native=_cand())
        result = orch.hub.decide(KIND_TURN_REPLY, req)
        assert result.outcome == "respond" and not result.fallback and result.engine.tier == "rule"
        assert result.plan is not None and result.plan.route == "direct_chat"
        assert result.plan.reply is not None and result.plan.reply.mode == "respond"
    finally:
        orch.close()


def test_fallback_result_marked_and_roundtrips():
    cand = _cand(count=3)
    orch = _orch(_P(_boom))
    try:
        result = orch.hub.decide(KIND_TURN_REPLY, DecisionRequest(request_id="r", kind=KIND_TURN_REPLY, native=cand))
    finally:
        orch.close()
    assert result.fallback and result.engine.tier == "fallback" and result.reason_code == "proposer_error"
    assert result.outcome == "passive"
    assert result_to_proposal(result) == fallback_proposal(cand, "proposer_error")


@pytest.mark.parametrize("proposal", [
    _good(None),
    Proposal(reply_mode=ReplyMode.RESPOND),
    Proposal(reply_mode=ReplyMode.DEFER, addressed=False, confidence=0.0, route="ignore",
             reason_code="a:b-c.d", proposer_id="x", proposer_version="1"),
])
def test_proposal_result_roundtrip_is_lossless(proposal):
    req = DecisionRequest(request_id="r", kind=KIND_TURN_REPLY)
    assert result_to_proposal(proposal_to_result(proposal, req)) == proposal


def test_shadow_records_unchanged():
    records: list[ShadowRecord] = []
    shadows = (
        _P(lambda c: Proposal(reply_mode=ReplyMode.RESPOND, reason_code="s1", proposer_id="sh.one"), "sh.one"),
        _P(_boom, "sh.two"),
        _P(lambda c: Proposal(reply_mode=ReplyMode.PASS, reason_code="s3", proposer_id="sh.three"), "sh.three"),
    )
    orch = _orch(_P(lambda c: Proposal(reply_mode=ReplyMode.RESPOND, reason_code="main", proposer_id="m")),
                 shadows=shadows, on_shadow=records.append)
    try:
        orch._propose(_cand("cx"))
        assert orch.wait_shadow(2)
    finally:
        orch.close()
    by_id = {r.shadow_id: r for r in orch.shadow_log}
    assert len(records) == 3 and set(by_id) == {"sh.one", "sh.three", "sh.two"}
    one, two, three = by_id["sh.one"], by_id["sh.two"], by_id["sh.three"]
    assert (one.candidate_id, one.primary_mode, one.primary_reason) == ("cx", "respond", "main")
    assert (one.shadow_mode, one.shadow_reason, one.agree, one.status) == ("respond", "s1", True, "ok")
    assert (two.shadow_mode, two.shadow_reason, two.agree, two.status) == (None, None, False, "error")
    assert (three.shadow_mode, three.agree, three.status) == ("pass", False, "ok")
    assert orch.metrics["shadow.agree"] == 1 and orch.metrics["shadow.disagree"] == 2
    assert orch.metrics["shadow.error"] == 1


def test_shadow_does_not_affect_primary_result():
    plain = _orch(_P(_good))
    shadowed = _orch(_P(_good), shadows=(_P(_boom),))
    try:
        assert plain._propose(_cand()) == shadowed._propose(_cand())
        shadowed.wait_shadow(2)
    finally:
        plain.close()
        shadowed.close()


def test_hub_rejects_unknown_kind_mismatch_and_duplicates():
    hub = DecisionHub()
    with pytest.raises(KeyError):
        hub.decide("nope", DecisionRequest(request_id="r", kind="nope"))
    engine = TurnReplyEngine(RespondProposer(), registry=hub.registry, metrics=Counter())
    try:
        hub.register(turn_reply_spec(300), engine)
        with pytest.raises(ValueError):
            hub.register(turn_reply_spec(300), engine)
        with pytest.raises(ValueError):
            hub.decide(KIND_TURN_REPLY, DecisionRequest(request_id="r", kind="other"))
        hub.registry.register(DecisionPointSpec(kind="x.async", mode="async"))
        with pytest.raises(ValueError):
            hub.decide("x.async", DecisionRequest(request_id="r", kind="x.async"))
        with pytest.raises(TypeError):
            hub.decide(KIND_TURN_REPLY, DecisionRequest(request_id="r", kind=KIND_TURN_REPLY))
    finally:
        engine.close()
    assert DecisionPointRegistry().kinds() == []


# ---- import 邊界 ----

UEP_ROOT = Path(__file__).resolve().parents[2] / "uep"
DECISION_DIR = UEP_ROOT / "decision"


def _uep_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names if a.name.split(".")[0] == "uep")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] == "uep":
                names.add(node.module)
                if node.module == "uep":
                    names.update(f"uep.{a.name}" for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            names.add(f"<relative:{node.level}>")
    return names


def test_decision_package_boundary():
    """contracts／registry／hub／log／replay／__init__ 是葉節點（只可 import uep.decision）；turn_reply、replay_turn 可 import uep.turn，db 可 import uep.db／uep.config。"""
    files = sorted(DECISION_DIR.glob("*.py"))
    assert {p.name for p in files} >= {"contracts.py", "registry.py", "hub.py", "turn_reply.py"}
    for path in files:
        imports = _uep_imports(path)
        assert not any(i.startswith("<relative") for i in imports), f"{path.name}: 不用相對 import"
        outside = {i for i in imports if not (i == "uep.decision" or i.startswith("uep.decision."))}
        allowed_outside = {
            "turn_reply.py": {"uep.turn.models", "uep.turn.proposer", "uep.ids"},
            # DL-P1：turn.interject 轉接層（與 turn_reply 共用 ShadowRecord／engine）
            "turn_interject.py": {"uep.turn.models", "uep.turn.proposer", "uep.ids"},
            # DL-3：DB 落盤與 turn.reply 離線重跑是葉節點之外的轉接層
            "db.py": {"uep.config", "uep.db.base", "uep.db.engine"},
            "replay_turn.py": {"uep.turn.models", "uep.turn.proposer", "uep.turn.registry", "uep.ids"},
            "loop_consult.py": {"uep.ids"},  # DL-C1：決定性 decision_id 用 derived_id
            # DL-P1c：memory.dream 包裝 DreamPlanner.decide（memory 端不反向 import decision）
            "memory_dream.py": {"uep.ids", "uep.memory.dream_planner"},
            "policy_publish.py": {"uep.ids"},  # DL-P1d：policy.snapshot／channel.mode（鴨子型別接 HostPolicySource）
            # agent.budget：比照 policy_publish 的 publish 模式，decision_id 用 new_id；不 import uep.agent／uep.echo
            "budget_events.py": {"uep.ids"},
            # schedule.abandon_work：訊號型別 AbandonWorkSignal 在零依賴的 contracts.schedule，decision_id／入口用 uep.ids
            "schedule_abandon_work.py": {"uep.contracts.schedule", "uep.ids"},
            "presence_absent_intent.py": {"uep.ids"},  # V18 後續：presence.absent_intent（不 import uep.turn／contracts.platform）
            # DL-6a：proactive.initiate（Agent 層只認 trigger；scope 驗證與 Presence 共用型別）
            "proactive.py": {
                "uep.agent.trigger", "uep.context.snapshot", "uep.contracts.presence", "uep.events.envelope", "uep.ids",
            },
        }
        if path.name in allowed_outside:
            allowed = allowed_outside[path.name]
            assert outside <= allowed, f"{path.name}: {sorted(outside - allowed)}"
        else:
            assert not outside, f"{path.name}: 葉節點不得 import {sorted(outside)}"


def test_turn_package_only_uses_hub_public_modules():
    """uep.turn 只允許經 contracts／hub／turn_reply／turn_interject 使用中樞。"""
    allowed = {
        "uep.decision.contracts", "uep.decision.hub", "uep.decision.turn_reply", "uep.decision.turn_interject",
        "uep.decision.presence_absent_intent",  # V18 後續：缺席端輸入的意圖判斷（presence.absent_intent）
        "uep.decision.schedule_abandon_work",  # 放棄工作判斷（schedule.abandon_work），turn／absent 路徑的 sink 接點
    }
    for path in sorted((UEP_ROOT / "turn").glob("*.py")):
        bad = {i for i in _uep_imports(path) if i.startswith("uep.decision")} - allowed
        assert not bad, f"{path.name}: {sorted(bad)}"


# ---- 搬移後仍須保持的既有行為（缺口測試）----

def _ok(_c):
    return Proposal(reply_mode=ReplyMode.RESPOND, reason_code="main", proposer_id="m")


def test_propose_after_close_falls_back_with_proposer_error():
    orch = _orch(_P(_ok))
    orch.close()
    result = orch._propose(_cand(addressed=True, count=3))
    assert (result.reason_code, result.reply_mode) == ("proposer_error", ReplyMode.RESPOND)
    assert orch.metrics["proposer.error"] == 1 and sum(orch.metrics.values()) == 1


@pytest.mark.parametrize("budget_ms", [0, -1])
def test_no_budget_waits_for_slow_proposer(budget_ms):
    orch = _orch(_P(_slow), budget_ms=budget_ms)
    try:
        assert orch._budget_s is None
        result = orch._propose(_cand())
    finally:
        orch.close()
    assert result.reason_code == "ctx" and result.proposer_id == "t.p"  # _slow 等 0.5s 仍採用結果
    assert not orch.metrics


def test_no_budget_shadow_is_never_late():
    orch = _orch(_P(_ok), budget_ms=0, shadows=(_P(_slow, "sh.slow"),))
    try:
        orch._propose(_cand())
        assert orch.wait_shadow(3)
    finally:
        orch.close()
    [rec] = list(orch.shadow_log)
    assert rec.status == "ok" and orch.metrics["shadow.late"] == 0


def test_shadow_still_running_skips_next_call():
    release = threading.Event()

    def _stuck(c):
        release.wait(3)
        return _ok(c)

    orch = _orch(_P(_ok), shadows=(_P(_stuck, "sh.stuck"),))
    try:
        orch._propose(_cand("a"))
        orch._propose(_cand("b"))
        assert orch.metrics["shadow.skipped_busy"] == 1
        assert not orch.wait_shadow(0.05)
    finally:
        release.set()
        assert orch.wait_shadow(3)
        orch.close()
    assert [r.candidate_id for r in orch.shadow_log] == ["a"]


def test_shadow_late_status():
    def _a_bit_slow(c):
        threading.Event().wait(0.15)
        return _ok(c)

    orch = _orch(_P(_ok), budget_ms=30, shadows=(_P(_a_bit_slow, "sh.late"),))
    try:
        orch._propose(_cand())
        assert orch.wait_shadow(3)
    finally:
        orch.close()
    [rec] = list(orch.shadow_log)
    assert (rec.status, rec.shadow_mode, rec.agree) == ("late", "respond", True)
    assert rec.elapsed_ms >= 30 and orch.metrics["shadow.late"] == 1 and orch.metrics["shadow.agree"] == 1


def test_on_shadow_exception_does_not_affect_other_shadows(caplog):
    calls: list[str] = []

    def _bad_callback(record):
        calls.append(record.shadow_id)
        raise RuntimeError("callback boom")

    shadows = (_P(_ok, "sh.one"), _P(_ok, "sh.two"))
    orch = _orch(_P(_ok), shadows=shadows, on_shadow=_bad_callback)
    try:
        with caplog.at_level("ERROR", logger="uep.decision.turn_reply"):
            primary = orch._propose(_cand())
            assert orch.wait_shadow(3)
            # wait_shadow 以 future 完成為準，done callback（含 log）可能稍晚才跑完：輪詢等候
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and sum("on_shadow" in r.getMessage() for r in caplog.records) < 2:
                time.sleep(0.01)
    finally:
        orch.close()
    assert primary.reason_code == "main"
    assert len(calls) == 2  # 第一個回呼丟例外後，第二個 shadow 的回呼仍被呼叫
    assert len(orch.shadow_log) == 2 and orch.metrics["shadow.agree"] == 2
    assert any("on_shadow" in r.getMessage() for r in caplog.records)


def test_wait_shadow_times_out_then_completes():
    release = threading.Event()

    def _stuck(c):
        release.wait(3)
        return _ok(c)

    orch = _orch(_P(_ok), shadows=(_P(_stuck, "sh.stuck"),))
    try:
        orch._propose(_cand())
        assert orch.wait_shadow(0.05) is False
        release.set()
        assert orch.wait_shadow(3) is True
    finally:
        release.set()
        orch.close()


# ---- 重設語意：Orchestrator 屬性與 engine 只有單一份狀態 ----

def test_reassigning_proposer_after_construction_takes_effect():
    orch = _orch(_P(_ok))
    try:
        assert orch._propose(_cand()).reason_code == "main"
        orch._proposer = _P(_good)  # 與 test_p3_s5_slot 相同的做法
        assert orch.proposer.proposer_id == "t.p"
        after = orch._propose(_cand())
        assert (after.reason_code, after.reply_mode) == ("ctx", ReplyMode.PASS)
    finally:
        orch.close()


def test_reassigning_budget_after_construction_takes_effect():
    orch = _orch(_P(_slow), budget_ms=0)
    try:
        orch._budget_s = 0.03
        assert orch._propose(_cand()).reason_code == "proposer_timeout"
        assert orch.metrics["proposer.timeout"] == 1
    finally:
        orch.close()


def test_orchestrator_attributes_share_state_with_engine():
    seen: list[ShadowRecord] = []
    orch = _orch(_P(_ok), shadows=(_P(_ok, "sh.one"),))
    engine = orch._reply_engine
    try:
        assert orch._guard is engine.guard and orch._shadows is engine.shadows
        assert orch.shadow_log is engine.shadow_log and orch._proposer is engine.proposer
        orch._on_shadow = seen.append
        assert engine.on_shadow == seen.append
        orch._propose(_cand())
        assert orch.wait_shadow(3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not seen:
            time.sleep(0.01)
    finally:
        orch.close()
    assert len(seen) == 1 and len(orch.shadow_log) == 1


class _Full:
    """實作所有選用 Port 的 proposer，記錄每個入口被呼叫的次數。"""

    proposer_id = "full.p"

    def __init__(self):
        self.calls: Counter[str] = Counter()

    def propose(self, candidate):
        self.calls["propose"] += 1
        return Proposal(reply_mode=ReplyMode.RESPOND, reason_code="full", proposer_id="full.p")

    def tick(self):
        self.calls["tick"] += 1
        return []

    def is_listener_continuation(self, text):
        self.calls["listener"] += 1
        return False

    def on_audience_changed(self):
        self.calls["audience"] += 1

    def on_turn_started(self, turn_id, candidate_id, speakers):
        self.calls["started"] += 1

    def on_turn_outcome(self, facts):
        self.calls["outcome"] += 1


def test_replaced_proposer_is_the_single_target_of_decide_hooks_and_tick():
    from uep.turn.models import UtteranceFact

    old, new = _Full(), _Full()
    orch = _orch(old)
    try:
        orch._proposer = new
        orch._ctx = CTX  # 讓 tick 走到 proposer.tick（不需 DB，aggregator 預設逐句通過）
        orch._propose(_cand())
        assert orch.tick() == []
        orch._listener_yield(UtteranceFact(speaker_ref="discord:1", text="hi", seq=1))
        orch._feedback("on_turn_started", "t1", "cand1", ("discord:1",))
        orch.clear_aggregation()
        assert orch.proposer is new and orch._reply_engine.proposer is new
    finally:
        orch.close()
    assert dict(new.calls) == {"propose": 1, "tick": 1, "listener": 1, "started": 1, "audience": 1}
    assert not old.calls


# ---- P1 前置：decision_id 唯一、預算單一來源、hub 注入、native 限制 ----


def test_decision_id_is_unique_per_decide_and_request_id_kept():
    orch = _orch()
    try:
        req = DecisionRequest(request_id="same", kind=KIND_TURN_REPLY, native=_cand())
        first = orch.hub.decide(KIND_TURN_REPLY, req)
        second = orch.hub.decide(KIND_TURN_REPLY, req)
    finally:
        orch.close()
    assert first.request_id == second.request_id == "same"
    assert first.decision_id != second.decision_id
    assert re.fullmatch(r"dec_[0-9A-HJKMNP-TV-Z]{26}", first.decision_id)  # 與 ID 格式（前綴_ULID）一致


def test_budget_and_reasons_have_single_source_in_spec():
    orch = _orch(_P(_ok), budget_ms=300)
    try:
        spec = orch.hub.registry.get(KIND_TURN_REPLY)
        assert (spec.budget_ms, spec.on_timeout_reason, spec.on_busy_reason) == (300, "proposer_timeout", "proposer_busy")
        assert (spec.on_error_reason, spec.on_invalid_reason) == ("proposer_error", "proposal_invalid")
        assert orch._budget_s == pytest.approx(0.3)
        orch._budget_s = 0.05  # 寫入轉到 spec
        assert orch.hub.registry.get(KIND_TURN_REPLY).budget_ms == 50
        orch._budget_s = None
        assert orch.hub.registry.get(KIND_TURN_REPLY).budget_ms == 0 and orch._budget_s is None
        assert "budget_s" not in vars(orch._reply_engine)  # engine 不另存預算
    finally:
        orch.close()


def test_engine_fallback_reasons_come_from_spec():
    orch = _orch(_P(_boom))
    try:
        hub = orch.hub
        hub.registry.register(
            dataclasses.replace(hub.registry.get(KIND_TURN_REPLY), on_error_reason="custom_error"), replace=True
        )
        result = orch._propose(_cand(count=3))
    finally:
        orch.close()
    assert result.reason_code == "custom_error"
    assert orch.metrics["proposer.error"] == 1  # 計數鍵不隨 reason 改變


def test_injected_hub_gets_turn_reply_registered():
    hub = DecisionHub()
    orch = TurnOrchestrator(None, None, auth_provider=lambda: None, decision_hub=hub)  # type: ignore[arg-type]
    try:
        assert orch.hub is hub and KIND_TURN_REPLY in hub.registry.kinds()
        assert orch._propose(_cand()).reply_mode == ReplyMode.RESPOND
    finally:
        orch.close()


def test_injected_hub_rejects_duplicate_turn_reply_registration():
    hub = DecisionHub()
    first = TurnOrchestrator(None, None, auth_provider=lambda: None, decision_hub=hub)  # type: ignore[arg-type]
    try:
        with pytest.raises(ValueError):
            TurnOrchestrator(None, None, auth_provider=lambda: None, decision_hub=hub)  # type: ignore[arg-type]
        assert first._propose(_cand()).reply_mode == ReplyMode.RESPOND  # 原註冊未被破壞
    finally:
        first.close()


def test_native_only_accepted_by_allow_native_spec():
    orch = _orch()
    try:
        hub = orch.hub
        assert hub.registry.get(KIND_TURN_REPLY).allow_native
        hub.register(DecisionPointSpec(kind="x.plain", mode="sync"), orch._reply_engine)
        with pytest.raises(ValueError, match="native"):
            hub.decide("x.plain", DecisionRequest(request_id="r", kind="x.plain", native=_cand()))
        # 無 native 的請求不受此限制（交給 engine 自己處理）
        with pytest.raises(TypeError):
            hub.decide("x.plain", DecisionRequest(request_id="r", kind="x.plain"))
    finally:
        orch.close()
