"""PP-1：行程持久化（需要 UEP_TEST_DATABASE_URL）。VC-PP-1/2/5/7/13/14/15/18/22 的 DB 部分與 migration 0021。"""
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from uep.contracts.schedule import ActivityKind, PlanSource
from uep.ids import is_valid_id, new_id
from uep.schedule import resolver
from uep.schedule.store import PlanActor, PlanItemCommand, PlanStore

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)
PLAN_TABLES = ("presence_plan_meta", "presence_plan_items", "presence_plan_overrides", "presence_plan_slot_marks")
OWNER = PlanActor("owner_console:dev", "owner_console", True, True)


def tpe(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TPE)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(tpe(2026, 11, 12, 10))


@pytest.fixture
def sf(engine, session_factory):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE presence_plan_meta, presence_plan_items, presence_plan_overrides, "
                          "presence_plan_slot_marks RESTART IDENTITY CASCADE"))
    return session_factory


@pytest.fixture
def store(sf, clock):
    return PlanStore(sf, clock=clock)


def work_cmd(**kw):
    base = {"activity_kind": "work", "location": "discord", "recurrence": "weekly", "weekdays": (1, 2, 3, 4, 5, 6, 7),
            "local_start": time(9), "local_end": time(12), "tz": TZ}
    base.update(kw)
    return PlanItemCommand(**base)


def create(store, **kw):
    res = store.create_item(OWNER, work_cmd(**kw), expected_revision=store.revision())
    assert res.status == "OK", res
    return res.ref_id


# ---- migration ----

def test_migration_0021_upgrade_downgrade_upgrade(engine, alembic_cfg):
    def tables():
        return set(inspect(engine).get_table_names())

    assert set(PLAN_TABLES) <= tables()
    command.downgrade(alembic_cfg, "-1")
    try:
        assert not set(PLAN_TABLES) & tables()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0020_decision_log_chain"
    finally:
        command.upgrade(alembic_cfg, "head")
    assert set(PLAN_TABLES) <= tables()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0021_presence_plan"


def test_id_prefixes_registered():
    for prefix in ("plan", "povr", "pmark"):
        assert is_valid_id(new_id(prefix), prefix)


# ---- 解析與儲存不可用 ----

def test_empty_store_resolves_default(store, clock):  # VC-PP-1
    s = store.resolve()
    assert (s.availability, s.location, s.source, s.plan_revision) == ("available", "host", PlanSource.DEFAULT, 0)
    assert store.revision() == 0


def test_store_failure_yields_unavailable_without_touching_data(sf, clock, store):  # VC-PP-7
    item_id = create(store)

    def broken():
        raise OperationalError("select", {}, Exception("db down"))

    bad = PlanStore(broken, clock=clock)
    s = bad.resolve()
    assert (s.availability, s.location, s.reason_code) == ("unavailable", "host", "store_unavailable")
    assert store.resolve().item_ref == item_id  # 資料未被誤刪，恢復後照常


# ---- items ----

def test_create_item_resolves_and_bumps_revision(store):
    item_id = create(store)
    assert is_valid_id(item_id, "plan") and store.revision() == 1
    s = store.resolve()
    assert (s.item_ref, s.location, s.plan_revision, s.activity_kind) == (item_id, "discord", 1, ActivityKind.WORK)


