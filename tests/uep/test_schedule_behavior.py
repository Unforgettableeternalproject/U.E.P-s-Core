"""Behavior 合理化（PP-0，純函式）：VC-PP-9/10/11（純函式部分）與 profile。"""
from datetime import UTC, datetime, timedelta

from uep.contracts.schedule import (
    ActivityKind,
    BehaviorSuggestion,
    InterruptPolicy,
    PlanSlot,
    PlanSource,
)
from uep.schedule import behavior as b
from uep.schedule import resolver as r

NOW = datetime(2026, 11, 12, 10, tzinfo=UTC)


def slot(kind=ActivityKind.WORK, source=PlanSource.ITEM, suggestions=None, avail="available"):
    if avail == "unavailable":
        return r.unavailable_slot(NOW)
    return PlanSlot(
        "available", "discord", kind, source, "x", InterruptPolicy.SAFE_POINT,
        suggestions if suggestions is not None else r.suggestions_for(kind, source), NOW, None, None, 1)


def run(sl, presence="ACTIVE", current="CHAT", **kw):
    return b.rationalize(b.RationalizeInput(slot=sl, presence_status=presence, current_behavior=current, now=NOW, **kw))


def test_presence_family_consistency():  # VC-PP-9 / BH-1
    d = run(slot(ActivityKind.REST, PlanSource.REST_WINDOW), "ACTIVE", "CHAT")
    assert d.outcome == "NOOP" and d.target == "CHAT"  # SLEEP/REST 不會被選，改由 Presence 補預設 CHAT
    d = run(slot(ActivityKind.REST, PlanSource.REST_WINDOW), "ACTIVE", "REST")
    assert d.target == "CHAT" and d.reason_code == "presence_fallback" and d.outcome == "SUBSTITUTE"
    d = run(slot(ActivityKind.WORK), "NONE", "CHAT")
    assert d.target == "REST" and d.reason_code == "presence_fallback"
    d = run(slot(ActivityKind.WORK), "RECOVERING", "REST")
    assert d.outcome == "NOOP" and d.target == "REST"


def test_mischief_rejected_in_v1():  # VC-PP-9
    s = slot(suggestions=(BehaviorSuggestion("MISCHIEF", 0, "x"),))
    d = run(s, "ACTIVE", "CHAT")
    assert (d.outcome, d.reason_code, d.target) == ("REJECT_ALL", "autonomy_not_enabled", None)
    assert b.BEHAVIOR_PROFILES["MISCHIEF"].enterable is False


def test_unavailable_slot_uses_presence_only():
    assert run(slot(avail="unavailable"), "NONE", "CHAT").target == "REST"
    assert run(slot(avail="unavailable"), "ACTIVE", "REST").target == "CHAT"


def test_accept_suggestion_rank0():
    d = run(slot(ActivityKind.WORK), "ACTIVE", "CHAT")
    assert (d.outcome, d.target, d.reason_code, d.suggestion_rank) == ("ACCEPT_SUGGESTION", "WORK", "plan_work", 0)


def test_hold_rules():  # VC-PP-10
    rest = slot(ActivityKind.REST, PlanSource.ITEM)
    held = run(rest, "NONE", "CHAT", interaction_idle=False)
    assert (held.outcome, held.reason_code, held.target) == ("HOLD", "interaction_active", "REST")
    assert held.recheck_at == NOW + timedelta(seconds=5)
    work = slot(ActivityKind.WORK)
    d = run(work, "ACTIVE", "SLEEP", dream_in_flight=True)
    assert (d.outcome, d.reason_code) == ("HOLD", "dream_in_flight")
    assert run(work, "ACTIVE", "SLEEP", dream_in_flight=False).outcome == "ACCEPT_SUGGESTION"
    d = run(rest, "NONE", "WORK", has_uninterruptible_work=True)
    assert d.reason_code == "uninterruptible_work"
    # 防抖：60 秒內非硬約束切換 HOLD；硬約束（REST 在 ACTIVE 違反 BH-1）不吃防抖
    entered = NOW - timedelta(seconds=20)
    assert run(work, "ACTIVE", "REST", behavior_entered_at=entered).outcome != "HOLD"
    d = run(slot(ActivityKind.SOCIAL), "ACTIVE", "WORK", behavior_entered_at=entered)
    assert (d.outcome, d.reason_code) == ("HOLD", "min_dwell") and d.recheck_at == entered + timedelta(seconds=60)
    assert run(slot(ActivityKind.SOCIAL), "ACTIVE", "WORK",
               behavior_entered_at=NOW - timedelta(seconds=61)).outcome == "ACCEPT_SUGGESTION"


def test_global_stop_substitutes_work_with_chat():  # VC-PP-10
    d = run(slot(ActivityKind.WORK), "ACTIVE", "REST", global_stop=True)
    assert (d.outcome, d.target, d.reason_code, d.suggestion_rank) == ("SUBSTITUTE", "CHAT", "substituted_stop", 1)
    only_work = slot(suggestions=(BehaviorSuggestion("WORK", 0, "x"),))
    d = run(only_work, "ACTIVE", "CHAT", global_stop=True)
    assert d.outcome == "NOOP" and d.target == "CHAT"
    d = run(only_work, "ACTIVE", "WORK", global_stop=True, behavior_entered_at=NOW - timedelta(seconds=5))
    assert d.target == "CHAT" and d.outcome == "SUBSTITUTE"  # 全域停止拉出 WORK，不吃防抖


def test_runtime_not_ready_holds():
    d = run(slot(ActivityKind.WORK), "ACTIVE", "CHAT", runtime_ready=False)
    assert (d.outcome, d.reason_code) == ("HOLD", "runtime_not_ready")


def test_transition_path_planning():  # VC-PP-11（純函式部分）
    assert b.plan_path("SLEEP", "WORK") == ("CHAT", "WORK")
    assert b.plan_path("REST", "WORK") == ("CHAT", "WORK")
    assert b.plan_path("CHAT", "REST") == ("REST",)
    assert b.plan_path("WORK", "WORK") == ()
    d = run(slot(ActivityKind.WORK), "ACTIVE", "SLEEP")
    assert d.target == "CHAT" and d.path == ("CHAT", "WORK")
    assert "SLEEP" in b.BEHAVIOR_TRANSITIONS["REST"] and "REST" in b.BEHAVIOR_TRANSITIONS["SLEEP"]


def test_profiles_separate_muted_and_dream_policy():  # BH-6
    p = b.BEHAVIOR_PROFILES
    assert p["REST"].muted and p["SLEEP"].muted and not p["CHAT"].muted
    assert p["SLEEP"].dream_policy == "rest_relaxed" and p["REST"].dream_policy == "normal"


def test_rationalize_is_deterministic():
    s = slot(ActivityKind.WORK)
    assert run(s, "ACTIVE", "CHAT") == run(s, "ACTIVE", "CHAT")
