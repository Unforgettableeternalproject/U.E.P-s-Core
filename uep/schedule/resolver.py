"""行程解析（純函式，PP-0）：覆蓋層 > 行程項（藍圖→投影）> 預設層（主機＋REST）。

藍圖／投影（Bernie 裁 Q-PP-4）：
- 藍圖＝原始 ``PlanItem``，永不改寫。
- 投影＝套用 overlay 與 ``PlanDelay`` 後的有效時間軸。delay 只存在於投影、只在當前輪次內（Q-PP-3）：
  目前格延長到 ``held_until``，其後起點落在延長區間內的行程項，實際起點順延到 ``held_until``，
  結束時間維持原結束（壓縮，Bernie 已裁）；壓縮後時長 <= 0 的項在投影中視為被吃掉（藍圖不改）。
行程不可用是狀態值（``availability="unavailable"``），不是例外（PP-1）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from uep.contracts.schedule import (
    MODE_CHAT,
    MODE_REST,
    MODE_SLEEP,
    MODE_WORK,
    PLAN_LOCATIONS,
    PRESENCE_TARGET_HOST,
    ActivityKind,
    BehaviorSuggestion,
    InterruptPolicy,
    OverrideView,
    PlanDelay,
    PlanDirective,
    PlanItem,
    PlanSlot,
    PlanSource,
)

_UTC = UTC
_EPS = timedelta(microseconds=1)
SCAN_HORIZON = timedelta(hours=48)  # §6.1：掃描視野 48 小時
_MAX_UPCOMING = 500


@dataclass(frozen=True)
class OverridePolicy:
    """臨時指示政策值（Q-PP-1 暫定，待 Bernie 明示）。"""

    idle_timeout_s: int = 1800  # 暫定，待 Bernie 明示
    ttl_s: int = 14400  # 暫定，待 Bernie 明示


@dataclass(frozen=True)
class DirectivePolicy:
    """directive 政策值（Q-PP-4）。上限存在已由 Bernie 確認；數值為可調參數，暫定。"""

    grace_s: int = 120  # 互動 IDLE 後的安靜寬限；數值暫定
    defer_max_s: int = 1800  # 單次嘗試的最長延後；數值暫定
    retry_s: int = 300  # 嘗試結束後的下次評估間隔／delay 延長步長；數值暫定
    open_slot_ttl_s: int = 3600  # 開放格（預設層）directive 的存活上限；暫定


@dataclass(frozen=True)
class DeferDecision:
    action: Literal["wait", "drop_attempt", "drop_final"]
    reason_code: str
    next_eval_at: datetime | None


# ---- 時間工具 ----

def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime 必須帶時區")
    return value.astimezone(_UTC)


def _local_to_utc(day: date, at: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, at.replace(tzinfo=None), tzinfo=zone).astimezone(_UTC)


def round_bounds(at: datetime, tz: str) -> tuple[datetime, datetime]:
    """輪次＝本地日；回 (起點, 終點) UTC。時區取行程設定（meta.tz）。"""
    zone = ZoneInfo(tz)
    local = _utc(at).astimezone(zone)
    return (_local_to_utc(local.date(), time.min, zone),
            _local_to_utc(local.date() + timedelta(days=1), time.min, zone))


def round_end_at(at: datetime, tz: str) -> datetime:
    return round_bounds(at, tz)[1]


def override_expires_at(started_at: datetime, tz: str, policy: OverridePolicy | None = None) -> datetime:
    """expires_at = min(started_at + ttl, 輪次結束)（PP-7、PP-15）。"""
    policy = policy or OverridePolicy()
    return min(_utc(started_at) + timedelta(seconds=policy.ttl_s), round_end_at(started_at, tz))


def override_end_reason(override: OverrideView, now: datetime) -> str | None:
    """重啟／tick 時 override 是否該結束：回 end_reason 或 None（VC-PP-22）。"""
    now = _utc(now)
    expires, round_end = _utc(override.expires_at), _utc(override.round_end_at)
    if now >= expires:
        return "round_ended" if expires >= round_end else "expired"
    if now >= round_end:
        return "round_ended"
    return None


# ---- 預設與建議 ----

def default_interrupt_policy(kind: ActivityKind) -> InterruptPolicy:
    return InterruptPolicy.FREE if kind == ActivityKind.SOCIAL else InterruptPolicy.SAFE_POINT


def suggestions_for(kind: ActivityKind, source: PlanSource,
                    explicit: tuple[str, ...] | None = None) -> tuple[BehaviorSuggestion, ...]:
    """§8.3 預設對照；item 明確 suggested_behaviors 以其為準。"""
    if explicit:
        return tuple(BehaviorSuggestion(mode=m, rank=i, reason_code="plan_explicit") for i, m in enumerate(explicit))
    if kind == ActivityKind.REST:
        if source == PlanSource.REST_WINDOW:
            return (BehaviorSuggestion(MODE_SLEEP, 0, "plan_rest_window"),
                    BehaviorSuggestion(MODE_REST, 1, "plan_rest_fallback"))
        return (BehaviorSuggestion(MODE_REST, 0, "plan_rest"),)
    if kind == ActivityKind.WORK:
        return (BehaviorSuggestion(MODE_WORK, 0, "plan_work"), BehaviorSuggestion(MODE_CHAT, 1, "plan_work_fallback"))
    return (BehaviorSuggestion(MODE_CHAT, 0, "plan_social"),)


def unavailable_slot(at: datetime, plan_revision: int = 0) -> PlanSlot:
    """行程不可用（X1）：host、無建議、source=DEFAULT。"""
    return PlanSlot(
        availability="unavailable", location=PRESENCE_TARGET_HOST, activity_kind=ActivityKind.REST,
        source=PlanSource.DEFAULT, item_ref=None, interrupt_policy=InterruptPolicy.SAFE_POINT,
        suggestions=(), starts_at=_utc(at), ends_at=None, next_boundary_at=None, plan_revision=plan_revision)


# ---- 藍圖 → 出現區間 → 投影 ----

@dataclass(frozen=True)
class _Occ:
    item: PlanItem
    bp_start: datetime  # 藍圖起點（已套有效期限）
    bp_end: datetime
    start: datetime  # 投影起點
    end: datetime  # 投影終點
    delay_s: int = 0

    @property
    def key(self) -> tuple[str, datetime]:
        return (self.item.item_id, self.bp_start)


def _item_occurrences(item: PlanItem, lo: datetime, hi: datetime) -> list[_Occ]:
    spans: list[tuple[datetime, datetime]] = []
    if item.recurrence == "once":
        if item.once_start_at is None or item.once_end_at is None:
            raise ValueError(f"once 項缺少起迄：{item.item_id}")
        spans.append((_utc(item.once_start_at), _utc(item.once_end_at)))
    else:
        if item.tz is None or item.local_start is None or item.local_end is None:
            raise ValueError(f"weekly 項缺少 tz／local 起迄：{item.item_id}")
        zone = ZoneInfo(item.tz)
        day = lo.astimezone(zone).date() - timedelta(days=1)
        last = hi.astimezone(zone).date()
        overnight = item.local_end <= item.local_start
        while day <= last:
            if day.isoweekday() in item.weekdays:
                end_day = day + timedelta(days=1) if overnight else day
                spans.append((_local_to_utc(day, item.local_start, zone),
                              _local_to_utc(end_day, item.local_end, zone)))
            day += timedelta(days=1)
    out: list[_Occ] = []
    for start, end in spans:
        if item.valid_from is not None:
            start = max(start, _utc(item.valid_from))
        if item.valid_until is not None:
            end = min(end, _utc(item.valid_until))
        if end <= start or end <= lo or start >= hi:
            continue
        out.append(_Occ(item, start, end, start, end))
    return out


def _apply_delay(occs: list[_Occ], delay: PlanDelay | None, tz: str) -> list[_Occ]:
    """把藍圖出現區間投影成有效時間軸（delay 只在當前輪次內；順延後維持原結束＝壓縮）。"""
    if delay is None:
        return occs
    anchor_start = _utc(delay.planned_start_at)
    anchor = next((o for o in occs if o.item.item_id == delay.item_ref and o.bp_start == anchor_start), None)
    if anchor is None:
        return occs
    hold = min(_utc(delay.held_until), round_bounds(anchor.bp_start, tz)[1])  # 不跨輪次
    if hold <= anchor.bp_end:
        return occs
    out: list[_Occ] = []
    for o in occs:
        if o is anchor:
            out.append(_Occ(o.item, o.bp_start, o.bp_end, o.start, hold, int((hold - o.bp_end).total_seconds())))
        elif anchor.bp_end <= o.bp_start < hold:  # 起點落在延長區間內：實際起點順延，結束不變
            if o.bp_end > hold:  # 壓縮後時長 <= 0 → 投影中被吃掉
                out.append(_Occ(o.item, o.bp_start, o.bp_end, hold, o.bp_end,
                                int((hold - o.bp_start).total_seconds())))
        else:
            out.append(o)
    return out


def _winner(occs: list[_Occ], t: datetime) -> _Occ | None:
    """重疊決勝：priority 高 → 窗較窄 → created_at 較晚 → item_id 較小（確定性）。"""
    active = [o for o in occs if o.start <= t < o.end]
    if not active:
        return None

    def rank(o: _Occ):
        created = _utc(o.item.created_at).timestamp() if o.item.created_at else 0.0
        return (-o.item.priority, (o.bp_end - o.bp_start).total_seconds(), -created, o.item.item_id)

    return min(active, key=rank)


def _override_effective(ov: OverrideView | None, t: datetime) -> bool:
    return ov is not None and _utc(ov.started_at) <= t < _utc(ov.expires_at) and t < _utc(ov.round_end_at)


# ---- 對外 ----

def resolve(items: list[PlanItem] | tuple[PlanItem, ...], override: OverrideView | None, now: datetime, tz: str,
            *, plan_revision: int = 0, delay: PlanDelay | None = None) -> PlanSlot:
    """此刻的 PlanSlot（投影）。恆回 PlanSlot：內部任何錯誤（含 tz 資料不可用）→ unavailable 狀態值。"""
    now = _utc(now)  # 無時區是呼叫端錯誤，照樣拋 ValueError
    try:
        return _resolve(items, override, now, tz, plan_revision, delay)
    except Exception:  # noqa: BLE001 - 行程不可用是狀態值，不是例外（PP-1）
        return unavailable_slot(now, plan_revision)


def _resolve(items, override, now: datetime, tz: str, rev: int, delay) -> PlanSlot:
    ZoneInfo(tz)  # tz 資料不可用 → 例外 → unavailable
    lo, hi = now - SCAN_HORIZON, now + SCAN_HORIZON
    occs: list[_Occ] = []
    for item in items:
        occs.extend(_item_occurrences(item, lo, hi))
    occs = _apply_delay(occs, delay, tz)
    ov = override if override is not None and override.status in ("pending", "active") else None

    def key_at(t: datetime):
        if _override_effective(ov, t):
            return ("override", ov.override_id)
        w = _winner(occs, t)
        return w.key if w else ("default",)

    cur = key_at(now)
    cands = {o.start for o in occs} | {o.end for o in occs}
    if ov is not None:
        cands |= {_utc(ov.started_at), _utc(ov.expires_at)}
    ordered = sorted(cands)
    next_b = next((b for b in ordered if b > now and key_at(b) != cur), None)
    starts = next((b for b in reversed([b for b in ordered if b <= now]) if key_at(b - _EPS) != cur), None)

    if cur[0] == "override":
        return PlanSlot(
            availability="available", location=ov.location, activity_kind=ov.activity_kind,
            source=PlanSource.OVERRIDE, item_ref=ov.override_id,
            interrupt_policy=default_interrupt_policy(ov.activity_kind),
            suggestions=suggestions_for(ov.activity_kind, PlanSource.OVERRIDE),
            starts_at=_utc(ov.started_at), ends_at=_utc(ov.expires_at), next_boundary_at=_utc(ov.expires_at),
            plan_revision=rev, override_state=ov.status)
    win = _winner(occs, now)
    if win is None:
        return PlanSlot(
            availability="available", location=PRESENCE_TARGET_HOST, activity_kind=ActivityKind.REST,
            source=PlanSource.DEFAULT, item_ref=None, interrupt_policy=InterruptPolicy.SAFE_POINT,
            suggestions=suggestions_for(ActivityKind.REST, PlanSource.DEFAULT),
            starts_at=starts or round_bounds(now, tz)[0], ends_at=next_b, next_boundary_at=next_b,
            plan_revision=rev)
    item = win.item
    location = item.location if item.location is not None else PRESENCE_TARGET_HOST
    if location not in PLAN_LOCATIONS:
        raise ValueError(f"未知地點：{location}")
    kind = ActivityKind.REST if item.location is None else item.activity_kind  # X3
    source = PlanSource.REST_WINDOW if item.role == "rest_window" else PlanSource.ITEM
    return PlanSlot(
        availability="available", location=location, activity_kind=kind, source=source, item_ref=item.item_id,
        interrupt_policy=item.interrupt_policy or default_interrupt_policy(kind),
        suggestions=suggestions_for(kind, source, item.suggested_behaviors),
        starts_at=starts or win.start, ends_at=next_b, next_boundary_at=next_b, plan_revision=rev,
        delay_s=win.delay_s)


def upcoming(items, override: OverrideView | None, start: datetime, horizon_s: int, tz: str,
             *, plan_revision: int = 0, delay: PlanDelay | None = None) -> tuple[PlanSlot, ...]:
    """[start, start+horizon) 內依序的投影格（含 start 當下那一格）。"""
    end = _utc(start) + timedelta(seconds=horizon_s)
    slot = resolve(items, override, start, tz, plan_revision=plan_revision, delay=delay)
    out = [slot]
    while slot.next_boundary_at is not None and slot.next_boundary_at < end and len(out) < _MAX_UPCOMING:
        slot = resolve(items, override, slot.next_boundary_at, tz, plan_revision=plan_revision, delay=delay)
        out.append(slot)
    return tuple(out)


def make_delay(slot: PlanSlot, planned_start_at: datetime, now: datetime,
               policy: DirectivePolicy | None = None) -> PlanDelay | None:
    """目前格超時仍在進行時產生 delay 輸入：held_until = now + retry_s（投影延長一個評估步長）。

    ``planned_start_at`` 為該格藍圖起點。僅 ITEM／REST_WINDOW 格可延長；呼叫端每個評估點重新產生。
    """
    policy = policy or DirectivePolicy()
    if slot.availability != "available" or slot.item_ref is None or slot.source not in (
            PlanSource.ITEM, PlanSource.REST_WINDOW):
        return None
    return PlanDelay(slot.item_ref, _utc(planned_start_at), _utc(now) + timedelta(seconds=policy.retry_s))


def defer_decision(first_deferred_at: datetime, now: datetime, slot_ends_at: datetime | None,
                   policy: DirectivePolicy | None = None) -> DeferDecision:
    """延後判定（PP-14）：計時由 first_deferred_at 起算，不因 tick 重置。

    - 格已結束 → drop_final（真正放棄）。
    - 達 defer_max_s → drop_attempt（只結束本次嘗試），下次評估點 now+retry_s；若該點已不早於格結束則無下次。
    - 其餘 wait。
    """
    policy = policy or DirectivePolicy()
    now, first = _utc(now), _utc(first_deferred_at)
    if slot_ends_at is not None and _utc(slot_ends_at) <= now:
        return DeferDecision("drop_final", "slot_ended", None)
    if now - first >= timedelta(seconds=policy.defer_max_s):
        nxt = now + timedelta(seconds=policy.retry_s)
        if slot_ends_at is not None and nxt >= _utc(slot_ends_at):
            nxt = None
        return DeferDecision("drop_attempt", "defer_max", nxt)
    return DeferDecision("wait", "deferred", None)


def reconcile_directive(slot: PlanSlot, actual_location: str | None, now: datetime, *,
                        reason: Literal["plan_boundary", "return_to_plan", "plan_changed",
                                        "startup_reconcile"] = "plan_boundary",
                        policy: DirectivePolicy | None = None) -> PlanDirective | None:
    """行程格與實際 Presence 位置不一致時的 directive；過期格、不可用、overlay 格一律不發（不重播）。

    ``actual_location`` 為 None 或 "host" 皆視為在主機（Presence NONE）。
    """
    policy = policy or DirectivePolicy()
    now = _utc(now)
    if slot.availability != "available" or slot.source == PlanSource.OVERRIDE:
        return None
    if (actual_location or PRESENCE_TARGET_HOST) == slot.location:
        return None
    if slot.ends_at is not None and _utc(slot.ends_at) <= now:
        return None
    expires = _utc(slot.ends_at) if slot.ends_at is not None else now + timedelta(seconds=policy.open_slot_ttl_s)
    ref = slot.item_ref or "default"
    return PlanDirective(
        directive_id=f"{ref}:{_utc(slot.starts_at).isoformat()}:{slot.plan_revision}", target=slot.location,
        reason=reason, slot=slot, not_before=now, expires_at=expires)