def test_invalid_commands_and_db_check(store, sf):  # VC-PP-2
    res = store.create_item(OWNER, work_cmd(location=None), expected_revision=0)
    assert (res.status, res.reason_code) == ("INVALID", "location_required_for_non_rest")
    assert store.revision() == 0
    for bad, reason in ((work_cmd(local_end=time(9)), "weekly_equal_times"), (work_cmd(tz="Not/AZone"), "invalid_tz"),
                        (work_cmd(weekdays=()), "invalid_weekdays"), (work_cmd(priority=100), "invalid_priority"),
                        (work_cmd(recurrence="rrule"), "invalid_recurrence")):
        assert store.create_item(OWNER, bad, expected_revision=0).reason_code == reason
    with sf() as s, pytest.raises(IntegrityError):  # DB CHECK 同樣擋 location NULL＋work
        s.execute(text(
            "INSERT INTO presence_plan_items (item_id, persona_id, role, activity_kind, location, recurrence, weekdays, "
            "local_start, local_end, tz, created_by, updated_by) VALUES ('plan_x', 'uep', 'user', 'work', NULL, "
            "'weekly', ARRAY[1], '09:00', '10:00', 'Asia/Taipei', 'a', 'a')"))
        s.commit()


def test_owner_gate_and_revision_conflict(store):  # VC-PP-14
    cmd = work_cmd()
    for actor in (PlanActor("owner_console:dev", "owner_console", False, True),
                  PlanActor("owner_console:dev", "owner_console", True, False),
                  PlanActor("Bernie The Owner", "owner_console", True, True),  # 顯示名稱當 ID
                  PlanActor("discord:1", "discord", True, True)):
        assert store.create_item(actor, cmd, expected_revision=0).status == "NOT_OWNER"
    with pytest.raises(PermissionError):
        store.snapshot(PlanActor("x", "owner_console", False, False))
    item_id = create(store)
    stale = store.update_item(OWNER, item_id, work_cmd(priority=60), expected_revision=0)
    assert (stale.status, stale.revision) == ("REVISION_CONFLICT", 1)
    ok = store.update_item(OWNER, item_id, work_cmd(priority=60), expected_revision=1)
    assert ok.status == "OK" and ok.revision == 2


def test_rest_window_set_update_not_deletable(store):  # VC-PP-6/14
    res = store.set_rest_window(OWNER, local_start=time(3), local_end=time(5, 30), tz=TZ, expected_revision=0)
    assert res.status == "OK"
    window_id = res.ref_id
    assert store.resolve(tpe(2026, 11, 12, 4)).source == PlanSource.REST_WINDOW
    assert store.resolve(tpe(2026, 11, 12, 6)).source == PlanSource.DEFAULT
    assert store.delete_item(OWNER, window_id, expected_revision=1).reason_code == "rest_window_not_deletable"
    assert store.update_item(OWNER, window_id, work_cmd(), expected_revision=1).reason_code == "use_set_rest_window"
    again = store.set_rest_window(OWNER, local_start=time(2), local_end=time(4), tz=TZ, expected_revision=1)
    assert again.ref_id == window_id  # 更新同一筆，不新增第二個窗口
    assert store.resolve(tpe(2026, 11, 12, 2, 30)).source == PlanSource.REST_WINDOW


def test_delete_item_soft_deletes_and_ends_marks(store, sf):
    item_id = create(store)
    store.begin_mark(kind="pause", item_ref=item_id, planned_start_at=tpe(2026, 11, 12, 9), cause_request_id="c1")
    assert store.delete_item(OWNER, item_id, expected_revision=store.revision()).status == "OK"
    assert store.resolve().source == PlanSource.DEFAULT
    assert store.active_marks() == ()
    with sf() as s:
        assert s.execute(text("SELECT count(*) FROM presence_plan_items WHERE deleted_at IS NOT NULL")).scalar_one() == 1


def test_user_item_limit(store, sf):
    with sf() as s:
        s.execute(text(
            "INSERT INTO presence_plan_items (item_id, persona_id, role, activity_kind, location, recurrence, "
            "weekdays, local_start, local_end, tz, created_by, updated_by) SELECT 'plan_' || g, 'uep', 'user', 'work', "
            "'discord', 'weekly', ARRAY[1], '09:00', '10:00', 'Asia/Taipei', 'a', 'a' FROM generate_series(1, 200) g"))
        s.commit()
    assert store.create_item(OWNER, work_cmd(), expected_revision=0).status == "LIMIT_EXCEEDED"


