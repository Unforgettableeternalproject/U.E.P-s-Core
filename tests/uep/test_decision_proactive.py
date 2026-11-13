"""DL-6a：proactive.initiate 契約、9 關規則仲裁、reason_code 覆蓋、to_loop_trigger、shadow 日誌與「無消費者」鎖。

離線部分不需 DB；DB 落盤測試（最後一節）需要 UEP_TEST_DATABASE_URL（未設定時 skip）。
"""
from __future__ import annotations

import ast
import json
from dataclasses import fields, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.uep.test_decision_log import ListSink
from uep.agent.trigger import LoopTrigger
from uep.contracts.presence import PresenceLocation, PresenceStatus
from uep.decision import DecisionHub, DecisionPlan
from uep.decision.contracts import DecisionEngineInfo, DecisionPolicy, DecisionResult
from uep.decision.log import COLUMNS, DecisionLogWriter, primary_row
from uep.decision.proactive import (
    FALLBACK_REASONS,
    GATES,
    GOAL_CODES,
    GOAL_JOIN_VOICE,
    KIND_PROACTIVE_INITIATE,
    LT_BAD_TRIGGER,
    LT_CAPABILITY_EMPTY,
    LT_NO_LOOP,
    LT_NOT_READY,
    LT_WOULD_START,
    ON_BUSY_REASON,
    ON_ERROR_REASON,
    ON_INVALID_REASON,
    ON_TIMEOUT_REASON,
    REASON_CODES,
    GrantFact,
    ProactiveCandidate,
    ProactiveContext,
    ProactiveEngine,
    ProactiveInjections,
    SourceKind,
    Urgency,
    arbitrate,
    build_request,
    candidate_from_facts,
    candidate_to_facts,
    context_from_facts,
    context_to_facts,
    loop_trigger_decision,
    proactive_initiate_spec,
    register_proactive,
    to_loop_trigger,
)
from uep.decision.replay import analyze
from uep.decision.replay_proactive import FIXED_NOW, rerun_records, run_records

FIXTURE = Path(__file__).parent / "fixtures" / "proactive_eval" / "candidates.jsonl"
SECRET = "這是不該進日誌的原文"
NOW = FIXED_NOW


def _cand(**over) -> ProactiveCandidate:
    base = {
        "candidate_id": "pcand_01HZ", "trigger_event_ids": ("evt_1",), "rule_version": "rv1", "goal_ref": "remind_due",
        "evidence_refs": (), "memory_salience": 0.6, "urgency": "normal", "confidence": 0.8,
        "privacy_scope": "scope_private", "expires_at": NOW + timedelta(hours=1), "dedupe_key": "remind:t1:o",
        "source_kind": "schedule", "owner_actor_id": "actor_owner", "created_at": NOW - timedelta(minutes=5),
        "authorization_ref": "grant_1",
    }
    return ProactiveCandidate(**{**base, **over})


_GRANT = GrantFact(
    valid=True, scope_refs=("scope_private",), locations=(PresenceLocation.DESKTOP, PresenceLocation.DISCORD),
)


def _ctx(**over) -> ProactiveContext:
    base = {
        "now": NOW, "owner_actor_id": "actor_owner", "audience_scope": "scope_private",
        "presence_status": PresenceStatus.ACTIVE, "current_platform": PresenceLocation.DESKTOP,
        "private_locations": (PresenceLocation.DESKTOP,), "grant": _GRANT,
    }
    return ProactiveContext(**{**base, **over})


def _records() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---- 契約驗證 ----

@pytest.mark.parametrize("over", [
    {"expires_at": datetime(2026, 11, 10, 13, 0)},  # naive
    {"created_at": datetime(2026, 11, 10, 13, 0)},
    {"privacy_scope": "private"}, {"privacy_scope": "scope_secret"},
    {"dedupe_key": "has space"}, {"dedupe_key": SECRET}, {"candidate_id": ""}, {"owner_actor_id": "x" * 129},
    {"trigger_event_ids": ()}, {"trigger_event_ids": ("ok", "bad id")}, {"evidence_refs": "evt_1"},
    {"urgency": "urgent"}, {"source_kind": "email"},
    {"memory_salience": 1.2}, {"confidence": -0.1}, {"confidence": True}, {"interruptibility_cost": 2},
    {"desired_location": "mars"}, {"goal_ref": "free text goal"}, {"goal_ref": GOAL_JOIN_VOICE},
    {"decision_reason": "Has Upper"}, {"authorization_ref": "bad ref"},
    {"urgency": "critical"},  # 沒有 policy 來源標記
    {"urgency": "critical", "urgency_source": "event"}, {"urgency_source": "llm"},
])
def test_candidate_rejects_invalid_values(over):
    with pytest.raises(ValueError):
        _cand(**over)


def test_candidate_accepts_critical_from_policy_and_coerces_enums():
    c = _cand(urgency="critical", urgency_source="policy", source_kind="assignment", desired_location="discord",
              trigger_event_ids=["evt_1", "evt_2"])
    assert c.urgency is Urgency.CRITICAL and c.source_kind is SourceKind.ASSIGNMENT
    assert c.desired_location is PresenceLocation.DISCORD and c.trigger_event_ids == ("evt_1", "evt_2")


def test_goal_code_set_is_closed():
    assert GOAL_CODES == {
        "remind_due", "report_task_done", "request_presence_move", "assignment_pickup", "memory_followup", "join_voice",
    }


