"""行程持久化（PP-1）：PlanReader／PlanWriter／OverrideWriter／SlotMarkWriter 的 PostgreSQL 實作。

- 解析一律委派 ``uep.schedule.resolver``（純函式）；本模組只負責讀寫資料表與重啟調和。
- 每次行程寫入（items／overrides／marks／tz）同一交易內 ``presence_plan_meta.revision`` +1（樂觀併發與變更偵測）。
- 時間以可注入 clock 為準（不依賴 DB 時鐘）；DB 讀取失敗時 ``resolve`` 回 ``unavailable``（PP-1、X1）。
- 依賴方向：只依賴 ``uep.contracts``、``uep.schedule``、``uep.ids``、sqlalchemy；不 import Presence Manager 或 ``uep.memory``。
- 資料表以 ``text()`` 存取（不新增 ORM model）；DDL 由 migration 0021 負責。
- 事件（``schedule.plan_changed`` 等）屬 PP-4：這裡只提供提交後的 ``on_change`` callback。
"""
from __future__ import annotations

import logging
import re
import time as _clock_time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Literal, NamedTuple, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from uep.contracts.schedule import (
    PLAN_LOCATIONS,
    ActivityKind,
    InterruptPolicy,
    OverrideView,
    PlanDelay,
    PlanItem,
    PlanLocation,
    PlanSlot,
    SlotMark,
)
from uep.ids import new_id
from uep.schedule import resolver

logger = logging.getLogger(__name__)

DEFAULT_TZ = "Asia/Taipei"
MAX_USER_ITEMS = 200  # 每 persona 未刪除 user 項上限（§9.2）
REST_WINDOW_PRIORITY = 10
USER_PRIORITY = 50
_ALL_WEEKDAYS = (1, 2, 3, 4, 5, 6, 7)
_BEHAVIOR_MODES = frozenset({"REST", "SLEEP", "CHAT", "WORK", "MISCHIEF"})
_ACTOR_REF = re.compile(r"^[a-z0-9_.:-]{1,128}$")
_BLOB_REF = re.compile(r"^[a-z0-9_.:-]{1,128}$")
OVERRIDE_END_REASONS = frozenset({
    "completed", "idle_timeout", "round_ended", "expired", "owner_ended", "superseded", "emergency",
    "transfer_failed", "presence_lost", "restart"})
MARK_END_REASONS = frozenset({"owner_resumed", "item_ended", "round_ended", "superseded"})
ALLOWED_SOURCES = ("owner_console",)  # 第一階段只接受 owner_console（§9.2）


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime, name: str) -> datetime:
    """naive datetime 一律拒絕（比照 resolver 的 ``_utc``）。"""
    if value.tzinfo is None:
        raise ValueError(f"{name} 必須帶時區")
    return value


# ---- 寫入介面型別（PlanWriter；之後可移入 contracts）----

class MarkRecord(NamedTuple):
    """``find_mark_by_cause`` 的結果（含已結束的 mark）。"""

    mark_id: str
    kind: str
    item_ref: str
    planned_start_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True)
class PlanActor:
    """與 PresenceRequest 同一套身分規則：不收顯示名稱；is_owner 由 Core 計算，非自報。"""

    actor_ref: str
    source_platform: str
    actor_authenticated: bool
    is_owner: bool


@dataclass(frozen=True)
class PlanItemCommand:
    activity_kind: str
    location: str | None
    recurrence: str
    once_start_at: datetime | None = None
    once_end_at: datetime | None = None
    weekdays: tuple[int, ...] = ()
    local_start: time | None = None
    local_end: time | None = None
    tz: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    priority: int = USER_PRIORITY
    flexibility: str = "fixed"
    interrupt_policy: str | None = None
    suggested_behaviors: tuple[str, ...] | None = None
    note_ref: str | None = None


@dataclass(frozen=True)
class WriteResult:
    status: Literal["OK", "REVISION_CONFLICT", "INVALID", "NOT_OWNER", "LIMIT_EXCEEDED"]
    reason_code: str | None = None
    revision: int | None = None
    ref_id: str | None = None


@dataclass(frozen=True)
class PlanSnapshot:
    revision: int
    tz: str
    items: tuple[PlanItem, ...]
    rest_window: PlanItem | None
    active_override: OverrideView | None
    slots: tuple[PlanSlot, ...]


@dataclass(frozen=True)
class ReconcileSummary:
    overrides_ended: dict[str, int]  # end_reason -> 數量
    marks_ended: dict[str, int]
    revision: int | None  # 有變更時 +1 後的 revision；無變更為 None


class PlanWriter(Protocol):
    def snapshot(self, actor: PlanActor) -> PlanSnapshot: ...
    def create_item(self, actor: PlanActor, cmd: PlanItemCommand, *, expected_revision: int) -> WriteResult: ...
    def update_item(self, actor: PlanActor, item_id: str, cmd: PlanItemCommand, *,
                    expected_revision: int) -> WriteResult: ...
    def delete_item(self, actor: PlanActor, item_id: str, *, expected_revision: int) -> WriteResult: ...
    def set_rest_window(self, actor: PlanActor, *, local_start: time, local_end: time, tz: str,
                        expected_revision: int) -> WriteResult: ...
    def end_assignment(self, actor: PlanActor, *, expected_revision: int) -> WriteResult: ...


# ---- 驗證（純函式）----

