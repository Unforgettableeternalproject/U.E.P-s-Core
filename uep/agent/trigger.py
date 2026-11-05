"""``LoopTrigger``：迴圈的唯一啟動輸入（Agent 架構提案 §4.2、§10.2；AL-1）。

迴圈只由 ``LoopTrigger`` 啟動；Runtime、模型輸出、工具結果都不能啟動新 Run 或新 Task。
``capability_tags`` 是決策層 I-1 已收窄的結果，在 Run 內凍結（AL-3）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TriggerKind = Literal["user_turn", "task_request", "external_event", "proactive", "resume"]
TRIGGER_KINDS: tuple[str, ...] = ("user_turn", "task_request", "external_event", "proactive", "resume")


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

    def __post_init__(self) -> None:
        if self.kind not in TRIGGER_KINDS:
            raise ValueError(f"未知的 LoopTrigger.kind：{self.kind!r}")
        if isinstance(self.capability_tags, str):
            raise TypeError("capability_tags 必須是標籤集合，不是單一字串")
