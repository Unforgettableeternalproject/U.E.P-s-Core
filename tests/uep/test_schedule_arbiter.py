"""AR-1：在場請求仲裁（純函式；VC-PP-18/19/20 的仲裁部分、PP-6/7/9/12/13/17/18/20/21）。不碰 DB。"""
import itertools
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from uep.contracts.platform import PRESENCE_REQUEST_AUTHORITIES
from uep.contracts.schedule import (
    PRIORITY_EMERGENCY,
    PRIORITY_OWNER,
    PRIORITY_PLAN,
    PRIORITY_REQUESTER,
    PRIORITY_SYSTEM,
    ActivityKind,
    ArbiterRequest,
    InterruptFacts,
    InterruptPolicy,
    OverrideView,
    PlanItem,
    SlotMark,
)
from uep.schedule import arbiter as ar
from uep.schedule import resolver
from uep.schedule.planner import PresenceReading

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)


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
NOW = tpe(9, 30)


def slot_at(h, mi=0, *, items=ITEMS, override=None, marks=()):
    return resolver.resolve(items, override, tpe(h, mi), TZ, plan_revision=1, marks=marks)


def req(authority="owner", target="desktop", **kw):
    base = {"request_id": "r-1", "authority": authority, "requester_ref": "discord:1", "actor_authenticated": True,
            "target": target}
    base.update(kw)
    return ArbiterRequest(**base)


HOST = PresenceReading("NONE", None)


def at(location):
    return PresenceReading("ACTIVE", location)


QUIET = InterruptFacts()


def decide(request, slot, presence=HOST, override=None, probe=QUIET, now=None, **kw):
    return ar.arbitrate_request(request, slot=slot, presence=presence, override=override, probe=probe,
                                now=now or tpe(slot.starts_at.astimezone(TPE).hour, 30), **kw)


def override(priority=PRIORITY_OWNER, status="active", expires=None):
    return OverrideView("povr_1", "desktop", ActivityKind.WORK, tpe(8), expires or tpe(23), datetime(2026, 11, 13, tzinfo=TPE),
                        status, "r-0", "owner", priority)


# ---- 契約 ----

def test_priority_bands_and_contract_validation():
    assert (PRIORITY_PLAN, PRIORITY_REQUESTER, PRIORITY_SYSTEM, PRIORITY_OWNER, PRIORITY_EMERGENCY) == (
        0, 50, 100, 200, 300)
    assert ar.priority_of(req("member")) == 50 and ar.priority_of(req("system")) == 100
    assert ar.priority_of(req("owner")) == 200 and ar.priority_of(req("system", emergency=True)) == 300
    assert PRESENCE_REQUEST_AUTHORITIES == {"owner", "system"}  # 既有請求的建構路徑不變
    for bad in ({"authority": "guest"}, {"target": "mars"}, {"request_id": "has space"}):
        with pytest.raises(ValueError):
            req(**bad)


def test_unauthenticated_and_bad_emergency_are_rejected():
    slot = slot_at(12)
    assert decide(req(actor_authenticated=False), slot).reason_code == ar.R_UNAUTHENTICATED
    assert decide(req("owner", emergency=True), slot).reason_code == ar.R_EMERGENCY_REQUIRES_SYSTEM


# ---- 受理分流 ----

def test_standby_slots_accept_by_priority_including_member():
    for slot in (slot_at(15), slot_at(13, 30)):  # 預設層、rest 行程項
        for authority, band in (("member", 50), ("system", 100), ("owner", 200)):
            d = decide(req(authority, "desktop"), slot)
            assert (d.outcome, d.reason_code, d.priority) == ("ACCEPTED_TRANSFER", ar.R_ACCEPTED, band)
            assert (d.override_action, d.transfer_target, d.override_location) == ("begin", "desktop", "desktop")


