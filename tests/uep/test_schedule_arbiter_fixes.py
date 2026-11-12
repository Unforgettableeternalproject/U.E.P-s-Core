"""仲裁器審查修正：M1 abandon 適用非工作格、M2 先算 mark、m1 授權先於排隊、m2 already_at_target、m3 緊急 override。不碰 DB。"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

from uep.contracts.schedule import (
    PRIORITY_EMERGENCY,
    AbandonWorkSignal,
    ActivityKind,
    ArbiterRequest,
    InterruptFacts,
    OverrideView,
    PlanItem,
    SlotMark,
)
from uep.schedule import abandon as ab
from uep.schedule import arbiter as ar
from uep.schedule import resolver
from uep.schedule.planner import PresenceReading

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)


def tpe(h, mi=0, d=12):
    return datetime(2026, 11, d, h, mi, tzinfo=TPE)


def item(item_id, start, end, kind, location="discord", role="user"):
    return PlanItem(item_id, kind, location, "weekly", role=role, weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=start,
                    local_end=end, tz=TZ)


SOCIAL = item("social", time(11), time(12), ActivityKind.SOCIAL)
WORK = item("work", time(9), time(10), ActivityKind.WORK)
WINDOW = item("rw", time(3), time(5, 30), ActivityKind.REST, location=None, role="rest_window")
HOST = PresenceReading("NONE", None)
QUIET = InterruptFacts()


def req(authority="owner", target="host", **kw):
    base = {"request_id": "r-1", "authority": authority, "requester_ref": "discord:1", "actor_authenticated": True,
            "target": target}
    base.update(kw)
    return ArbiterRequest(**base)


def decide(request, slot, presence=HOST, override=None, probe=QUIET, now=None):
    return ar.arbitrate_request(request, slot=slot, presence=presence, override=override, probe=probe,
                                now=now or tpe(11, 30))


def slot_at(h, mi=0, items=(SOCIAL, WORK, WINDOW), marks=(), override=None):
    return resolver.resolve(list(items), override, tpe(h, mi), TZ, plan_revision=1, marks=marks)


# ---- M1：resolver 的 abandon 適用任何非 rest_window 行程項 ----

def test_abandon_mark_turns_a_social_item_into_host_rest():
    base = slot_at(11, 30)
    mark = SlotMark("abandon", "social", base.blueprint_start_at, tpe(11, 20))
    s = slot_at(11, 30, marks=[mark])
    assert (s.slot_state, s.location, s.activity_kind, s.standby) == ("abandoned", "host", ActivityKind.REST, True)
    assert s.item_ref == "social" and s.suggestions[0].reason_code == "plan_abandoned"
    assert slot_at(11, 10, marks=[mark]).slot_state == "normal"  # mark 之前不受影響
    assert slot_at(12, 30, marks=[mark]).source.value == "default"  # 格結束後依藍圖


def test_pause_still_only_applies_to_work_and_rest_window_cannot_be_abandoned():
    social_pause = SlotMark("pause", "social", slot_at(11, 30).blueprint_start_at, tpe(11, 20))
    assert slot_at(11, 30, marks=[social_pause]).slot_state == "normal"
    window = slot_at(4)
    window_abandon = SlotMark("abandon", "rw", window.blueprint_start_at, tpe(3, 30))
    assert slot_at(4, marks=[window_abandon]).slot_state == "normal"  # 休息窗口固定，不可放棄


def test_signal_path_still_accepts_only_work():  # PP-21
    signal = AbandonWorkSignal(
        signal_id="abandon:u1", actor_ref="discord:1", actor_authenticated=True, source_platform="discord",
        path="turn", decided_at=tpe(11, 25), decider_id="rules.abandon_work", decider_version="1")
    social = slot_at(11, 30)
    res = ab.consume_abandon(signal, is_owner=True, now=tpe(11, 30), slot=social, tz=TZ)
    assert (res.status, res.reason_code) == ("IGNORED", "not_in_work")
    work = slot_at(9, 30)
    ok = ab.consume_abandon(signal, is_owner=True, now=tpe(9, 30), slot=work, tz=TZ)
    assert ok.status == "ACCEPTED" and ok.mark.item_ref == "work"


def test_end_to_end_arbiter_to_mark_to_projection():
    for presence in (PresenceReading("ACTIVE", "discord"), HOST):  # 人在 discord 或已在主機，行程格都要轉 REST
        slot = slot_at(11, 30)
        decision = decide(req("owner", "host"), slot, presence)
        assert (decision.outcome, decision.override_action) == ("ACCEPTED_TRANSFER", "mark_abandon")
        assert decision.transfer_target == (None if presence is HOST else "host")
        mark = ar.abandon_mark_for(slot, "r-1", tpe(11, 30))
        assert mark is not None and mark.cause_request_id == "r-1" and mark.kind == "abandon"
        projected = slot_at(11, 31, marks=[mark])
        assert (projected.slot_state, projected.location, projected.activity_kind) == (
            "abandoned", "host", ActivityKind.REST)
        # 投影轉 REST 之後（待命）再次回主機：沒有可標記的格 → already_at_target
        again = decide(req("owner", "host", request_id="r-2"), projected, HOST, now=tpe(11, 32))
        assert again.reason_code == ar.R_ALREADY_AT_TARGET
    assert ar.abandon_mark_for(slot_at(15), "r-1", tpe(15)) is None  # 預設層沒有可標記的行程項


# ---- M2 ----

def test_leave_at_host_with_a_markable_slot_still_abandons_it():
    d = decide(req("owner", "host"), slot_at(11, 30), HOST)
    assert (d.outcome, d.override_action, d.transfer_target) == ("ACCEPTED_TRANSFER", "mark_abandon", None)
    default = decide(req("owner", "host"), slot_at(15), HOST, now=tpe(15))
    assert (default.outcome, default.reason_code) == ("REJECTED", ar.R_ALREADY_AT_TARGET)  # 沒有可標記的格
    work = decide(req("owner", "host", explicit_abandon=True), slot_at(9, 30), HOST, now=tpe(9, 30))
    assert (work.override_action, work.transfer_target) == ("mark_abandon", None)


# ---- m1：授權類拒絕先於 DEFERRED／QUEUED ----

def test_authorization_rejections_come_before_deferral_and_queueing():
    recovering = PresenceReading("RECOVERING", None)
    busy = PresenceReading("NONE", None, transfer_active=True)
    work = slot_at(9, 30)
    for presence in (recovering, busy):
        assert decide(req("owner", "discord"), work, presence, now=tpe(9, 30)).reason_code == ar.R_WORK_IN_PROGRESS
        assert decide(req("member", "desktop"), slot_at(11, 30), presence).reason_code == ar.R_MEMBER_NOT_ACCEPTED
    owner_ov = OverrideView("povr_1", "desktop", ActivityKind.WORK, tpe(8), tpe(23), tpe(0, d=13), "active", "r0",
                            "owner", 200)
    d = decide(req("member", "discord"), slot_at(15, override=owner_ov), busy, owner_ov, now=tpe(15))
    assert d.reason_code == ar.R_LOWER_THAN_OVERRIDE
    d = decide(req("member", "host"), slot_at(15, override=owner_ov), recovering, owner_ov, now=tpe(15))
    assert d.reason_code == ar.R_NOT_OVERRIDE_OWNER
    # 有資格的請求才會碰到狀態類的 DEFERRED／QUEUED
    assert decide(req("owner", "desktop"), slot_at(15), recovering, now=tpe(15)).outcome == "DEFERRED"
    assert decide(req("owner", "desktop"), slot_at(15), busy, now=tpe(15)).outcome == "QUEUED"


# ---- m2 ----

def test_move_to_where_she_already_is_and_plan_agrees_is_not_an_override():
    default_at_host = slot_at(15)
    d = decide(req("owner", "host"), default_at_host, HOST, now=tpe(15))
    assert d.reason_code == ar.R_ALREADY_AT_TARGET
    social_slot = slot_at(11, 30)  # 行程在 discord
    here = decide(req("owner", "discord"), social_slot, PresenceReading("ACTIVE", "discord"))
    assert (here.outcome, here.reason_code) == ("REJECTED", ar.R_ALREADY_AT_TARGET)
    # 人在 desktop、行程在 discord，請求 desktop：要 override 把她留在 desktop（不需移轉）
    hold = decide(req("owner", "desktop"), social_slot, PresenceReading("ACTIVE", "desktop"))
    assert (hold.override_action, hold.transfer_target) == ("begin", None)


# ---- m3 ----

def test_emergency_override_can_only_be_ended_by_system():
    emergency = OverrideView("povr_9", "desktop", ActivityKind.WORK, tpe(8), tpe(23), tpe(0, d=13), "active", "e",
                             "system", PRIORITY_EMERGENCY)
    slot = slot_at(15, override=emergency)
    here = PresenceReading("ACTIVE", "desktop")
    owner = decide(req("owner", "host"), slot, here, emergency, now=tpe(15))
    assert (owner.outcome, owner.reason_code) == ("REJECTED", ar.R_EMERGENCY_OVERRIDE_ACTIVE)
    system = decide(req("system", "host"), slot, here, emergency, now=tpe(15))
    assert (system.outcome, system.override_action, system.return_to_plan) == ("ACCEPTED_TRANSFER", "end", True)
    # 一般 owner override 仍可由 owner 結束
    normal = OverrideView("povr_8", "desktop", ActivityKind.WORK, tpe(8), tpe(23), tpe(0, d=13), "active", "r", "owner",
                          200)
    ended = decide(req("owner", "host"), slot_at(15, override=normal), here, normal, now=tpe(15))
    assert ended.override_action == "end"
    assert ar.R_EMERGENCY_OVERRIDE_ACTIVE in ar.REASON_CODES
