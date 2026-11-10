"""DL-4 PR1：DecisionPlan.capability 區段、Proposal.capability_hint、雙向轉換與 plan_sections 白名單。"""
from __future__ import annotations

import json
from collections import Counter

import pytest

from uep.decision import DecisionHub, DecisionPlan, DecisionReply, DecisionRequest
from uep.decision.contracts import (
    DecisionCapability,
    DecisionEngineInfo,
    DecisionResult,
    plan_from_dict,
    plan_to_dict,
)
from uep.decision.hub import REASON_PLAN_SECTION_REJECTED
from uep.decision.registry import DecisionPointSpec
from uep.decision.turn_reply import (
    KIND_TURN_REPLY,
    TurnReplyEngine,
    proposal_to_result,
    result_to_proposal,
    turn_reply_spec,
)
from uep.turn.models import Proposal, ReplyMode, SessionContext, TurnCandidate
from uep.turn.proposer import validate_proposal

CTX = SessionContext(conversation_id="c1", segment_id="s1", audience_scope="aud", session_ref="sess")


def _cand() -> TurnCandidate:
    return TurnCandidate(
        candidate_id="cand1", utterance_refs=("e1",), speakers=("discord:1",), ctx=CTX, interaction_state="IDLE",
        interaction_version=1, presence_active=True, text="hi", participant_count=1, addressed_hint=True,
    )


def _request() -> DecisionRequest:
    return DecisionRequest(request_id="r1", kind=KIND_TURN_REPLY, native=_cand())


# ---- 序列化往返 ----

def test_old_plan_roundtrip_unchanged():
    legacy = {"route": "direct_chat", "reply": {"mode": "respond", "interaction_to": "u1", "addressed": True}}
    plan = plan_from_dict(legacy)
    assert plan.capability is None
    assert plan_to_dict(plan) == legacy
    assert "capability" not in plan_to_dict(DecisionPlan(route="ignore"))


def test_none_and_empty_hint_are_distinct_through_json():
    none_plan = DecisionPlan(capability=DecisionCapability())
    empty_plan = DecisionPlan(capability=DecisionCapability(capability_hint=()))
    for plan in (none_plan, empty_plan):
        assert plan_from_dict(json.loads(json.dumps(plan_to_dict(plan)))) == plan
    assert none_plan != empty_plan
    assert plan_from_dict(json.loads(json.dumps(plan_to_dict(empty_plan)))).capability.capability_hint == ()
    assert plan_from_dict(json.loads(json.dumps(plan_to_dict(none_plan)))).capability.capability_hint is None


def test_hint_roundtrip_with_other_sections():
    plan = DecisionPlan(
        route="agent_loop", reply=DecisionReply(mode="respond"),
        capability=DecisionCapability(capability_hint=("memory.read", "web.fetch")),
    )
    assert plan_from_dict(json.loads(json.dumps(plan_to_dict(plan)))) == plan
    assert plan_to_dict(plan)["capability"] == {"capability_hint": ("memory.read", "web.fetch")}


def test_unknown_keys_in_capability_section_ignored():
    plan = plan_from_dict({"capability": {"capability_hint": ["memory.read"], "grant": ["x"]}})
    assert plan.capability == DecisionCapability(capability_hint=("memory.read",))


@pytest.mark.parametrize("bad", ["memory.read", "", b"x", {"memory.read"}, 3, [1], ["memory.read", None], [["a"]]])
def test_invalid_hint_makes_section_invalid(bad):
    with pytest.raises(ValueError):
        plan_from_dict({"capability": {"capability_hint": bad}})
    with pytest.raises(ValueError):
        DecisionCapability(capability_hint=bad)


# ---- Proposal 與雙向轉換 ----

def test_proposal_default_hint_is_none_and_p0_plan_unchanged():
    p = Proposal(reply_mode=ReplyMode.RESPOND)
    assert p.capability_hint is None
    result = proposal_to_result(p, _request())
    assert result.plan.capability is None
    assert set(plan_to_dict(result.plan)) == {"route", "reply"}
    assert result_to_proposal(result) == p


