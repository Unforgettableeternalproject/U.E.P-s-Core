"""決策中樞契約型別（提案 §3.2、§3.3）。葉節點：不 import ``uep`` 其他部分。

P0 的 ``DecisionPlan`` 只有 ``route`` 與 ``reply``（``reply.mode``、``interaction_to``）；其餘區段
（output、capability、provisioning、placement、model、loop、followups）P2 之後依提案 §3.3 命名擴充。
所有欄位是英文列舉／ID，使用者可見字串不進 plan。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Trigger = Literal["initial", "reeval", "event", "tick"]
EngineTier = Literal["rule", "feature_rule", "model", "llm", "fallback"]


@dataclass(frozen=True)
class DecisionRequest:
    """一次決策請求。``facts`` 為依 input_schema 的英文欄位；``native`` 為同行程原生物件。

    ``native`` 是 P0 偏離提案之處：``turn.reply`` 的 engine 吃 ``TurnCandidate``（含原文，行程內直接帶，
    ``repr=False`` 不進日誌）。契約改成跨行程前須以 ``text_refs``＋``facts`` 取代。
    """

    request_id: str
    kind: str
    trigger: Trigger = "initial"
    parent_decision_id: str | None = None
    plan_version: int | None = None
    subject: dict[str, str] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    text_refs: tuple[str, ...] = ()
    allowed_outcomes: tuple[str, ...] = ()
    snapshot_versions: dict[str, int] = field(default_factory=dict)
    deadline: float | None = None  # time.monotonic()；sync 決策點用
    native: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class DecisionReply:
    """``DecisionPlan.reply``：沿用 Proposal 語彙（respond／passive／pass／defer）。

    ``addressed`` 為 P0 為了與 ``Proposal`` 雙向無損轉換而加的欄位（提案 §3.3 未列）。
    """

    mode: str
    interaction_to: str | None = None
    addressed: bool = True


@dataclass(frozen=True)
class DecisionPlan:
    """提議的集合（不是命令）；所有區段選填。P0 只有 ``route`` 與 ``reply``。"""

    route: str | None = None  # direct_chat | agent_loop | task_request | ignore
    reply: DecisionReply | None = None


@dataclass(frozen=True)
class DecisionEngineInfo:
    tier: EngineTier
    id: str
    version: str


@dataclass(frozen=True)
class DecisionResult:
    decision_id: str
    request_id: str
    kind: str
    outcome: str
    plan: DecisionPlan | None
    confidence: float
    reason_code: str  # 安全字元集，不含原文
    engine: DecisionEngineInfo
    fallback: bool = False  # 逾時／busy／例外／驗證失敗
    plan_version: int = 1
    supersedes: str | None = None