def test_tz_change_applies_to_new_rounds(store, clock):  # VC-PP-22：meta.tz 變更後新建 override 以新時區計算日界
    clock.now = datetime(2026, 11, 12, 15, tzinfo=UTC)  # 台北 23:00、紐約 10:00
    first = store.begin_override(cause_request_id="r1", authority="owner", priority=200, location="desktop",
                                 activity_kind=ActivityKind.WORK, expires_at=clock.now + timedelta(hours=8))
    assert store.active_override().round_end_at == tpe(2026, 11, 13)
    store.end_override(first, "completed")
    assert store.set_tz(OWNER, "America/New_York", expected_revision=store.revision()).status == "OK"
    store.begin_override(cause_request_id="r2", authority="owner", priority=200, location="desktop",
                         activity_kind=ActivityKind.WORK, expires_at=clock.now + timedelta(hours=20))
    assert store.active_override().round_end_at == datetime(2026, 11, 13, 5, tzinfo=UTC)  # 紐約 11/13 00:00


# ---- overrides ----

def test_override_lifecycle_and_idempotency(store, clock):  # VC-PP-5/15/18
    item_id = create(store)
    expires = clock.now + timedelta(hours=1)
    oid = store.begin_override(cause_request_id="req-1", authority="owner", priority=200, location="desktop",
                               activity_kind=ActivityKind.WORK, expires_at=expires)
    assert is_valid_id(oid, "povr")
    assert store.begin_override(cause_request_id="req-1", authority="owner", priority=200, location="desktop",
                                activity_kind=ActivityKind.WORK, expires_at=expires) == oid
    s = store.resolve()
    assert (s.source, s.location, s.override_state) == (PlanSource.OVERRIDE, "desktop", "pending")
    assert store.activate_override(oid) is True and store.activate_override(oid) is False
    assert store.resolve().override_state == "active"
    clock.now = expires  # expires_at 當下自動回到底下行程
    assert store.resolve().item_ref == item_id
    clock.now = tpe(2026, 11, 12, 10, 30)
    assert store.end_override(oid, "owner_ended") is True and store.end_override(oid, "owner_ended") is False
    assert store.active_override() is None and store.resolve().item_ref == item_id
    with pytest.raises(ValueError):
        store.end_override(oid, "not-a-reason")


def test_override_failed_transfer_ends_and_returns_to_plan(store, clock):  # VC-PP-18
    item_id = create(store)
    oid = store.begin_override(cause_request_id="req-f", authority="owner", priority=200, location="website",
                               activity_kind=ActivityKind.WORK, expires_at=clock.now + timedelta(hours=1))
    assert store.end_override(oid, "transfer_failed")
    assert store.resolve().item_ref == item_id


def test_override_expiry_is_capped_by_round_and_host_rejected(store, clock, sf):  # VC-PP-22
    clock.now = tpe(2026, 11, 12, 22)
    store.begin_override(cause_request_id="r-late", authority="owner", priority=200, location="desktop",
                         activity_kind=ActivityKind.WORK, expires_at=clock.now + timedelta(hours=4))
    ov = store.active_override()
    assert ov.expires_at == ov.round_end_at == tpe(2026, 11, 13)
    clock.now = tpe(2026, 11, 13, 22, 30)  # 次日同時段不含前一輪 overlay
    assert store.resolve().source == PlanSource.DEFAULT
    with pytest.raises(ValueError):
        store.begin_override(cause_request_id="r-host", authority="owner", priority=200, location="host",
                             activity_kind=ActivityKind.REST, expires_at=clock.now + timedelta(hours=1))
    with sf() as s, pytest.raises(IntegrityError):  # DB CHECK：expires_at <= round_end_at
        s.execute(text(
            "INSERT INTO presence_plan_overrides (override_id, persona_id, cause_request_id, authority, priority, "
            "location, activity_kind, started_at, expires_at, round_end_at, status) VALUES ('povr_x', 'uep', 'cx', "
            "'owner', 200, 'desktop', 'work', now(), now() + interval '2 day', now() + interval '1 day', 'ACTIVE')"))
        s.commit()