def test_candidate_and_context_facts_roundtrip():
    c = _cand(desired_location="discord", decision_reason="muted")
    assert candidate_from_facts(json.loads(json.dumps(candidate_to_facts(c)))) == c
    ctx = _ctx(ledger=(), private_locations=(PresenceLocation.DESKTOP,))
    assert context_from_facts(json.loads(json.dumps(context_to_facts(ctx)))) == ctx


def test_candidate_facts_whitelist_has_no_raw_text_fields():
    """欄位白名單：候選只有 ID／列舉／數值／時間，沒有原文、summary、Audience、Presence。"""
    assert {f.name for f in fields(ProactiveCandidate)} == {
        "candidate_id", "trigger_event_ids", "rule_version", "goal_ref", "evidence_refs", "memory_salience", "urgency",
        "confidence", "privacy_scope", "expires_at", "dedupe_key", "source_kind", "owner_actor_id", "created_at",
        "interruptibility_cost", "desired_location", "decision_reason", "authorization_ref", "urgency_source",
    }
    assert set(candidate_to_facts(_cand())) == {f.name for f in fields(ProactiveCandidate)}


@pytest.mark.parametrize("bad", [
    {"now": datetime(2026, 11, 10)}, {"audience_scope": "x"}, {"activity_cost": 3}, {"budget_used": -1},
    {"presence_status": "WEIRD"},
])
def test_context_rejects_invalid_values(bad):
    with pytest.raises(ValueError):
        _ctx(**bad)


# ---- fixture 與 reason_code 覆蓋 ----

def test_fixture_has_enough_records_and_each_matches_expectation():
    records = _records()
    assert len(records) >= 12
    mismatched = []
    for record, request, result in run_records(records):
        e = record["expect"]
        got = (result.outcome, result.reason_code, request.facts.get("sub_kind"))
        if got != (e["outcome"], e["reason_code"], e["gate"]) or result.fallback:
            mismatched.append((record["id"], got, e))
    assert not mismatched, mismatched


def test_reason_codes_total_29_and_unique():
    assert len(REASON_CODES) == 29 and len(set(REASON_CODES)) == 29
    assert set(FALLBACK_REASONS) == {ON_TIMEOUT_REASON, ON_BUSY_REASON, ON_ERROR_REASON, ON_INVALID_REASON}
    assert all(r == r.lower() and r.replace("_", "").isalnum() for r in REASON_CODES)


def test_fixture_covers_every_non_fallback_reason_code_and_every_gate():
    seen = {r["expect"]["reason_code"] for r in _records()}
    # target_not_movable 需要非可移轉場域（console）才觸發，fixture 不收；由專屬測試覆蓋
    assert seen == set(REASON_CODES) - set(FALLBACK_REASONS) - {"target_not_movable"}
    by_gate: dict[str, set[str]] = {}
    for r in _records():
        by_gate.setdefault(r["expect"]["gate"], set()).add(r["expect"]["outcome"])
    for gate in GATES:
        assert gate in by_gate, gate
    for gate in GATES[:-1]:  # 前 9 關各自至少有一筆 reject 或 defer
        assert by_gate[gate] & {"rejected", "deferred"}, gate
    assert "ready" in by_gate["ready"]


def test_fixture_rerun_rows_have_log_shape_and_no_text():
    rows = rerun_records(_records())
    assert len(rows) == len(_records())
    assert all(set(r) == set(COLUMNS) for r in rows)
    assert {r["sub_kind"] for r in rows} == set(GATES)
    assert all(r["candidate_id"].startswith("pcand_") and r["namespace"] == "ns_main" for r in rows)
    report = analyze(rows)
    assert report["total_rows"] == len(rows)


# ---- 每一關 ----

def test_g0_expired_boundary_is_inclusive():
    assert arbitrate(_cand(expires_at=NOW), _ctx()).reason_code == "candidate_expired"
    assert arbitrate(_cand(expires_at=NOW + timedelta(seconds=1)), _ctx()).reason_code == "ready_present"


def test_g1_stop_beats_everything_including_assignment_and_expired_precedes_it():
    a = _cand(source_kind="assignment", goal_ref="assignment_pickup", authorization_ref=None)
    assert arbitrate(a, _ctx(global_stop=True, grant=None)).reason_code == "global_stop"
    assert arbitrate(_cand(expires_at=NOW), _ctx(global_stop=True)).reason_code == "candidate_expired"
    revoked = replace(_GRANT, revoked=True)
    assert arbitrate(_cand(), _ctx(grant=revoked, muted=True, audience_scope="scope_public")).reason_code == (
        "authorization_revoked")


def test_g2_authorization_rules():
    assert arbitrate(_cand(authorization_ref=None), _ctx()).reason_code == "no_authorization"
    assert arbitrate(_cand(), _ctx(grant=None)).reason_code == "no_authorization"
    assert arbitrate(_cand(), _ctx(grant=replace(_GRANT, valid=False))).reason_code == "no_authorization"
    assert arbitrate(_cand(privacy_scope="scope_group_a"), _ctx()).reason_code == "authorization_scope_insufficient"
    no_channel = replace(_GRANT, locations=(PresenceLocation.DISCORD,))
    assert arbitrate(_cand(), _ctx(grant=no_channel)).reason_code == "authorization_scope_insufficient"
    # assignment 只豁免授權關卡，且 owner 必須是已知 owner
    a = {"source_kind": "assignment", "goal_ref": "assignment_pickup", "authorization_ref": None}
    assert arbitrate(_cand(**a), _ctx(grant=None)).outcome == "ready"
    assert arbitrate(_cand(owner_actor_id="actor_other", **a), _ctx(grant=None)).reason_code == "no_authorization"


