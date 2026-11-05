"""``decision.consult`` executor 薄層（切片 D2）：facts 組裝、結果形狀、中樞失敗 ⇒ advice=∅、重放不重算；不需 DB。"""
from __future__ import annotations

import json

import pytest

from uep.adapters.tool import ToolCall
from uep.decision.contracts import DecisionResult
from uep.decision.hub import DecisionHub
from uep.decision.loop_consult import (
    FAILSAFE_DEFAULTS,
    LOOP_CONSULT_SPEC,
    LoopConsultEngine,
    consult_decision_id,
    register_loop_consult,
)
from uep.tools.catalog.decision import CONSULT_TAG
from uep.tools.executors.decision import ConsultResults, ConsultToolExecutor

TAGS = ["memory.read", CONSULT_TAG]


def params(sub_kind="loop_end", **over):
    base = {
        "sub_kind": sub_kind, "run_id": "run_a", "step_id": "step_1", "ordinal": 0,
        "frozen_capability_tags": TAGS, "frozen_modalities": ["text", "audio_stream"], "frozen_platforms": ["desktop"],
        "snapshot_capability_tags": TAGS, "snapshot_modalities": ["text"], "snapshot_platforms": ["desktop"],
        "consults_used": 0, "no_progress_count": 0,
    }
    base.update(over)
    return base


def call(p, action_id="act_1"):
    return ToolCall(action_id=action_id, tool="decision.consult", params=p, params_hash="h", side_effect_class="internal_read", idempotency="native")


@pytest.fixture
def hub():
    h = DecisionHub()
    register_loop_consult(h)
    return h


def test_loop_end_advice_and_shape(hub):
    ex = ConsultToolExecutor(hub)
    receipt = ex.execute(call(params(goal_satisfied=True)))
    assert receipt.succeeded and receipt.result_ref == "blob_act_1"
    out = ex.results.get("act_1")
    assert out["advice"]["loop"]["outcome"] == "finalize" and set(out["advice"]) == {"loop"}
    assert out["escalate"] == {"needed": False, "reason_code": ""} and out["decision_id"] == consult_decision_id("run_a", "step_1", 0, "loop_end")
    assert out["fallback"] is False and "default" not in out
    text = ex.results.content("act_1")
    assert json.loads(text)["decision_id"] == out["decision_id"]


def test_counts_are_passed_through_to_loop_end(hub):
    ex = ConsultToolExecutor(hub)
    ex.execute(call(params(made_progress=True, no_progress_count=3), "act_np"))
    assert ex.results.get("act_np")["reason_code"] == "loop_end_no_progress"
    ex.execute(call(params(made_progress=True, consults_used=8, ordinal=1), "act_cap"))
    assert ex.results.get("act_cap")["reason_code"] == "loop_end_consult_cap"


def test_missing_consult_tag_in_frozen_set_escalates(hub):
    ex = ConsultToolExecutor(hub)
    ex.execute(call(params(frozen_capability_tags=["memory.read"])))
    out = ex.results.get("act_1")
    assert out["escalate"]["needed"] is True and out["advice"] is None and "default" not in out


def test_scope_is_frozen_intersect_snapshot_for_modalities(hub):
    ex = ConsultToolExecutor(hub)
    ex.execute(call(params("output_modality", proposed_modalities=["audio_stream"])))  # snapshot 沒有 audio_stream
    assert ex.results.get("act_1")["escalate"]["needed"] is True
    ex.execute(call(params("output_modality", ordinal=1, proposed_modalities=["text"]), "act_2"))
    assert ex.results.get("act_2")["advice"]["output"]["modalities"] == ["text"]


def test_placement_always_escalates(hub):
    ex = ConsultToolExecutor(hub)
    ex.execute(call(params("placement")))
    assert ex.results.get("act_1")["escalate"]["needed"] is True


class _Boom:
    def decide(self, request):
        raise RuntimeError("中樞壞了")


def test_hub_failure_becomes_empty_advice_with_default_and_same_id():
    hub = DecisionHub()
    hub.register(LOOP_CONSULT_SPEC, _Boom())
    ex = ConsultToolExecutor(hub)
    receipt = ex.execute(call(params()))
    assert receipt.succeeded  # status 仍是 ok（CN-6）
    out = ex.results.get("act_1")
    assert out["advice"] is None and out["fallback"] is True and out["escalate"]["needed"] is False
    assert out["default"] == FAILSAFE_DEFAULTS["loop_end"] and out["decision_id"] == consult_decision_id("run_a", "step_1", 0, "loop_end")


@pytest.mark.parametrize("reason", ["consult_timeout", "consult_busy", "consult_invalid"])
def test_timeout_busy_invalid_results_are_empty_advice(reason):
    class Fallback:
        def decide(self, request):
            from uep.decision.contracts import DecisionEngineInfo

            return DecisionResult(
                decision_id=consult_decision_id("run_a", "step_1", 0, "speak"), request_id=request.request_id, kind=request.kind,
                outcome="none", plan=None, confidence=0.0, reason_code=reason,
                engine=DecisionEngineInfo(tier="fallback", id="x", version="1"), fallback=True,
            )

    hub = DecisionHub()
    hub.register(LOOP_CONSULT_SPEC, Fallback())
    ex = ConsultToolExecutor(hub)
    ex.execute(call(params("speak")))
    out = ex.results.get("act_1")
    assert out["advice"] is None and out["reason_code"] == reason and out["default"] == "hold" and out["escalate"]["needed"] is False


def test_replay_does_not_recompute_and_same_decision_id_after_cache_loss(hub):
    calls = []
    engine = LoopConsultEngine(registry=hub.registry)
    real = engine.decide

    def counting(request):
        calls.append(request.request_id)
        return real(request)

    engine.decide = counting  # type: ignore[method-assign]
    hub2 = DecisionHub()
    hub2.register(hub.registry.get("loop.consult"), engine)
    ex = ConsultToolExecutor(hub2)
    ex.execute(call(params(goal_satisfied=True)))
    first = ex.results.get("act_1")
    ex.execute(call(params(goal_satisfied=True)))  # 同 action 重放
    ex.results._receipts.clear()  # receipt 查核表遺失，但同身分的結果仍在
    ex.execute(call(params(goal_satisfied=True), "act_other"))  # 同 (run, step, ordinal, sub_kind) 換 action id
    assert len(calls) == 1 and ex.results.get("act_other") == first
    # 「崩潰」：整個結果暫存遺失，重算得到同一個 decision_id
    fresh = ConsultToolExecutor(hub2, ConsultResults())
    fresh.execute(call(params(goal_satisfied=True)))
    assert fresh.results.get("act_1")["decision_id"] == first["decision_id"] and len(calls) == 2
