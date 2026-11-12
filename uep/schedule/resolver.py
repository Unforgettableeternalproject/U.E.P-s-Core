"""行程解析（純函式，PP-0）：覆蓋層 > 行程項（藍圖→投影）> 預設層（主機＋REST）。

藍圖／投影（Bernie 裁 Q-PP-4）：
- 藍圖＝原始 ``PlanItem``，永不改寫。
- 投影＝套用 overlay 與 ``PlanDelay`` 後的有效時間軸。delay 只存在於投影、只在當前輪次內（Q-PP-3）：
  目前格延長到 ``held_until``，其後起點落在延長區間內的行程項，實際起點順延到 ``held_until``，
  結束時間維持原結束（壓縮，Bernie 已裁）；壓縮後時長 <= 0 的項在投影中視為被吃掉（藍圖不改）。
行程不可用是狀態值（``availability="unavailable"``），不是例外（PP-1）。
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    SlotMark,
    SlotNotice,
    SlotState,
)

_UTC = UTC
_EPS = timedelta(microseconds=1)
SCAN_HORIZON = timedelta(hours=48)  # §6.1：掃描視野 48 小時
_MAX_UPCOMING = 500
LOOKBACK = timedelta(days=8)  # 向後掃描：只用來找上一個邊界（starts_at／directive_id 錨）
DEFAULT_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)  # 預設格找不到上一個邊界時的固定錨
logger = logging.getLogger(__name__)
_warned_keys: set[tuple[str, str]] = set()  # (對象, 錯誤類別) 去重：同一問題不在每次 resolve 都重複 warning


def _warn_once(key: tuple[str, str], message: str, *args) -> None:
    if key in _warned_keys:
        return
    if len(_warned_keys) >= 1024:  # 上限：避免無限成長
        _warned_keys.clear()
    _warned_keys.add(key)
    logger.warning(message, *args)


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
                    explicit: tuple[str, ...] | None = None,
                    slot_state: SlotState = "normal") -> tuple[BehaviorSuggestion, ...]:
    """§8.3 預設對照；abandoned／paused 優先於其他列；item 明確 suggested_behaviors 以其為準。"""
    if slot_state == "abandoned":
        return (BehaviorSuggestion(MODE_REST, 0, "plan_abandoned"),)
    if slot_state == "paused" and kind == ActivityKind.WORK:
        return (BehaviorSuggestion(MODE_CHAT, 0, "work_paused"),)
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


def unavailable_slot(at: datetime, plan_revision: int = 0, reason_code: str | None = None) -> PlanSlot:
    """行程不可用（X1）：host、無建議、source=DEFAULT；``reason_code`` 說明原因（如 tz_unavailable）。"""
    return PlanSlot(
        availability="unavailable", location=PRESENCE_TARGET_HOST, activity_kind=ActivityKind.REST,
        source=PlanSource.DEFAULT, item_ref=None, interrupt_policy=InterruptPolicy.SAFE_POINT,
        suggestions=(), starts_at=_utc(at), ends_at=None, next_boundary_at=None, plan_revision=plan_revision,
        standby=True, reason_code=reason_code)


# ---- 藍圖 → 出現區間 → 投影 ----

@dataclass(frozen=True)
class _Occ:
    item: PlanItem
    bp_start: datetime  # 藍圖起點（已套有效期限）
    bp_end: datetime
    start: datetime  # 投影起點
    end: datetime  # 投影終點
    delay_s: int = 0
    tag: str = ""  # "abandoned"＝放棄後的 fallback REST 片段

    @property
    def key(self) -> tuple[str, datetime, str]:
        return (self.item.item_id, self.bp_start, self.tag)


def _item_occurrences(item: PlanItem, lo: datetime, hi: datetime) -> list[_Occ]:
    if item.location is not None and item.location not in PLAN_LOCATIONS:
        raise ValueError(f"未知地點：{item.location}（{item.item_id}）")
    spans: list[tuple[datetime, datetime]] = []
    if item.recurrence == "once":
        if item.once_start_at is None or item.once_end_at is None:
            raise ValueError(f"once 項缺少起迄：{item.item_id}")
        spans.append((_utc(item.once_start_at), _utc(item.once_end_at)))
    elif item.recurrence == "weekly":
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
    else:
        raise ValueError(f"未知 recurrence：{item.recurrence}（{item.item_id}）")
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


def _apply_delay(occs: list[_Occ], delay: PlanDelay | None, tz: str,
                 marked: frozenset[tuple[str, datetime]] = frozenset()) -> list[_Occ]:
    """把藍圖出現區間投影成有效時間軸（delay 只在當前輪次內；順延後維持原結束＝壓縮）。

    休息窗口固定不順延（PP-10）：rest_window 不能當 anchor，也不被順延或吃掉。
    """
    if delay is None:
        return occs
    anchor_start = _utc(delay.planned_start_at)
    anchor = next((o for o in occs if o.item.item_id == delay.item_ref and o.bp_start == anchor_start), None)
    if anchor is None:
        _warn_once((delay.item_ref, "delay_anchor_missing"), "delay 的 anchor 不在藍圖中，已忽略：item=%s planned_start=%s",
                   delay.item_ref, anchor_start)
        return occs
    if (delay.item_ref, anchor_start) in marked:  # 投影優先序 abandon > pause > delay：被標記的出現不延長
        logger.debug("該次出現已被暫停／放棄，delay 失效：item=%s", delay.item_ref)
        return occs
    if anchor.item.role == "rest_window":
        _warn_once((delay.item_ref, "delay_rest_window"), "rest_window 不可被順延（PP-10），已忽略 delay：item=%s",
                   delay.item_ref)
        return occs
    hold = min(_utc(delay.held_until), round_bounds(anchor.bp_start, tz)[1])  # 不跨輪次
    if hold <= anchor.bp_end:
        return occs
    out: list[_Occ] = []
    for o in occs:
        if o is anchor:
            out.append(_Occ(o.item, o.bp_start, o.bp_end, o.start, hold, int((hold - o.bp_end).total_seconds())))
        elif o.item.role != "rest_window" and anchor.bp_end <= o.bp_start < hold:
            # 起點落在延長區間內：實際起點順延，結束不變；壓縮後時長 <= 0 → 投影中被吃掉
            if o.bp_end > hold:
                out.append(_Occ(o.item, o.bp_start, o.bp_end, hold, o.bp_end,
                                int((hold - o.bp_start).total_seconds())))
        else:
            out.append(o)
    return out


def _apply_marks(occs: list[_Occ], marks: Sequence[SlotMark], tz: str) -> list[_Occ]:
    """abandon／pause：該次出現自 mark.started_at 起轉 abandoned（host＋REST）或 paused（留原平台待命）。

    兩者效力上限一致：出現終點或 mark 所屬輪次結束（取先到）；輪次結束後依藍圖。先處理 abandon 再處理 pause。
    abandon 適用於任何非 rest_window 的行程項（WORK、SOCIAL，或 location 非 host 的項；契約 §9.1 (c)：非工作格的
    「回主機」也是放棄當前格）；pause 只適用於 WORK。放棄訊號路徑（``consume_abandon``）仍只收 WORK（PP-21）。
    """
    for kind, tag in (("abandon", "abandoned"), ("pause", "paused")):
        for mark in marks:
            if mark.kind != kind:
                continue
            at, planned = _utc(mark.started_at), _utc(mark.planned_start_at)
            target = next((o for o in occs if o.item.item_id == mark.item_ref and o.bp_start == planned
                           and o.tag == "" and o.item.role != "rest_window"
                           and (kind == "abandon" or o.item.activity_kind == ActivityKind.WORK)
                           and o.start <= at < o.end), None)
            if target is None:
                logger.debug("%s mark 沒有對應的工作出現，已忽略：item=%s", kind, mark.item_ref)
                continue
            rend = round_bounds(at, tz)[1]
            pieces: list[_Occ] = []
            if at > target.start:
                pieces.append(_Occ(target.item, target.bp_start, target.bp_end, target.start, at, target.delay_s))
            pieces.append(_Occ(target.item, target.bp_start, target.bp_end, at, min(target.end, rend),
                               target.delay_s, tag))
            if rend < target.end:
                pieces.append(_Occ(target.item, target.bp_start, target.bp_end, rend, target.end, target.delay_s))
            occs = [x for o in occs for x in (pieces if o is target else [o])]
    return occs


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
            *, plan_revision: int = 0, delay: PlanDelay | None = None,
            marks: Sequence[SlotMark] = ()) -> PlanSlot:
    """此刻的 PlanSlot（投影）。

    - 行程不可用是狀態值：行程時區（``tz``）無法載入 → ``availability="unavailable"``、``reason_code="tz_unavailable"``。
    - 單一壞項（資料驗證失敗：缺欄位、未知地點、項自身 tz 不可用）略過並 ``logger.warning``，override 與其他項照常解析。
    - TypeError／AttributeError 等程式錯誤不攔截，往上拋。
    - 壞項 warning 以 (項, 錯誤類別) 去重，同一問題只記一次（進程內）。
    - 輪次邊界（本地日界）與 mark／override 的效力上限一致；``delay`` 同樣不跨輪次。
    - ``marks``：暫停／放棄標記。投影優先序 override > abandon > pause > delay > 藍圖：
      abandon 令該次出現轉 host＋REST（``slot_state="abandoned"``）；pause 令工作格留在原平台待命、建議 [CHAT]
      （``slot_state="paused"``）；被標記的出現其 delay 失效。藍圖不改寫。
    - ``next_boundary_at`` 為 None 只表示「掃描窗（前方 48 小時）內沒有」，不保證永不改變；需要更遠請用 ``upcoming``。
    """
    now = _utc(now)  # 無時區是呼叫端錯誤，照樣拋 ValueError
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        logger.warning("行程時區不可用，行程標示為不可用：tz=%s error=%s", tz, exc)
        return unavailable_slot(now, plan_revision, "tz_unavailable")
    return _resolve(items, override, now, tz, plan_revision, delay, marks)


def _resolve(items, override, now: datetime, tz: str, rev: int, delay, marks) -> PlanSlot:
    lo, hi = now - LOOKBACK, now + SCAN_HORIZON  # 向後多看只為了讓 starts_at（directive_id 錨）穩定
    occs: list[_Occ] = []
    for item in items:
        try:
            occs.extend(_item_occurrences(item, lo, hi))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            _warn_once((str(getattr(item, "item_id", "?")), type(exc).__name__),
                       "行程項資料無效，已略過：item=%s error=%s", getattr(item, "item_id", "?"), exc)
    marked = frozenset((m.item_ref, _utc(m.planned_start_at)) for m in marks)
    occs = _apply_marks(_apply_delay(occs, delay, tz, marked), marks, tz)
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
            plan_revision=rev, override_state=ov.status, standby=ov.activity_kind == ActivityKind.REST)
    win = _winner(occs, now)
    if win is None:
        # 預設格的 starts_at 是 directive_id 的錨：向後看 8 天找上一個邊界，找不到用固定值（不隨時間改變）
        return PlanSlot(
            availability="available", location=PRESENCE_TARGET_HOST, activity_kind=ActivityKind.REST,
            source=PlanSource.DEFAULT, item_ref=None, interrupt_policy=InterruptPolicy.SAFE_POINT,
            suggestions=suggestions_for(ActivityKind.REST, PlanSource.DEFAULT),
            starts_at=starts or DEFAULT_EPOCH, ends_at=next_b, next_boundary_at=next_b, plan_revision=rev,
            standby=True)
    item = win.item
    source = PlanSource.REST_WINDOW if item.role == "rest_window" else PlanSource.ITEM
    common = {"availability": "available", "source": source, "item_ref": item.item_id,
              "starts_at": starts or win.start, "ends_at": next_b, "next_boundary_at": next_b, "plan_revision": rev,
              "blueprint_start_at": win.bp_start, "delay_s": win.delay_s, "tz": item.tz}
    if win.tag == "abandoned":  # fallback＝REST（主機），藍圖不改
        return PlanSlot(location=PRESENCE_TARGET_HOST, activity_kind=ActivityKind.REST,
                        interrupt_policy=InterruptPolicy.SAFE_POINT,
                        suggestions=suggestions_for(ActivityKind.REST, source, slot_state="abandoned"),
                        standby=True, slot_state="abandoned", **common)
    location = item.location if item.location is not None else PRESENCE_TARGET_HOST
    kind = ActivityKind.REST if item.location is None else item.activity_kind  # X3
    interrupt = item.interrupt_policy or default_interrupt_policy(kind)
    paused = win.tag == "paused"
    if paused:  # 留在原平台待命：位置與 activity_kind 不變，建議 [CHAT]
        return PlanSlot(location=location, activity_kind=kind, interrupt_policy=interrupt,
                        suggestions=suggestions_for(kind, source, slot_state="paused"),
                        standby=True, slot_state="paused", **common)
    # 待命只有 REST（含預設、fallback）；SOCIAL、休息時段（SLEEP）、WORK 不是
    return PlanSlot(location=location, activity_kind=kind, interrupt_policy=interrupt,
                    suggestions=suggestions_for(kind, source, item.suggested_behaviors),
                    standby=kind == ActivityKind.REST and source != PlanSource.REST_WINDOW, **common)


def upcoming(items, override: OverrideView | None, start: datetime, horizon_s: int, tz: str,
             *, plan_revision: int = 0, delay: PlanDelay | None = None,
             marks: Sequence[SlotMark] = ()) -> tuple[PlanSlot, ...]:
    """[start, start+horizon) 內依序的投影格（含 start 當下那一格）。

    ``resolve`` 的 next_boundary_at 為 None 只代表掃描窗內沒有；這裡在 horizon 未到時往前推窗繼續找。
    ``marks``／``delay`` 與 resolve 相同，對整段投影套用（「現在」的狀態，不預測未來才加入的標記）。
    推窗時，前一格的 ends_at／next_boundary_at 補成下一格的起點。
    """
    end = _utc(start) + timedelta(seconds=horizon_s)
    kw = {"plan_revision": plan_revision, "delay": delay, "marks": marks}
    cur_t = _utc(start)
    slot = resolve(items, override, cur_t, tz, **kw)
    out = [slot]
    while len(out) < _MAX_UPCOMING and slot.availability == "available":
        if slot.next_boundary_at is None:
            probe = cur_t + SCAN_HORIZON  # 掃描窗內沒有邊界：推窗
            if probe >= end:
                break
            cur_t = probe
            nxt = resolve(items, override, cur_t, tz, **kw)
            same = (nxt.source, nxt.item_ref, nxt.blueprint_start_at, nxt.slot_state) == (
                slot.source, slot.item_ref, slot.blueprint_start_at, slot.slot_state)
            if not same:  # 前一格在新格起點結束：補上 ends_at／next_boundary_at
                out[-1] = replace(out[-1], ends_at=nxt.starts_at, next_boundary_at=nxt.starts_at)
                out.append(nxt)
            else:  # 同一格延續：把後續掃描找到的終點補回該格
                out[-1] = replace(out[-1], ends_at=nxt.ends_at, next_boundary_at=nxt.next_boundary_at)
            slot = nxt
            continue
        if slot.next_boundary_at >= end:
            break
        cur_t = slot.next_boundary_at
        slot = resolve(items, override, cur_t, tz, **kw)
        out.append(slot)
    return tuple(out)


def slot_transition(prev: PlanSlot | None, cur: PlanSlot) -> tuple[SlotNotice, ...]:
    """相鄰投影格的差異 → 離開／進入通知（純函式；PP-0 不發事件、不渲染）。

    同一行程項僅因 delay 延長不算變化；回傳空 tuple 表示沒有進入／離開。
    """
    def ident(x: PlanSlot):
        return (x.availability, x.source, x.item_ref, x.blueprint_start_at, x.slot_state)

    if prev is not None and ident(prev) == ident(cur):
        return ()
    if cur.availability == "unavailable":
        reason = "unavailable"
    elif prev is not None and prev.availability == "unavailable":
        reason = "available"
    elif cur.slot_state == "abandoned":
        reason = "abandoned"
    elif cur.slot_state == "paused":
        reason = "paused"
    elif prev is not None and prev.slot_state == "paused" and prev.item_ref == cur.item_ref:
        reason = "resumed"
    elif cur.source == PlanSource.OVERRIDE:
        reason = "override_started"
    elif prev is not None and prev.source == PlanSource.OVERRIDE:
        reason = "override_ended"
    else:
        reason = "boundary"
    left = (SlotNotice("left", prev, reason),) if prev is not None else ()
    return (*left, SlotNotice("entered", cur, reason))


# ---- 延後（delay）與 directive ----

@dataclass(frozen=True)
class DelayStep:
    """``make_delay`` 的結果。delay 為 None 時依 action 判讀：

    - ``drop_attempt``：達延後上限，結束本次嘗試（投影不再順延），下一評估點為 ``next_eval_at``。
    - ``drop_final``：目標格已結束，放棄。
    - ``not_eligible``：此格不可延長（rest_window、override、預設層、不可用）。
    """

    delay: PlanDelay | None
    action: Literal["wait", "drop_attempt", "drop_final", "not_eligible"]
    reason_code: str
    next_eval_at: datetime | None = None


class DelayAnchorError(ValueError):
    """行程項格缺少藍圖起點，無法建立 delay anchor（可辨識的錯誤，不靜默）。"""


def make_delay(slot: PlanSlot, now: datetime, first_deferred_at: datetime, hard_end_at: datetime | None,
               policy: DirectivePolicy | None = None) -> DelayStep:
    """目前格超時仍在進行時，產生投影用的 delay（held_until = now + retry_s，不超過 hard_end_at）。

    ``first_deferred_at``：本次延後嘗試的起點（不因 tick 重置）。``hard_end_at``：被延後的目標格的硬終點
    （目標格自己的結束，與 delay 投影後的終點分開）。達 ``defer_max_s`` 回 delay=None 並給下一評估點。
    anchor 取自 ``slot.blueprint_start_at``；rest_window 固定不順延，回 not_eligible。
    """
    policy = policy or DirectivePolicy()
    if slot.availability != "available" or slot.source not in (PlanSource.ITEM, PlanSource.REST_WINDOW):
        return DelayStep(None, "not_eligible", "source_not_extendable")
    if slot.source == PlanSource.REST_WINDOW:
        return DelayStep(None, "not_eligible", "rest_window_fixed")
    if slot.slot_state != "normal":  # 暫停或放棄的格不產生 delay（pause／abandon 優先於 delay）
        return DelayStep(None, "not_eligible", f"slot_{slot.slot_state}")
    if slot.item_ref is None or slot.blueprint_start_at is None:
        logger.error("行程項格缺少 blueprint_start_at，無法建立 delay anchor：item=%s", slot.item_ref)
        raise DelayAnchorError(f"slot 缺少 blueprint_start_at：{slot.item_ref}")
    decision = defer_decision(first_deferred_at, now, hard_end_at, policy)
    if decision.action != "wait":
        return DelayStep(None, decision.action, decision.reason_code, decision.next_eval_at)
    held = _utc(now) + timedelta(seconds=policy.retry_s)
    if hard_end_at is not None:
        held = min(held, _utc(hard_end_at))
    return DelayStep(PlanDelay(slot.item_ref, _utc(slot.blueprint_start_at), held), "wait", "deferred")


def defer_decision(first_deferred_at: datetime, now: datetime, hard_end_at: datetime | None,
                   policy: DirectivePolicy | None = None) -> DeferDecision:
    """延後判定（PP-14）：計時由 first_deferred_at 起算，不因 tick 重置。

    ``hard_end_at`` 是目標格自己的結束（不是 delay 投影後的終點）。
    - 格已結束 → drop_final（真正放棄）。
    - 達 defer_max_s → drop_attempt（只結束本次嘗試），下次評估點 now+retry_s；
      若該點不早於格結束，改在格結束前 1 秒再評一次（格內最後 retry_s 內仍要再評一次）。
    - 其餘 wait。
    """
    policy = policy or DirectivePolicy()
    now, first = _utc(now), _utc(first_deferred_at)
    end = _utc(hard_end_at) if hard_end_at is not None else None
    if end is not None and end <= now:
        return DeferDecision("drop_final", "slot_ended", None)
    if now - first >= timedelta(seconds=policy.defer_max_s):
        nxt: datetime | None = now + timedelta(seconds=policy.retry_s)
        if end is not None and nxt >= end:
            nxt = end - timedelta(seconds=1)
            if nxt <= now:
                nxt = None
        return DeferDecision("drop_attempt", "defer_max", nxt)
    return DeferDecision("wait", "deferred", None)


def reconcile_directive(slot: PlanSlot, actual_location: str | None, now: datetime, *,
                        reason: Literal["plan_boundary", "return_to_plan", "plan_changed",
                                        "startup_reconcile"] = "plan_boundary",
                        policy: DirectivePolicy | None = None) -> PlanDirective | None:
    """行程格與實際 Presence 位置不一致時的 directive；過期格、不可用、overlay 格一律不發（不重播）。

    ``actual_location`` 為 None 或 "host" 皆視為在主機（Presence NONE）。
    directive_id 的錨：預設格用穩定的 starts_at（見 ``_resolve``），不會因時間推移改變。
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