def test_hard_gates_beat_high_scores():
    hot = {"memory_salience": 1.0, "urgency": "high", "confidence": 1.0}
    assert arbitrate(_cand(authorization_ref=None, **hot), _ctx()).reason_code == "no_authorization"
    assert arbitrate(_cand(**hot), _ctx(audience_scope="scope_public")).outcome == "deferred"
    assert arbitrate(_cand(**hot), _ctx(transfer_pending=True)).reason_code == "presence_transferring"
    assert arbitrate(_cand(**hot), _ctx(activity_interruptible=False)).reason_code == "activity_not_interruptible"


def test_g3_audience_move_vs_wait_and_placement():
    v = arbitrate(_cand(), _ctx(audience_scope="scope_public"))
    assert (v.outcome, v.reason_code, v.gate) == ("deferred", "audience_too_wide_move", "audience")
    assert v.plan.placement.presence_intent == "request_move" and v.plan.placement.target_platform == "desktop"
    assert v.plan.placement.audience_scope_max == "scope_private"
    w = arbitrate(_cand(), _ctx(audience_scope="scope_public", private_locations=()))
    assert w.reason_code == "audience_too_wide_wait" and w.plan.placement is None
    assert w.plan.followups[0].kind == KIND_PROACTIVE_INITIATE
    # 同級但歸屬不同的 group 也不放行
    g = arbitrate(
        _cand(privacy_scope="scope_group_a"),
        _ctx(audience_scope="scope_group_b", grant=replace(_GRANT, scope_refs=("scope_group_a",))),
    )
    assert g.reason_code in {"audience_too_wide_move", "audience_too_wide_wait"}


def test_g4_presence_branches():
    assert arbitrate(_cand(), _ctx(presence_status=PresenceStatus.RECOVERING)).reason_code == "presence_transferring"
    queued = arbitrate(_cand(), _ctx(presence_status=PresenceStatus.NONE, current_platform=None))
    assert queued.reason_code == "presence_none_queued" and queued.plan.followups[0].after_ms == 60_000
    pre = arbitrate(
        _cand(desired_location="desktop"),
        _ctx(presence_status=PresenceStatus.NONE, current_platform=None, grant=replace(_GRANT, may_choose_location=True)),
    )
    assert (pre.outcome, pre.reason_code) == ("ready", "presence_none_preauthorized")
    assert pre.plan.placement.target_platform == "desktop"
    mismatch = arbitrate(_cand(desired_location="discord"), _ctx())
    assert mismatch.reason_code == "presence_mismatch_move" and mismatch.plan.placement.presence_intent == "request_move"
    moved = arbitrate(_cand(desired_location="discord"), _ctx(grant=replace(_GRANT, may_move=True)))
    assert moved.reason_code == "ready_move" and moved.plan.placement.target_platform == "discord"


def test_none_preauthorized_still_passes_later_gates():
    ctx = _ctx(presence_status=PresenceStatus.NONE, current_platform=None,
               grant=replace(_GRANT, may_choose_location=True), muted=True)
    assert arbitrate(_cand(desired_location="desktop"), ctx).reason_code == "muted"


def test_g5_cost_rules():
    assert arbitrate(_cand(), _ctx(muted=True)).reason_code == "muted"
    assert arbitrate(_cand(), _ctx(in_committed_interaction=True)).reason_code == "in_committed_interaction"
    crit = _cand(urgency="critical", urgency_source="policy")
    # critical 不略過不可中斷，但略過「價值 < 成本」
    assert arbitrate(crit, _ctx(activity_interruptible=False)).reason_code == "activity_not_interruptible"
    assert arbitrate(crit, _ctx(activity_cost=1.0)).outcome == "ready"
    low = _cand(memory_salience=0.1, urgency="low", confidence=0.6)
    assert arbitrate(low, _ctx(activity_cost=0.9)).reason_code == "cost_exceeds_value"
    assert arbitrate(low, _ctx(activity_cost=0.1)).outcome == "ready"


def test_g6_cooldown_and_dedupe():
    from uep.decision.proactive import LedgerEntry

    other = LedgerEntry("remind:x", "actor_owner", NOW - timedelta(seconds=60))
    v = arbitrate(_cand(), _ctx(ledger=(other,)))
    assert v.reason_code == "cooldown_active" and v.plan.followups[0].after_ms == 540_000
    old = LedgerEntry("remind:x", "actor_owner", NOW - timedelta(seconds=601))
    assert arbitrate(_cand(), _ctx(ledger=(old,))).outcome == "ready"
    elsewhere = LedgerEntry("remind:x", "actor_other", NOW - timedelta(seconds=5))
    assert arbitrate(_cand(), _ctx(ledger=(elsewhere,))).outcome == "ready"
    dup = LedgerEntry("remind:t1:o", "actor_owner", NOW - timedelta(hours=3))
    assert arbitrate(_cand(), _ctx(ledger=(dup,))).reason_code == "duplicate_delivered"
    unknown = replace(dup, ack="unknown")
    assert arbitrate(_cand(), _ctx(ledger=(dup, unknown))).reason_code == "delivery_unknown"


