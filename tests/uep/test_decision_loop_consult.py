"""loop.consult 決策層（DL-C1）：schema、fail-safe 表、範圍檢查、fallback、決定性 ID、契約相容、日誌欄位（不需資料庫）。"""
from __future__ import annotations

import dataclasses
import json
import threading

import pytest

from uep.decision import DecisionHub, DecisionPlan, DecisionReply, DecisionRequest
from uep.decision.contracts import (
    DecisionFollowup,
    DecisionLoop,
    DecisionOutput,
    DecisionPlacement,
    Escalate,
    plan_from_dict,
    plan_to_dict,
)
from uep.decision.log import primary_row
from uep.decision.loop_consult import (
    ADVICE_SECTION,
    ASK_USER_MODES,
    DEFAULT_RULES,
    FAILSAFE_DEFAULTS,
    KIND_LOOP_CONSULT,
    LOOP_CONSULT_SPEC,
    MAX_CONSULTS,
    NO_PROGRESS_N,
    SUB_KINDS,
    LoopConsultEngine,
    consult_decision_id,
    input_schema,
    output_schema,
    register_loop_consult,
)
from uep.ids import is_valid_id

FROZEN = {"capability_tags": ["decision.consult", "web.search"], "modalities": ["text", "typing"], "platforms": ["pc"]}
SNAP = {"capability_tags": ["decision.consult", "web.search"], "modalities": ["text", "audio_stream"], "platforms": ["pc"]}

KIND_FACTS = {
    "speak": {"user_present": True, "result_ready": True},
    "output_modality": {"proposed_modalities": ["text"]},
    "placement": {
        "target_platform": "pc", "executor_ref": "exec.pc.local", "current_platform": "pc",
        "available_executors": ["exec.pc.local", "exec.pc.alt"],
    },
    "ask_user": {"confident_default": False},
    "loop_end": {"goal_satisfied": False, "made_progress": True, "consults_used": 1, "no_progress_count": 0},
    "followup": {"report_pending": True},
}


def facts(sub_kind: str = "speak", **over):
    base = {"sub_kind": sub_kind, "run_id": "run_1", "step_id": "step_1", "ordinal": 0,
            "frozen": FROZEN, "capability_snapshot": SNAP, **KIND_FACTS[sub_kind]}
    base.update(over)
    return base


@pytest.fixture
def engine():
    return LoopConsultEngine()


# ---- spec ----

def test_spec_shape():
    s = LOOP_CONSULT_SPEC
    assert (s.kind, s.mode, s.budget_ms, s.shadow, s.allow_native) == (KIND_LOOP_CONSULT, "sync", 300, True, False)
    assert (s.on_timeout_reason, s.on_busy_reason, s.on_invalid_reason) == (
        "consult_timeout", "consult_busy", "consult_invalid")
    assert SUB_KINDS == ("speak", "output_modality", "placement", "ask_user", "loop_end", "followup")
    assert (MAX_CONSULTS, NO_PROGRESS_N) == (8, 3)


def test_register_on_hub_and_decide():
    hub = DecisionHub()
    register_loop_consult(hub)
    assert KIND_LOOP_CONSULT in hub.registry.kinds()
    req = DecisionRequest(request_id="r1", kind=KIND_LOOP_CONSULT, facts=facts("ask_user"))
    res = hub.decide(KIND_LOOP_CONSULT, req)
    assert res.kind == KIND_LOOP_CONSULT and res.plan.reply.mode == "ask" and not res.fallback
    with pytest.raises(ValueError):  # 不開 native
        hub.decide(KIND_LOOP_CONSULT, dataclasses.replace(req, native=object()))


# ---- schema 與 fail-safe（6 個 kind 各一）----