def test_work_accepts_only_explicit_abandon_and_emergency():  # PP-18／PP-20
    work = slot_at(9, 30)
    for authority in ("owner", "system", "member"):
        assert decide(req(authority, "discord"), work).reason_code == ar.R_WORK_IN_PROGRESS
        assert decide(req(authority, "host"), work).reason_code == ar.R_WORK_IN_PROGRESS  # 回主機不是臨時指示（PP-17）
    paused = slot_at(9, 30, marks=[SlotMark("pause", "work", tpe(9), tpe(9, 10))])
    assert paused.slot_state == "paused"
    assert decide(req("owner", "host"), paused, at("discord")).reason_code == ar.R_WORK_IN_PROGRESS
    abandon = decide(req("owner", "host", explicit_abandon=True), work, at("discord"))
    assert (abandon.outcome, abandon.override_action, abandon.transfer_target) == (
        "ACCEPTED_TRANSFER", "mark_abandon", "host")
    assert decide(req("owner", "desktop", explicit_abandon=True), work).reason_code == ar.R_ABANDON_REQUIRES_HOST
    # 放棄訊號只認 owner（PP-21）：system 帶 explicit_abandon 也不行
    assert decide(req("system", "host", explicit_abandon=True), work).reason_code == ar.R_WORK_IN_PROGRESS
    emergency = decide(req("system", "desktop", emergency=True), work, at("discord"))
    assert (emergency.outcome, emergency.override_action, emergency.priority) == ("ACCEPTED_TRANSFER", "begin", 300)


def test_social_and_sleep_window_accept_only_owner_and_system():
    for slot in (slot_at(11, 30), slot_at(4)):
        assert decide(req("member", "desktop"), slot).reason_code == ar.R_MEMBER_NOT_ACCEPTED
        assert decide(req("owner", "desktop"), slot).outcome == "ACCEPTED_TRANSFER"
        assert decide(req("system", "desktop"), slot).outcome == "ACCEPTED_TRANSFER"


def test_leave_outside_work_abandons_the_current_item_or_just_moves():  # §9.1 (c)
    social = decide(req("owner", "host"), slot_at(11, 30), at("discord"))
    assert (social.override_action, social.transfer_target) == ("mark_abandon", "host")
    default = decide(req("owner", "host"), slot_at(15), at("desktop"))
    assert (default.override_action, default.transfer_target) == ("none", "host")  # 預設層沒有行程項可放棄
    assert decide(req("owner", "host"), slot_at(15), HOST).reason_code == ar.R_ALREADY_AT_TARGET


def test_owner_only_interrupt_policy():
    guarded = item("g", time(13), time(14), ActivityKind.REST, location=None, interrupt_policy=InterruptPolicy.OWNER_ONLY)
    slot = slot_at(13, 30, items=[guarded])
    assert decide(req("system", "desktop"), slot).reason_code == ar.R_OWNER_ONLY
    assert decide(req("owner", "desktop"), slot).outcome == "ACCEPTED_TRANSFER"


# ---- override：只比優先序、Q-PP-5 ----

def test_existing_override_priority_rules():
    ov_slot = slot_at(15, override=override(PRIORITY_REQUESTER))
    higher = decide(req("owner", "discord"), ov_slot, at("desktop"), override(PRIORITY_REQUESTER))
    assert (higher.outcome, higher.override_action, higher.transfer_target) == (
        "ACCEPTED_TRANSFER", "supersede", "discord")
    owner_ov = override(PRIORITY_OWNER)
    ov_slot = slot_at(15, override=owner_ov)
    lower = decide(req("member", "discord"), ov_slot, at("desktop"), owner_ov)
    assert (lower.outcome, lower.reason_code) == ("REJECTED", ar.R_LOWER_THAN_OVERRIDE)
    same_owner = decide(req("owner", "discord"), ov_slot, at("desktop"), owner_ov)
    assert (same_owner.outcome, same_owner.override_action) == ("ACCEPTED_TRANSFER", "supersede")  # Q-PP-5 後到者優先
    member_ov = override(PRIORITY_REQUESTER)
    same_member = decide(req("member", "discord"), slot_at(15, override=member_ov), at("desktop"), member_ov)
    assert (same_member.outcome, same_member.reason_code) == ("QUEUED", ar.R_SAME_PRIORITY_QUEUED)  # 其餘 FIFO
    # 可替換：換成「先到者優先」
    first_wins = decide(req("owner", "discord"), ov_slot, at("desktop"), owner_ov, same_band_policy=lambda r, o: False)
    assert first_wins.outcome == "QUEUED"