def test_g7_g8_and_ready_forms():
    assert arbitrate(_cand(), _ctx(budget_used=5)).reason_code == "budget_exhausted"
    assert arbitrate(_cand(), _ctx(budget_used=4)).outcome == "ready"
    assert arbitrate(_cand(confidence=0.49), _ctx()).reason_code == "low_confidence"
    assert arbitrate(_cand(confidence=0.5), _ctx()).outcome == "ready"
    # 預算先於信心門檻
    assert arbitrate(_cand(confidence=0.1), _ctx(budget_used=9)).reason_code == "budget_exhausted"


def test_ready_plan_shapes():
    reminder = arbitrate(_cand(), _ctx())
    assert reminder.plan.loop is None and reminder.plan.placement.presence_intent == "stay"
    assert reminder.plan.placement.target_platform == "desktop" and reminder.plan.followups is None
    run = arbitrate(_cand(goal_ref="memory_followup", source_kind="memory"), _ctx())
    assert (run.plan.loop.start, run.plan.loop.trigger_kind, run.plan.loop.goal_code) == (True, "proactive", "memory_followup")
    assert run.plan.loop.policy_ref == "proactive_default"
    pickup = arbitrate(
        _cand(goal_ref="assignment_pickup", source_kind="assignment", authorization_ref=None), _ctx(grant=None))
    assert pickup.reason_code == "ready_assignment" and pickup.plan.loop.trigger_kind == "external_event"
    move = arbitrate(_cand(goal_ref="request_presence_move"), _ctx())
    assert move.plan.loop is None and move.plan.placement.presence_intent == "request_move"


def test_arbitrate_is_deterministic():
    for record, _, first in run_records(_records()):
        again = run_records([record])[0][2]
        assert (first.outcome, first.reason_code, first.plan) == (again.outcome, again.reason_code, again.plan)


# ---- spec／hub／fallback ----

def test_spec_is_async_shadow_only_with_declared_sections():
    spec = proactive_initiate_spec()
    assert (spec.kind, spec.mode, spec.shadow, spec.allow_native) == (KIND_PROACTIVE_INITIATE, "async", True, False)
    assert spec.outcomes == ("ready", "deferred", "rejected") and spec.budget_ms == 1000
    assert spec.plan_sections == ("loop", "placement", "followups")
    assert (spec.on_timeout_reason, spec.on_busy_reason, spec.on_error_reason, spec.on_invalid_reason) == (
        ON_TIMEOUT_REASON, ON_BUSY_REASON, ON_ERROR_REASON, ON_INVALID_REASON)


def _hub():
    hub = DecisionHub()
    engine = register_proactive(hub)
    return hub, engine


def test_hub_submit_path_and_sync_decide_refused():
    hub, _ = _hub()
    request = build_request(_cand(), _ctx(), namespace="ns_main")
    assert request.request_id == "pcand_01HZ:eval:1" and request.subject == {
        "candidate_id": "pcand_01HZ", "namespace": "ns_main"}
    result = hub.submit(KIND_PROACTIVE_INITIATE, request)
    assert (result.outcome, result.reason_code, result.fallback) == ("ready", "ready_present", False)
    assert result.engine.tier == "rule"
    with pytest.raises(ValueError):
        hub.decide(KIND_PROACTIVE_INITIATE, request)
    with pytest.raises(ValueError):
        hub.submit(KIND_PROACTIVE_INITIATE, replace(request, native=object()))


def test_result_only_uses_declared_sections_over_fixture():
    for _, _, result in run_records(_records()):
        plan = result.plan
        assert plan.route is None and plan.reply is None and plan.output is None and plan.policy is None


def test_hub_strips_policy_section_from_proactive_engine():
    class Sneaky:
        def decide(self, request):
            return DecisionResult(
                decision_id="dec_x", request_id=request.request_id, kind=request.kind, outcome="ready",
                plan=DecisionPlan(policy=DecisionPolicy(channel_mode="active")), confidence=1.0, reason_code="ready_present",
                engine=DecisionEngineInfo(tier="rule", id="x", version="1"),
            )

    hub = DecisionHub()
    hub.register(proactive_initiate_spec(), Sneaky())
    result = hub.submit(KIND_PROACTIVE_INITIATE, build_request(_cand(), _ctx(), namespace="ns_main"))
    assert result.plan.policy is None and result.fallback and result.reason_code == ON_INVALID_REASON


def _assert_fallback(result, reason):
    assert result.fallback and result.outcome == "deferred" and result.reason_code == reason
    assert result.plan == DecisionPlan() and result.engine.tier == "fallback"


def test_fallback_invalid_when_facts_missing_or_context_broken():
    engine = ProactiveEngine()
    base = build_request(_cand(), _ctx(), namespace="ns_main")
    _assert_fallback(engine.decide(replace(base, facts={})), ON_INVALID_REASON)
    _assert_fallback(engine.decide(replace(base, facts={"candidate": base.facts["candidate"], "context": "x"})), ON_INVALID_REASON)
    broken = {**base.facts["context"], "audience_scope": "nonsense"}
    _assert_fallback(engine.decide(replace(base, facts={"candidate": base.facts["candidate"], "context": broken})), ON_INVALID_REASON)
    missing = {k: v for k, v in base.facts["context"].items() if k != "ledger"}
    _assert_fallback(engine.decide(replace(base, facts={"candidate": base.facts["candidate"], "context": missing})), ON_INVALID_REASON)


