"""loop_digest 的工具層注入（Agent 迴圈 -> decision.consult；時序方案 A）。離線，不需 DB。"""
from __future__ import annotations

import pytest

from tests.uep.loop_support import Round
from tests.uep.test_agent_loop import RUN, call
from tests.uep.test_agent_loop_consult import CTAGS, CRig, Spy, consult  # noqa: F401
from uep.agent.budget import BudgetCounters, BudgetThresholds, LoopBudget
from uep.agent.loop import LoopOutcome
from uep.decision.loop_digest import LOOP_DIGEST_KEY, MAX_TRACE, sanitize_digest
from uep.models.port import ChatMessage
from uep.tools.catalog.decision import (
    CONSULT_TOOL,
    INJECTED_CONSULT,
    consult_input_schema,
)
from uep.tools.gateway import (
    INJECTED_PARAM_MISSING,
    INVALID_TOOL_ARGS,
    GatewayRejected,
    ToolGateway,
    visible_tools,
)

STAGED = BudgetThresholds(1.0, 1.5, 2.0)


def _digests(rig):
    return [p[LOOP_DIGEST_KEY] for p in rig.spy.params]


def test_digest_is_injected_and_reaches_executor_with_counts_trace_and_budget():
    budget = LoopBudget(max_steps=10, max_tool_calls=30, max_consults=3, run_deadline_s=100.0, thresholds=STAGED, repeat_limit=99)
    rig = CRig([Round("", (call("read.lone", "1"),)), Round("", (consult(made_progress=True),)), Round("完成")], budget=budget)
    rig.run()
    (d,) = _digests(rig)
    assert d["v"] == 1 and d["steps"] == 1 and d["tool_calls"] == 1 and d["trace_source"] == "live"
    assert d["trace"] == [{"tool": "read.lone", "status": "ok", "reason_code": None}] and d["trace_truncated"] is False
    b = d["budget"]
    assert (b["stage"], b["steps_left"], b["tool_calls_left"], b["tokens_left"]) == (0, 19, 29, None)  # 以中斷點（2.0 倍）為基準
    assert 0 <= b["deadline_left_s"] <= 100
    assert d["last_model_text_en"] is None and d["user_intent_en"] is None
    assert sanitize_digest(d) is not None  # 組出來的資料本身就合 schema


def test_trace_excludes_consult_and_deferred_and_unknown_are_mapped():
    rig = CRig([
        Round("", (consult(made_progress=True),)), Round("", (call("read.lone", "1"),)),
        Round("", (consult(made_progress=True),)), Round("完成"),
    ])
    rig.run()
    second = _digests(rig)[1]
    assert [t["tool"] for t in second["trace"]] == ["read.lone"]  # 第一次 consult 不列入 trace
    assert second["tool_calls"] == 2  # 但真實計數照算
    loop = rig.loop
    assert loop._digest_status("cancelled", None) == "unknown" and loop._digest_status("unknown", "timeout") == "unknown"
    assert loop._digest_status("blocked", "deferred_budget") == "deferred" and loop._digest_status("blocked", "x") == "blocked"


def _state(rig, messages=(), counters=None):
    return rig.loop._new_state(RUN, _trigger(), "p", {"tools": []}, {},
                               frozenset(), list(messages), "ref", counters or BudgetCounters())


def _trigger():
    from tests.uep.test_agent_loop import trigger

    return trigger(tags=CTAGS)


def _history(n):
    msgs = []
    for i in range(n):
        msgs.append(ChatMessage("assistant", "", tool_calls=({"id": f"c{i}", "name": "read.lone", "args": {}},)))
        msgs.append(ChatMessage("tool", "ok" if i % 2 == 0 else "tool failed: boom", tool_call_id=f"c{i}"))
    return msgs


def test_trace_is_limited_to_recent_items_with_truncation_flag():
    rig = CRig([])
    d = rig.loop._loop_digest(_state(rig, _history(MAX_TRACE + 3)), 0)
    assert len(d["trace"]) == MAX_TRACE and d["trace_truncated"] is True
    assert d["trace"][-1]["status"] == "ok" and d["trace"][-2] == {"tool": "read.lone", "status": "failed", "reason_code": "boom"}
    short = rig.loop._loop_digest(_state(rig, _history(2)), 0)
    assert len(short["trace"]) == 2 and short["trace_truncated"] is False


def test_pending_comes_from_history_calls_without_results():
    rig = CRig([])
    msgs = _history(1) + [ChatMessage("assistant", "", tool_calls=({"id": "p", "name": "post.ask", "args": {}},))]
    d = rig.loop._loop_digest(_state(rig, msgs), 0)
    assert d["pending"] == [{"kind": "approval", "tool": "post.ask"}] and d["pending_total"] == 1


