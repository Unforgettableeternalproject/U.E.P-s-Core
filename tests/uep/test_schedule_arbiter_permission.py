"""PM-2：仲裁器消費權限對照表（權限契約 §3.3、§3.4、§3.6、§4）。不碰 DB（DB 相關見 test_schedule_store.py）。"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from uep.contracts.permission import (
    DEFAULT_RULES,
    InstructionLevel,
    InstructionRule,
    PermissionPolicy,
    RequesterTier,
)
from uep.contracts.schedule import (
    ActivityKind,
    ArbiterRequest,
    InterruptFacts,
    InterruptPolicy,
    InterruptTiming,
    OverrideView,
    PlanItem,
    PlanSlot,
    PlanSource,
    SlotMark,
    split_interrupt_policy,
)
from uep.events.registry import DEFAULT_REGISTRY
from uep.schedule import arbiter as ar
from uep.schedule import resolver
from uep.schedule.planner import PresenceReading

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)
PLANNER = "core.schedule_planner"


def tpe(h, mi=0):
    return datetime(2026, 11, 12, h, mi, tzinfo=TPE)


def item(item_id, start, end, kind, location="discord", role="user", **kw):
    return PlanItem(item_id, kind, location, "weekly", role=role, weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=start,
                    local_end=end, tz=TZ, **kw)


WORK = item("work", time(9), time(10), ActivityKind.WORK)
SOCIAL = item("social", time(11), time(12), ActivityKind.SOCIAL)
REST_ITEM = item("rest", time(13), time(14), ActivityKind.REST, location=None)
WINDOW = item("rw", time(3), time(5, 30), ActivityKind.REST, location=None, role="rest_window", priority=10)
ITEMS = [WORK, SOCIAL, REST_ITEM, WINDOW]
HOST = PresenceReading("NONE", None)
QUIET = InterruptFacts()


def at(location):
    return PresenceReading("ACTIVE", location)


def slot_at(h, mi=0, *, items=ITEMS, override=None, marks=()):
    return resolver.resolve(items, override, tpe(h, mi), TZ, plan_revision=1, marks=marks)


def req(authority="owner", target="desktop", *, tier=None, ref=None, **kw):
    ref = ref or (PLANNER if authority == "system" else "discord:1")
    return ArbiterRequest(request_id="r-1", authority=authority, requester_ref=ref, actor_authenticated=True,
                          target=target, tier=tier, **kw)


def admin(target="desktop", **kw):
    return req("member", target, tier=RequesterTier.ADMIN, **kw)


def decide(request, slot, presence=HOST, override=None, probe=QUIET, **kw):
    return ar.arbitrate_request(request, slot=slot, presence=presence, override=override, probe=probe,
                                now=tpe(slot.starts_at.astimezone(TPE).hour, 30), **kw)


# ---- 契約：min_rank／interrupt_timing 與相容讀取 ----

def test_interrupt_policy_compat_read_mapping():
    assert split_interrupt_policy(None) == (None, None)
    assert split_interrupt_policy(InterruptPolicy.FREE) == (None, InterruptTiming.FREE)
    assert split_interrupt_policy("safe_point") == (None, InterruptTiming.SAFE_POINT)
    assert split_interrupt_policy(InterruptPolicy.OWNER_ONLY) == (RequesterTier.OWNER, InterruptTiming.FREE)
    with pytest.raises(ValueError):
        split_interrupt_policy("bogus")


def test_plan_item_and_slot_compat_construction():
    legacy = item("g", time(13), time(14), ActivityKind.REST, location=None, interrupt_policy=InterruptPolicy.OWNER_ONLY)
    assert (legacy.min_rank, legacy.interrupt_timing) == (RequesterTier.OWNER, InterruptTiming.FREE)
    new = item("g", time(13), time(14), ActivityKind.REST, location=None, min_rank=RequesterTier.ADMIN,
               interrupt_timing=InterruptTiming.SAFE_POINT, interrupt_policy=InterruptPolicy.OWNER_ONLY)
    assert (new.min_rank, new.interrupt_timing) == (RequesterTier.ADMIN, InterruptTiming.SAFE_POINT)  # 新欄位優先
    assert item("x", time(1), time(2), ActivityKind.REST, location=None).min_rank is None
    for bad in (RequesterTier.GUEST, RequesterTier.EMERGENCY, 75):
        with pytest.raises(ValueError):
            item("b", time(1), time(2), ActivityKind.REST, location=None, min_rank=bad)
    # 投影：min_rank 由行程項帶入；舊值位置參數仍可建構 PlanSlot
    slot = slot_at(13, 30, items=[legacy])
    assert (slot.min_rank, slot.interrupt_timing) == (RequesterTier.OWNER, InterruptTiming.FREE)
    old_style = PlanSlot("available", "discord", ActivityKind.WORK, PlanSource.ITEM, "x", InterruptPolicy.OWNER_ONLY, (),
                         tpe(9), None, None, 1)
    assert (old_style.min_rank, old_style.interrupt_timing) == (RequesterTier.OWNER, InterruptTiming.FREE)
    assert slot_at(11, 30).interrupt_timing == InterruptTiming.FREE and slot_at(9, 30).interrupt_timing == (
        InterruptTiming.SAFE_POINT)


# ---- ADMIN 生效；各格受理規則（§3.4）----

def test_admin_is_accepted_in_work_social_and_standby_but_not_in_sleep_window():
    for slot in (slot_at(9, 30), slot_at(11, 30), slot_at(15), slot_at(13, 30)):
        d = decide(admin(), slot, at("discord"))
        assert (d.outcome, d.reason_code, d.priority) == ("ACCEPTED_TRANSFER", ar.R_ACCEPTED, 75), slot.source
    d = decide(admin(), slot_at(4))  # 管理員不可吵醒睡眠（Q-PM-5）
    assert (d.outcome, d.reason_code) == ("REJECTED", ar.R_MEMBER_NOT_ACCEPTED)
    assert (d.blocked_axis, d.required, d.actual) == ("tier", 100, 75) and d.also_failed == ()


def test_member_is_rejected_in_work_social_and_sleep_but_accepted_in_standby():
    assert decide(req("member"), slot_at(15)).outcome == "ACCEPTED_TRANSFER"
    assert decide(req("member"), slot_at(9, 30)).reason_code == ar.R_WORK_IN_PROGRESS
    assert decide(req("member"), slot_at(11, 30)).reason_code == ar.R_MEMBER_NOT_ACCEPTED
    sleep = decide(req("member"), slot_at(4))
    assert sleep.reason_code == ar.R_MEMBER_NOT_ACCEPTED
    # 睡眠窗：TEMP_SESSION（ADMIN）與 floor（SYSTEM）都不足 → 第一個擋下的是指示等級，另一軸進 also_failed
    assert (sleep.blocked_axis, sleep.also_failed, sleep.required, sleep.actual) == ("instruction", ("tier",), 75, 50)
    assert sleep.policy_revision == 1


def test_sleep_window_accepts_owner_and_trusted_system_only():
    for authority in ("owner", "system"):
        assert decide(req(authority), slot_at(4)).outcome == "ACCEPTED_TRANSFER"


def test_paused_work_still_uses_work_slot_rules_even_though_standby():
    paused = slot_at(9, 30, marks=[SlotMark("pause", "work", tpe(9), tpe(9, 10))])
    assert paused.standby is True and paused.slot_state == "paused"
    assert decide(req("member"), paused, at("discord")).reason_code == ar.R_WORK_IN_PROGRESS
    assert decide(admin(), paused, at("desktop")).outcome == "ACCEPTED_TRANSFER"


def test_leave_and_abandon_rules_are_unchanged_for_work():
    work = slot_at(9, 30)
    for request in (req("owner", "host"), admin("host"), req("system", "host"), req("member", "host")):
        d = decide(request, work, at("discord"))
        assert d.reason_code == ar.R_WORK_IN_PROGRESS and d.blocked_axis == "instruction" and d.required == 200
    abandon = decide(req("owner", "host", explicit_abandon=True), work, at("discord"))
    assert (abandon.outcome, abandon.override_action) == ("ACCEPTED_TRANSFER", "mark_abandon")
    # 只有 owner（SESSION_INTERRUPT）能明示放棄：ADMIN／system 帶 explicit_abandon 也不行
    for request in (admin("host", explicit_abandon=True), req("system", "host", explicit_abandon=True),
                    admin("desktop", explicit_abandon=True)):
        assert decide(request, work, at("discord")).reason_code == ar.R_WORK_IN_PROGRESS


# ---- 系統元件（§1.2a、§3.4 名詞約定）----

def test_only_registered_system_components_that_may_propose_can_transfer():
    for slot in (slot_at(15), slot_at(9, 30), slot_at(11, 30), slot_at(4)):
        ok = decide(req("system", ref=PLANNER), slot, at("discord"))
        assert ok.outcome == "ACCEPTED_TRANSFER" and ok.priority == 100
        for ref in ("discord:1", "core.unknown", "core.chatroom"):
            d = decide(req("system", ref=ref), slot, at("discord"))
            assert (d.outcome, d.reason_code, d.blocked_axis) == ("REJECTED", ar.R_TIER_INSUFFICIENT, "tier"), (slot.source, ref)
    # 閉集內旗標為 True 的其他元件照旗標判（global_stop／health_watchdog 也 may_propose_transfer）
    assert decide(req("system", ref="core.global_stop"), slot_at(15)).outcome == "ACCEPTED_TRANSFER"
    # 「回主機」也是移轉：未登記的 system 不能結束 override
    ov = OverrideView("povr_1", "desktop", ActivityKind.WORK, tpe(8), tpe(23), datetime(2026, 11, 13, tzinfo=TPE),
                      "active", "r-0", "owner", 100)
    leave = decide(req("system", "host", ref="core.unknown"), slot_at(15, override=ov), at("desktop"), ov)
    assert leave.reason_code == ar.R_TIER_INSUFFICIENT


def test_emergency_path_is_unchanged_and_skips_component_check():
    d = decide(req("system", "desktop", emergency=True, ref="whatever"), slot_at(9, 30), at("discord"))
    assert (d.outcome, d.override_action, d.priority, d.blocked_axis) == ("ACCEPTED_TRANSFER", "begin", 300, None)


# ---- 格上 min_rank（§3.3、§3.6）----

def test_slot_min_rank_blocks_with_owner_only_or_min_rank_insufficient_codes():
    owner_only = item("g", time(13), time(14), ActivityKind.REST, location=None, min_rank=RequesterTier.OWNER)
    slot = slot_at(13, 30, items=[owner_only])
    d = decide(req("system"), slot)
    assert (d.reason_code, d.blocked_axis, d.required, d.actual) == (ar.R_OWNER_ONLY, "tier", 200, 100)
    assert decide(req("owner"), slot).outcome == "ACCEPTED_TRANSFER"
    admin_only = item("g", time(13), time(14), ActivityKind.REST, location=None, min_rank=RequesterTier.ADMIN)
    slot = slot_at(13, 30, items=[admin_only])
    assert decide(req("member"), slot).reason_code == ar.R_MIN_RANK_INSUFFICIENT
    assert decide(admin(), slot).outcome == "ACCEPTED_TRANSFER"
    # 工作格：三者取最大——min_rank 高於 TEMP_SESSION 時，ADMIN 被 min_rank 擋下，owner 通過
    guarded = item("w", time(9), time(10), ActivityKind.WORK, min_rank=RequesterTier.SYSTEM)
    work = slot_at(9, 30, items=[guarded])
    d = decide(admin(), work, at("discord"))
    assert (d.reason_code, d.blocked_axis, d.required) == (ar.R_MIN_RANK_INSUFFICIENT, "tier", 100)
    assert decide(req("system"), work, at("discord")).outcome == "ACCEPTED_TRANSFER"


def test_grade_axis_follows_the_policy_table():
    # 對照表被重新設定後，work_grade 推導的門檻自動連動（§3.3）；這裡只驗仲裁器消費 policy，不驗單調性
    rules = dict(DEFAULT_RULES)
    rules[InstructionLevel.TASK_NORMAL] = InstructionRule(RequesterTier.SYSTEM)
    policy = PermissionPolicy(7, rules)
    d = decide(admin(), slot_at(9, 30), at("discord"), policy=policy)
    assert (d.reason_code, d.blocked_axis, d.required, d.policy_revision) == (ar.R_WORK_IN_PROGRESS, "grade", 100, 7)
    assert decide(req("system"), slot_at(9, 30), at("discord"), policy=policy).outcome == "ACCEPTED_TRANSFER"
    # 預設表下 grade 軸恆被 TEMP_SESSION 蓋過
    assert decide(admin(), slot_at(9, 30), at("discord")).outcome == "ACCEPTED_TRANSFER"


# ---- interrupt_timing 與安全點（owner_only 行為變更）----

def test_owner_only_slot_no_longer_waits_for_interaction_but_uninterruptible_still_blocks():
    guarded = item("g", time(13), time(14), ActivityKind.REST, location=None, interrupt_policy=InterruptPolicy.OWNER_ONLY)
    slot = slot_at(13, 30, items=[guarded])
    owner = req("owner", "discord")
    d = decide(owner, slot, at("desktop"), probe=InterruptFacts(interaction_active=True))
    assert (d.outcome, d.reason_code) == ("ACCEPTED_TRANSFER", ar.R_ACCEPTED)  # 舊行為：DEFERRED(interaction_active)
    d = decide(owner, slot, at("desktop"), probe=InterruptFacts(has_uninterruptible_work=True))
    assert (d.outcome, d.reason_code, d.blocked_axis) == ("DEFERRED", ar.R_UNINTERRUPTIBLE_WORK, "timing")
    d = decide(owner, slot_at(4), HOST, probe=InterruptFacts(dream_in_flight=True))
    assert (d.reason_code, d.blocked_axis) == (ar.R_DREAM_IN_FLIGHT, "timing")


def test_safe_point_timing_still_defers_on_interaction():
    d = decide(req("owner", "discord"), slot_at(15), at("desktop"), probe=InterruptFacts(interaction_active=True))
    assert (d.outcome, d.reason_code, d.blocked_axis) == ("DEFERRED", ar.R_INTERACTION_ACTIVE, "timing")
    explicit = item("g", time(13), time(14), ActivityKind.REST, location=None, interrupt_timing=InterruptTiming.SAFE_POINT,
                    min_rank=RequesterTier.OWNER)
    d = decide(req("owner", "discord"), slot_at(13, 30, items=[explicit]), at("desktop"),
               probe=InterruptFacts(interaction_active=True))
    assert d.reason_code == ar.R_INTERACTION_ACTIVE


# ---- 結束 override（§3.5 階級條款）----

def test_ending_override_needs_temp_session_and_tier_not_below_creator():
    def ov(priority):
        return OverrideView("povr_1", "desktop", ActivityKind.WORK, tpe(8), tpe(23), datetime(2026, 11, 13, tzinfo=TPE),
                            "active", "r-0", "member", priority)

    member_ov = ov(50)
    slot = slot_at(15, override=member_ov)
    for request in (admin("host"), req("owner", "host")):
        d = decide(request, slot, at("desktop"), member_ov)
        assert (d.override_action, d.return_to_plan) == ("end", True)
    d = decide(req("member", "host"), slot, at("desktop"), member_ov)  # 同階但沒有 TEMP_SESSION 權限
    assert (d.reason_code, d.blocked_axis, d.required) == (ar.R_NOT_OVERRIDE_OWNER, "instruction", 75)
    owner_ov = ov(200)
    d = decide(admin("host"), slot_at(15, override=owner_ov), at("desktop"), owner_ov)  # 階級低於建立者的帶
    assert (d.reason_code, d.blocked_axis, d.required, d.actual) == (ar.R_NOT_OVERRIDE_OWNER, "tier", 200, 75)
    d = decide(admin("discord"), slot_at(15, override=owner_ov), at("desktop"), owner_ov)
    assert (d.reason_code, d.blocked_axis) == (ar.R_LOWER_THAN_OVERRIDE, "tier")
    emergency_ov = ov(300)
    assert decide(req("owner", "host"), slot_at(15, override=emergency_ov), at("desktop"), emergency_ov
                  ).reason_code == ar.R_EMERGENCY_OVERRIDE_ACTIVE
    assert decide(req("system", "host"), slot_at(15, override=emergency_ov), at("desktop"), emergency_ov
                  ).override_action == "end"


# ---- reason_code 與審計欄位 ----

def test_new_reason_codes_registered_and_old_ones_kept():
    assert {ar.R_MIN_RANK_INSUFFICIENT, ar.R_TIER_INSUFFICIENT, ar.R_TIER_BELOW_FLOOR} <= ar.REASON_CODES
    for old in ("work_in_progress", "member_not_accepted_in_slot", "owner_only", "lower_priority_than_override",
                "not_override_owner", "emergency_override_active", "interaction_active", "uninterruptible_work"):
        assert old in ar.REASON_CODES


def test_accepted_and_state_decisions_carry_no_audit_axis():
    d = decide(req("owner"), slot_at(15))
    assert (d.blocked_axis, d.also_failed, d.required, d.actual) == (None, (), None, None)
    d = decide(req("owner"), slot_at(15), PresenceReading("RECOVERING", None))
    assert d.reason_code == ar.R_PRESENCE_RECOVERING and d.blocked_axis is None


def test_audit_fields_are_allowed_in_event_payload_keys():
    keys = DEFAULT_REGISTRY.lookup("schedule.arbitration_decided").payload_keys
    assert {"blocked_axis", "also_failed", "required", "actual", "policy_revision"} <= keys