def test_malformed_candidate_is_rejected_not_fallback():
    engine = ProactiveEngine()
    base = build_request(_cand(), _ctx(), namespace="ns_main")
    for bad in ({**base.facts["candidate"], "urgency": "critical"}, {"candidate_id": "pcand_z"}):
        result = engine.decide(replace(base, facts={"candidate": bad, "context": base.facts["context"]}))
        assert (result.outcome, result.reason_code, result.fallback) == ("rejected", "candidate_malformed", False)


def test_fallback_busy_timeout_error():
    engine = ProactiveEngine()
    request = build_request(_cand(), _ctx(), namespace="ns_main")
    assert engine._guard.acquire(blocking=False)
    try:
        _assert_fallback(engine.decide(request), ON_BUSY_REASON)
    finally:
        engine._guard.release()
    ticks = iter([0.0, 5.0, 10.0, 15.0])  # 單次 decide 讀兩次 clock：5 秒 > 預算 1000ms
    slow = ProactiveEngine(clock=lambda: next(ticks))
    _assert_fallback(slow.decide(request), ON_TIMEOUT_REASON)


def test_fallback_error_on_unexpected_exception(monkeypatch):
    import uep.decision.proactive as mod

    def boom(*_a, **_k):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(mod, "arbitrate", boom)
    result = ProactiveEngine().decide(build_request(_cand(), _ctx(), namespace="ns_main"))
    _assert_fallback(result, ON_ERROR_REASON)
    assert SECRET not in json.dumps(result.reason_code)


def test_sub_kind_written_for_log_and_not_for_fallback():
    engine = ProactiveEngine()
    request = build_request(_cand(), _ctx(muted=True), namespace="ns_main")
    engine.decide(request)
    assert request.facts["sub_kind"] == "cost"
    bad = replace(request, facts={})
    engine.decide(bad)
    assert "sub_kind" not in bad.facts


# ---- 日誌 ----

def test_primary_row_fills_candidate_id_from_subject_with_safe_check():
    hub, _ = _hub()
    request = build_request(_cand(), _ctx(), namespace="ns_main", trigger="reeval", parent_decision_id="dec_prev")
    result = hub.submit(KIND_PROACTIVE_INITIATE, request)
    row = primary_row(request, result, hub.registry.get(KIND_PROACTIVE_INITIATE), 2)
    assert (row["candidate_id"], row["sub_kind"], row["trigger"], row["namespace"], row["snapshot_version"]) == (
        "pcand_01HZ", "ready", "reeval", "ns_main", 0)
    assert row["status"] == "ok" and row["outcome"] == "ready" and row["engine"] == "rule"
    unsafe = replace(request, subject={"candidate_id": SECRET, "namespace": "ns_main"})
    assert primary_row(unsafe, result, hub.registry.get(KIND_PROACTIVE_INITIATE), 2)["candidate_id"] is None
    assert SECRET not in json.dumps(primary_row(unsafe, result, hub.registry.get(KIND_PROACTIVE_INITIATE), 2), default=str)


def test_attach_default_covers_proactive_and_log_quiet_counts_repeats():
    hub, _ = _hub()
    sink = ListSink()
    writer = DecisionLogWriter(sink)
    writer.attach(hub)  # 預設清單要含 proactive.initiate，才會串上 log_quiet
    try:
        ctx = _ctx(muted=True)
        for seq in (1, 2, 3):
            hub.submit(KIND_PROACTIVE_INITIATE, build_request(_cand(), ctx, namespace="ns_main", seq=seq))
        hub.submit(KIND_PROACTIVE_INITIATE, build_request(_cand(), _ctx(), namespace="ns_main", seq=4))  # reason 改變 -> 記
        hub.submit(KIND_PROACTIVE_INITIATE, build_request(_cand(candidate_id="pcand_other"), ctx, namespace="ns_main"))
        assert writer.flush()
    finally:
        writer.close()
    assert writer.metrics[f"{KIND_PROACTIVE_INITIATE}.quiet"] == 2
    assert [r["reason_code"] for r in sink.rows] == ["muted", "ready_present", "muted"]
    assert all(r["kind"] == KIND_PROACTIVE_INITIATE and r["role"] == "primary" for r in sink.rows)


def test_log_quiet_never_suppresses_fallback_and_is_asked_once():
    engine = ProactiveEngine()
    request = build_request(_cand(), _ctx(muted=True), namespace="ns_main")
    first = engine.decide(request)
    assert engine.log_quiet(first) is False
    second = engine.decide(replace(request, request_id="r2"))
    assert engine.log_quiet(second) is True
    fb = engine.decide(replace(request, facts={}))
    assert engine.log_quiet(fb) is False


# ---- to_loop_trigger ----

def _inj(tags=("agent.read",)) -> ProactiveInjections:
    return ProactiveInjections(capability_lookup=lambda goal: frozenset(tags) if tags else None, snapshot_ref="snap_1")


def _ready(**over):
    c = _cand(goal_ref="memory_followup", source_kind="memory", **over)
    return c, run_records([{"candidate": candidate_to_facts(c), "context": {}}])[0][2]


def test_to_loop_trigger_maps_fields():
    c, result = _ready()
    t = to_loop_trigger(c, result, _inj(("a", "b")))
    assert isinstance(t, LoopTrigger)
    assert (t.kind, t.origin_ref, t.goal_code, t.scope_ref, t.owner_actor_id) == (
        "proactive", "pcand_01HZ", "memory_followup", "scope_private", "actor_owner")
    assert t.capability_tags == frozenset({"a", "b"}) and t.policy_ref == "proactive_default"
    assert t.snapshot_ref == "snap_1" and t.plan_ref == result.decision_id
    assert loop_trigger_decision(c, result, _inj())[1] == LT_WOULD_START


