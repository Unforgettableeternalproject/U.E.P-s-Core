"""PresencePlan 行程契約型別（PP-0；零依賴，只含純資料與 Protocol）。

依據：hidden_docs/架構重構/PresencePlan與Behavior契約草案.md §4、§5.2、§8。
- 「藍圖」＝行程項（PlanItem，原始意向，不被改寫）；「投影」＝套用 overlay 與 delay 後的有效時間軸（PlanSlot）。
- 行程是意向不是授權：本模組不含任何 Presence 寫入面。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from typing import Literal, Protocol

from uep.contracts.permission import (
    InstructionLevel,
    RequesterTier,
    check_authority_tier,
    derive_tier,
)

PlanLocation = str  # PlatformId（desktop|discord|website）或 "host"
PRESENCE_TARGET_HOST = "host"
PLAN_LOCATIONS = frozenset({"desktop", "discord", "website", PRESENCE_TARGET_HOST})
# 在場請求（Laya／事件）可指定的移轉目標：等於 PLAN_LOCATIONS。console 不在其中——測試 Console 只存在於測試模式，
# 永遠不是行程可指派、也不是請求可移轉的位置
REQUEST_TARGET_LOCATIONS = PLAN_LOCATIONS

# 優先序帶：仲裁器擁有數值；本契約只要求 PLAN 嚴格最低
PRIORITY_PLAN = 0  # 建議：PLAN 0 < REQUESTER 50 < SYSTEM 100 < OWNER 200 < EMERGENCY 300；PLAN 不是階級
# 其餘數值與名稱保留，對應到請求者階級（權限契約 §1.1）
PRIORITY_REQUESTER = int(RequesterTier.MEMBER)  # 已認證＋已登記的一般請求者（authority="member"，屬仲裁器階段）
PRIORITY_SYSTEM = int(RequesterTier.SYSTEM)
PRIORITY_OWNER = int(RequesterTier.OWNER)
PRIORITY_EMERGENCY = int(RequesterTier.EMERGENCY)

# Behavior 模式值（BehaviorState 的字串值；REST 由 PP-3 加入 coordinator）
MODE_REST = "REST"
MODE_SLEEP = "SLEEP"
MODE_CHAT = "CHAT"
MODE_WORK = "WORK"
MODE_MISCHIEF = "MISCHIEF"

# muted 拆兩組（房內 #304）：主動行為（Proactive）在休息族都靜音；回應（續跑回覆等）只在 SLEEP 靜音
PROACTIVE_MUTED_MODES = frozenset({MODE_REST, MODE_SLEEP})
RESPONSE_MUTED_MODES = frozenset({MODE_SLEEP})


class ActivityKind(StrEnum):
    REST = "rest"  # 休息（含夜間休息時段與非工作時間的主機待命；不綁日夜）
    WORK = "work"
    SOCIAL = "social"


class PlanSource(StrEnum):
    DEFAULT = "default"  # 預設層：host + rest
    REST_WINDOW = "rest_window"  # role=rest_window 的行程項（固定時窗，不順延）
    ITEM = "item"  # 一般行程項
    OVERRIDE = "override"  # 臨時覆蓋


class InterruptPolicy(StrEnum):
    """舊的行程中斷政策（權限契約 §3.6 起由 ``min_rank`` 與 ``InterruptTiming`` 取代；只保留作相容輸入與舊欄位讀取）。"""

    FREE = "free"
    SAFE_POINT = "safe_point"
    OWNER_ONLY = "owner_only"


class InterruptTiming(StrEnum):
    """何時可打斷（與「誰能叫走她」的 ``min_rank`` 互相獨立；§3.6）。"""

    FREE = "free"  # 不因進行中的互動等待
    SAFE_POINT = "safe_point"  # 等安全點（互動／不可中斷工作／做夢照 _safe_point_block）


# 格上 min_rank 允許的階級：只能抬高、不得低於移轉門檻（PERM-9）；緊急不是可排程的門檻
MIN_RANK_CHOICES = frozenset({RequesterTier.MEMBER, RequesterTier.ADMIN, RequesterTier.SYSTEM, RequesterTier.OWNER})


def default_interrupt_timing(kind: ActivityKind) -> InterruptTiming:
    """依活動類別的預設打斷時機：SOCIAL 不等互動，其餘等安全點。"""
    return InterruptTiming.FREE if kind == ActivityKind.SOCIAL else InterruptTiming.SAFE_POINT


def split_interrupt_policy(policy: InterruptPolicy | str | None) -> tuple[RequesterTier | None, InterruptTiming | None]:
    """舊 ``interrupt_policy`` 的相容讀取（§3.6，單一實作，store／契約型別共用）。

    free → (None, FREE)；safe_point → (None, SAFE_POINT)；owner_only → (OWNER, FREE，刻意的行為變更：
    owner_only 格不再因進行中互動而等待)；None → (None, None)。未知值拋 ValueError。
    """
    if policy is None:
        return None, None
    value = InterruptPolicy(policy)
    if value == InterruptPolicy.OWNER_ONLY:
        return RequesterTier.OWNER, InterruptTiming.FREE
    return None, InterruptTiming(value.value)


def resolve_interrupt_fields(
    policy: InterruptPolicy | str | None, min_rank: RequesterTier | None, timing: InterruptTiming | str | None,
) -> tuple[RequesterTier | None, InterruptTiming | None]:
    """新欄位優先；兩個新欄位都沒給時才回退舊 ``interrupt_policy``。同時驗證型別與 min_rank 的允許值（fail-closed）。"""
    if min_rank is not None and (not isinstance(min_rank, RequesterTier) or min_rank not in MIN_RANK_CHOICES):
        raise ValueError(f"min_rank 只收 MEMBER／ADMIN／SYSTEM／OWNER：{min_rank!r}")
    if timing is not None:
        timing = InterruptTiming(timing)
    if min_rank is None and timing is None:
        return split_interrupt_policy(policy)
    return min_rank, timing


@dataclass(frozen=True)
class PlanItem:
    """藍圖：一筆 owner 安排的行程項（欄位對應 presence_plan_items；解析用，不含審計欄位）。"""

    item_id: str
    activity_kind: ActivityKind
    location: PlanLocation | None  # None＝未指定地點（解析為 host＋rest）
    recurrence: Literal["once", "weekly"]
    role: Literal["rest_window", "user"] = "user"
    once_start_at: datetime | None = None
    once_end_at: datetime | None = None
    weekdays: tuple[int, ...] = ()  # ISO 1..7
    local_start: time | None = None
    local_end: time | None = None  # end<=start 視為跨午夜
    tz: str | None = None  # IANA；weekly 必填
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    priority: int = 50  # 僅用於行程內部重疊決勝
    flexibility: Literal["fixed", "deferrable", "flexible"] = "fixed"
    interrupt_policy: InterruptPolicy | None = None  # 舊欄位（相容輸入）：兩個新欄位都沒給時才讀，見 resolve_interrupt_fields
    suggested_behaviors: tuple[str, ...] | None = None
    created_at: datetime | None = None
    min_rank: RequesterTier | None = None  # 誰能叫走她；只能抬高（§3.6）；None＝無抬高
    interrupt_timing: InterruptTiming | None = None  # None＝依活動類別預設（SOCIAL→FREE，其餘 SAFE_POINT）

    def __post_init__(self) -> None:
        min_rank, timing = resolve_interrupt_fields(self.interrupt_policy, self.min_rank, self.interrupt_timing)
        object.__setattr__(self, "min_rank", min_rank)
        object.__setattr__(self, "interrupt_timing", timing)


@dataclass(frozen=True)
class OverrideView:
    """臨時覆蓋（overlay）：不改寫底下的行程項；生命期上限＝建立當下輪次。"""

    override_id: str
    location: PlanLocation
    activity_kind: ActivityKind
    started_at: datetime
    expires_at: datetime
    round_end_at: datetime
    status: Literal["pending", "active"] = "active"
    cause_request_id: str | None = None
    authority: str | None = None
    priority: int | None = None

    def __post_init__(self) -> None:
        if self.location == PRESENCE_TARGET_HOST:  # 回主機不是臨時指示（PP-17）
            raise ValueError("override.location 不得為 host")
        # 對應 DB CHECK：expires_at <= round_end_at（PP-7、PP-15）
        if self.expires_at > self.round_end_at:
            raise ValueError("override.expires_at 不得晚於 round_end_at")
        if self.expires_at <= self.started_at:
            raise ValueError("override.expires_at 必須晚於 started_at")


@dataclass(frozen=True)
class PlanDelay:
    """投影輸入：目前行程格「實際仍在進行／延長到 held_until」（Q-PP-4）。

    以 (item_ref, planned_start_at) 指向藍圖中的那一格（planned_start_at 為藍圖起點，不隨投影改變）。
    只存在於投影、只在當前輪次內（held_until 會被封頂在該格所屬輪次邊界）。
    """

    item_ref: str
    planned_start_at: datetime
    held_until: datetime


SlotState = Literal["normal", "paused", "abandoned"]


@dataclass(frozen=True)
class SlotMark:
    """投影用的標記（§6.4）：暫停或放棄某一次工作出現；以 (item_ref, planned_start_at) 定址。

    abandon：該次出現自 ``started_at`` 起轉 host＋REST（藍圖未改，只到該次出現終點或輪次結束）。
    pause：留在原平台待命（建議 [CHAT]），不改位置、不延長。藍圖永不改寫。
    """

    kind: Literal["pause", "abandon"]
    item_ref: str
    planned_start_at: datetime  # 藍圖起點，與 PlanSlot.blueprint_start_at 對應
    started_at: datetime
    mark_id: str | None = None
    cause_request_id: str | None = None


@dataclass(frozen=True)
class BehaviorSuggestion:
    mode: str  # BehaviorState 值
    rank: int  # 0 最優先
    reason_code: str


@dataclass(frozen=True)
class PlanSlot:
    availability: Literal["available", "unavailable"]  # unavailable ＝ 行程不可用（X1；R-PP-1）
    location: PlanLocation  # unavailable 時恆為 "host"
    activity_kind: ActivityKind
    source: PlanSource
    item_ref: str | None  # item_id / override_id；DEFAULT 為 None
    interrupt_timing: InterruptTiming | InterruptPolicy  # 位置參數保留；舊 InterruptPolicy 值於建構時相容轉換（§3.6）
    suggestions: tuple[BehaviorSuggestion, ...]
    starts_at: datetime  # 本格起點（投影後的實際起點，UTC）
    ends_at: datetime | None  # 本格終點（投影）；None＝開放
    next_boundary_at: datetime | None  # None 只表示「掃描窗內沒有」（resolve 掃描前方 48 小時），不保證永不改變
    plan_revision: int
    override_state: Literal["pending", "active"] | None = None
    delay_s: int = 0  # 投影相對藍圖的順延秒數（延長的目前格＝終點延後；被順延的下一格＝起點延後）
    blueprint_start_at: datetime | None = None  # 行程項格的藍圖起點（delay anchor 用）；非行程項格為 None
    standby: bool = False  # 待命：REST 格（含 fallback REST）與工作暫停中為 True；SOCIAL、休息時段（SLEEP）為 False
    slot_state: SlotState = "normal"  # paused＝工作暫停（留在原平台待命）；abandoned＝fallback REST（藍圖未改）
    reason_code: str | None = None  # unavailable 時的固定原因碼（如 tz_unavailable）
    tz: str | None = None  # 行程項自己的時區（weekly 項）；once 項、預設層、override 為 None
    min_rank: RequesterTier | None = None  # 誰能叫走她（投影值，由行程項帶入）；None＝無抬高

    def __post_init__(self) -> None:
        legacy_rank, timing = resolve_interrupt_fields(
            self.interrupt_timing if isinstance(self.interrupt_timing, InterruptPolicy) else None, self.min_rank, None)
        if isinstance(self.interrupt_timing, InterruptPolicy):  # 舊值：owner_only → (OWNER, FREE)
            object.__setattr__(self, "min_rank", legacy_rank)
            object.__setattr__(self, "interrupt_timing", timing)
        else:
            object.__setattr__(self, "interrupt_timing", InterruptTiming(self.interrupt_timing))


@dataclass(frozen=True)
class SlotNotice:
    """進入／離開行程格的表示接點（PP-0 只偵測，不發事件、不渲染；事件屬 PP-4、輸出意圖屬 PP-3）。"""

    kind: Literal["entered", "left"]
    slot: PlanSlot
    reason: str  # boundary／override_started／override_ended／paused／resumed／abandoned／unavailable／available


@dataclass(frozen=True)
class PlanDirective:
    """Planner → 仲裁器；不是 PresenceRequest，沒有 authority。"""

    directive_id: str
    target: PlanLocation
    reason: Literal["plan_boundary", "return_to_plan", "plan_changed", "startup_reconcile"]
    slot: PlanSlot
    not_before: datetime
    expires_at: datetime  # 過期即丟，不重播


class PlanReader(Protocol):
    def resolve(self, at: datetime | None = None) -> PlanSlot: ...
    def upcoming(self, start: datetime, horizon_s: int) -> tuple[PlanSlot, ...]: ...
    def revision(self) -> int: ...


class OverrideWriter(Protocol):  # 僅仲裁器持有
    def begin_override(self, *, cause_request_id: str, authority: str, priority: int,
                       location: PlanLocation, activity_kind: ActivityKind,
                       expires_at: datetime) -> str: ...

    def activate_override(self, override_id: str) -> bool: ...

    def end_override(self, override_id: str, reason: str) -> bool: ...

    def active_override(self) -> OverrideView | None: ...


class SlotMarkWriter(Protocol):  # 仲裁器／PlanWriter 持有；只定介面
    def begin_mark(self, *, kind: Literal["pause", "abandon"], item_ref: str, planned_start_at: datetime,
                   cause_request_id: str) -> str: ...  # 回 mark_id；同 cause 冪等

    def end_mark(self, mark_id: str, reason: str) -> bool: ...

    def active_marks(self) -> tuple[SlotMark, ...]: ...


# ---- 行程控制訊號 schedule.abandon_work（暫名；§9.5）----

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9_.:\-]{1,116}$")  # CommitmentChangeSignal 的 utterance 後綴；加 "commitment:" 前綴後仍在 128 內
_ACTOR_REF = re.compile(r"^[a-z0-9_.:-]{1,128}$")
_BLOB_REF = re.compile(r"^[a-z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class AbandonWorkSignal:
    """owner 放棄當前工作階段的話語層事實。不含目標項／輪次：消費端依收到當下的投影綁定。"""

    signal_id: str  # 冪等鍵，由來源 utterance id 衍生
    actor_ref: str  # 平台認證 ID 形式，不收顯示名稱；不含 is_owner（消費端自行計算）
    actor_authenticated: bool
    source_platform: str  # 只描述來源，不代表她在場
    path: Literal["turn", "absent"]  # 稽核用，不影響授權
    decided_at: datetime
    decider_id: str
    decider_version: str
    reason_ref: str | None = None  # blob 引用，不存原文
    confidence: float | None = None  # 僅資訊，不影響授權

    def __post_init__(self) -> None:  # fail-closed
        for name in ("signal_id", "decider_id", "decider_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ValueError(f"{name} 不符安全字元集")
        if not isinstance(self.actor_ref, str) or _ACTOR_REF.fullmatch(self.actor_ref) is None:
            raise ValueError("actor_ref 必須是平台認證 ID 形式")
        if not isinstance(self.source_platform, str) or not self.source_platform:
            raise ValueError("source_platform 不可為空")
        if self.path not in ("turn", "absent"):
            raise ValueError("path 必須是 turn 或 absent")
        if self.decided_at.tzinfo is None:
            raise ValueError("decided_at 必須帶時區")
        if self.reason_ref is not None and (
                not isinstance(self.reason_ref, str) or _BLOB_REF.fullmatch(self.reason_ref) is None):
            raise ValueError("reason_ref 必須是 blob 引用（不得存放自由文字）")
        if self.confidence is not None and not (
                isinstance(self.confidence, (int, float)) and math.isfinite(self.confidence)
                and 0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence 必須在 [0, 1]")


@dataclass(frozen=True)
class CommitmentChangeSignal:
    """owner 要求變更「她答應的事」的話語層事實（v1 只有取消）。不含原文、不含 is_owner（消費端自行計算）。"""

    signal_id: str  # 冪等鍵，格式 commitment:<utterance_id>
    actor_ref: str  # 平台認證 ID 形式，不收顯示名稱
    kind: Literal["cancel"]  # v1 固定為 cancel；改時間留待後續版本
    source_platform: str  # 只描述來源，不代表她在場
    path: Literal["turn", "absent"]  # 稽核用，不影響授權
    decided_at: datetime
    decider_id: str
    decider_version: str
    reason_code: str  # 固定原因碼，不存自由文字

    def __post_init__(self) -> None:  # fail-closed
        for name in ("signal_id", "decider_id", "decider_version", "reason_code"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ValueError(f"{name} 不符安全字元集")
        if not self.signal_id.startswith("commitment:"):
            raise ValueError("signal_id 必須是 commitment:<utterance_id>")
        if _SAFE_REF.fullmatch(self.signal_id[len("commitment:"):]) is None:  # 後綴不可為空，且須符合安全字元集
            raise ValueError("signal_id 的 utterance 後綴不合法")
        if not isinstance(self.actor_ref, str) or _ACTOR_REF.fullmatch(self.actor_ref) is None:
            raise ValueError("actor_ref 必須是平台認證 ID 形式")
        if self.kind != "cancel":
            raise ValueError("kind 目前只支援 cancel")
        if not isinstance(self.source_platform, str) or not self.source_platform:
            raise ValueError("source_platform 不可為空")
        if self.path not in ("turn", "absent"):
            raise ValueError("path 必須是 turn 或 absent")
        if not isinstance(self.decided_at, datetime) or self.decided_at.tzinfo is None:
            raise ValueError("decided_at 必須是帶時區的 datetime")


@dataclass(frozen=True)
class AbandonResult:
    status: Literal["ACCEPTED", "IGNORED"]
    reason_code: str  # ACCEPTED＝accepted；IGNORED＝not_owner／stale_round／not_abandonable
    mark: SlotMark | None = None  # ACCEPTED 時：交給 SlotMarkWriter.begin_mark 的內容（cause_request_id＝signal_id）


# ---- 在場請求仲裁（AR-1；純資料，仲裁邏輯在 uep/schedule/arbiter.py）----

# 一般請求者 ``member``（已認證＋已登記）只在仲裁器內使用；``PresenceRequest`` 的既有建構路徑（authority 只收 owner／system）
# 不變。優先序帶由 Core 依 authority 計算，模型輸出不能影響（M-10）
PRIORITY_BY_AUTHORITY: dict[str, int] = {
    "member": PRIORITY_REQUESTER, "system": PRIORITY_SYSTEM, "owner": PRIORITY_OWNER}
ARBITER_AUTHORITIES = frozenset(PRIORITY_BY_AUTHORITY)

_REQUEST_ID_RE = re.compile(r"^\S{1,256}$")


@dataclass(frozen=True)
class ArbiterRequest:
    """仲裁器看到的在場請求（由已驗證的 ``PresenceRequest`` 或放棄訊號衍生；本身不授權）。

    ``emergency``＝系統緊急（只允許 authority=system）：優先序 EMERGENCY，可覆寫一切、只形成 override 不放棄工作項。
    ``explicit_abandon``＝伴隨已驗證的 owner 放棄訊號（``AbandonResult`` ACCEPTED 之後才設）。
    ``received_seq``＝Core 收到的順序（不信任端點時間戳；Q-PP-5 同級衝突用）。
    ``tier``＝請求者階級，預設由 authority／emergency 推導，建構時檢查與 authority 一致（權限契約 §1.3，fail-closed）；
    ``instruction``＝決策層歸類出的指示等級（None＝尚未歸類，PM-2 起由仲裁器依 target／slot 推導）；
    ``human_origin``＝是否為人類發起（強制睡眠專用，PM-6 才消費）。
    """

    request_id: str
    authority: str
    requester_ref: str
    actor_authenticated: bool
    target: PlanLocation
    received_seq: int = 0
    emergency: bool = False
    explicit_abandon: bool = False
    tier: RequesterTier | None = None
    instruction: InstructionLevel | None = None
    human_origin: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or _REQUEST_ID_RE.fullmatch(self.request_id) is None:
            raise ValueError(f"request_id 不合法：{self.request_id!r}")
        if self.authority not in ARBITER_AUTHORITIES:
            raise ValueError(f"authority 只收 owner／system／member：{self.authority!r}")
        if self.tier is None:
            object.__setattr__(self, "tier", derive_tier(self.authority, self.emergency))
        check_authority_tier(self.authority, self.tier, self.emergency)
        if self.instruction is not None and not isinstance(self.instruction, InstructionLevel):
            raise ValueError(f"instruction 必須是 InstructionLevel：{self.instruction!r}")
        if self.human_origin not in (True, False):
            raise ValueError("human_origin 必須是布林")
        if self.target not in REQUEST_TARGET_LOCATIONS:
            raise ValueError(f"target 不合法：{self.target!r}")
        if self.actor_authenticated not in (True, False):
            raise ValueError("actor_authenticated 必須是布林")


@dataclass(frozen=True)
class InterruptFacts:
    """``InterruptProbe`` 的執行期事實（行程只給 interrupt_policy 提示，事實由 probe 取得）。"""

    dream_in_flight: bool = False
    interaction_active: bool = False  # Interaction 不是 IDLE
    has_uninterruptible_work: bool = False


OverrideAction = Literal["none", "begin", "supersede", "end", "mark_abandon"]


@dataclass(frozen=True)
class ArbitrationDecision:
    """仲裁判決（純資料；執行端依此呼叫 OverrideWriter／SlotMarkWriter／Presence Manager，AR-1 不執行）。

    - ACCEPTED_TRANSFER：受理。``transfer_target`` 為要移轉的位置（None＝不需移轉）；``override_action`` 說明 overlay 動作：
      begin（建立）／supersede（結束舊的 superseded 並建立）／end（owner_ended，之後走 return_to_plan）／
      mark_abandon（先放棄當前行程項，再移轉到主機）／none。
    - QUEUED：排隊（移轉進行中、同級非 owner FIFO）；DEFERRED：等安全點／做夢一輪／Presence 恢復；
      REJECTED：拒絕（``reason_code`` 固定代碼）。
    """

    outcome: Literal["ACCEPTED_TRANSFER", "QUEUED", "DEFERRED", "REJECTED"]
    reason_code: str
    priority: int = PRIORITY_PLAN
    override_action: OverrideAction = "none"
    transfer_target: PlanLocation | None = None
    override_location: PlanLocation | None = None
    override_end_reason: str | None = None
    return_to_plan: bool = False
    # 仲裁審計（權限契約 §4.4）：只有授權／時機類的拒絕或延後才帶；不含私人內容，對請求者的回條仍只給中性文字（INV-07）
    blocked_axis: Literal["tier", "instruction", "grade", "tool", "timing", "state"] | None = None
    also_failed: tuple[str, ...] = ()  # 同時不足的其他軸（供調參數據）
    required: int | None = None  # 擋下那一軸需要的階級值（RequesterTier 值）
    actual: int | None = None  # 請求者實際階級值
    policy_revision: int | None = None  # 判定當下的對照表版本