@pytest.mark.parametrize("sub_kind", SUB_KINDS)
def test_schema_per_kind(sub_kind):
    inp, out = input_schema(sub_kind), output_schema(sub_kind)
    json.dumps(inp)
    json.dumps(out)  # 可序列化
    assert inp["properties"]["sub_kind"] == {"const": sub_kind}
    advice_schema = out["properties"]["advice"]["anyOf"][1]
    assert advice_schema["required"] == [ADVICE_SECTION[sub_kind]]
    assert set(out["required"]) == {"advice", "reason_code", "confidence", "escalate", "decision_id"}
    assert set(inp["required"]) >= {"sub_kind", "run_id", "step_id", "ordinal"}


def test_schema_rejects_unknown_kind():
    with pytest.raises(ValueError):
        input_schema("nope")
    with pytest.raises(ValueError):
        output_schema("nope")


def test_failsafe_defaults_table():
    assert dict(FAILSAFE_DEFAULTS) == {
        "speak": "hold", "output_modality": "keep_current", "placement": "keep_current_platform",
        "ask_user": "ask", "loop_end": "finalize", "followup": "none",
    }
    assert set(FAILSAFE_DEFAULTS) == set(SUB_KINDS)
    with pytest.raises(TypeError):
        FAILSAFE_DEFAULTS["speak"] = "respond"  # type: ignore[index]


# ---- 正常 advice 對應區段 ----

def test_advice_sections(engine):
    attrs = ("reply", "output", "loop", "followups")
    for sub_kind in ("speak", "output_modality", "ask_user", "loop_end", "followup"):
        r = engine.consult(facts(sub_kind))
        assert r.advice is not None and not r.escalate.needed and not r.fallback, sub_kind
        want = ADVICE_SECTION[sub_kind]
        assert getattr(r.advice, want) is not None
        assert all(getattr(r.advice, k) is None for k in attrs if k != want)
        assert is_valid_id(r.decision_id, "dec")


def test_ask_user_only_defer_or_ask(engine):
    assert ASK_USER_MODES == ("defer", "ask")
    modes = {engine.consult(facts("ask_user", confident_default=v)).advice.reply.mode for v in (True, False)}
    assert modes == {"defer", "ask"}


def test_loop_end_outcome_only(engine):
    adv = engine.consult(facts("loop_end")).advice
    assert adv.loop == DecisionLoop(outcome="continue")
    for over in ({"goal_satisfied": True}, {"consults_used": MAX_CONSULTS}, {"no_progress_count": NO_PROGRESS_N},
                 {"made_progress": False}):
        assert engine.consult(facts("loop_end", **over)).advice.loop.outcome == "finalize"


def test_followup_and_output(engine):
    assert engine.consult(facts("followup")).advice.followups == (DecisionFollowup(kind="completion_report"),)
    assert engine.consult(facts("followup", report_pending=False)).advice.followups == ()
    out = engine.consult(facts("output_modality", capability_snapshot=SNAP, proposed_modalities=["text"]))
    assert out.advice.output == DecisionOutput(modalities=("text",), tts=False)
    # 沒有要改的：advice=∅，迴圈維持現狀
    empty = engine.consult(facts("output_modality", proposed_modalities=[]))
    assert empty.advice is None and not empty.fallback and not empty.escalate.needed


# ---- 範圍檢查（CN-1）----

def test_out_of_scope_escalates_without_advice(engine):
    # animation 不在凍結集合也不在 snapshot；audio_stream 只在 snapshot（交集收窄）
    for mods in (["animation"], ["audio_stream"], ["text", "audio_stream"]):
        r = engine.consult(facts("output_modality", proposed_modalities=mods))
        assert r.advice is None and r.escalate == Escalate(True, "scope_exceeded") and not r.fallback
        assert r.outcome == "escalate"


def test_snapshot_can_only_narrow(engine):
    narrowed = dict(SNAP, modalities=["typing"])
    assert engine.consult(facts("output_modality", capability_snapshot=narrowed)).escalate.needed  # text 被快照移除
    assert not engine.consult(
        facts("output_modality", capability_snapshot=narrowed, proposed_modalities=["typing"])).escalate.needed
    # 快照多出來的模態不會擴權
    wide = dict(SNAP, modalities=["text", "typing", "animation"])
    assert engine.consult(
        facts("output_modality", capability_snapshot=wide, proposed_modalities=["animation"])).escalate.needed


