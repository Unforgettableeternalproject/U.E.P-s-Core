"""決策中樞 P0 包裝（DL-1）：turn.reply 在包裝前後行為零變化（不需資料庫）。

``_legacy_propose`` 逐行複製包裝前 ``TurnOrchestrator._propose`` 的決策邏輯（不含 shadow），作為對照基準。
"""
from __future__ import annotations

import ast
import threading
from collections import Counter
from pathlib import Path

import pytest

from uep.decision import DecisionHub, DecisionPointRegistry, DecisionPointSpec, DecisionRequest
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
from uep.turn.proposer import ProposerGuard, RespondProposer, fallback_proposal, validate_proposal

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
    engine = TurnReplyEngine(RespondProposer(), budget_s=0.3, metrics=Counter())
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
    """contracts／registry／hub／__init__ 是葉節點（只可 import uep.decision）；只有 turn_reply 可以 import uep.turn。"""
    files = sorted(DECISION_DIR.glob("*.py"))
    assert {p.name for p in files} >= {"contracts.py", "registry.py", "hub.py", "turn_reply.py"}
    for path in files:
        imports = _uep_imports(path)
        assert not any(i.startswith("<relative") for i in imports), f"{path.name}: 不用相對 import"
        outside = {i for i in imports if not (i == "uep.decision" or i.startswith("uep.decision."))}
        if path.name == "turn_reply.py":
            allowed = {"uep.turn.models", "uep.turn.proposer"}
            assert outside <= allowed, f"{path.name}: {sorted(outside - allowed)}"
        else:
            assert not outside, f"{path.name}: 葉節點不得 import {sorted(outside)}"


def test_turn_package_only_uses_hub_public_modules():
    """uep.turn 只允許經 contracts／hub／turn_reply 使用中樞。"""
    allowed = {"uep.decision.contracts", "uep.decision.hub", "uep.decision.turn_reply"}
    for path in sorted((UEP_ROOT / "turn").glob("*.py")):
        bad = {i for i in _uep_imports(path) if i.startswith("uep.decision")} - allowed
        assert not bad, f"{path.name}: {sorted(bad)}"
