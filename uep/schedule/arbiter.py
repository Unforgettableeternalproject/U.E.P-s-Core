"""在場請求仲裁（AR-1；純函式；契約 §9.1）。

輸入：當前投影 ``PlanSlot``、Presence 讀值、現有 override、請求或 directive、``InterruptFacts``。
輸出：``ArbitrationDecision``（ACCEPTED_TRANSFER／QUEUED／DEFERRED／REJECTED(固定 reason_code) 加 override 動作）。
本模組不執行任何東西：不呼叫 Presence Manager、不寫 override／mark（執行端另案，AR-3）。

位置：放在 ``uep/schedule/``（而不是 ``uep/presence/``）——純仲裁只依賴行程契約與 Planner 的 ``PresenceReading``，
不碰 Presence 寫入面；之後執行端（會呼叫 Presence Manager）才需要放在 ``uep/presence/`` 或 echo 組裝層。

優先序：PLAN 0 ＜ REQUESTER 50 ＜ SYSTEM 100 ＜ OWNER 200 ＜ EMERGENCY 300（``priority_of``）。
同級 owner 衝突（Q-PP-5，暫定：後到者優先，待 Bernie 明示）由 ``same_band_prevails`` 決定，可整個替換
（``arbitrate_request(..., same_band_policy=...)``）。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from uep.contracts.schedule import (
    PRESENCE_TARGET_HOST,
    PRIORITY_BY_AUTHORITY,
    PRIORITY_EMERGENCY,
    PRIORITY_OWNER,
    PRIORITY_PLAN,
    ActivityKind,
    ArbiterRequest,
    ArbitrationDecision,
    InterruptFacts,
    InterruptPolicy,
    OverrideView,
    PlanDirective,
    PlanSlot,
    PlanSource,
)
from uep.schedule.planner import PresenceReading

# ---- 固定的 reason_code ----
R_ACCEPTED = "accepted"
R_ALREADY_AT_TARGET = "already_at_target"
R_UNAUTHENTICATED = "unauthenticated"
R_EMERGENCY_REQUIRES_SYSTEM = "emergency_requires_system"
R_PRESENCE_RECOVERING = "presence_recovering"
R_TRANSFER_IN_PROGRESS = "transfer_in_progress"
R_WORK_IN_PROGRESS = "work_in_progress"
R_ABANDON_REQUIRES_HOST = "abandon_requires_host_target"
R_MEMBER_NOT_ACCEPTED = "member_not_accepted_in_slot"
R_OWNER_ONLY = "owner_only"
R_LOWER_THAN_OVERRIDE = "lower_priority_than_override"
R_SAME_PRIORITY_QUEUED = "same_priority_queued"
R_NOT_OVERRIDE_OWNER = "not_override_owner"
R_OVERRIDE_ENDED = "override_ended_return_to_plan"
R_DREAM_IN_FLIGHT = "dream_in_flight"
R_INTERACTION_ACTIVE = "interaction_active"
R_UNINTERRUPTIBLE_WORK = "uninterruptible_work"
R_OVERRIDE_ACTIVE = "override_active"
R_STALE_DIRECTIVE = "stale_directive"
R_DIRECTIVE_EXPIRED = "directive_expired"
REASON_CODES = frozenset({
    R_ACCEPTED, R_ALREADY_AT_TARGET, R_UNAUTHENTICATED, R_EMERGENCY_REQUIRES_SYSTEM, R_PRESENCE_RECOVERING,
    R_TRANSFER_IN_PROGRESS, R_WORK_IN_PROGRESS, R_ABANDON_REQUIRES_HOST, R_MEMBER_NOT_ACCEPTED, R_OWNER_ONLY,
    R_LOWER_THAN_OVERRIDE, R_SAME_PRIORITY_QUEUED, R_NOT_OVERRIDE_OWNER, R_OVERRIDE_ENDED, R_DREAM_IN_FLIGHT,
    R_INTERACTION_ACTIVE, R_UNINTERRUPTIBLE_WORK, R_OVERRIDE_ACTIVE, R_STALE_DIRECTIVE, R_DIRECTIVE_EXPIRED,
})

SameBandPolicy = Callable[[ArbiterRequest, OverrideView], bool]


def priority_of(request: ArbiterRequest) -> int:
    """優先序帶：緊急 300（僅 system）；其餘依 authority（member 50／system 100／owner 200）。"""
    return PRIORITY_EMERGENCY if request.emergency else PRIORITY_BY_AUTHORITY[request.authority]


def same_band_prevails(request: ArbiterRequest, current: OverrideView) -> bool:
    """同級衝突（Q-PP-5，暫定，待 Bernie 明示）：owner 後到者優先；其餘一律排隊（FIFO）。

    要換成別的規則時，把函式傳給 ``arbitrate_request(same_band_policy=...)``。順序依 Core 收到時間，
    現有 override 一定比新請求早受理，所以這裡只需要看 authority。
    """
    return request.authority == "owner"


def actual_location(presence: PresenceReading) -> str | None:
    """ACTIVE＝其位置；NONE＝主機；RECOVERING／不明＝None。"""
    if presence.status == "ACTIVE":
        return presence.location
    if presence.status == "NONE":
        return PRESENCE_TARGET_HOST
    return None


def _reject(code: str, priority: int = PRIORITY_PLAN) -> ArbitrationDecision:
    return ArbitrationDecision("REJECTED", code, priority)


def _defer(code: str, priority: int) -> ArbitrationDecision:
    return ArbitrationDecision("DEFERRED", code, priority)


def _safe_point_block(slot: PlanSlot, probe: InterruptFacts, actual: str | None, target: str) -> str | None:
    """離開目前位置前的安全點檢查：回需要等待的 reason_code（None＝可以走）。

    ``interrupt_policy=free`` 的格不因進行中的互動等待；不可中斷工作與做夢一律要等（緊急例外，呼叫端先處理）。
    """
    if actual == target:
        return None  # 不離開目前位置
    if probe.dream_in_flight and actual == PRESENCE_TARGET_HOST:
        return R_DREAM_IN_FLIGHT  # 在主機做夢中被叫：先做完這一輪（X9）
    if probe.has_uninterruptible_work:
        return R_UNINTERRUPTIBLE_WORK
    if probe.interaction_active and slot.interrupt_policy != InterruptPolicy.FREE:
        return R_INTERACTION_ACTIVE
    return None


def _is_work(slot: PlanSlot) -> bool:
    return slot.availability == "available" and slot.source == PlanSource.ITEM and slot.activity_kind == ActivityKind.WORK


def arbitrate_request(
    request: ArbiterRequest, *, slot: PlanSlot, presence: PresenceReading, override: OverrideView | None,
    probe: InterruptFacts, now: datetime, same_band_policy: SameBandPolicy = same_band_prevails,
) -> ArbitrationDecision:
    """owner／system／member 的在場請求（含「回主機」target=host）。"""
    band = priority_of(request)
    if request.actor_authenticated is not True:
        return _reject(R_UNAUTHENTICATED)  # 未認證者不建立請求（只留言／中性回條）
    if request.emergency and request.authority != "system":
        return _reject(R_EMERGENCY_REQUIRES_SYSTEM)
    actual = actual_location(presence)
    leave = request.target == PRESENCE_TARGET_HOST
    if override is not None and override.expires_at <= now:
        override = None  # 資料列還沒被 reconcile，但已過期：不算生效
    has_override = override is not None or slot.source == PlanSource.OVERRIDE
    emergency = request.emergency

    if not emergency:
        if actual is None:
            return _defer(R_PRESENCE_RECOVERING, band)
        if presence.transfer_active:
            return ArbitrationDecision("QUEUED", R_TRANSFER_IN_PROGRESS, band)  # 進行中的移轉不中斷

    # ---- 緊急：覆寫一切，只形成 override，不放棄工作項 ----
    if emergency:
        if leave:
            return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, band, "supersede" if has_override else "none",
                                       PRESENCE_TARGET_HOST)
        return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, band, "supersede" if has_override else "begin",
                                   None if actual == request.target else request.target, request.target)

    # ---- 她已被叫到行程外（有 override）：只比優先序 ----
    if has_override:
        if leave:  # 回主機＝owner 顯式結束該 override，之後走 return_to_plan 一般仲裁
            if request.authority in ("owner", "system"):
                return ArbitrationDecision("ACCEPTED_TRANSFER", R_OVERRIDE_ENDED, band, "end",
                                           override_end_reason="owner_ended", return_to_plan=True)
            return _reject(R_NOT_OVERRIDE_OWNER, band)
        current_band = override.priority if override is not None and override.priority is not None else PRIORITY_OWNER
        if band < current_band:
            return _reject(R_LOWER_THAN_OVERRIDE, band)
        if band == current_band and not (override is not None and same_band_policy(request, override)):
            return ArbitrationDecision("QUEUED", R_SAME_PRIORITY_QUEUED, band)
        blocked = _safe_point_block(slot, probe, actual, request.target)
        if blocked is not None:
            return _defer(blocked, band)
        return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, band, "supersede",
                                   None if actual == request.target else request.target, request.target)

    # ---- 沒有 override：依當前行程格分流 ----
    abandon = False
    if _is_work(slot):
        if request.explicit_abandon and request.authority == "owner":
            if not leave:
                return _reject(R_ABANDON_REQUIRES_HOST, band)
            abandon = True
        else:
            return _reject(R_WORK_IN_PROGRESS, band)  # WORK（含 pause）只受理明示放棄（owner）與系統緊急
    elif (slot.activity_kind == ActivityKind.SOCIAL or slot.source == PlanSource.REST_WINDOW)             and request.authority == "member":
        return _reject(R_MEMBER_NOT_ACCEPTED, band)  # SOCIAL／SLEEP 時段保守：只受理 owner 與系統
    if slot.interrupt_policy == InterruptPolicy.OWNER_ONLY and request.authority != "owner":
        return _reject(R_OWNER_ONLY, band)

    if leave and actual == PRESENCE_TARGET_HOST:
        return _reject(R_ALREADY_AT_TARGET, band)
    blocked = _safe_point_block(slot, probe, actual, request.target)
    if blocked is not None:
        return _defer(blocked, band)
    if leave:
        # 放棄工作（明示）或非工作格的「回主機」（外推：視為放棄當前行程項）→ mark(abandon) 加移轉
        mark = abandon or (slot.source == PlanSource.ITEM and slot.item_ref is not None
                           and slot.slot_state != "abandoned")
        return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, band, "mark_abandon" if mark else "none",
                                   PRESENCE_TARGET_HOST)
    return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, band, "begin",
                               None if actual == request.target else request.target, request.target)


def arbitrate_directive(
    directive: PlanDirective, *, slot: PlanSlot, presence: PresenceReading, override: OverrideView | None,
    probe: InterruptFacts, now: datetime,
) -> ArbitrationDecision:
    """行程 directive（優先序 PLAN，嚴格最低）：無更高優先事項、符合中斷規則時才移轉；不建立 override。"""
    if directive.expires_at <= now:
        return _reject(R_DIRECTIVE_EXPIRED)  # 過期即丟，不重播
    if override is not None and override.expires_at <= now:
        override = None
    if override is not None or slot.source == PlanSource.OVERRIDE:
        return _reject(R_OVERRIDE_ACTIVE)
    if slot.availability != "available" or slot.location != directive.target:
        return _reject(R_STALE_DIRECTIVE)  # 行程已變：目標不再是當前格的位置
    actual = actual_location(presence)
    if actual is None:
        return _defer(R_PRESENCE_RECOVERING, PRIORITY_PLAN)
    if actual == directive.target:
        return _reject(R_ALREADY_AT_TARGET)
    if presence.transfer_active:
        return ArbitrationDecision("QUEUED", R_TRANSFER_IN_PROGRESS, PRIORITY_PLAN)
    blocked = _safe_point_block(slot, probe, actual, directive.target)
    if blocked is not None:
        return _defer(blocked, PRIORITY_PLAN)
    return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, PRIORITY_PLAN, "none", directive.target)
