"""AgentLoop 的預算與無進展偵測（Agent 架構提案 §4.4、AL-2）。

- ``LoopBudget`` 是 Policy 參數（數值非契約，由呼叫端依 ``policy_ref`` 解析後傳入；目前沒有 Policy 服務，不在這裡發明預設政策）。
- ``BudgetCounters`` 由 AgentLoop 計數、進 checkpoint（``budget_counters``），跨 resume 保持；不信任模型自述（AL-2）。
- 牆鐘以「累計經過秒數」計：每段執行結束把本段秒數併入 ``elapsed_s``，resume 後從該值續算。
- 無進展：同一 ``(tool, 參數指紋)`` 連續重複 N 次，或連續 ``rejected`` N 次。``decision.consult`` 的同結果偵測屬 D2。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

ExhaustedDimension = Literal["steps", "tool_calls", "tokens", "run_deadline"]
NO_PROGRESS_REPEAT = "repeated_call"
NO_PROGRESS_REJECTED = "repeated_rejection"


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

    def __post_init__(self) -> None:
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

    status 為 ``rejected`` 者計入「連續被拒」；其餘狀態重置該計數。相同指紋連續出現就累計（不論成敗）。"""
    fp = fingerprint(tool, args)
    if fp == counters.last_fingerprint:
        counters.repeat_count += 1
    else:
        counters.last_fingerprint, counters.repeat_count = fp, 1
    counters.rejected_streak = counters.rejected_streak + 1 if status == "rejected" else 0
    if counters.repeat_count >= budget.repeat_limit:
        return NO_PROGRESS_REPEAT
    if counters.rejected_streak >= budget.rejected_limit:
        return NO_PROGRESS_REJECTED
    return None