def test_consult_tag_must_be_in_intersection(engine):
    snap = dict(SNAP, capability_tags=["web.search"])
    r = engine.consult(facts("speak", capability_snapshot=snap))
    assert r.advice is None and r.escalate == Escalate(True, "scope_exceeded")


@pytest.mark.parametrize("bad", [
    None, "x", [], {}, {"capability_tags": "decision.consult"}, {"capability_tags": [1]},
    {"capability_tags": None}, {"modalities": ["text"]},
])
def test_snapshot_missing_or_malformed_fails_closed(engine, bad):
    for sub_kind in ("speak", "output_modality", "ask_user", "loop_end", "followup"):
        for key in ("capability_snapshot", "frozen"):
            r = engine.consult(facts(sub_kind, **{key: bad}))
            assert r.advice is None and r.escalate == Escalate(True, "snapshot_invalid"), (sub_kind, key, bad)
    f = facts("speak")
    del f["capability_snapshot"]
    assert engine.consult(f).escalate == Escalate(True, "snapshot_invalid")


def test_output_modality_needs_modalities_key(engine):
    snap = {"capability_tags": ["decision.consult"]}
    assert engine.consult(facts("output_modality", capability_snapshot=snap)).escalate.reason_code == "snapshot_invalid"
    assert not engine.consult(facts("speak", capability_snapshot=snap)).escalate.needed  # speak 不需要 modalities


def test_placement_same_platform_executor_swap_is_advice(engine):
    r = engine.consult(facts("placement"))
    assert r.escalate == Escalate() and not r.fallback and r.outcome == "placement_set"
    assert r.reason_code == "placement_same_platform"
    assert r.advice == DecisionPlan(placement=DecisionPlacement(
        target_platform="pc", executor_ref="exec.pc.local", presence_intent="none"))
    stay = engine.consult(facts("placement", presence_intent="stay", executor_ref="exec.pc.alt"))
    assert stay.advice.placement == DecisionPlacement("pc", "exec.pc.alt", "stay")
    assert set(plan_to_dict(r.advice)) == {"placement"}  # 只有 placement 區段


def _placement_escalates(engine, reason, **over):
    r = engine.consult(facts("placement", **over))
    assert r.advice is None and r.escalate == Escalate(True, reason) and not r.fallback and r.outcome == "escalate"


def test_placement_cross_platform_or_missing_current_platform_needs_review(engine):
    _placement_escalates(engine, "placement_review", target_platform="phone")
    _placement_escalates(engine, "placement_review", target_platform="phone", current_platform="phone")  # 不在交集
    _placement_escalates(engine, "placement_review", current_platform=None)
    _placement_escalates(engine, "placement_review", current_platform="")
    # 目前平台只在凍結、快照任一方 -> 不在交集
    _placement_escalates(engine, "placement_review", frozen={**FROZEN, "platforms": ["phone"]})
    _placement_escalates(engine, "placement_review", capability_snapshot={**SNAP, "platforms": []})


def test_placement_scope_exceeded_cases(engine):
    _placement_escalates(engine, "scope_exceeded", executor_ref="exec.unknown")
    _placement_escalates(engine, "scope_exceeded", presence_intent="request_move")
    _placement_escalates(engine, "scope_exceeded", audience_scope_max="public")
    _placement_escalates(engine, "scope_exceeded", available_executors=[])
    # 缺 available_executors：fail-closed，無法確認 executor 合法
    base = facts("placement")
    del base["available_executors"]
    r = engine.consult(base)
    assert r.advice is None and r.escalate == Escalate(True, "scope_exceeded") and not r.fallback