def test_new_override_supersedes_open_one(store, clock, sf):
    first = store.begin_override(cause_request_id="a", authority="owner", priority=200, location="desktop",
                                 activity_kind=ActivityKind.WORK, expires_at=clock.now + timedelta(hours=1))
    second = store.begin_override(cause_request_id="b", authority="owner", priority=200, location="discord",
                                  activity_kind=ActivityKind.WORK, expires_at=clock.now + timedelta(hours=1))
    assert first != second and store.active_override().override_id == second
    with sf() as s:
        row = s.execute(text("SELECT end_reason FROM presence_plan_overrides WHERE override_id = :i"),
                        {"i": first}).one()
    assert row.end_reason == "superseded"


def test_end_assignment(store, clock):
    oid = store.begin_override(cause_request_id="a", authority="owner", priority=200, location="desktop",
                               activity_kind=ActivityKind.WORK, expires_at=clock.now + timedelta(hours=1))
    rev = store.revision()
    res = store.end_assignment(OWNER, expected_revision=rev)
    assert (res.status, res.ref_id) == ("OK", oid) and store.active_override() is None
    noop = store.end_assignment(OWNER, expected_revision=store.revision())
    assert (noop.status, noop.ref_id) == ("OK", None)
    assert store.end_assignment(OWNER, expected_revision=0).status == "REVISION_CONFLICT"


# ---- marks ----

def test_marks_pause_abandon_flow(store, clock):
    item_id = create(store, local_start=time(9), local_end=time(12))
    planned = tpe(2026, 11, 12, 9)
    pid = store.begin_mark(kind="pause", item_ref=item_id, planned_start_at=planned, cause_request_id="p1")
    assert is_valid_id(pid, "pmark")
    assert store.begin_mark(kind="pause", item_ref=item_id, planned_start_at=planned, cause_request_id="p1") == pid
    assert store.begin_mark(kind="pause", item_ref=item_id, planned_start_at=planned, cause_request_id="p2") == pid
    assert store.resolve().slot_state == "paused"
    aid = store.begin_mark(kind="abandon", item_ref=item_id, planned_start_at=planned, cause_request_id="a1")
    assert [m.kind for m in store.active_marks()] == ["abandon"]  # pause 以 superseded 結束
    s = store.resolve()
    assert (s.slot_state, s.location) == ("abandoned", "host")
    with pytest.raises(ValueError):
        store.end_mark(aid, "owner_resumed")
    assert store.end_mark(pid, "owner_resumed") is False  # 已結束
    assert store.end_mark(aid, "item_ended") is True
    assert store.resolve().slot_state == "normal"


def test_pause_resume(store):
    item_id = create(store)
    pid = store.begin_mark(kind="pause", item_ref=item_id, planned_start_at=tpe(2026, 11, 12, 9), cause_request_id="p")
    assert store.end_mark(pid, "owner_resumed") and store.resolve().slot_state == "normal"


# ---- 重啟調和 ----

