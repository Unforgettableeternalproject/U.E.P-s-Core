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

PlanLocation = str  # PlatformId（desktop|discord|website）或 "host"
PRESENCE_TARGET_HOST = "host"
PLAN_LOCATIONS = frozenset({"desktop", "discord", "website", PRESENCE_TARGET_HOST})

# 優先序帶：仲裁器擁有數值；本契約只要求 PLAN 嚴格最低
PRIORITY_PLAN = 0  # 建議：PLAN 0 < REQUESTER 50 < SYSTEM 100 < OWNER 200 < EMERGENCY 300
PRIORITY_REQUESTER = 50  # 已認證＋已登記的一般請求者（authority="member"，屬仲裁器階段）
PRIORITY_SYSTEM = 100
PRIORITY_OWNER = 200
PRIORITY_EMERGENCY = 300

# Behavior 模式值（BehaviorState 的字串值；REST 由 PP-3 加入 coordinator）
MODE_REST = "REST"
MODE_SLEEP = "SLEEP"
MODE_CHAT = "CHAT"
MODE_WORK = "WORK"
MODE_MISCHIEF = "MISCHIEF"


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
    FREE = "free"
    SAFE_POINT = "safe_point"
    OWNER_ONLY = "owner_only"


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
    interrupt_policy: InterruptPolicy | None = None  # None＝依活動類別預設
    suggested_behaviors: tuple[str, ...] | None = None
    created_at: datetime | None = None


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
    interrupt_policy: InterruptPolicy
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
class AbandonResult:
    status: Literal["ACCEPTED", "IGNORED"]
    reason_code: str  # ACCEPTED＝accepted；IGNORED＝not_owner／stale_round／not_in_work
    mark: SlotMark | None = None  # ACCEPTED 時：交給 SlotMarkWriter.begin_mark 的內容（cause_request_id＝signal_id）
