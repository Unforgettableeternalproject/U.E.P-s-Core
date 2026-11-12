"""``LoopTrigger``：迴圈的唯一啟動輸入（Agent 架構提案 §4.2、§10.2；AL-1）。

迴圈只由 ``LoopTrigger`` 啟動；Runtime、模型輸出、工具結果都不能啟動新 Run 或新 Task。
``capability_tags`` 是決策層 I-1 已收窄的結果，在 Run 內凍結（AL-3）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TriggerKind = Literal["user_turn", "task_request", "external_event", "proactive", "resume"]
TRIGGER_KINDS: tuple[str, ...] = ("user_turn", "task_request", "external_event", "proactive", "resume")
# Run 的處理剖面（預算與中斷政策的查表鍵）；direct_chat／ignore 不產生 LoopTrigger，型別上建構不出來（BG-3）
RunRoute = Literal["agent_loop", "task_request"]
RUN_ROUTES: tuple[str, ...] = ("agent_loop", "task_request")
BUDGET_TIERS: tuple[str, ...] = ("lite", "standard", "extended")  # 閉集名稱，不含數值


@dataclass(frozen=True)
class BudgetCap:
    """套用者要求的上限（只能收窄；每維度 None = 不額外限制）。綁在中斷點（abort 門檻）：Run 最多只能用到這個量。"""

    max_steps: int | None = None
    max_tool_calls: int | None = None
    max_tokens: int | None = None
    run_deadline_s: float | None = None
    max_cost: float | None = None  # 保留維度

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_tool_calls", "max_tokens", "run_deadline_s", "max_cost"):
            value = getattr(self, name)
            if value is not None and not value > 0:
                raise ValueError(f"BudgetCap.{name} 必須是正數")


@dataclass(frozen=True)
class BudgetRequest:
    tier: str | None = None  # None = 該 route 的 "standard"
    cap: BudgetCap | None = None

    def __post_init__(self) -> None:
        if self.tier is not None and self.tier not in BUDGET_TIERS:
            raise ValueError(f"未知的預算 tier：{self.tier!r}")


@dataclass(frozen=True)
class LoopTrigger:
    kind: TriggerKind
    origin_ref: str  # 來源引用（ticket／候選／指派 id）；外部文字不進 goal
    goal_code: str  # 語意短碼，非自由文字
    capability_tags: frozenset[str]  # I-1 的結果，Run 內凍結（AL-3）
    scope_ref: str
    owner_actor_id: str  # 非 owner 來源固定為艾斯維爾（D64）
    policy_ref: str  # 解析出預算與中斷政策
    snapshot_ref: str
    plan_ref: str | None = None  # 對應的 DecisionPlan 引用（審計用）；resume 沿用原值
    route: RunRoute = "agent_loop"  # 新欄位排在既有欄位之後且有預設（位置參數建構不受影響）
    budget: BudgetRequest | None = None

    def __post_init__(self) -> None:
        if self.kind not in TRIGGER_KINDS:
            raise ValueError(f"未知的 LoopTrigger.kind：{self.kind!r}")
        if isinstance(self.capability_tags, str):
            raise TypeError("capability_tags 必須是標籤集合，不是單一字串")
        if self.route not in RUN_ROUTES:
            raise ValueError(f"未知的 LoopTrigger.route：{self.route!r}")
        if (self.kind == "user_turn" and self.route != "agent_loop") or (
            self.kind == "task_request" and self.route != "task_request"
        ):
            raise ValueError(f"kind={self.kind!r} 與 route={self.route!r} 不相容")