def test_reconcile_override_cases(store, clock):  # VC-PP-13 / VC-PP-22
    clock.now = tpe(2026, 11, 12, 15)
    store.begin_override(cause_request_id="a", authority="owner", priority=200, location="desktop",
                         activity_kind=ActivityKind.WORK, expires_at=clock.now + timedelta(hours=1))
    rev = store.revision()
    quiet = store.reconcile(clock.now + timedelta(minutes=10), presence_status="ACTIVE")
    assert quiet.overrides_ended == {} and quiet.revision is None and store.revision() == rev  # 無變更不 bump
    expired = store.reconcile(clock.now + timedelta(hours=2))
    assert expired.overrides_ended == {"expired": 1} and expired.revision == rev + 1
    assert store.active_override() is None

    clock.now = tpe(2026, 11, 12, 22)
    store.begin_override(cause_request_id="b", authority="owner", priority=200, location="desktop",
                         activity_kind=ActivityKind.WORK, expires_at=clock.now + timedelta(hours=4))
    over_round = store.reconcile(tpe(2026, 11, 13, 9))
    assert over_round.overrides_ended == {"round_ended": 1}

    clock.now = tpe(2026, 11, 13, 10)
    store.begin_override(cause_request_id="c", authority="owner", priority=200, location="desktop",
                         activity_kind=ActivityKind.WORK, expires_at=clock.now + timedelta(hours=2))
    restarted = store.reconcile(clock.now + timedelta(minutes=5), presence_status="RECOVERING")
    assert restarted.overrides_ended == {"restart": 1}


def test_reconcile_marks(store, clock):
    item_id = create(store)
    other = create(store, local_start=time(13), local_end=time(14))
    keep = store.begin_mark(kind="pause", item_ref=item_id, planned_start_at=tpe(2026, 11, 12, 9), cause_request_id="k")
    store.begin_mark(kind="abandon", item_ref=other, planned_start_at=tpe(2026, 11, 12, 13), cause_request_id="g")
    assert store.delete_item(OWNER, other, expected_revision=store.revision()).status == "OK"  # 刪除已先結束其標記
    kept = store.reconcile(tpe(2026, 11, 12, 11))
    assert kept.marks_ended == {} and [m.mark_id for m in store.active_marks()] == [keep]  # 未過輪次保留
    ended = store.reconcile(tpe(2026, 11, 13, 0, 1))
    assert ended.marks_ended == {"round_ended": 1} and store.active_marks() == ()


def test_reconcile_marks_of_missing_item_ends_item_ended(store, sf):
    store.begin_mark(kind="pause", item_ref="plan_ghost", planned_start_at=tpe(2026, 11, 12, 9), cause_request_id="x")
    assert store.reconcile(tpe(2026, 11, 12, 11)).marks_ended == {"item_ended": 1}


def test_open_mark_partial_unique_in_db(store, sf):
    item_id = create(store)
    store.begin_mark(kind="pause", item_ref=item_id, planned_start_at=tpe(2026, 11, 12, 9), cause_request_id="u1")
    with sf() as s, pytest.raises(IntegrityError):
        s.execute(text(
            "INSERT INTO presence_plan_slot_marks (mark_id, persona_id, kind, item_ref, planned_start_at, "
            "cause_request_id, started_at, round_end_at) SELECT 'pmark_dup', persona_id, kind, item_ref, "
            "planned_start_at, 'u2', started_at, round_end_at FROM presence_plan_slot_marks LIMIT 1"))
        s.commit()


# ---- 通知與快照 ----

def test_on_change_and_snapshot(sf, clock):
    seen = []
    store = PlanStore(sf, clock=clock, on_change=lambda rev, kind, ref: seen.append((rev, kind, ref)))
    item_id = create(store)
    assert seen == [(1, "item_created", item_id)]
    snap = store.snapshot(OWNER)
    assert snap.revision == 1 and snap.tz == TZ and [i.item_id for i in snap.items] == [item_id]
    assert snap.rest_window is None and snap.slots[0].item_ref == item_id
    # 通知失敗不影響已提交的寫入
    boom = PlanStore(sf, clock=clock, on_change=lambda *_: (_ for _ in ()).throw(RuntimeError("x")))
    assert boom.create_item(OWNER, work_cmd(local_start=time(13), local_end=time(14)), expected_revision=1).status == "OK"


def test_resolver_is_used_for_overlap_priority(store):
    low = create(store, priority=10)
    high = create(store, priority=70)
    assert store.resolve().item_ref == high and low != high
    assert resolver.round_end_at(tpe(2026, 11, 12, 10), TZ) == tpe(2026, 11, 13)