def _tz_ok(name: str | None) -> bool:
    if not isinstance(name, str) or not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def validate_item_command(cmd: PlanItemCommand) -> str | None:
    """寫入前驗證；回固定的 reason_code，None＝通過。DB CHECK 是第二道防線。"""
    if cmd.activity_kind not in {k.value for k in ActivityKind}:
        return "invalid_activity_kind"
    if cmd.location is not None and cmd.location not in PLAN_LOCATIONS:
        return "invalid_location"
    if cmd.location is None and cmd.activity_kind != ActivityKind.REST.value:
        return "location_required_for_non_rest"
    if cmd.flexibility not in ("fixed", "deferrable", "flexible"):
        return "invalid_flexibility"
    if cmd.interrupt_policy is not None and cmd.interrupt_policy not in {p.value for p in InterruptPolicy}:
        return "invalid_interrupt_policy"
    if not isinstance(cmd.priority, int) or isinstance(cmd.priority, bool) or not 0 <= cmd.priority <= 99:
        return "invalid_priority"
    if cmd.suggested_behaviors is not None and (
            not cmd.suggested_behaviors or any(m not in _BEHAVIOR_MODES for m in cmd.suggested_behaviors)):
        return "invalid_suggested_behaviors"
    if cmd.note_ref is not None and _BLOB_REF.fullmatch(cmd.note_ref) is None:
        return "invalid_note_ref"
    for stamp in (cmd.valid_from, cmd.valid_until, cmd.once_start_at, cmd.once_end_at):
        if stamp is not None and stamp.tzinfo is None:
            return "naive_datetime"
    if cmd.valid_from is not None and cmd.valid_until is not None and cmd.valid_until <= cmd.valid_from:
        return "invalid_validity"
    if cmd.recurrence == "once":
        if cmd.once_start_at is None or cmd.once_end_at is None:
            return "once_requires_range"
        if cmd.once_end_at <= cmd.once_start_at:
            return "once_end_before_start"
        if cmd.weekdays or cmd.local_start is not None or cmd.local_end is not None or cmd.tz is not None:
            return "once_has_weekly_fields"
    elif cmd.recurrence == "weekly":
        if cmd.once_start_at is not None or cmd.once_end_at is not None:
            return "weekly_has_once_fields"
        if not cmd.weekdays or any(d not in _ALL_WEEKDAYS for d in cmd.weekdays):
            return "invalid_weekdays"
        if cmd.local_start is None or cmd.local_end is None:
            return "weekly_requires_local_time"
        if cmd.local_start == cmd.local_end:
            return "weekly_equal_times"
        if not _tz_ok(cmd.tz):
            return "invalid_tz"
    else:
        return "invalid_recurrence"
    return None


def _is_owner(actor: PlanActor, allowed_sources: tuple[str, ...]) -> bool:
    return (actor.actor_authenticated is True and actor.is_owner is True
            and isinstance(actor.actor_ref, str) and _ACTOR_REF.fullmatch(actor.actor_ref) is not None
            and actor.source_platform in allowed_sources)


# ---- 資料列 → 契約型別 ----

_ITEM_COLUMNS = (
    "item_id, role, activity_kind, location, recurrence, once_start_at, once_end_at, weekdays, local_start, local_end, "
    "tz, valid_from, valid_until, priority, flexibility, interrupt_policy, suggested_behaviors, created_at")
_OVERRIDE_COLUMNS = ("override_id, location, activity_kind, started_at, expires_at, round_end_at, status, "
                     "cause_request_id, authority, priority")
_MARK_COLUMNS = "mark_id, kind, item_ref, planned_start_at, started_at, cause_request_id"


def _item_from_row(m) -> PlanItem:
    return PlanItem(
        item_id=m["item_id"], activity_kind=ActivityKind(m["activity_kind"]), location=m["location"],
        recurrence=m["recurrence"], role=m["role"], once_start_at=m["once_start_at"], once_end_at=m["once_end_at"],
        weekdays=tuple(int(d) for d in (m["weekdays"] or ())), local_start=m["local_start"],
        local_end=m["local_end"], tz=m["tz"], valid_from=m["valid_from"], valid_until=m["valid_until"],
        priority=int(m["priority"]), flexibility=m["flexibility"],
        interrupt_policy=InterruptPolicy(m["interrupt_policy"]) if m["interrupt_policy"] else None,
        suggested_behaviors=tuple(m["suggested_behaviors"]) if m["suggested_behaviors"] is not None else None,
        created_at=m["created_at"])


def _override_from_row(m) -> OverrideView:
    return OverrideView(
        override_id=m["override_id"], location=m["location"], activity_kind=ActivityKind(m["activity_kind"]),
        started_at=m["started_at"], expires_at=m["expires_at"], round_end_at=m["round_end_at"],
        status="pending" if m["status"] == "PENDING" else "active", cause_request_id=m["cause_request_id"],
        authority=m["authority"], priority=m["priority"])


def _mark_from_row(m) -> SlotMark:
    return SlotMark(kind=m["kind"], item_ref=m["item_ref"], planned_start_at=m["planned_start_at"],
                    started_at=m["started_at"], mark_id=m["mark_id"], cause_request_id=m["cause_request_id"])