@pytest.mark.parametrize("hint", [None, (), ("memory.read",), ("memory.read", "bogus.tag")])
def test_proposal_result_roundtrip_keeps_hint_and_p0_fields(hint):
    base = Proposal(reply_mode=ReplyMode.PASSIVE, addressed=False, confidence=0.4, route="agent_loop",
                    interaction_to="LISTENING", reason_code="ctx", proposer_id="t.p", proposer_version="9")
    with_hint = Proposal(**{**base.__dict__, "capability_hint": hint})
    r0, r1 = proposal_to_result(base, _request()), proposal_to_result(with_hint, _request())
    assert (r1.outcome, r1.reason_code, r1.confidence) == (r0.outcome, r0.reason_code, r0.confidence)
    assert result_to_proposal(r1) == with_hint
    assert result_to_proposal(r1).capability_hint == hint


# ---- validate_proposal ----

@pytest.mark.parametrize("hint", ["memory.read", ("memory.read", 1), (None,), {"memory.read"}, 5])
def test_validate_proposal_rejects_malformed_hint(hint):
    assert validate_proposal(Proposal(reply_mode=ReplyMode.RESPOND, capability_hint=hint)) == "capability_hint"


@pytest.mark.parametrize("hint", [None, (), ("memory.read",), ("not.in.table", "x")])
def test_validate_proposal_accepts_string_tuples_including_unknown_tags(hint):
    assert validate_proposal(Proposal(reply_mode=ReplyMode.RESPOND, capability_hint=hint)) is None


# ---- engine 與 hub 白名單 ----

class _FixedProposer:
    proposer_id = "t.fixed"

    def __init__(self, proposal: Proposal) -> None:
        self.proposal = proposal

    def propose(self, candidate):
        return self.proposal


def _hub_with(proposal: Proposal) -> tuple[DecisionHub, Counter]:
    hub = DecisionHub()
    metrics: Counter = Counter()
    engine = TurnReplyEngine(_FixedProposer(proposal), registry=hub.registry, metrics=metrics)
    hub.register(turn_reply_spec(300), engine)
    return hub, metrics


def test_turn_reply_engine_passes_hint_and_outcome_unchanged():
    plain = Proposal(reply_mode=ReplyMode.PASSIVE, route="agent_loop", reason_code="ctx", proposer_id="t.p")
    hinted = Proposal(**{**plain.__dict__, "capability_hint": ("memory.read",)})
    hub0, m0 = _hub_with(plain)
    hub1, m1 = _hub_with(hinted)
    r0, r1 = hub0.decide(KIND_TURN_REPLY, _request()), hub1.decide(KIND_TURN_REPLY, _request())
    assert (r1.outcome, r1.reason_code, r1.fallback) == (r0.outcome, r0.reason_code, r0.fallback) == ("passive", "ctx", False)
    assert r1.plan.capability == DecisionCapability(capability_hint=("memory.read",))
    assert r0.plan.capability is None
    assert m0 == m1 == Counter()


def test_malformed_hint_falls_back_to_default_and_counts_invalid():
    hub, metrics = _hub_with(Proposal(reply_mode=ReplyMode.PASSIVE, capability_hint="memory.read"))  # type: ignore[arg-type]
    result = hub.decide(KIND_TURN_REPLY, _request())
    assert result.fallback and result.reason_code == "proposal_invalid" and result.plan.capability is None
    assert metrics["proposer.invalid"] == 1


class _CapabilityEngine:
    def decide(self, request):
        return DecisionResult(
            decision_id="d", request_id=request.request_id, kind="x.k", outcome="ok",
            plan=DecisionPlan(capability=DecisionCapability(capability_hint=("memory.read",))),
            confidence=1.0, reason_code="r", engine=DecisionEngineInfo(tier="rule", id="e", version="1"),
        )


def test_plan_sections_whitelist_strips_undeclared_capability():
    hub = DecisionHub()
    hub.register(DecisionPointSpec(kind="x.k", outcomes=("ok",)), _CapabilityEngine())
    result = hub.decide("x.k", DecisionRequest(request_id="r", kind="x.k"))
    assert result.plan.capability is None and result.fallback and result.reason_code == REASON_PLAN_SECTION_REJECTED
    hub2 = DecisionHub()
    hub2.register(DecisionPointSpec(kind="x.k", outcomes=("ok",), plan_sections=("capability",)), _CapabilityEngine())
    ok = hub2.decide("x.k", DecisionRequest(request_id="r", kind="x.k"))
    assert ok.plan.capability is not None and not ok.fallback


def test_turn_reply_spec_declares_capability_and_proactive_does_not():
    assert turn_reply_spec(300).plan_sections == ("capability",)
    from uep.decision.proactive import proactive_initiate_spec

    assert "capability" not in proactive_initiate_spec().plan_sections  # D4-2：Proactive 不吃 hint