def test_to_loop_trigger_assignment_is_external_event():
    c = _cand(goal_ref="assignment_pickup", source_kind="assignment", authorization_ref=None)
    result = run_records([{"candidate": candidate_to_facts(c), "context": {"grant": None}}])[0][2]
    assert to_loop_trigger(c, result, _inj()).kind == "external_event"


@pytest.mark.parametrize("tags", [None, ()])
def test_to_loop_trigger_capability_empty(tags):
    c, result = _ready()
    assert to_loop_trigger(c, result, _inj(tags)) is None
    assert loop_trigger_decision(c, result, _inj(tags))[1] == LT_CAPABILITY_EMPTY


def test_to_loop_trigger_lookup_exception_and_goal_passthrough():
    c, result = _ready()
    seen = []

    def lookup(goal):
        seen.append(goal)
        raise RuntimeError("boom")

    inj = ProactiveInjections(capability_lookup=lookup, snapshot_ref="snap_1")
    assert loop_trigger_decision(c, result, inj) == (None, LT_CAPABILITY_EMPTY) and seen == ["memory_followup"]


def test_to_loop_trigger_none_cases():
    c = _cand()
    reminder = arbitrate(c, _ctx())
    res = lambda v: DecisionResult(
        decision_id="dec_1", request_id="r", kind=KIND_PROACTIVE_INITIATE, outcome=v.outcome, plan=v.plan,
        confidence=1.0, reason_code=v.reason_code, engine=DecisionEngineInfo(tier="rule", id="x", version="1"))
    assert loop_trigger_decision(c, res(reminder), _inj()) == (None, LT_NO_LOOP)  # 提醒交付不走 Agent 迴圈
    assert loop_trigger_decision(c, res(arbitrate(c, _ctx(muted=True))), _inj()) == (None, LT_NOT_READY)
    assert loop_trigger_decision(c, res(arbitrate(c, _ctx(global_stop=True))), _inj()) == (None, LT_NOT_READY)
    run = arbitrate(_cand(goal_ref="memory_followup", source_kind="memory"), _ctx())
    bad = replace(run, plan=replace(run.plan, loop=replace(run.plan.loop, trigger_kind="user_turn_x")))
    assert loop_trigger_decision(c, res(bad), _inj()) == (None, LT_BAD_TRIGGER)
    off = replace(run, plan=replace(run.plan, loop=replace(run.plan.loop, start=False)))
    assert loop_trigger_decision(c, res(off), _inj()) == (None, LT_NO_LOOP)
    none_plan = DecisionResult(
        decision_id="d", request_id="r", kind=KIND_PROACTIVE_INITIATE, outcome="ready", plan=None, confidence=1.0,
        reason_code="ready_present", engine=DecisionEngineInfo(tier="rule", id="x", version="1"))
    assert to_loop_trigger(c, none_plan, _inj()) is None


def test_to_loop_trigger_scope_is_not_wider_than_privacy_scope():
    from uep.events.envelope import scope_rank

    c, result = _ready(privacy_scope="scope_group_g1")
    result = run_records([{"candidate": candidate_to_facts(c), "context": {
        "grant": {"scope_refs": ["scope_group_g1"]}, "audience_scope": "scope_group_g1"}}])[0][2]
    t = to_loop_trigger(c, result, _inj())
    assert t is not None and scope_rank(t.scope_ref) <= scope_rank(c.privacy_scope)


# ---- Phase 1 無消費者鎖 ----

_UEP = Path(__file__).resolve().parents[2] / "uep"
# 只有這些檔案可以出現 proactive 決策點的符號：本體、重播工具（離線）、DL-6b 的 shadow 接線（assembly 只 import 接線檔）、
# 決策日誌（只認 kind 字串以串接 log_quiet）
_PROACTIVE_MODULE_ALLOWED = {"decision/proactive.py", "decision/replay_proactive.py", "echo/proactive_wiring.py"}
_KIND_STRING_ALLOWED = {"decision/proactive.py", "decision/log.py"}  # 接線檔用 KIND_PROACTIVE_INITIATE 常數，不寫字串
_GUARDED_NAMES = {
    "register_proactive", "ProactiveEngine", "to_loop_trigger", "loop_trigger_decision", "KIND_PROACTIVE_INITIATE",
    "ProactiveInjections", "proactive_initiate_spec",
}


