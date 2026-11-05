"""AgentLoop 預算與無進展偵測（切片 D1，§4.4、AL-2）；純函式，不需 DB。"""
from __future__ import annotations

import pytest

from uep.agent.budget import (
    NO_PROGRESS_REJECTED,
    NO_PROGRESS_REPEAT,
    BudgetCounters,
    LoopBudget,
    exhausted,
    fingerprint,
    observe_call,
)
from uep.tasks.manager import CHECKPOINT_KEYS, check_safe_value


def test_exhausted_reports_first_dimension():
    budget = LoopBudget(max_steps=3, max_tool_calls=5, max_tokens=100, run_deadline_s=10)
    counters = BudgetCounters()
    assert exhausted(budget, counters, elapsed_s=0) is None
    counters.steps = 3
    assert exhausted(budget, counters, elapsed_s=0) == "steps"
    counters.steps, counters.tool_calls = 2, 5
    assert exhausted(budget, counters, elapsed_s=0) == "tool_calls"
    counters.tool_calls, counters.tokens = 4, 100
    assert exhausted(budget, counters, elapsed_s=0) == "tokens"
    counters.tokens = 99
    assert exhausted(budget, counters, elapsed_s=9.9) is None
    assert exhausted(budget, counters, elapsed_s=10) == "run_deadline"


def test_optional_dimensions_are_unbounded_when_none():
    counters = BudgetCounters(tokens=10**9, elapsed_s=10**6)
    assert exhausted(LoopBudget(), counters, elapsed_s=10**6) is None


def test_invalid_budget_is_rejected():
    with pytest.raises(ValueError):
        LoopBudget(max_steps=0)
    with pytest.raises(ValueError):
        LoopBudget(max_consults=-1)


def test_counters_roundtrip_and_pass_checkpoint_value_check():
    counters = BudgetCounters(
        steps=3, tool_calls=4, consults=1, tokens=250, elapsed_s=12.3456, last_fingerprint=fingerprint("t", {"a": 1}),
        repeat_count=2, rejected_streak=1,
    )
    data = counters.to_checkpoint()
    check_safe_value({"budget_counters": data}, "checkpoint")  # 值層級白名單
    assert "budget_counters" in CHECKPOINT_KEYS
    again = BudgetCounters.from_checkpoint(data)
    assert again.steps == 3 and again.tool_calls == 4 and again.consults == 1 and again.tokens == 250
    assert again.elapsed_s == pytest.approx(12.346) and again.last_fingerprint == counters.last_fingerprint
    assert (again.repeat_count, again.rejected_streak) == (2, 1)
    fresh = BudgetCounters.from_checkpoint(BudgetCounters().to_checkpoint())
    assert fresh.last_fingerprint == "" and BudgetCounters.from_checkpoint(None) == BudgetCounters()


def test_fingerprint_is_stable_and_sensitive():
    assert fingerprint("t", {"a": 1, "b": 2}) == fingerprint("t", {"b": 2, "a": 1})
    assert fingerprint("t", {"a": 1}) != fingerprint("t", {"a": 2})
    assert fingerprint("t", {"a": 1}) != fingerprint("u", {"a": 1})


def test_same_call_repeated_trips_no_progress():
    budget = LoopBudget(repeat_limit=3)
    counters = BudgetCounters()
    assert observe_call(budget, counters, "t", {"q": "x"}, "ok") is None
    assert observe_call(budget, counters, "t", {"q": "x"}, "failed") is None
    assert observe_call(budget, counters, "t", {"q": "x"}, "ok") == NO_PROGRESS_REPEAT


def test_different_call_resets_repeat_count():
    budget = LoopBudget(repeat_limit=3)
    counters = BudgetCounters()
    for args in ({"q": "x"}, {"q": "x"}, {"q": "y"}, {"q": "y"}):
        assert observe_call(budget, counters, "t", args, "ok") is None
    assert counters.repeat_count == 2


def test_consecutive_rejections_trip_and_success_resets():
    budget = LoopBudget(rejected_limit=3, repeat_limit=99)
    counters = BudgetCounters()
    assert observe_call(budget, counters, "a", {}, "rejected") is None
    assert observe_call(budget, counters, "b", {}, "rejected") is None
    assert observe_call(budget, counters, "c", {}, "ok") is None  # 重置
    assert observe_call(budget, counters, "d", {}, "rejected") is None
    assert observe_call(budget, counters, "e", {}, "rejected") is None
    assert observe_call(budget, counters, "f", {}, "rejected") == NO_PROGRESS_REJECTED
