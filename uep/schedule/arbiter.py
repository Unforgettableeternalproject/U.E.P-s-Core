"""在場請求仲裁（AR-1；純函式；契約 §9.1）。

輸入：當前投影 ``PlanSlot``、Presence 讀值、現有 override、請求或 directive、``InterruptFacts``。
輸出：``ArbitrationDecision``（ACCEPTED_TRANSFER／QUEUED／DEFERRED／REJECTED(固定 reason_code) 加 override 動作）。
本模組不執行任何東西：不呼叫 Presence Manager、不寫 override／mark（執行端另案，AR-3）。

位置：放在 ``uep/schedule/``（而不是 ``uep/presence/``）——純仲裁只依賴行程契約與 Planner 的 ``PresenceReading``，
不碰 Presence 寫入面；之後執行端（會呼叫 Presence Manager）才需要放在 ``uep/presence/`` 或 echo 組裝層。

優先序：PLAN 0 ＜ REQUESTER 50 ＜ ADMIN 75 ＜ SYSTEM 100 ＜ OWNER 200 ＜ EMERGENCY 300（``priority_of``）。
同級 owner 衝突（Q-PP-5，暫定：後到者優先，待 Bernie 明示）由 ``same_band_prevails`` 決定，可整個替換
（``arbitrate_request(..., same_band_policy=...)``）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from uep.contracts.permission import (
    DEFAULT_POLICY,
    GRADE_LEVEL,
    PRESENCE_REQUEST_MIN_TIER,
    InstructionLevel,
    PermissionPolicy,
    RequesterTier,
    WorkGrade,
    system_component,
)
from uep.contracts.schedule import (
    PRESENCE_TARGET_HOST,
    PRIORITY_EMERGENCY,
    PRIORITY_OWNER,
    PRIORITY_PLAN,
    ActivityKind,
    ArbiterRequest,
    ArbitrationDecision,
    InterruptFacts,
    InterruptTiming,
    OverrideView,
    PlanDirective,
    PlanSlot,
    PlanSource,
    SlotMark,
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
R_EMERGENCY_OVERRIDE_ACTIVE = "emergency_override_active"  # 緊急 override 只有 system 能結束（待 Bernie 確認）
R_OVERRIDE_ENDED = "override_ended_return_to_plan"
R_DREAM_IN_FLIGHT = "dream_in_flight"
R_INTERACTION_ACTIVE = "interaction_active"
R_UNINTERRUPTIBLE_WORK = "uninterruptible_work"
R_OVERRIDE_ACTIVE = "override_active"
R_STALE_DIRECTIVE = "stale_directive"
R_DIRECTIVE_EXPIRED = "directive_expired"
# 權限契約 §4.3 新增：格上 min_rank 擋下（有效門檻為 OWNER 時沿用舊碼 owner_only）、通用階級不足、認證但低於移轉門檻
R_MIN_RANK_INSUFFICIENT = "min_rank_insufficient"
R_TIER_INSUFFICIENT = "tier_insufficient"
R_TIER_BELOW_FLOOR = "tier_below_floor"
REASON_CODES = frozenset({
    R_ACCEPTED, R_ALREADY_AT_TARGET, R_UNAUTHENTICATED, R_EMERGENCY_REQUIRES_SYSTEM, R_PRESENCE_RECOVERING,
    R_TRANSFER_IN_PROGRESS, R_WORK_IN_PROGRESS, R_ABANDON_REQUIRES_HOST, R_MEMBER_NOT_ACCEPTED, R_OWNER_ONLY,
    R_LOWER_THAN_OVERRIDE, R_SAME_PRIORITY_QUEUED, R_NOT_OVERRIDE_OWNER, R_EMERGENCY_OVERRIDE_ACTIVE, R_OVERRIDE_ENDED, R_DREAM_IN_FLIGHT,
    R_INTERACTION_ACTIVE, R_UNINTERRUPTIBLE_WORK, R_OVERRIDE_ACTIVE, R_STALE_DIRECTIVE, R_DIRECTIVE_EXPIRED,
    R_MIN_RANK_INSUFFICIENT, R_TIER_INSUFFICIENT, R_TIER_BELOW_FLOOR,
})

SameBandPolicy = Callable[[ArbiterRequest, OverrideView], bool]


def priority_of(request: ArbiterRequest) -> int:
    """優先序帶＝請求者階級的值：緊急 300（僅 system）；其餘 member 50／admin 75／system 100／owner 200。"""
    return int(request.tier)


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


def _reject(code: str, priority: int = PRIORITY_PLAN, **audit) -> ArbitrationDecision:
    return ArbitrationDecision("REJECTED", code, priority, **audit)


def _defer(code: str, priority: int, **audit) -> ArbitrationDecision:
    return ArbitrationDecision("DEFERRED", code, priority, **audit)


@dataclass(frozen=True)
class _Check:
    """一道授權門檻：``axis`` 是審計軸、``required`` 是階級門檻、``code`` 是不足時的 reason_code。"""

    axis: str
    required: int  # 階級值（RequesterTier 值或歷史 override 的帶值）
    code: str


def _denial(request: ArbiterRequest, band: int, checks: list[_Check], policy: PermissionPolicy,
            ) -> ArbitrationDecision | None:
    """依序檢查門檻；全部通過回 None。第一個不足的門檻決定 reason_code 與 blocked_axis，其餘不足的軸進 also_failed。"""
    failed = [c for c in checks if request.tier < c.required]
    if not failed:
        return None
    first = failed[0]
    also = tuple(dict.fromkeys(c.axis for c in failed[1:] if c.axis != first.axis))
    return _reject(first.code, band, blocked_axis=first.axis, also_failed=also, required=int(first.required),
                   actual=int(request.tier), policy_revision=policy.revision)


def _min_rank_check(slot: PlanSlot) -> _Check | None:
    """格上 ``min_rank``（誰能叫走她）；有效門檻為 OWNER 時沿用舊碼 ``owner_only``（§3.6）。"""
    if slot.min_rank is None:
        return None
    code = R_OWNER_ONLY if slot.min_rank == RequesterTier.OWNER else R_MIN_RANK_INSUFFICIENT
    return _Check("tier", slot.min_rank, code)


def _slot_grade(slot: PlanSlot) -> WorkGrade:
    """工作格的分級；``PlanSlot`` 暫無 work_grade 欄位（PM-4 才加），恆為 NORMAL。"""
    return getattr(slot, "work_grade", None) or WorkGrade.NORMAL


def _displacement_checks(slot: PlanSlot, policy: PermissionPolicy) -> list[_Check]:
    """§3.4 各格類別的受理門檻（非放棄；依判定順序：指示等級 → 階級下限 → 工作分級 → 格上 min_rank）。

    有效門檻＝三者取最大（§3.3）：格上 min_rank、work_grade 推導的門檻、臨時階段的最低階級；每一道各自成檢查，
    取最大等價於全部通過，同時保留是哪一軸擋下。
    """
    checks: list[_Check] = []
    temp = policy.required_tier(InstructionLevel.TEMP_SESSION)
    if _is_work(slot):
        checks.append(_Check("instruction", temp, R_WORK_IN_PROGRESS))
        checks.append(_Check("grade", policy.required_tier(GRADE_LEVEL[_slot_grade(slot)]), R_WORK_IN_PROGRESS))
    elif slot.source == PlanSource.REST_WINDOW:  # SLEEP 休息窗：TEMP_SESSION 且階級 ≥ SYSTEM（受信任系統指令；Q-PM-5）
        checks.append(_Check("instruction", temp, R_MEMBER_NOT_ACCEPTED))
        checks.append(_Check("tier", RequesterTier.SYSTEM, R_MEMBER_NOT_ACCEPTED))
    elif slot.activity_kind == ActivityKind.SOCIAL:
        checks.append(_Check("instruction", temp, R_MEMBER_NOT_ACCEPTED))
    else:  # 待命（REST 行程、fallback、預設層、暫停中的工作也屬工作格，已在上面處理）：沒有東西被取代，不是臨時階段
        checks.append(_Check("instruction", policy.required_tier(InstructionLevel.ACTION), R_TIER_INSUFFICIENT))
        checks.append(_Check("tier", PRESENCE_REQUEST_MIN_TIER, R_TIER_BELOW_FLOOR))
    min_rank = _min_rank_check(slot)
    if min_rank is not None:
        checks.append(min_rank)
    return checks


def _safe_point_block(slot: PlanSlot, probe: InterruptFacts, actual: str | None, target: str) -> str | None:
    """離開目前位置前的安全點檢查：回需要等待的 reason_code（None＝可以走）。

    ``interrupt_timing=free`` 的格（含 ``min_rank`` 為 OWNER 的舊 owner_only 格）不因進行中的互動等待；
    不可中斷工作與做夢一律要等（緊急例外，呼叫端先處理）。
    """
    if actual == target:
        return None  # 不離開目前位置
    if probe.dream_in_flight and actual == PRESENCE_TARGET_HOST:
        return R_DREAM_IN_FLIGHT  # 在主機做夢中被叫：先做完這一輪（X9）
    if probe.has_uninterruptible_work:
        return R_UNINTERRUPTIBLE_WORK
    if probe.interaction_active and slot.interrupt_timing != InterruptTiming.FREE:
        return R_INTERACTION_ACTIVE
    return None


def _is_work(slot: PlanSlot) -> bool:
    return slot.availability == "available" and slot.source == PlanSource.ITEM and slot.activity_kind == ActivityKind.WORK


def arbitrate_request(
    request: ArbiterRequest, *, slot: PlanSlot, presence: PresenceReading, override: OverrideView | None,
    probe: InterruptFacts, now: datetime, same_band_policy: SameBandPolicy = same_band_prevails,
    policy: PermissionPolicy = DEFAULT_POLICY,
) -> ArbitrationDecision:
    """owner／admin／member／system 的在場請求（含「回主機」target=host）。

    授權來自請求者階級 × 指示等級 × 工作分級 × 格上 min_rank，依 ``policy`` 對照表消費（權限契約 §3.3、§3.4、§4.1）。
    """
    band = priority_of(request)
    if request.actor_authenticated is not True:
        return _reject(R_UNAUTHENTICATED)  # 未認證者不建立請求（只留言／中性回條）
    if not request.emergency and request.tier < PRESENCE_REQUEST_MIN_TIER:
        return _reject(R_TIER_BELOW_FLOOR, band, blocked_axis="tier", required=int(PRESENCE_REQUEST_MIN_TIER),
                       actual=int(request.tier), policy_revision=policy.revision)  # 防線：認證但階級不足移轉門檻
    if request.emergency and request.authority != "system":
        return _reject(R_EMERGENCY_REQUIRES_SYSTEM)
    actual = actual_location(presence)
    leave = request.target == PRESENCE_TARGET_HOST
    if override is not None and override.expires_at <= now:
        override = None  # 資料列還沒被 reconcile，但已過期：不算生效
    has_override = override is not None or slot.source == PlanSource.OVERRIDE
    emergency = request.emergency

    # ---- 緊急：覆寫一切（含不可中斷工作、移轉中），只形成 override，不放棄工作項 ----
    if emergency:
        if leave:
            return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, band, "supersede" if has_override else "none",
                                       PRESENCE_TARGET_HOST)
        return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, band, "supersede" if has_override else "begin",
                                   None if actual == request.target else request.target, request.target)

    # ---- 受信任的系統指令（§1.2a、§3.4 名詞約定）：非緊急 system 只有登記表內且可提移轉的系統元件能提出移轉 ----
    if request.authority == "system":
        component = system_component(request.requester_ref)
        if component is None or not component.may_propose_transfer:
            return _reject(R_TIER_INSUFFICIENT, band, blocked_axis="tier", actual=int(request.tier),
                           policy_revision=policy.revision)

    # ---- 授權類判定先於 DEFERRED／QUEUED：沒有資格的請求不該被排隊或等待 ----
    abandon = False
    held_priority = PRIORITY_PLAN
    if has_override:
        held_priority = override.priority if override is not None and override.priority is not None else PRIORITY_OWNER
        if leave:
            if held_priority >= PRIORITY_EMERGENCY:
                if request.authority != "system":
                    # 保守做法（待 Bernie 確認）：緊急 override 只有 system 能結束，owner 的「回主機」也不行
                    return _reject(R_EMERGENCY_OVERRIDE_ACTIVE, band, blocked_axis="tier",
                                   required=PRIORITY_EMERGENCY, actual=int(request.tier),
                                   policy_revision=policy.revision)
            else:
                # 結束 override（§3.5）：需 TEMP_SESSION 權限，且階級不低於 override 建立者的帶。
                # 「同一請求者」條款需要 override 記錄 requester_ref，目前沒有，未實作（回報待裁決）
                denied = _denial(request, band, [
                    _Check("instruction", policy.required_tier(InstructionLevel.TEMP_SESSION), R_NOT_OVERRIDE_OWNER),
                    _Check("tier", held_priority, R_NOT_OVERRIDE_OWNER)], policy)
                if denied is not None:
                    return denied
        elif band < held_priority:
            return _reject(R_LOWER_THAN_OVERRIDE, band, blocked_axis="tier", required=held_priority,
                           actual=int(request.tier), policy_revision=policy.revision)
    else:
        if _is_work(slot):
            # 工作格規則（§3.3、§3.4）：「回主機」＝放棄工作（SESSION_INTERRUPT，只有 owner 且須明示放棄）；
            # 叫她去別處是臨時指示（形成 override），門檻＝三者取最大（格上 min_rank、work_grade、TEMP_SESSION），
            # 通過後交給後續安全點／狀態判定；系統緊急在上方已處理。
            abandon_ok = request.explicit_abandon and policy.authorize(request.tier, InstructionLevel.SESSION_INTERRUPT)
            if abandon_ok:
                if not leave:
                    return _reject(R_ABANDON_REQUIRES_HOST, band)
                abandon = True
            elif leave or request.explicit_abandon:
                return _reject(R_WORK_IN_PROGRESS, band, blocked_axis="instruction",
                               required=int(policy.required_tier(InstructionLevel.SESSION_INTERRUPT)),
                               actual=int(request.tier), policy_revision=policy.revision)
        if not abandon:
            denied = _denial(request, band, _displacement_checks(slot, policy), policy)
            if denied is not None:
                return denied

    # ---- 目前狀態類：Presence 不明、移轉中、同級排隊 ----
    if actual is None:
        return _defer(R_PRESENCE_RECOVERING, band)
    if presence.transfer_active:
        return ArbitrationDecision("QUEUED", R_TRANSFER_IN_PROGRESS, band)  # 進行中的移轉不中斷

    if has_override:
        if leave:  # 回主機＝owner 顯式結束該 override，之後走 return_to_plan 一般仲裁
            return ArbitrationDecision("ACCEPTED_TRANSFER", R_OVERRIDE_ENDED, band, "end",
                                       override_end_reason="owner_ended", return_to_plan=True)
        if band == held_priority and not (override is not None and same_band_policy(request, override)):
            return ArbitrationDecision("QUEUED", R_SAME_PRIORITY_QUEUED, band)
        blocked = _safe_point_block(slot, probe, actual, request.target)
        if blocked is not None:
            return _defer(blocked, band, blocked_axis="timing")
        return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, band, "supersede",
                                   None if actual == request.target else request.target, request.target)

    # ---- 沒有 override ----
    if leave:
        # 放棄工作（明示）或非工作格的「回主機」（外推 §9.1 (c)：視為放棄當前行程項）→ mark(abandon)。
        # 先算 mark 再判斷 already_at_target：她已在主機、但行程格還在，仍要放棄該格（投影轉 REST）
        mark = abandon or (slot.source == PlanSource.ITEM and slot.item_ref is not None
                           and slot.slot_state != "abandoned"
                           and slot.location != PRESENCE_TARGET_HOST)  # 本來就在主機的格：放棄沒有意義
        if actual == PRESENCE_TARGET_HOST:
            if mark:
                return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, band, "mark_abandon", None)
            return _reject(R_ALREADY_AT_TARGET, band)
        blocked = _safe_point_block(slot, probe, actual, request.target)
        if blocked is not None:
            return _defer(blocked, band, blocked_axis="timing")
        return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, band, "mark_abandon" if mark else "none",
                                   PRESENCE_TARGET_HOST)
    if actual == request.target and slot.location == request.target:
        return _reject(R_ALREADY_AT_TARGET, band)  # 人已在目標、行程也在目標：不需要 override
    blocked = _safe_point_block(slot, probe, actual, request.target)
    if blocked is not None:
        return _defer(blocked, band, blocked_axis="timing")
    return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, band, "begin",
                               None if actual == request.target else request.target, request.target)


def abandon_mark_for(slot: PlanSlot, request_id: str, now: datetime) -> SlotMark | None:
    """``override_action="mark_abandon"`` 的判決要交給 ``SlotMarkWriter.begin_mark`` 的內容（純函式）。

    只有「行程項格」（source=ITEM，有藍圖起點）可標記；``cause_request_id`` 取請求 id（冪等）。
    """
    if slot.source != PlanSource.ITEM or slot.item_ref is None or slot.blueprint_start_at is None:
        return None
    return SlotMark(kind="abandon", item_ref=slot.item_ref, planned_start_at=slot.blueprint_start_at, started_at=now,
                    cause_request_id=request_id)


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
        return _defer(blocked, PRIORITY_PLAN, blocked_axis="timing")
    return ArbitrationDecision("ACCEPTED_TRANSFER", R_ACCEPTED, PRIORITY_PLAN, "none", directive.target)