def test_placement_consult_tag_revoked_or_scope_key_missing(engine):
    snap = {**SNAP, "capability_tags": ["web.search"]}
    _placement_escalates(engine, "scope_exceeded", capability_snapshot=snap)
    _placement_escalates(engine, "scope_exceeded", frozen={**FROZEN, "capability_tags": ["web.search"]})
    no_platforms = {k: v for k, v in SNAP.items() if k != "platforms"}
    _placement_escalates(engine, "snapshot_invalid", capability_snapshot=no_platforms)
    _placement_escalates(engine, "snapshot_invalid", frozen={k: v for k, v in FROZEN.items() if k != "platforms"})
    _placement_escalates(engine, "snapshot_invalid", capability_snapshot=None)
    _placement_escalates(engine, "snapshot_invalid", frozen={})


@pytest.mark.parametrize("over", [
    {"executor_ref": ""}, {"executor_ref": "Exec Bad/../x"}, {"executor_ref": "x" * 65}, {"executor_ref": 3},
    {"executor_ref": None}, {"target_platform": ""}, {"target_platform": None}, {"target_platform": 5},
    {"presence_intent": "teleport"}, {"presence_intent": 1}, {"audience_scope_max": 3}, {"current_platform": 1},
    {"available_executors": "exec.pc.local"}, {"available_executors": [1]},
])
def test_placement_malformed_facts_are_invalid_fallback(engine, over):
    r = engine.consult(facts("placement", **over))
    assert r.advice is None and r.fallback and r.reason_code == "consult_invalid" and not r.escalate.needed


def test_placement_validate_advice_rejects_other_sections_and_bad_values():
    from uep.decision.loop_consult import _ConsultInvalid, _validate_advice
    ok = DecisionPlacement("pc", "exec.pc.local", "stay")
    _validate_advice("placement", DecisionPlan(placement=ok))
    bad = (
        DecisionPlan(), DecisionPlan(placement=ok, reply=DecisionReply(mode="respond")),
        DecisionPlan(placement=DecisionPlacement("pc", None)), DecisionPlan(placement=DecisionPlacement(None, "e")),
        DecisionPlan(placement=DecisionPlacement("pc", "BAD REF")),
        DecisionPlan(placement=DecisionPlacement("pc", "e", "request_move")),
        DecisionPlan(placement=DecisionPlacement("pc", "e", "none", "public")),
        DecisionPlan(route="direct_chat", placement=ok),
    )
    for plan in bad:
        with pytest.raises(_ConsultInvalid):
            _validate_advice("placement", plan)


def test_placement_decision_id_deterministic(engine):
    a, b = engine.consult(facts("placement")), engine.consult(facts("placement", executor_ref="exec.pc.alt"))
    assert a.decision_id == b.decision_id == consult_decision_id("run_1", "step_1", 0, "placement")
    assert engine.consult(facts("placement", target_platform="phone")).decision_id == a.decision_id


# ---- fallback：逾時／busy／invalid／例外 ----

def _assert_fallback(r, reason):
    assert r.advice is None and r.reason_code == reason and r.fallback
    assert r.escalate == Escalate(False, "") and r.confidence == 0.0
    assert is_valid_id(r.decision_id, "dec")


def test_timeout_by_budget():
    t = iter([0.0, 0.5, 0.5, 0.5])  # 規則耗時 500ms > 300ms
    eng = LoopConsultEngine(clock=lambda: next(t))
    _assert_fallback(eng.consult(facts()), "consult_timeout")


def test_timeout_by_expired_deadline(engine):
    _assert_fallback(engine.consult(facts(), deadline=0.0), "consult_timeout")


def test_busy_when_reentered():
    holder = {}

    def reenter(f, s):
        holder["inner"] = holder["eng"].consult(f)
        return DecisionPlan(reply=DecisionReply(mode="pass")), "pass", "x"

    eng = LoopConsultEngine(rules={**DEFAULT_RULES, "speak": reenter})
    holder["eng"] = eng
    outer = eng.consult(facts())
    _assert_fallback(holder["inner"], "consult_busy")
    assert outer.advice is not None  # 外層不受影響
    assert not eng.consult(facts("ask_user")).fallback  # 鎖已釋放


