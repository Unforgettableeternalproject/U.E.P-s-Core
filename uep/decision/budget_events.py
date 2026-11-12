"""``agent.budget`` 決策點：把 AgentLoop 的預算門檻事件寫進 decision_log（Chain 契約 §2.3.1、ADR-C4；publish 模式）。

- 預算事件不是中樞決策，只是別處已定案的事實；沿用 ``policy_publish`` 的 publish 模式：engine 只驗證並記錄，不重算任何值。
- sub_kind：``budget_warn``（1.0 倍）、``budget_notice``（1.5 倍）、``task_budget_exceeded``（2.0 倍中斷點）。
- 只記事實：request_id 內含 run_id、reason_code 為門檻倍率代碼；不含任何文字內容。
  decision_log 目前沒有 payload 欄位，計數器數值與硬上限留在 logger（``budget_stage`` 事件），不寫表。
- 本模組不 import ``uep.agent``／``uep.echo``：由組裝層把 ``BudgetEventRecorder`` 以鴨子型別注入 launcher。
"""
from __future__ import annotations

import logging

from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionPlan,
    DecisionRequest,
    DecisionResult,
)
from uep.decision.hub import DecisionHub
from uep.decision.registry import DecisionPointSpec
from uep.ids import new_id

logger = logging.getLogger("uep.decision.budget_events")

KIND_AGENT_BUDGET = "agent.budget"
OUTCOME_RECORDED = "recorded"
ON_INVALID_REASON = "budget_event_invalid"
# 迴圈回呼的 kind -> (日誌 sub_kind, 門檻倍率代碼)
SUB_KIND_BY_STAGE: dict[str, tuple[str, str]] = {
    "warn": ("budget_warn", "threshold_1_0x"),
    "notice": ("budget_notice", "threshold_1_5x"),
    "abort": ("task_budget_exceeded", "threshold_2_0x"),
}
_REASON_BY_SUB_KIND = {sub: reason for sub, reason in SUB_KIND_BY_STAGE.values()}
_ENGINE_ID = "budget_events"
_ENGINE_VERSION = "dl-budget1"


def agent_budget_spec() -> DecisionPointSpec:
    return DecisionPointSpec(
        kind=KIND_AGENT_BUDGET, mode="publish", owner="agent", outcomes=(OUTCOME_RECORDED,),
        budget_ms=None, on_invalid_reason=ON_INVALID_REASON, allow_native=False, shadow=False,
    )


class AgentBudgetEngine:
    """記錄預算門檻事件；facts 不合法時回 fallback（invalid），不拋例外。"""

    def decide(self, request: DecisionRequest) -> DecisionResult:
        facts = request.facts
        sub_kind = facts.get("sub_kind")
        valid = sub_kind in _REASON_BY_SUB_KIND and isinstance(facts.get("run_id"), str) and bool(facts["run_id"])
        if not valid:
            return DecisionResult(
                decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome="invalid",
                plan=DecisionPlan(), confidence=0.0, reason_code=ON_INVALID_REASON,
                engine=DecisionEngineInfo(tier="fallback", id="core.fallback", version="d80"), fallback=True,
            )
        return DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome=OUTCOME_RECORDED,
            plan=DecisionPlan(), confidence=1.0, reason_code=_REASON_BY_SUB_KIND[sub_kind],
            engine=DecisionEngineInfo(tier="rule", id=_ENGINE_ID, version=_ENGINE_VERSION),
        )


class BudgetEventRecorder:
    """可呼叫物件 ``(run_id, task_id, stage)``：stage 為迴圈的 ``warn``／``notice``／``abort``。"""

    def __init__(self, hub: DecisionHub) -> None:
        self._hub = hub

    def __call__(self, run_id: str, task_id: str | None, stage: str) -> None:
        mapped = SUB_KIND_BY_STAGE.get(stage)
        if mapped is None:
            return
        sub_kind = mapped[0]
        try:
            self._hub.publish(KIND_AGENT_BUDGET, DecisionRequest(
                request_id=f"{run_id}:budget:{sub_kind}", kind=KIND_AGENT_BUDGET, trigger="event",
                facts={"sub_kind": sub_kind, "run_id": run_id, "task_id": task_id},
            ))
        except Exception:
            logger.exception("預算事件記錄失敗：run=%s stage=%s", run_id, stage)


def register_budget_events(hub: DecisionHub) -> BudgetEventRecorder:
    """登記 ``agent.budget`` 並回傳記錄器；日誌掛的是 hub 觀察者，與 ``log.attach`` 先後無關。"""
    hub.register(agent_budget_spec(), AgentBudgetEngine())
    return BudgetEventRecorder(hub)