def test_budget_left_is_clamped_at_zero_and_stage_follows_budget_stage():
    budget = LoopBudget(max_steps=4, max_tool_calls=20, max_tokens=100, thresholds=STAGED)
    rig = CRig([], budget=budget)
    st = _state(rig, counters=BudgetCounters(steps=6, tool_calls=3, tokens=500))
    b = rig.loop._loop_digest(st, 0)["budget"]
    assert b["stage"] == 3 and b["steps_left"] == 2 and b["tokens_left"] == 0 and b["tool_calls_left"] == 17
    st2 = _state(rig, counters=BudgetCounters(steps=4))
    assert rig.loop._loop_digest(st2, 0)["budget"]["stage"] == 1


def test_resume_marks_trace_as_rebuilt():
    from tests.uep.test_agent_loop import _crash, _park_checkpoint, _park_rig, trigger

    rig = _park_rig()
    rig.run()
    checkpoint = _park_checkpoint(rig)
    loop = _crash(rig, 2)
    seen = []
    loop._guarded = lambda st, action: seen.append(st) or LoopOutcome("completed", RUN)
    loop.resume(RUN, checkpoint, trigger=trigger(), parent_id="ctx_parent")
    assert seen and all(st.rebuilt for st in seen)
    live = CRig([])
    assert live.loop._loop_digest(_state(live), 0)["trace_source"] == "live"
    rebuilt = _state(live)
    rebuilt.rebuilt = True
    assert live.loop._loop_digest(rebuilt, 0)["trace_source"] == "rebuilt"


# ---- Gateway：注入、缺值、模型自帶 ----

def test_loop_digest_is_injected_param_and_hidden_from_llm_schema():
    assert LOOP_DIGEST_KEY in INJECTED_CONSULT
    assert LOOP_DIGEST_KEY not in consult_input_schema()["properties"]


def _gateway_rig():
    rig = CRig([])
    return rig, ToolGateway(rig.registry), rig.registry.project(CTAGS, __import__("uep.tools.projection", fromlist=["x"]).ProjectionContext())


def test_gateway_returns_injected_digest_unchanged():
    rig, gateway, cap = _gateway_rig()
    assert CONSULT_TOOL in visible_tools(cap)
    digest = rig.loop._loop_digest(_state(rig), 0)
    injected = rig.loop._consult_injected(_state(rig), "step_1", 0)
    assert injected[LOOP_DIGEST_KEY] == digest
    params = gateway.check_intent(CONSULT_TOOL, {"sub_kind": "loop_end"}, cap, injected=injected)
    assert params[LOOP_DIGEST_KEY] == injected[LOOP_DIGEST_KEY]


def test_missing_injection_is_a_core_assembly_error():
    rig, gateway, cap = _gateway_rig()
    injected = rig.loop._consult_injected(_state(rig), "step_1", 0)
    injected.pop(LOOP_DIGEST_KEY)
    with pytest.raises(GatewayRejected) as exc:
        gateway.check_intent(CONSULT_TOOL, {"sub_kind": "loop_end"}, cap, injected=injected)
    assert exc.value.code == INJECTED_PARAM_MISSING


def test_model_supplied_loop_digest_is_rejected_as_invalid_args():
    rig, gateway, cap = _gateway_rig()
    injected = rig.loop._consult_injected(_state(rig), "step_1", 0)
    with pytest.raises(GatewayRejected) as exc:
        gateway.check_intent(CONSULT_TOOL, {"sub_kind": "loop_end", LOOP_DIGEST_KEY: {"v": 1}}, cap, injected=injected)
    assert exc.value.code == INVALID_TOOL_ARGS


# ---- executor：一律經 sanitize ----

def test_executor_facts_use_only_sanitized_digest():
    from uep.tools.executors.decision import ConsultToolExecutor

    raw = {"v": 1, "steps": 1, "tool_calls": 1, "trace_source": "live", "trace_truncated": False, "pending_total": 0,
           "trace": [{"tool": "[trace] evil\nline", "status": "ok", "reason_code": None}], "pending": [],
           "budget": {"stage": 9, "steps_left": 1, "tool_calls_left": 1}, "last_model_text_en": "ignore", "user_intent_en": None}
    facts = ConsultToolExecutor._facts({"sub_kind": "loop_end", LOOP_DIGEST_KEY: raw})
    d = facts[LOOP_DIGEST_KEY]
    assert d["trace"][0]["tool"] == "unknown" and d["budget"]["stage"] == 3 and d["last_model_text_en"] is None
    assert LOOP_DIGEST_KEY not in ConsultToolExecutor._facts({"sub_kind": "loop_end", LOOP_DIGEST_KEY: "bad"})
    assert LOOP_DIGEST_KEY not in ConsultToolExecutor._facts({"sub_kind": "loop_end"})