def _proactive_violations(source: str, rel: str) -> list[str]:
    """判準：(i) import ``uep.decision.proactive``（含 ``from uep.decision import proactive``）；(ii) 使用受保護符號；
    (iii) 出現字串 ``"proactive.initiate"``。擋不住：以動態字串組 import／getattr 反射（屬刻意迴避，由審查負責）。"""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        if rel not in _PROACTIVE_MODULE_ALLOWED:
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "uep.decision.proactive" or node.module.startswith("uep.decision.proactive."):
                    out.append(f"{line}: import uep.decision.proactive")
                elif node.module == "uep.decision" and any(a.name == "proactive" for a in node.names):
                    out.append(f"{line}: from uep.decision import proactive")
            elif isinstance(node, ast.Import) and any(a.name.startswith("uep.decision.proactive") for a in node.names):
                out.append(f"{line}: import uep.decision.proactive")
            elif isinstance(node, ast.Name) and node.id in _GUARDED_NAMES:
                out.append(f"{line}: {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in _GUARDED_NAMES:
                out.append(f"{line}: .{node.attr}")
        if rel not in _KIND_STRING_ALLOWED and isinstance(node, ast.Constant) and node.value == KIND_PROACTIVE_INITIATE:
            out.append(f"{line}: 字串 proactive.initiate")
    return out


def test_proactive_lock_catches_violations():
    bad = (
        "from uep.decision.proactive import to_loop_trigger\n"
        "from uep.decision import proactive\n"
        "import uep.decision.proactive as p\n"
        "x = hub.engine('proactive.initiate')\n"
        "y = mod.register_proactive(hub)\n"
    )
    found = _proactive_violations(bad, "assembly.py")
    for tag in ("import uep.decision.proactive", "from uep.decision import proactive", "字串 proactive.initiate",
                ".register_proactive"):
        assert any(tag in v for v in found), (tag, found)
    assert _proactive_violations("x = 1\nfrom uep.decision import DecisionHub\n", "assembly.py") == []
    assert _proactive_violations(bad, "decision/log.py") != []  # 日誌檔只放行 kind 字串，import 與符號仍算違規
    assert _proactive_violations('k = "proactive.initiate"\n', "decision/log.py") == []
    assert _proactive_violations("from uep.decision.proactive import ProactiveEngine\n", "decision/replay_proactive.py") == []


def test_no_consumer_starts_runs_from_proactive_results_in_phase_1():
    """Phase 1：uep/ 內除本體、離線重播工具、DL-6b shadow 接線檔與日誌 kind 串接外，沒有任何模組引用 proactive 決策點，
    因此沒有消費者會依 ready 啟動 Run 或投遞提醒；assembly 只經接線檔掛載（見 test_assembly_proactive_shadow）。"""
    problems: list[str] = []
    for path in sorted(_UEP.rglob("*.py")):
        rel = path.relative_to(_UEP).as_posix()
        problems.extend(f"{rel}:{v}" for v in _proactive_violations(path.read_text(encoding="utf-8"), rel))
    assert problems == [], "Phase 1 不得有任何 proactive.initiate 的消費者：" + "; ".join(problems)


def test_shadow_wiring_cannot_start_runs_or_deliver():
    """DL-6b 接線檔只准讀 Presence／State 型別與決策層；不得 import 會建立 Run、投遞或路由的模組。"""
    tree = ast.parse((_UEP / "echo" / "proactive_wiring.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    forbidden = ("uep.agent", "uep.tasks", "uep.tools", "uep.turn", "uep.output", "uep.report")
    assert not {m for m in imported if m.startswith(forbidden)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"to_loop_trigger", "join_or_move", "route", "create_run", "launch", "AgentLoop"}


def test_proactive_module_does_not_import_agent_loop_or_tasks():
    tree = ast.parse((_UEP / "decision" / "proactive.py").read_text(encoding="utf-8"))
    imported = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    } | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in imported if m.startswith(("uep.tasks", "uep.tools", "uep.turn", "uep.output"))}
    assert {m for m in imported if m.startswith("uep.agent")} == {"uep.agent.trigger"}


# ---- shadow 日誌落盤（DB）----

def test_db_shadow_log_persists_primary_rows(engine, session_factory):
    from uep.decision.db import DbSink, load_rows

    hub, _ = _hub()
    writer = DecisionLogWriter(DbSink(session_factory))
    writer.attach(hub)
    try:
        ctx = _ctx(muted=True)
        hub.submit(KIND_PROACTIVE_INITIATE, build_request(_cand(), ctx, namespace="ns_main", seq=1))
        hub.submit(KIND_PROACTIVE_INITIATE, build_request(_cand(), ctx, namespace="ns_main", seq=2))  # 靜默
        hub.submit(KIND_PROACTIVE_INITIATE, build_request(_cand(), _ctx(), namespace="ns_main", seq=3, trigger="reeval"))
        hub.submit(KIND_PROACTIVE_INITIATE, build_request(
            _cand(candidate_id="pcand_exp", expires_at=NOW), _ctx(), namespace="ns_main"))
        assert writer.flush(10)
    finally:
        writer.close()
    rows = load_rows(session_factory, kind=KIND_PROACTIVE_INITIATE)
    assert len(rows) == 3 and writer.metrics[f"{KIND_PROACTIVE_INITIATE}.quiet"] == 1
    by = {(r["candidate_id"], r["reason_code"]): r for r in rows}
    muted = by[("pcand_01HZ", "muted")]
    assert (muted["outcome"], muted["sub_kind"], muted["trigger"], muted["namespace"], muted["snapshot_version"]) == (
        "deferred", "cost", "event", "ns_main", 0)
    ready = by[("pcand_01HZ", "ready_present")]
    assert (ready["outcome"], ready["sub_kind"], ready["trigger"], ready["status"]) == ("ready", "ready", "reeval", "ok")
    assert by[("pcand_exp", "candidate_expired")]["sub_kind"] == "validity"
    assert all(r["role"] == "primary" and r["fallback"] is False and r["utterance_refs"] is None for r in rows)
    assert SECRET not in json.dumps(rows, default=str)


# ---- console 不得成為移轉目標（enum 尚無 console 時以字串守住，加入後自動切換到行為斷言）----

_ENUM_HAS_CONSOLE = "console" in {m.value for m in PresenceLocation}


@pytest.mark.skipif(_ENUM_HAS_CONSOLE, reason="PresenceLocation 已含 console；改由行為測試守住")
def test_console_string_is_rejected_by_contract_while_enum_lacks_it():
    """PresenceLocation 尚無 console：任何位置欄位帶 console 都 fail-closed（ValueError）。"""
    with pytest.raises(ValueError):
        _cand(desired_location="console")
    with pytest.raises(ValueError):
        _ctx(private_locations=("console",))
    facts = json.loads(json.dumps(context_to_facts(_ctx())))
    facts["grant"]["locations"] = ["console"]
    with pytest.raises(ValueError):
        context_from_facts(facts)


@pytest.mark.skipif(not _ENUM_HAS_CONSOLE, reason="PresenceLocation 尚無 console；加入後此測試自動生效")
def test_console_never_chosen_as_move_target_when_not_in_private_locations():
    console = PresenceLocation("console")
    # 聽眾太寬且 desired 為 console，但 private_locations 不含 console：只能改選私人位置，絕不選 console
    verdict = arbitrate(
        _cand(source_kind="assignment", authorization_ref=None, desired_location=console),
        _ctx(grant=None, audience_scope="scope_group_g1", private_locations=(PresenceLocation.DESKTOP,)),
    )
    assert verdict.plan is None or verdict.plan.placement is None or verdict.plan.placement.target_platform != "console"
    # 有授權但 grant.locations 不含 console：授權範圍不足
    verdict = arbitrate(_cand(desired_location=console), _ctx())
    assert verdict.reason_code == "authorization_scope_insufficient"


def test_movable_targets_match_absent_intent_target_locations():
    """proactive 的可移轉目標與 presence.absent_intent 的 TARGET_LOCATIONS 同源，且不含 console。"""
    from uep.decision import presence_absent_intent as pai
    from uep.decision import proactive as pro

    assert pro.TARGET_LOCATIONS is pai.TARGET_LOCATIONS
    assert "console" not in pro.TARGET_LOCATIONS
    assert "target_not_movable" in REASON_CODES


@pytest.mark.skipif(not _ENUM_HAS_CONSOLE, reason="PresenceLocation 尚無 console；加入後此測試自動生效")
def test_g4_console_desired_location_is_target_not_movable():
    console = PresenceLocation("console")
    cand = _cand(source_kind="assignment", authorization_ref=None, desired_location=console)
    # assignment 預授權、grant=None、聽眾夠窄、目前在 desktop、desired 為 console
    verdict = arbitrate(cand, _ctx(grant=None))
    assert (verdict.outcome, verdict.reason_code, verdict.gate) == ("rejected", "target_not_movable", "presence")
    assert verdict.plan.placement is None and verdict.plan.loop is None
    # presence NONE 且 grant 可自選場域：不得選 console
    verdict = arbitrate(cand, _ctx(
        presence_status=PresenceStatus.NONE, current_platform=None,
        grant=GrantFact(valid=True, scope_refs=("scope_private",), locations=(console,), may_choose_location=True),
    ))
    assert verdict.reason_code == "target_not_movable"


@pytest.mark.skipif(not _ENUM_HAS_CONSOLE, reason="PresenceLocation 尚無 console；加入後此測試自動生效")
def test_console_in_private_or_grant_locations_never_becomes_move_target():
    console = PresenceLocation("console")
    # G3：desired 未指定，private_locations 只有 console -> 視為無私人位置可移，走 wait
    verdict = arbitrate(
        _cand(source_kind="assignment", authorization_ref=None),
        _ctx(grant=None, audience_scope="scope_group_g1", private_locations=(console,)),
    )
    assert verdict.reason_code == "audience_too_wide_wait" and verdict.plan.placement is None
    # G4 NONE：grant.locations 首位為 console、desired 未指定 -> 不選 console，改為排隊
    verdict = arbitrate(
        _cand(source_kind="assignment", authorization_ref=None),
        _ctx(presence_status=PresenceStatus.NONE, current_platform=None, grant=GrantFact(
            valid=True, scope_refs=("scope_private",), locations=(console,), may_choose_location=True)),
    )
    assert verdict.reason_code == "presence_none_queued" and verdict.plan.placement is None


@pytest.mark.skipif(not _ENUM_HAS_CONSOLE, reason="PresenceLocation 尚無 console；加入後此測試自動生效")
def test_target_not_movable_only_blocks_real_moves():
    console = PresenceLocation("console")
    asg = {"source_kind": "assignment", "authorization_ref": None}
    # 原地（current=console、desired=console）：照常處理，不被擋
    stay = arbitrate(_cand(desired_location=console, **asg), _ctx(grant=None, current_platform=console))
    assert stay.reason_code != "target_not_movable" and stay.outcome == "ready"
    # 從 console 移走（desired=desktop）照常可移
    away = arbitrate(_cand(desired_location=PresenceLocation.DESKTOP, **asg), _ctx(
        grant=GrantFact(valid=True, scope_refs=("scope_private",), locations=(PresenceLocation.DESKTOP,), may_move=True),
        current_platform=console))
    assert away.reason_code == "ready_move" and away.plan.placement.target_platform == "desktop"
    # G1 stop 優先於這道守門
    stopped = arbitrate(_cand(desired_location=console, **asg), _ctx(grant=None, global_stop=True))
    assert stopped.reason_code == "global_stop"