@pytest.mark.parametrize("bad", [
    {"sub_kind": "bogus"}, {"run_id": ""}, {"step_id": None}, {"ordinal": -1}, {"ordinal": "0"},
    {"user_present": "yes"},
])
def test_invalid_facts(engine, bad):
    _assert_fallback(engine.consult({**facts("speak"), **bad}, fallback_key="req_1"), "consult_invalid")


def test_invalid_non_mapping(engine):
    _assert_fallback(engine.consult(None, fallback_key="req_1"), "consult_invalid")  # type: ignore[arg-type]


def test_rule_output_invalid_is_caught():
    def wrong(f, s):
        return DecisionPlan(reply=DecisionReply(mode="respond")), "respond", "x"

    eng = LoopConsultEngine(rules={k: wrong for k in SUB_KINDS})
    _assert_fallback(eng.consult(facts("ask_user")), "consult_invalid")  # ask_user 只能 defer／ask
    _assert_fallback(eng.consult(facts("loop_end")), "consult_invalid")  # 區段不對


def test_engine_exception_never_raises():
    def boom(f, s):
        raise RuntimeError("boom")

    eng = LoopConsultEngine(rules={k: boom for k in SUB_KINDS})
    for sub_kind in ("speak", "ask_user", "loop_end", "followup", "output_modality"):
        _assert_fallback(eng.consult(facts(sub_kind)), "consult_invalid")
    # 經 hub 也是 DecisionResult，不拋例外
    hub = DecisionHub()
    register_loop_consult(hub, eng)
    res = hub.decide(KIND_LOOP_CONSULT, DecisionRequest(request_id="r", kind=KIND_LOOP_CONSULT, facts=facts()))
    assert res.fallback and res.plan is None and res.reason_code == "consult_invalid" and res.engine.tier == "fallback"


# ---- decision_id 決定性 ----

def test_decision_id_deterministic_and_kind_sensitive(engine):
    a = engine.consult(facts("speak")).decision_id
    assert a == engine.consult(facts("speak")).decision_id == consult_decision_id("run_1", "step_1", 0, "speak")
    ids = {engine.consult(facts(k)).decision_id for k in SUB_KINDS}
    assert len(ids) == len(SUB_KINDS)  # 同 ordinal 換 kind 不撞號
    assert engine.consult(facts("speak", ordinal=1)).decision_id != a
    assert engine.consult(facts("speak", step_id="step_2")).decision_id != a
    assert engine.consult(facts("speak", run_id="run_2")).decision_id != a
    assert is_valid_id(a, "dec")


def test_decision_id_stable_across_outcomes(engine):
    ok = engine.consult(facts("output_modality")).decision_id
    esc = engine.consult(facts("output_modality", proposed_modalities=["animation"])).decision_id
    assert ok == esc  # 鍵只由 (run, step, ordinal, sub_kind) 決定


# ---- 舊格式 DecisionPlan 相容 ----

def test_legacy_plan_roundtrip():
    legacy = {"route": "direct_chat", "reply": {"mode": "respond", "interaction_to": "u1", "addressed": True}}
    plan = plan_from_dict(legacy)
    assert plan == DecisionPlan(route="direct_chat", reply=DecisionReply(mode="respond", interaction_to="u1"))
    assert plan.output is plan.placement is plan.loop is plan.followups is None
    assert plan_to_dict(plan) == legacy  # 舊格式輸出不變，新區段不外洩
    assert plan_from_dict(plan_to_dict(plan)) == plan
    assert plan_from_dict({}) == DecisionPlan()
    assert DecisionPlan("ignore", None) == DecisionPlan(route="ignore")  # 位置參數相容


def test_new_sections_roundtrip():
    plan = DecisionPlan(
        output=DecisionOutput(modalities=("text", "audio_stream"), tts=True),
        placement=DecisionPlacement(target_platform="pc", presence_intent="stay"),
        loop=DecisionLoop(outcome="finalize", reeval_on=("timeout",)),
        followups=(DecisionFollowup(kind="completion_report", after_ms=100),),
    )
    assert plan_from_dict(json.loads(json.dumps(plan_to_dict(plan)))) == plan