def test_leave_while_override_ends_it_and_returns_to_plan():  # PP-9
    ov = override()
    slot = slot_at(15, override=ov)
    for authority in ("owner", "system"):
        d = decide(req(authority, "host"), slot, at("desktop"), ov)
        assert (d.outcome, d.override_action, d.override_end_reason, d.return_to_plan, d.transfer_target) == (
            "ACCEPTED_TRANSFER", "end", "owner_ended", True, None)
    assert decide(req("member", "host"), slot, at("desktop"), ov).reason_code == ar.R_NOT_OVERRIDE_OWNER


def test_expired_override_row_is_ignored():
    ov = override(expires=tpe(9))
    d = decide(req("member", "desktop"), slot_at(15), HOST, ov, now=tpe(10))
    assert d.override_action == "begin"  # 過期列不當成生效的 override


def test_emergency_overrides_everything_and_never_abandons():
    ov = override()
    d = decide(req("system", "website", emergency=True), slot_at(15, override=ov), at("desktop"), ov,
               probe=InterruptFacts(True, True, True))
    assert (d.outcome, d.override_action, d.priority) == ("ACCEPTED_TRANSFER", "supersede", 300)
    busy = PresenceReading("NONE", None, transfer_active=True)
    assert decide(req("system", "desktop", emergency=True), slot_at(15), busy).outcome == "ACCEPTED_TRANSFER"
    assert decide(req("system", "desktop", emergency=True), slot_at(15), PresenceReading("RECOVERING", None)
                  ).outcome == "ACCEPTED_TRANSFER"
    work_emergency = decide(req("system", "desktop", emergency=True), slot_at(9, 30), at("discord"))
    assert work_emergency.override_action == "begin" and work_emergency.override_action != "mark_abandon"


# ---- InterruptProbe：安全點 ----

def test_safe_point_deferrals_and_free_policy():
    owner = req("owner", "discord")
    default = slot_at(15)
    assert decide(owner, default, at("desktop"), probe=InterruptFacts(interaction_active=True)).reason_code == (
        ar.R_INTERACTION_ACTIVE)
    assert decide(owner, default, at("desktop"), probe=InterruptFacts(has_uninterruptible_work=True)
                  ).reason_code == ar.R_UNINTERRUPTIBLE_WORK
    # social 格 interrupt_policy=free：進行中的互動不用等；不可中斷工作照等
    social = slot_at(11, 30)
    assert social.interrupt_policy == InterruptPolicy.FREE
    assert decide(owner, social, at("desktop"), probe=InterruptFacts(interaction_active=True)).outcome == (
        "ACCEPTED_TRANSFER")
    assert decide(owner, social, at("desktop"), probe=InterruptFacts(has_uninterruptible_work=True)).outcome == (
        "DEFERRED")
    # 在主機做夢中被叫：先做完這一輪；沒在做夢就直接叫醒
    window = slot_at(4)
    assert decide(owner, window, HOST, probe=InterruptFacts(dream_in_flight=True)).reason_code == ar.R_DREAM_IN_FLIGHT
    assert decide(owner, window, HOST, probe=QUIET).outcome == "ACCEPTED_TRANSFER"
    # 目標就是目前位置：不離開，不用等
    assert decide(req("owner", "desktop"), default, at("desktop"), probe=InterruptFacts(interaction_active=True)
                  ).transfer_target is None


def test_transfer_in_progress_queues_and_recovering_defers():
    busy = PresenceReading("NONE", None, transfer_active=True)
    d = decide(req("owner", "desktop"), slot_at(15), busy)
    assert (d.outcome, d.reason_code) == ("QUEUED", ar.R_TRANSFER_IN_PROGRESS)  # 進行中的移轉不中斷
    d = decide(req("owner", "desktop"), slot_at(15), PresenceReading("RECOVERING", None))
    assert (d.outcome, d.reason_code) == ("DEFERRED", ar.R_PRESENCE_RECOVERING)
    expired_lease = PresenceReading("ACTIVE", "desktop", expires_at=tpe(9))
    assert ar.actual_location(PresenceReading("ACTIVE", "desktop")) == "desktop" and expired_lease.status == "ACTIVE"


