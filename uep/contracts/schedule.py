"""PresencePlan 行程契約型別（PP-0；零依賴，只含純資料與 Protocol）。

依據：hidden_docs/架構重構/PresencePlan與Behavior契約草案.md §4、§5.2、§8。
- 「藍圖」＝行程項（PlanItem，原始意向，不被改寫）；「投影」＝套用 overlay 與 delay 後的有效時間軸（PlanSlot）。
- 行程是意向不是授權：本模組不含任何 Presence 寫入面。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from typing import Literal, Protocol

PlanLocation = str  # PlatformId（desktop|discord|website）或 "host"
PRESENCE_TARGET_HOST = "host"
PLAN_LOCATIONS = frozenset({"desktop", "discord", "website", PRESENCE_TARGET_HOST})

# 優先序帶：仲裁器擁有數值；本契約只要求 PLAN 嚴格最低
PRIORITY_PLAN = 0  # 建議：PLAN=0 < SYSTEM=100 < OWNER=200 < EMERGENCY=300

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
    next_boundary_at: datetime | None
    plan_revision: int
    override_state: Literal["pending", "active"] | None = None
    delay_s: int = 0  # 投影相對藍圖的順延秒數（延長的目前格＝終點延後；被順延的下一格＝起點延後）


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