# ---- 日誌欄位（hub observer 路徑；不存原文）----

def test_log_row_kind_and_no_raw_text():
    hub = DecisionHub()
    register_loop_consult(hub)
    seen = []
    hub.add_observer(lambda rq, rs, sp, ms: seen.append(primary_row(rq, rs, sp, ms)))
    secret = "SECRET_USER_TEXT"
    hub.decide(KIND_LOOP_CONSULT, DecisionRequest(
        request_id="r1", kind=KIND_LOOP_CONSULT, facts=facts("ask_user", utterance=secret)))
    (row,) = seen
    assert row["kind"] == "loop.consult" and row["role"] == "primary" and row["status"] == "ok"
    assert row["outcome"] == "ask" and row["reason_code"] == "ask_user_unsure"
    assert secret not in json.dumps({k: str(v) for k, v in row.items()})


def test_log_row_status_for_fallbacks():
    hub = DecisionHub()
    register_loop_consult(hub)
    seen = []
    hub.add_observer(lambda rq, rs, sp, ms: seen.append(primary_row(rq, rs, sp, ms)["status"]))
    hub.decide(KIND_LOOP_CONSULT, DecisionRequest(
        request_id="r1", kind=KIND_LOOP_CONSULT, facts=facts(), deadline=0.0))
    hub.decide(KIND_LOOP_CONSULT, DecisionRequest(request_id="r2", kind=KIND_LOOP_CONSULT, facts={}))
    assert seen == ["timeout", "invalid"]


def test_concurrent_runs_not_busy():
    """不同執行緒、不同 run_id 同時 consult 不得互相判成 busy（用 barrier 讓兩個規則確實重疊）。"""
    barrier = threading.Barrier(2, timeout=5)

    def slow(f, s):
        barrier.wait()  # 兩個呼叫都進到規則內才放行 => 一定重疊
        return DecisionPlan(reply=DecisionReply(mode="pass")), "pass", "x"

    eng = LoopConsultEngine(rules={**DEFAULT_RULES, "speak": slow})
    out = {}

    def run(name):
        out[name] = eng.consult(facts("speak", run_id=name))

    threads = [threading.Thread(target=run, args=(n,)) for n in ("run_a", "run_b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert set(out) == {"run_a", "run_b"}
    assert all(not r.fallback and r.advice is not None for r in out.values())
    assert out["run_a"].decision_id != out["run_b"].decision_id


def test_invalid_without_key_uses_fresh_id(engine):
    a = engine.consult(None, fallback_key="")  # type: ignore[arg-type]
    b = engine.consult(None, fallback_key="")  # type: ignore[arg-type]
    _assert_fallback(a, "consult_invalid")
    assert a.decision_id != b.decision_id  # 無鍵時不保證重放決定性
    k1 = engine.consult(None, fallback_key="req_1")  # type: ignore[arg-type]
    assert k1.decision_id == engine.consult(None, fallback_key="req_1").decision_id  # type: ignore[arg-type]


def test_plan_from_dict_ignores_unknown_keys_in_sections():
    data = {
        "route": "direct_chat", "future_top": 1,
        "reply": {"mode": "respond", "x": 1},
        "output": {"modalities": ["text"], "tts": True, "emotion": {"source": "preset", "y": 2}, "z": 3},
        "placement": {"target_platform": "pc", "q": 1},
        "loop": {"outcome": "finalize", "reeval_on": ["timeout"], "q": 1},
        "followups": [{"kind": "completion_report", "q": 1}],
    }
    plan = plan_from_dict(data)
    assert plan.reply == DecisionReply(mode="respond")
    assert plan.output == DecisionOutput(modalities=("text",), tts=True, emotion=plan.output.emotion)
    assert plan.output.emotion.source == "preset"
    assert plan.placement == DecisionPlacement(target_platform="pc")
    assert plan.loop == DecisionLoop(outcome="finalize", reeval_on=("timeout",))
    assert plan.followups == (DecisionFollowup(kind="completion_report"),)