# ---- directive（PLAN，最低優先）----

def directive_for(slot, now=NOW):
    return resolver.reconcile_directive(slot, None, now)


def test_directive_arbitration():
    slot = slot_at(9, 30)
    directive = directive_for(slot)
    assert directive is not None and directive.target == "discord"
    d = ar.arbitrate_directive(directive, slot=slot, presence=HOST, override=None, probe=QUIET, now=NOW)
    assert (d.outcome, d.priority, d.override_action, d.transfer_target) == ("ACCEPTED_TRANSFER", 0, "none", "discord")
    cases = [
        ({"presence": at("discord")}, ar.R_ALREADY_AT_TARGET, "REJECTED"),
        ({"override": override()}, ar.R_OVERRIDE_ACTIVE, "REJECTED"),
        ({"presence": PresenceReading("NONE", None, transfer_active=True)}, ar.R_TRANSFER_IN_PROGRESS, "QUEUED"),
        ({"presence": PresenceReading("RECOVERING", None)}, ar.R_PRESENCE_RECOVERING, "DEFERRED"),
        ({"probe": InterruptFacts(dream_in_flight=True)}, ar.R_DREAM_IN_FLIGHT, "DEFERRED"),
        ({"presence": at("desktop"), "probe": InterruptFacts(interaction_active=True)}, ar.R_INTERACTION_ACTIVE,
         "DEFERRED"),
        ({"now": NOW + timedelta(hours=2)}, ar.R_DIRECTIVE_EXPIRED, "REJECTED"),
    ]
    for kw, reason, outcome in cases:
        args = {"slot": slot, "presence": HOST, "override": None, "probe": QUIET, "now": NOW, **kw}
        got = ar.arbitrate_directive(directive, **args)
        assert (got.outcome, got.reason_code) == (outcome, reason), kw
    stale = slot_at(15)  # 行程已換格（預設層在主機）：directive 的目標不再是當前格的位置
    got = ar.arbitrate_directive(directive, slot=stale, presence=HOST, override=None, probe=QUIET, now=NOW)
    assert got.reason_code == ar.R_STALE_DIRECTIVE
    unavailable = resolver.resolve(ITEMS, None, NOW, "Not/AZone")
    got = ar.arbitrate_directive(directive, slot=unavailable, presence=HOST, override=None, probe=QUIET, now=NOW)
    assert got.reason_code == ar.R_STALE_DIRECTIVE


# ---- 性質：純函式、固定 reason code、不丟例外 ----

def test_decisions_are_total_pure_and_use_fixed_reason_codes():
    slots = [slot_at(h, m) for h, m in ((9, 30), (11, 30), (13, 30), (15, 0), (4, 0))]
    presences = [HOST, at("desktop"), at("discord"), PresenceReading("RECOVERING", None),
                 PresenceReading("NONE", None, transfer_active=True)]
    probes = [QUIET, InterruptFacts(True, False, False), InterruptFacts(False, True, True)]
    overrides = [None, override(PRIORITY_REQUESTER), override(PRIORITY_OWNER)]
    seen = set()
    for slot, presence, probe, ov, authority, target, emergency, abandon in itertools.product(
            slots, presences, probes, overrides, ("owner", "system", "member"), ("host", "desktop", "discord"),
            (False, True), (False, True)):
        request = req(authority, target, emergency=emergency, explicit_abandon=abandon)
        d1 = decide(request, slot, presence, ov, probe)
        d2 = decide(request, slot, presence, ov, probe)
        assert d1 == d2
        assert d1.reason_code in ar.REASON_CODES and d1.outcome in ("ACCEPTED_TRANSFER", "QUEUED", "DEFERRED", "REJECTED")
        if d1.outcome == "ACCEPTED_TRANSFER":
            assert d1.priority == ar.priority_of(request) and d1.override_location in (None, target)
            assert d1.override_action != "begin" or d1.override_location in ("desktop", "discord")
            assert not (emergency and d1.override_action == "mark_abandon")  # 緊急不放棄工作項
        seen.add(d1.outcome)
    assert seen == {"ACCEPTED_TRANSFER", "QUEUED", "DEFERRED", "REJECTED"}