class PlanStore:
    """PlanReader＋PlanWriter＋OverrideWriter＋SlotMarkWriter（單一 persona）。

    鎖順序：所有寫入先鎖 ``presence_plan_meta`` 的列（``_lock_meta``），再更新其他資料列。
    已知限制（審查 m4／m5／m6，本版維持現狀，待 PP-4／仲裁器階段評估）：
    - m4：``_load`` 的多條查詢不在同一快照內，回傳的 revision 可能落後於實際內容。
    - m5：``begin_mark`` 不驗證 item_ref／planned_start_at 是否實際存在於行程（交給 reconcile 以 item_ended 收斂）。
    - m6：``snapshot`` 對非 owner 拋 PermissionError，而不是回 WriteResult(NOT_OWNER)。
    """

    def __init__(self, session_factory: sessionmaker[Session], *, persona_id: str = "uep",
                 clock: Callable[[], datetime] = _utcnow, override_policy: resolver.OverridePolicy | None = None,
                 allowed_sources: tuple[str, ...] = ALLOWED_SOURCES,
                 on_change: Callable[[int, str, str | None], None] | None = None) -> None:
        self._sf = session_factory
        self._persona = persona_id
        self._clock = clock
        self._policy = override_policy or resolver.OverridePolicy()
        self._allowed_sources = allowed_sources
        self._on_change = on_change
        self._delay: PlanDelay | None = None
        self._last_unavailable_log = float("-inf")

    def set_on_change(self, callback: Callable[[int, str, str | None], None] | None) -> None:
        """設定提交後的變更通知（例如發布 ``schedule.plan_changed``）；寫入端的 store 實例需要設定。"""
        self._on_change = callback

    # ---- Planner 注入（delay 不持久化；只活在記憶體）----

    def set_delay(self, delay: PlanDelay | None) -> None:
        self._delay = delay

    # ---- 內部 ----

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock 必須回傳帶時區的 datetime")
        return now.astimezone(UTC)

    def _lock_meta(self, s: Session) -> tuple[int, str]:
        s.execute(text("INSERT INTO presence_plan_meta (persona_id) VALUES (:p) ON CONFLICT DO NOTHING"),
                  {"p": self._persona})
        row = s.execute(text("SELECT revision, tz FROM presence_plan_meta WHERE persona_id = :p FOR UPDATE"),
                        {"p": self._persona}).one()
        return int(row.revision), row.tz

    def _bump(self, s: Session, now: datetime) -> int:
        row = s.execute(text(
            "UPDATE presence_plan_meta SET revision = revision + 1, updated_at = :now WHERE persona_id = :p "
            "RETURNING revision"), {"p": self._persona, "now": now}).one()
        return int(row.revision)

    def _log_unavailable(self, exc: Exception) -> None:
        """讀取失敗的 log 限頻（60 秒一次，不印 traceback）：resolve 會被 tick 頻繁呼叫，不能每次都洗版。"""
        now = _clock_time.monotonic()
        if now - self._last_unavailable_log >= 60.0:
            self._last_unavailable_log = now
            logger.warning("行程儲存讀取失敗，行程標示為不可用：%s", type(exc).__name__)

    def _notify(self, revision: int, kind: str, ref_id: str | None) -> None:
        if self._on_change is not None:
            try:
                self._on_change(revision, kind, ref_id)
            except Exception:
                logger.exception("on_change callback 失敗：kind=%s", kind)

    def _owner_gate(self, actor: PlanActor) -> WriteResult | None:
        return None if _is_owner(actor, self._allowed_sources) else WriteResult("NOT_OWNER", "not_owner")

    # ---- PlanReader ----

    def revision(self) -> int:
        with self._sf() as s:
            row = s.execute(text("SELECT revision FROM presence_plan_meta WHERE persona_id = :p"),
                            {"p": self._persona}).first()
        return int(row.revision) if row else 0

    def tz(self) -> str:
        with self._sf() as s:
            row = s.execute(text("SELECT tz FROM presence_plan_meta WHERE persona_id = :p"),
                            {"p": self._persona}).first()
        return row.tz if row else DEFAULT_TZ

    def _load(self, now: datetime) -> tuple[list[PlanItem], OverrideView | None, list[SlotMark], int, str]:
        """讀取解析所需資料；已過期的 override 與已過輪次的 mark 不回傳（等 reconcile 正式結束）。"""
        with self._sf() as s:
            meta = s.execute(text("SELECT revision, tz FROM presence_plan_meta WHERE persona_id = :p"),
                             {"p": self._persona}).first()
            items = [_item_from_row(r._mapping) for r in s.execute(text(
                f"SELECT {_ITEM_COLUMNS} FROM presence_plan_items WHERE persona_id = :p AND deleted_at IS NULL "
                "ORDER BY item_id"), {"p": self._persona})]
            ov = s.execute(text(
                f"SELECT {_OVERRIDE_COLUMNS} FROM presence_plan_overrides "
                "WHERE persona_id = :p AND status IN ('PENDING', 'ACTIVE') AND expires_at > :now "
                "AND round_end_at > :now"), {"p": self._persona, "now": now}).first()
            marks = [_mark_from_row(r._mapping) for r in s.execute(text(
                f"SELECT {_MARK_COLUMNS} FROM presence_plan_slot_marks WHERE persona_id = :p AND ended_at IS NULL "
                "AND round_end_at > :now"), {"p": self._persona, "now": now})]
        return (items, _override_from_row(ov._mapping) if ov else None, marks,
                int(meta.revision) if meta else 0, meta.tz if meta else DEFAULT_TZ)

    def resolve(self, at: datetime | None = None) -> PlanSlot:
        now = _aware(at, "at").astimezone(UTC) if at is not None else self._now()
        try:
            items, override, marks, revision, tz = self._load(now)
        except SQLAlchemyError as exc:  # 儲存讀取失敗 → 行程不可用（不誤刪任何資料）
            self._log_unavailable(exc)
            return resolver.unavailable_slot(now, 0, "store_unavailable")
        return resolver.resolve(items, override, now, tz, plan_revision=revision, delay=self._delay, marks=marks)

    def upcoming(self, start: datetime, horizon_s: int) -> tuple[PlanSlot, ...]:
        start = _aware(start, "start").astimezone(UTC)
        try:
            items, override, marks, revision, tz = self._load(start)
        except SQLAlchemyError as exc:
            self._log_unavailable(exc)
            return (resolver.unavailable_slot(start, 0, "store_unavailable"),)
        return resolver.upcoming(items, override, start, horizon_s, tz, plan_revision=revision, delay=self._delay,
                                 marks=marks)

    # ---- OverrideWriter（僅仲裁器持有）----
    # 鎖順序（全部寫入一致）：先 ``_lock_meta``（presence_plan_meta 的列鎖），再更新其他資料列，避免交錯死鎖。

    def begin_override(self, *, cause_request_id: str, authority: str, priority: int, location: PlanLocation,
                       activity_kind: ActivityKind, expires_at: datetime) -> str:
        """建立 PENDING override；同 cause 冪等（參數不同 → ValueError）。已有未結束的 override 時，舊的以 superseded 結束。"""
        _aware(expires_at, "expires_at")
        started = self._now()
        kind_value = ActivityKind(activity_kind).value
        for attempt in range(2):
            try:
                with self._sf() as s, s.begin():
                    _, tz = self._lock_meta(s)
                    existing = s.execute(text(
                        "SELECT override_id, authority, priority, location, activity_kind FROM presence_plan_overrides "
                        "WHERE cause_request_id = :c"), {"c": cause_request_id}).first()  # 鎖內重查，避免競態
                    if existing:
                        if (existing.authority, existing.priority, existing.location, existing.activity_kind) != (
                                authority, priority, location, kind_value):
                            raise ValueError(f"cause_request_id 已用於不同參數的 override：{cause_request_id}")
                        return existing.override_id
                    round_end = resolver.round_end_at(started, tz)
                    expires = min(expires_at.astimezone(UTC), round_end)
                    # 先以契約型別驗證（host 不得為 override 位置；expires > started）
                    view = OverrideView("probe", location, ActivityKind(activity_kind), started, expires, round_end)
                    s.execute(text(
                        "UPDATE presence_plan_overrides SET status = 'ENDED', ended_at = :now, "
                        "end_reason = 'superseded' WHERE persona_id = :p AND status IN ('PENDING', 'ACTIVE')"),
                        {"p": self._persona, "now": started})
                    override_id = new_id("povr")
                    s.execute(text(
                        "INSERT INTO presence_plan_overrides (override_id, persona_id, cause_request_id, authority, "
                        "priority, location, activity_kind, started_at, expires_at, round_end_at, status, created_at) "
                        "VALUES (:id, :p, :c, :a, :pr, :loc, :k, :st, :ex, :re, 'PENDING', :st)"),
                        {"id": override_id, "p": self._persona, "c": cause_request_id, "a": authority,
                         "pr": priority, "loc": view.location, "k": kind_value, "st": started, "ex": expires,
                         "re": round_end})
                    revision = self._bump(s, started)
                self._notify(revision, "override_started", override_id)
                return override_id
            except IntegrityError as exc:
                # 只有 cause_request_id 的唯一性競態才重試（meta 鎖下理論上不會發生）；其他約束違反照實拋出
                if attempt or "cause_request_id" not in str(exc.orig):
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    def activate_override(self, override_id: str) -> bool:
        """PENDING → ACTIVE。已過期的 PENDING 不啟用：同交易以 expired 結束並回 False（狀態明確，不留下懸空列）。"""
        now = self._now()
        with self._sf() as s, s.begin():
            self._lock_meta(s)
            row = s.execute(text(
                "UPDATE presence_plan_overrides SET status = 'ACTIVE' WHERE override_id = :id AND persona_id = :p "
                "AND status = 'PENDING' AND expires_at > :now AND round_end_at > :now RETURNING override_id"),
                {"id": override_id, "p": self._persona, "now": now}).first()
            if row is None:
                expired = s.execute(text(
                    "UPDATE presence_plan_overrides SET status = 'ENDED', ended_at = :now, end_reason = 'expired' "
                    "WHERE override_id = :id AND persona_id = :p AND status = 'PENDING' RETURNING override_id"),
                    {"id": override_id, "p": self._persona, "now": now}).first()
                if expired is None:
                    return False
                revision = self._bump(s, now)
                logger.info("override 在啟用前已過期，已結束：%s", override_id)
                notify = (revision, "override_ended", override_id)
                activated = False
            else:
                notify = (self._bump(s, now), "override_activated", override_id)
                activated = True
        self._notify(*notify)
        return activated

    def end_override(self, override_id: str, reason: str) -> bool:
        if reason not in OVERRIDE_END_REASONS:
            raise ValueError(f"未知的 override 結束原因：{reason}")
        now = self._now()
        with self._sf() as s, s.begin():
            self._lock_meta(s)
            row = s.execute(text(
                "UPDATE presence_plan_overrides SET status = 'ENDED', ended_at = :now, end_reason = :r "
                "WHERE override_id = :id AND persona_id = :p AND status <> 'ENDED' RETURNING override_id"),
                {"id": override_id, "p": self._persona, "now": now, "r": reason}).first()
            if row is None:
                return False
            revision = self._bump(s, now)
        self._notify(revision, "override_ended", override_id)
        return True

    def active_override(self) -> OverrideView | None:
        """未結束且未過期、未過輪次的 override（過期列等 reconcile 結束，不回傳）。"""
        now = self._now()
        with self._sf() as s:
            row = s.execute(text(
                f"SELECT {_OVERRIDE_COLUMNS} FROM presence_plan_overrides "
                "WHERE persona_id = :p AND status IN ('PENDING', 'ACTIVE') AND expires_at > :now "
                "AND round_end_at > :now"), {"p": self._persona, "now": now}).first()
        return _override_from_row(row._mapping) if row else None

    # ---- SlotMarkWriter ----

    def begin_mark(self, *, kind: Literal["pause", "abandon"], item_ref: str, planned_start_at: datetime,
                   cause_request_id: str) -> str:
        """建立標記，兩層冪等（都在 meta 鎖內判斷）：

        1. 同 ``cause_request_id``：同參數回既有 mark_id，參數不同 → ValueError。
        2. 同一出現（item_ref＋planned_start_at）、同 kind、**未結束**的 mark 已存在：回既有 mark_id（即使它是別的
           cause 建的）——呼叫端若要分辨是不是自己建的，用 ``find_mark_by_cause`` 唯讀查詢。

        abandon 令同一出現的 pause 以 superseded 結束。
        """
        if kind not in ("pause", "abandon"):
            raise ValueError(f"未知的 mark kind：{kind}")
        planned = _aware(planned_start_at, "planned_start_at").astimezone(UTC)
        started = self._now()
        for attempt in range(2):
            try:
                with self._sf() as s, s.begin():
                    _, tz = self._lock_meta(s)
                    existing = s.execute(text(
                        "SELECT mark_id, kind, item_ref, planned_start_at FROM presence_plan_slot_marks "
                        "WHERE cause_request_id = :c"), {"c": cause_request_id}).first()  # 鎖內重查，避免競態
                    if existing:
                        if (existing.kind, existing.item_ref, existing.planned_start_at) != (kind, item_ref, planned):
                            raise ValueError(f"cause_request_id 已用於不同參數的 mark：{cause_request_id}")
                        return existing.mark_id
                    open_same = s.execute(text(
                        "SELECT mark_id FROM presence_plan_slot_marks WHERE persona_id = :p AND item_ref = :i "
                        "AND planned_start_at = :ps AND kind = :k AND ended_at IS NULL"),
                        {"p": self._persona, "i": item_ref, "ps": planned, "k": kind}).first()
                    if open_same:
                        return open_same.mark_id
                    if kind == "abandon":
                        s.execute(text(
                            "UPDATE presence_plan_slot_marks SET ended_at = :now, end_reason = 'superseded' "
                            "WHERE persona_id = :p AND item_ref = :i AND planned_start_at = :ps AND kind = 'pause' "
                            "AND ended_at IS NULL"),
                            {"p": self._persona, "i": item_ref, "ps": planned, "now": started})
                    mark_id = new_id("pmark")
                    s.execute(text(
                        "INSERT INTO presence_plan_slot_marks (mark_id, persona_id, kind, item_ref, planned_start_at, "
                        "cause_request_id, started_at, round_end_at, created_at) "
                        "VALUES (:id, :p, :k, :i, :ps, :c, :st, :re, :st)"),
                        {"id": mark_id, "p": self._persona, "k": kind, "i": item_ref, "ps": planned,
                         "c": cause_request_id, "st": started, "re": resolver.round_end_at(started, tz)})
                    revision = self._bump(s, started)
                self._notify(revision, "mark_started", mark_id)
                return mark_id
            except IntegrityError as exc:
                if attempt or "cause_request_id" not in str(exc.orig):
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    def end_mark(self, mark_id: str, reason: str) -> bool:
        if reason not in MARK_END_REASONS:
            raise ValueError(f"未知的 mark 結束原因：{reason}")
        now = self._now()
        with self._sf() as s, s.begin():
            self._lock_meta(s)
            kind_row = s.execute(text(
                "SELECT kind FROM presence_plan_slot_marks WHERE mark_id = :id AND persona_id = :p"),
                {"id": mark_id, "p": self._persona}).first()
            if kind_row is None:
                return False
            if kind_row.kind == "abandon" and reason in ("owner_resumed", "superseded"):
                raise ValueError("abandon 不可恢復，只會 item_ended／round_ended")
            row = s.execute(text(
                "UPDATE presence_plan_slot_marks SET ended_at = :now, end_reason = :r "
                "WHERE mark_id = :id AND persona_id = :p AND ended_at IS NULL RETURNING mark_id"),
                {"id": mark_id, "p": self._persona, "now": now, "r": reason}).first()
            if row is None:
                return False
            revision = self._bump(s, now)
        self._notify(revision, "mark_ended", mark_id)
        return True

    def find_mark_by_cause(self, cause_request_id: str) -> MarkRecord | None:
        """唯讀查詢（不取寫鎖、不寫入）：依 cause 找 mark，含已結束的；查無回 None。"""
        with self._sf() as s:
            row = s.execute(text(
                "SELECT mark_id, kind, item_ref, planned_start_at, ended_at FROM presence_plan_slot_marks "
                "WHERE persona_id = :p AND cause_request_id = :c"),
                {"p": self._persona, "c": cause_request_id}).first()
        if row is None:
            return None
        return MarkRecord(row.mark_id, row.kind, row.item_ref, row.planned_start_at, row.ended_at)

    def active_marks(self) -> tuple[SlotMark, ...]:
        """未結束且未過輪次的 mark。"""
        now = self._now()
        with self._sf() as s:
            rows = s.execute(text(
                f"SELECT {_MARK_COLUMNS} FROM presence_plan_slot_marks WHERE persona_id = :p AND ended_at IS NULL "
                "AND round_end_at > :now ORDER BY started_at"), {"p": self._persona, "now": now})
            return tuple(_mark_from_row(r._mapping) for r in rows)

    # ---- PlanWriter（僅已認證 owner）----

    def snapshot(self, actor: PlanActor) -> PlanSnapshot:
        if not _is_owner(actor, self._allowed_sources):
            raise PermissionError("not_owner")
        now = self._now()
        items, override, marks, revision, tz = self._load(now)
        slots = resolver.upcoming(items, override, now, 24 * 3600, tz, plan_revision=revision, delay=self._delay,
                                  marks=marks)
        rest = next((i for i in items if i.role == "rest_window"), None)
        return PlanSnapshot(revision, tz, tuple(items), rest, override, slots)

    def _write(self, expected_revision: int, change_kind: str, body: Callable[[Session, datetime, str], WriteResult]
               ) -> WriteResult:
        now = self._now()
        with self._sf() as s, s.begin():
            revision, tz = self._lock_meta(s)
            if revision != expected_revision:
                return WriteResult("REVISION_CONFLICT", "revision_conflict", revision)
            result = body(s, now, tz)
            if result.status != "OK":  # body 在任何寫入前就回報失敗，不留下半套寫入
                return WriteResult(result.status, result.reason_code, revision)
            new_revision = self._bump(s, now)
            result = WriteResult("OK", None, new_revision, result.ref_id)
        self._notify(new_revision, change_kind, result.ref_id)
        return result

    @staticmethod
    def _item_params(cmd: PlanItemCommand) -> dict:
        return {
            "kind": cmd.activity_kind, "loc": cmd.location, "rec": cmd.recurrence, "os": cmd.once_start_at,
            "oe": cmd.once_end_at, "wd": list(cmd.weekdays) if cmd.recurrence == "weekly" else None,
            "ls": cmd.local_start, "le": cmd.local_end, "tz": cmd.tz, "vf": cmd.valid_from, "vu": cmd.valid_until,
            "pr": cmd.priority, "fx": cmd.flexibility, "ip": cmd.interrupt_policy,
            "sb": list(cmd.suggested_behaviors) if cmd.suggested_behaviors is not None else None,
            "nr": cmd.note_ref}

    def create_item(self, actor: PlanActor, cmd: PlanItemCommand, *, expected_revision: int) -> WriteResult:
        if (denied := self._owner_gate(actor)) is not None:
            return denied
        if (reason := validate_item_command(cmd)) is not None:
            return WriteResult("INVALID", reason)

        def body(s: Session, now: datetime, tz: str) -> WriteResult:
            count = s.execute(text(
                "SELECT count(*) FROM presence_plan_items WHERE persona_id = :p AND role = 'user' "
                "AND deleted_at IS NULL"), {"p": self._persona}).scalar_one()
            if count >= MAX_USER_ITEMS:
                return WriteResult("LIMIT_EXCEEDED", "user_item_limit")
            item_id = new_id("plan")
            s.execute(text(
                "INSERT INTO presence_plan_items (item_id, persona_id, role, activity_kind, location, recurrence, "
                "once_start_at, once_end_at, weekdays, local_start, local_end, tz, valid_from, valid_until, priority, "
                "flexibility, interrupt_policy, suggested_behaviors, note_ref, created_by, updated_by, created_at, "
                "updated_at) VALUES (:id, :p, 'user', :kind, :loc, :rec, :os, :oe, :wd, :ls, :le, :tz, :vf, :vu, :pr, "
                ":fx, :ip, :sb, :nr, :by, :by, :now, :now)"),
                {**self._item_params(cmd), "id": item_id, "p": self._persona, "by": actor.actor_ref, "now": now})
            return WriteResult("OK", ref_id=item_id)

        return self._write(expected_revision, "item_created", body)

    def update_item(self, actor: PlanActor, item_id: str, cmd: PlanItemCommand, *,
                    expected_revision: int) -> WriteResult:
        if (denied := self._owner_gate(actor)) is not None:
            return denied
        if (reason := validate_item_command(cmd)) is not None:
            return WriteResult("INVALID", reason)

        def body(s: Session, now: datetime, tz: str) -> WriteResult:
            row = s.execute(text(
                "SELECT role FROM presence_plan_items WHERE item_id = :id AND persona_id = :p AND deleted_at IS NULL "
                "FOR UPDATE"), {"id": item_id, "p": self._persona}).first()
            if row is None:
                return WriteResult("INVALID", "item_not_found")
            if row.role == "rest_window":
                return WriteResult("INVALID", "use_set_rest_window")
            s.execute(text(
                "UPDATE presence_plan_items SET activity_kind = :kind, location = :loc, recurrence = :rec, "
                "once_start_at = :os, once_end_at = :oe, weekdays = :wd, local_start = :ls, local_end = :le, "
                "tz = :tz, valid_from = :vf, valid_until = :vu, priority = :pr, flexibility = :fx, "
                "interrupt_policy = :ip, suggested_behaviors = :sb, note_ref = :nr, version = version + 1, "
                "updated_by = :by, updated_at = :now WHERE item_id = :id"),
                {**self._item_params(cmd), "id": item_id, "by": actor.actor_ref, "now": now})
            return WriteResult("OK", ref_id=item_id)

        return self._write(expected_revision, "item_updated", body)

    def delete_item(self, actor: PlanActor, item_id: str, *, expected_revision: int) -> WriteResult:
        if (denied := self._owner_gate(actor)) is not None:
            return denied

        def body(s: Session, now: datetime, tz: str) -> WriteResult:
            row = s.execute(text(
                "SELECT role FROM presence_plan_items WHERE item_id = :id AND persona_id = :p AND deleted_at IS NULL "
                "FOR UPDATE"), {"id": item_id, "p": self._persona}).first()
            if row is None:
                return WriteResult("INVALID", "item_not_found")
            if row.role == "rest_window":  # 避免行程失去休息時段
                return WriteResult("INVALID", "rest_window_not_deletable")
            s.execute(text(
                "UPDATE presence_plan_items SET deleted_at = :now, updated_at = :now, updated_by = :by, "
                "version = version + 1 WHERE item_id = :id"), {"id": item_id, "now": now, "by": actor.actor_ref})
            # 指向它的未結束標記隨項目結束
            s.execute(text(
                "UPDATE presence_plan_slot_marks SET ended_at = :now, end_reason = 'item_ended' "
                "WHERE persona_id = :p AND item_ref = :id AND ended_at IS NULL"),
                {"id": item_id, "p": self._persona, "now": now})
            return WriteResult("OK", ref_id=item_id)

        return self._write(expected_revision, "item_deleted", body)

    def set_rest_window(self, actor: PlanActor, *, local_start: time, local_end: time, tz: str,
                        expected_revision: int) -> WriteResult:
        if (denied := self._owner_gate(actor)) is not None:
            return denied
        if local_start == local_end:
            return WriteResult("INVALID", "weekly_equal_times")
        if not _tz_ok(tz):
            return WriteResult("INVALID", "invalid_tz")

        def body(s: Session, now: datetime, _tz: str) -> WriteResult:
            row = s.execute(text(
                "SELECT item_id FROM presence_plan_items WHERE persona_id = :p AND role = 'rest_window' "
                "AND deleted_at IS NULL FOR UPDATE"), {"p": self._persona}).first()
            params = {"p": self._persona, "ls": local_start, "le": local_end, "tz": tz, "by": actor.actor_ref,
                      "now": now}
            if row is not None:
                s.execute(text(
                    "UPDATE presence_plan_items SET local_start = :ls, local_end = :le, tz = :tz, "
                    "version = version + 1, updated_by = :by, updated_at = :now WHERE item_id = :id"),
                    {**params, "id": row.item_id})
                return WriteResult("OK", ref_id=row.item_id)
            item_id = new_id("plan")
            s.execute(text(
                "INSERT INTO presence_plan_items (item_id, persona_id, role, activity_kind, location, recurrence, "
                "weekdays, local_start, local_end, tz, priority, created_by, updated_by, created_at, updated_at) "
                "VALUES (:id, :p, 'rest_window', 'rest', NULL, 'weekly', :wd, :ls, :le, :tz, :pr, :by, :by, :now, :now)"),
                {**params, "id": item_id, "wd": list(_ALL_WEEKDAYS), "pr": REST_WINDOW_PRIORITY})
            return WriteResult("OK", ref_id=item_id)

        return self._write(expected_revision, "rest_window", body)

    def has_rest_window(self) -> bool:
        """是否存在未刪除的 rest_window 項（供休息時段轉接器偵測缺項）。"""
        with self._sf() as s:
            return s.execute(text(
                "SELECT 1 FROM presence_plan_items WHERE persona_id = :p AND role = 'rest_window' "
                "AND deleted_at IS NULL"), {"p": self._persona}).first() is not None

    def seed_rest_window(self, local_start: time, local_end: time, tz: str, *,
                         overwrite: bool = False) -> Literal["created", "updated", "kept"]:
        """系統種子（非 owner 寫入，PP-2）：沒有休息窗口項時建立；已存在時只有 ``overwrite=True`` 且值不同才改寫。

        用於啟動時以 ``UEP_DREAM_WINDOW_*`` 建立／覆寫（供白天驗證）。同一交易 revision +1；無變更不 bump。
        """
        if local_start == local_end:
            raise ValueError("休息窗口的 start 與 end 不得相同")
        if not _tz_ok(tz):
            raise ValueError(f"無效的時區：{tz}")
        now = self._now()
        with self._sf() as s, s.begin():
            self._lock_meta(s)
            row = s.execute(text(
                "SELECT item_id, local_start, local_end, tz FROM presence_plan_items WHERE persona_id = :p "
                "AND role = 'rest_window' AND deleted_at IS NULL FOR UPDATE"), {"p": self._persona}).first()
            if row is not None and (not overwrite or (row.local_start, row.local_end, row.tz) == (
                    local_start, local_end, tz)):
                return "kept"
            params = {"p": self._persona, "ls": local_start, "le": local_end, "tz": tz, "by": "system:seed",
                      "now": now}
            if row is not None:
                logger.warning("休息窗口被系統種子覆寫：%s-%s %s → %s-%s %s", row.local_start, row.local_end, row.tz,
                               local_start, local_end, tz)
                s.execute(text(
                    "UPDATE presence_plan_items SET local_start = :ls, local_end = :le, tz = :tz, "
                    "version = version + 1, updated_by = :by, updated_at = :now WHERE item_id = :id"),
                    {**params, "id": row.item_id})
                outcome, ref = "updated", row.item_id
            else:
                ref = new_id("plan")
                s.execute(text(
                    "INSERT INTO presence_plan_items (item_id, persona_id, role, activity_kind, location, recurrence, "
                    "weekdays, local_start, local_end, tz, priority, created_by, updated_by, created_at, updated_at) "
                    "VALUES (:id, :p, 'rest_window', 'rest', NULL, 'weekly', :wd, :ls, :le, :tz, :pr, :by, :by, "
                    ":now, :now)"),
                    {**params, "id": ref, "wd": list(_ALL_WEEKDAYS), "pr": REST_WINDOW_PRIORITY})
                outcome = "created"
            revision = self._bump(s, now)
        self._notify(revision, "rest_window", ref)
        return outcome  # type: ignore[return-value]

    def set_tz(self, actor: PlanActor, tz: str, *, expected_revision: int) -> WriteResult:
        """變更行程時區（輪次日界取此值；之後新建的 override／mark 以新時區計算）。"""
        if (denied := self._owner_gate(actor)) is not None:
            return denied
        if not _tz_ok(tz):
            return WriteResult("INVALID", "invalid_tz")

        def body(s: Session, now: datetime, _tz: str) -> WriteResult:
            s.execute(text("UPDATE presence_plan_meta SET tz = :tz WHERE persona_id = :p"),
                      {"tz": tz, "p": self._persona})
            # 輪次日界跟著新時區：未結束 override／mark 的 round_end_at 重算（override 的 expires_at 仍不得晚於它）
            for row in s.execute(text(
                    "SELECT override_id, started_at, expires_at FROM presence_plan_overrides "
                    "WHERE persona_id = :p AND status IN ('PENDING', 'ACTIVE') FOR UPDATE"),
                    {"p": self._persona}).all():
                round_end = resolver.round_end_at(row.started_at, tz)
                s.execute(text(
                    "UPDATE presence_plan_overrides SET round_end_at = :re, expires_at = :ex WHERE override_id = :id"),
                    {"re": round_end, "ex": min(row.expires_at, round_end), "id": row.override_id})
            for row in s.execute(text(
                    "SELECT mark_id, started_at FROM presence_plan_slot_marks "
                    "WHERE persona_id = :p AND ended_at IS NULL FOR UPDATE"), {"p": self._persona}).all():
                s.execute(text("UPDATE presence_plan_slot_marks SET round_end_at = :re WHERE mark_id = :id"),
                          {"re": resolver.round_end_at(row.started_at, tz), "id": row.mark_id})
            return WriteResult("OK")

        return self._write(expected_revision, "tz_changed", body)

    def end_assignment(self, actor: PlanActor, *, expected_revision: int) -> WriteResult:
        """owner 顯式結束臨時指示（沒有進行中的指示時為無操作，回 OK 與 ref_id=None；revision 不變）。"""
        if (denied := self._owner_gate(actor)) is not None:
            return denied
        now = self._now()
        with self._sf() as s, s.begin():
            revision, _ = self._lock_meta(s)
            if revision != expected_revision:
                return WriteResult("REVISION_CONFLICT", "revision_conflict", revision)
            row = s.execute(text(
                "UPDATE presence_plan_overrides SET status = 'ENDED', ended_at = :now, end_reason = 'owner_ended' "
                "WHERE persona_id = :p AND status IN ('PENDING', 'ACTIVE') RETURNING override_id"),
                {"p": self._persona, "now": now}).first()
            if row is None:
                return WriteResult("OK", None, revision, None)
            new_revision = self._bump(s, now)
        self._notify(new_revision, "override_ended", row.override_id)
        return WriteResult("OK", None, new_revision, row.override_id)

    # ---- 重啟調和（§7）----

    def reconcile(self, now: datetime | None = None, *, at_startup: bool = False,
                  presence_status: Literal["NONE", "ACTIVE", "RECOVERING"] | None = None) -> ReconcileSummary:
        """啟動／維護時的一次調和；不補錯過的邊界。

        - override：已過期／已過輪次 → ENDED(expired／round_ended)。``restart`` 只在顯式啟動呼叫
          （``at_startup=True``）且 ``presence_status`` 為 NONE／RECOVERING 時，對 **ACTIVE** override 套用
          （她已不在 override 的位置）；剛建立、尚在移轉的 PENDING 不誤殺。
        - mark：已過輪次 → ended(round_ended)；指向的行程項已刪除／不存在 → ended(item_ended)；其餘保留
          （owner 的明示決定不因重啟消失）。
        - 有任何變更時 revision +1。
        """
        stamp = _aware(now, "now").astimezone(UTC) if now is not None else self._now()
        ov_ended: dict[str, int] = {}
        mk_ended: dict[str, int] = {}
        with self._sf() as s, s.begin():
            self._lock_meta(s)
            for row in s.execute(text(
                    f"SELECT {_OVERRIDE_COLUMNS} FROM presence_plan_overrides "
                    "WHERE persona_id = :p AND status IN ('PENDING', 'ACTIVE') FOR UPDATE"), {"p": self._persona}).all():
                view = _override_from_row(row._mapping)
                reason = resolver.override_end_reason(view, stamp)
                if (reason is None and at_startup and view.status == "active"
                        and presence_status in ("NONE", "RECOVERING")):
                    reason = "restart"
                if reason is None:
                    continue
                s.execute(text(
                    "UPDATE presence_plan_overrides SET status = 'ENDED', ended_at = :now, end_reason = :r "
                    "WHERE override_id = :id"), {"id": view.override_id, "now": stamp, "r": reason})
                ov_ended[reason] = ov_ended.get(reason, 0) + 1
            for row in s.execute(text(
                    "SELECT m.mark_id, m.round_end_at, "
                    "EXISTS (SELECT 1 FROM presence_plan_items i WHERE i.item_id = m.item_ref "
                    "AND i.deleted_at IS NULL) AS item_alive "
                    "FROM presence_plan_slot_marks m WHERE m.persona_id = :p AND m.ended_at IS NULL FOR UPDATE OF m"),
                    {"p": self._persona}).all():
                reason = ("round_ended" if row.round_end_at <= stamp else None) or (
                    "item_ended" if not row.item_alive else None)
                if reason is None:
                    continue
                s.execute(text(
                    "UPDATE presence_plan_slot_marks SET ended_at = :now, end_reason = :r WHERE mark_id = :id"),
                    {"id": row.mark_id, "now": stamp, "r": reason})
                mk_ended[reason] = mk_ended.get(reason, 0) + 1
            revision = self._bump(s, stamp) if (ov_ended or mk_ended) else None
        if revision is not None:
            self._notify(revision, "reconcile", None)
        return ReconcileSummary(ov_ended, mk_ended, revision)


__all__ = [
    "ALLOWED_SOURCES", "MAX_USER_ITEMS", "PlanActor", "PlanItemCommand", "PlanSnapshot", "PlanStore", "PlanWriter",
    "ReconcileSummary", "WriteResult", "validate_item_command",
]
