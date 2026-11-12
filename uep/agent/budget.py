"""AgentLoop 的預算與無進展偵測（Agent 架構提案 §4.4、AL-2）。

- ``LoopBudget`` 是 Policy 參數（數值非契約，由呼叫端依 ``policy_ref`` 解析後傳入；目前沒有 Policy 服務，不在這裡發明預設政策）。
- ``BudgetCounters`` 由 AgentLoop 計數、進 checkpoint（``budget_counters``），跨 resume 保持；不信任模型自述（AL-2）。
- 牆鐘以「累計經過秒數」計：每段執行結束把本段秒數併入 ``elapsed_s``，resume 後從該值續算。
- 無進展：同一 ``(tool, 參數指紋)`` 連續重複 N 次，或連續 ``rejected`` N 次。``decision.consult`` 的同結果偵測屬 D2。
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from uep.agent.trigger import BudgetCap

ExhaustedDimension = Literal["steps", "tool_calls", "tokens", "run_deadline"]
NO_PROGRESS_REPEAT = "repeated_call"
NO_PROGRESS_REJECTED = "repeated_rejection"
STATUS_APPROVAL_PENDING = "approval_pending"  # park 中的待核准項：對連續被拒計數中性（不重置、不累加），由 resume 的結果決定


@dataclass(frozen=True)
class BudgetThresholds:
    """三段門檻（相對預設值 D 的倍率）：warn 只記錄並提示模型收斂、notice 送一次固定系統訊息、abort 為中斷點（ADR-C4）。
    預設 ``(1.0, 1.0, 1.0)`` ＝未啟用三段門檻，到上限直接 finalize／``FAILED(budget_exhausted)``（與今日行為相同）。"""

    warn: float = 1.0
    notice: float = 1.0
    abort: float = 1.0

    def __post_init__(self) -> None:
        if not 0 < self.warn <= self.notice <= self.abort:
            raise ValueError("門檻必須滿足 0 < warn <= notice <= abort")

    @property
    def enabled(self) -> bool:
        return self != BudgetThresholds()


@dataclass(frozen=True)
class LoopBudget:
    max_steps: int = 12
    max_tool_calls: int = 24
    max_consults: int = 0  # decision.consult 的子限（D2 才使用）
    max_tokens: int | None = None
    run_deadline_s: float | None = None  # 累計牆鐘上限（秒）
    step_timeout_s: float | None = None  # 單一 Step 的上限（只取消卡住的 Step，SV-2）
    max_batch: int = 8  # 同一則模型訊息內處理的 tool_intent 上限（§6.4-1）
    max_parallel: int = 1  # 同一波並行度；預設 1（序列）。>1 需 tasks.supports_parallel_execution（D1b 起真實 TaskManager 為 True）
    repeat_limit: int = 3
    rejected_limit: int = 3
    # 連續無進展達此次數就不再投出 consult（與 max_consults 誰先到就照誰）。None ⇒ 由 Run 的失敗閾值導出（見
    # ``consult_no_progress_limit``），確保 consult 在 Run 因無進展失敗「之前」就被收掉
    consult_no_progress_n: int | None = None
    # 主動諮詢（CN-8）：剩餘步數 <= 此值（且 >= 2，不吃掉最後一步）時問中樞 loop_end；None ⇒ 關閉。觸發條件屬 Policy，不寫死在迴圈
    consult_loop_end_margin: int | None = None
    finalize_on_exhausted: bool = True  # 耗盡時先以 tools=[] 要求收束；False 直接 FAILED(budget_exhausted)
    heartbeat_interval_s: float = 5.0
    thresholds: BudgetThresholds = field(default_factory=BudgetThresholds)
    max_cost: float | None = None  # 保留維度（尚無成本計量）

    def __post_init__(self) -> None:
        if self.thresholds.enabled and self.max_tool_calls < math.ceil(self.max_steps * self.thresholds.abort):
            # 工具次數是保險絲，不得早於輪數的中斷點，否則三段門檻來不及走完
            raise ValueError("max_tool_calls 不可小於輪數中斷點（max_steps × abort）")
        for name in ("max_steps", "max_tool_calls", "max_batch", "max_parallel", "repeat_limit", "rejected_limit"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 必須 >= 1")
        if self.max_consults < 0:
            raise ValueError("max_consults 不可為負")
        if self.consult_loop_end_margin is not None and self.consult_loop_end_margin < 2:
            raise ValueError("consult_loop_end_margin 必須 >= 2（主動諮詢本身要用掉一步，剩餘步數須 >= 2）")
        if self.consult_no_progress_n is not None:
            if self.consult_no_progress_n < 1:
                raise ValueError("consult_no_progress_n 必須 >= 1")
            if self.consult_no_progress_n > self._consult_no_progress_ceiling():
                raise ValueError("consult_no_progress_n 必須嚴格小於 Run 的無進展失敗閾值，否則 consult 來不及被收掉")

    def _consult_no_progress_ceiling(self) -> int:
        """consult 的無進展計數（重複次數不含第一次、連續被拒次數）可容許的上限：嚴格小於 Run 失敗的計數（repeat_limit-1／rejected_limit），下限 1。"""
        return max(min(self.repeat_limit - 1, self.rejected_limit) - 1, 1)

    @property
    def consult_no_progress_limit(self) -> int:
        n = self.consult_no_progress_n
        return n if n is not None else self._consult_no_progress_ceiling()


@dataclass
class BudgetCounters:
    steps: int = 0
    tool_calls: int = 0
    consults: int = 0
    tokens: int = 0
    elapsed_s: float = 0.0
    last_fingerprint: str = ""
    repeat_count: int = 0
    rejected_streak: int = 0

    def to_checkpoint(self) -> dict[str, Any]:
        """只含數字與 16 字元指紋（符合 checkpoint 的值層級白名單）。"""
        return {
            "steps": self.steps, "tool_calls": self.tool_calls, "consults": self.consults, "tokens": self.tokens,
            "elapsed_s": round(self.elapsed_s, 3), "last_fp": self.last_fingerprint or "none",
            "repeat": self.repeat_count, "rejected": self.rejected_streak,
        }

    @classmethod
    def from_checkpoint(cls, data: Mapping[str, Any] | None) -> BudgetCounters:
        if not data:
            return cls()
        fp = str(data.get("last_fp", ""))
        return cls(
            steps=int(data.get("steps", 0)), tool_calls=int(data.get("tool_calls", 0)),
            consults=int(data.get("consults", 0)), tokens=int(data.get("tokens", 0)),
            elapsed_s=float(data.get("elapsed_s", 0.0)), last_fingerprint="" if fp == "none" else fp,
            repeat_count=int(data.get("repeat", 0)), rejected_streak=int(data.get("rejected", 0)),
        )


def exhausted(budget: LoopBudget, counters: BudgetCounters, *, elapsed_s: float) -> ExhaustedDimension | None:
    """Step 邊界的預算檢查；``elapsed_s`` 是含本段執行的累計牆鐘。回傳第一個耗盡的維度，沒耗盡回 None。"""
    if counters.steps >= budget.max_steps:
        return "steps"
    if counters.tool_calls >= budget.max_tool_calls:
        return "tool_calls"
    if budget.max_tokens is not None and counters.tokens >= budget.max_tokens:
        return "tokens"
    if budget.run_deadline_s is not None and elapsed_s >= budget.run_deadline_s:
        return "run_deadline"
    return None


def fingerprint(tool: str, args: Mapping[str, Any]) -> str:
    """``(tool, 參數)`` 的短指紋，只用來比對「是不是同一個呼叫」；不可序列化的參數退為型別名稱。"""
    raw = json.dumps([tool, args], sort_keys=True, ensure_ascii=False, default=lambda o: type(o).__name__)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def observe_call(
    budget: LoopBudget, counters: BudgetCounters, tool: str, args: Mapping[str, Any], status: str,
) -> str | None:
    """記錄一次已處理的工具呼叫（依處理順序）並判斷無進展。回傳 ``NO_PROGRESS_*`` 原因碼，未觸發回 None。

    status 為 ``rejected`` 者計入「連續被拒」；``approval_pending`` 保持原值；其餘狀態重置該計數。相同指紋連續出現就累計（不論成敗）。"""
    fp = fingerprint(tool, args)
    if fp == counters.last_fingerprint:
        counters.repeat_count += 1
    else:
        counters.last_fingerprint, counters.repeat_count = fp, 1
    if status == "rejected":
        counters.rejected_streak += 1
    elif status != STATUS_APPROVAL_PENDING:
        counters.rejected_streak = 0
    if counters.repeat_count >= budget.repeat_limit:
        return NO_PROGRESS_REPEAT
    if counters.rejected_streak >= budget.rejected_limit:
        return NO_PROGRESS_REJECTED
    return None


def observe_resumed(budget: LoopBudget, counters: BudgetCounters, status: str) -> str | None:
    """resume 時 park 項的結果（指紋在 park 時已計過，這裡只更新連續被拒）：``rejected``（人類否決）累加，其餘重置。
    達 ``rejected_limit`` 回 ``NO_PROGRESS_REJECTED``。"""
    counters.rejected_streak = counters.rejected_streak + 1 if status == "rejected" else 0
    return NO_PROGRESS_REJECTED if counters.rejected_streak >= budget.rejected_limit else None


# ───────────────────────── 三段門檻與預算解析（Chain 契約 §2.3；ADR-C4） ─────────────────────────

def hard_budget(budget: LoopBudget) -> LoopBudget:
    """中斷點（abort）當作硬上限的預算：``exhausted()`` 用它判斷。分段維度（steps／tokens）乘上 abort 倍率；
    未啟用三段門檻時原樣回傳（行為與先前相同）。"""
    th = budget.thresholds
    if not th.enabled:
        return budget
    return replace(
        budget, max_steps=_point(budget.max_steps, th.abort),
        max_tokens=None if budget.max_tokens is None else _point(budget.max_tokens, th.abort),
        thresholds=BudgetThresholds(),
    )


def _point(base: int, ratio: float) -> int:
    """門檻點（步數或 token 數）：``ceil(D × 倍率)``。階段判斷與中斷點（``hard_budget``）共用同一個算法。"""
    return math.ceil(base * ratio)


def budget_stage(budget: LoopBudget, counters: BudgetCounters) -> int:
    """Step 邊界的階段判斷：0 未達、1 warn、2 notice、3 abort；未啟用三段門檻回 0。
    各維度（steps、tokens）逐一與 ``ceil(D × 倍率)`` 比較，取最高階段；abort 點與 ``hard_budget`` 完全一致。"""
    th = budget.thresholds
    if not th.enabled:
        return 0
    stage = 0
    for used, base in ((counters.steps, budget.max_steps), (counters.tokens, budget.max_tokens)):
        if base is None:
            continue
        for level, ratio in ((3, th.abort), (2, th.notice), (1, th.warn)):
            if used >= _point(base, ratio):
                stage = max(stage, level)
                break
    return stage


class BudgetPolicyTable:
    """``(policy_ref, route, tier) -> LoopBudget`` 的政策表（目前沒有 Policy 服務，由組裝層注入）。查不到回 None。"""

    def __init__(self, rows: Mapping[tuple[str, str, str], LoopBudget] | None = None) -> None:
        self._rows = dict(rows or {})

    def __len__(self) -> int:
        return len(self._rows)

    def lookup(self, policy_ref: str, route: str, tier: str) -> LoopBudget | None:
        return self._rows.get((policy_ref, route, tier))


def _min_opt(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    return a if b is None else min(a, b)


def _with_hard_limits(policy: LoopBudget, hard_steps: int, hard_tokens: int | None, tool_calls: int) -> LoopBudget:
    """把「中斷點硬上限」套回預算：D 與門檻由硬上限反推（``floor(硬上限 / abort)``）。D 推不出正整數就放棄分段
    （門檻回預設、D 直接等於硬上限），使中斷點仍不超過給定的硬上限。工具次數保險絲低於輪數中斷點時同樣縮小 D。"""
    th = policy.thresholds
    base = hard_budget(policy)
    steps_d, tokens_d = hard_steps, hard_tokens
    if th.enabled:
        steps_d = policy.max_steps if hard_steps == base.max_steps else math.floor(hard_steps / th.abort)
        if hard_tokens is None:
            tokens_d = None
        elif base.max_tokens is not None and hard_tokens == base.max_tokens:
            tokens_d = policy.max_tokens
        else:
            tokens_d = math.floor(hard_tokens / th.abort)
        if tool_calls < _point(steps_d, th.abort):
            steps_d = math.floor(tool_calls / th.abort)
        if steps_d < 1 or (tokens_d is not None and tokens_d < 1):
            th, steps_d, tokens_d = BudgetThresholds(), min(hard_steps, tool_calls), hard_tokens
    else:
        steps_d = hard_steps
    return replace(policy, max_steps=steps_d, max_tokens=tokens_d, thresholds=th, max_tool_calls=tool_calls)


def resolve_budget(
    table: BudgetPolicyTable | None, default: LoopBudget, *, policy_ref: str, route: str, tier: str | None,
    cap: BudgetCap | None, metrics: Counter[str] | None = None,
) -> LoopBudget:
    """政策值（查不到退回 ``default``）∩ cap，只收窄（BG-1）。cap 綁在中斷點：逐維度取
    ``min(政策的硬上限, cap)``，再反推 D 與門檻（三段門檻等比縮小、順序不變），使實際用量不超過 cap。
    政策表是空的（尚未配置任何列）時不計 ``budget.policy_unresolved``。"""
    policy = table.lookup(policy_ref, route, tier or "standard") if table is not None else None
    if policy is None:
        policy = default
        if table is not None and len(table) and metrics is not None:
            metrics["budget.policy_unresolved"] += 1
    if cap is None:
        return policy
    hard = hard_budget(policy)
    hard_steps = hard.max_steps if cap.max_steps is None else min(hard.max_steps, cap.max_steps)
    hard_tokens = _min_opt(hard.max_tokens, cap.max_tokens)
    tool_calls = hard.max_tool_calls if cap.max_tool_calls is None else min(hard.max_tool_calls, cap.max_tool_calls)
    widened = (
        (cap.max_steps is not None and cap.max_steps > hard.max_steps)
        or (cap.max_tool_calls is not None and cap.max_tool_calls > hard.max_tool_calls)
        or (cap.max_tokens is not None and hard.max_tokens is not None and cap.max_tokens > hard.max_tokens)
        or (cap.run_deadline_s is not None and policy.run_deadline_s is not None and cap.run_deadline_s > policy.run_deadline_s)
        or (cap.max_cost is not None and policy.max_cost is not None and cap.max_cost > policy.max_cost)
    )
    if widened and metrics is not None:
        metrics["budget.cap_clamped"] += 1  # 試圖放寬的維度被忽略（逐維度取 min），不報錯
    out = _with_hard_limits(policy, hard_steps, hard_tokens, tool_calls)
    return replace(
        out, run_deadline_s=_min_opt(policy.run_deadline_s, cap.run_deadline_s),
        max_cost=_min_opt(policy.max_cost, cap.max_cost),
    )


def tighten(a: LoopBudget, b: LoopBudget) -> LoopBudget:
    """兩份預算逐維度比較**中斷點硬上限**（不是 D）取較緊者（resume：凍結值與重新解析值，只收窄）；
    D 與門檻由較緊的硬上限以 ``a`` 的門檻反推。"""
    ha, hb = hard_budget(a), hard_budget(b)
    out = _with_hard_limits(
        a, min(ha.max_steps, hb.max_steps), _min_opt(ha.max_tokens, hb.max_tokens), min(ha.max_tool_calls, hb.max_tool_calls),
    )
    return replace(
        out, run_deadline_s=_min_opt(a.run_deadline_s, b.run_deadline_s), max_cost=_min_opt(a.max_cost, b.max_cost),
    )
