"""FakeToolExecutor：實作 ``uep.adapters.tool.ToolExecutor`` 協定的可腳本化替身。

- 以 ``executions`` 記錄每個 action_id 的「實際執行」次數（與呼叫次數 ``calls`` 分開）。
- idempotency 為 native／via_action_id 時，同 action_id 重呼叫回既有 receipt、不重複執行；
  idempotency=none 則真的再執行一次（可據此驗證 Core 不得盲目重跑）。
- ``lookup_authoritative``：宣告查核紀錄是否權威；未宣告權威時，沒紀錄只能回 undetermined。
- 注入點：
  * ``execute_behavior(action_id, mode)``：``ok`` / ``fail``（receipt succeeded=False）/
    ``lose_after_exec``（執行並記帳後拋 ToolResponseLost）/ ``lose_before_exec``（未執行就失聯）。
  * ``lookup_outcome(action_id, outcome)``：強制 ``found`` / ``confirmed_not_executed`` / ``undetermined``；
    對已執行的 action 強制後兩者即「已執行但查核遺失紀錄」（誤報），用以驗證 idempotency=none 不重做。
  * ``unverified_receipt(action_id)``：該 action 的 receipt 以 source_verified=False 簽回。
  * crash 點：``tool.before_execute``、``tool.after_execute``（已執行記帳、尚未回傳）。
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from uep.adapters.tool import ReceiptLookup, ToolCall, ToolReceipt, ToolResponseLost
from uep.fakes.crash import CrashInjector
from uep.ids import new_id

ExecuteMode = Literal["ok", "fail", "lose_after_exec", "lose_before_exec"]
LookupResult = Literal["found", "confirmed_not_executed", "undetermined"]


class FakeToolExecutor:
    def __init__(self, crash: CrashInjector | None = None, *, lookup_authoritative: bool = True) -> None:
        self.crash = crash or CrashInjector()
        self._authoritative = lookup_authoritative
        self.calls: list[ToolCall] = []
        self.executions: Counter[str] = Counter()
        self.lookups: Counter[str] = Counter()
        self._receipts: dict[str, ToolReceipt] = {}
        self._execute_modes: dict[str, ExecuteMode] = {}
        self._lookup_outcomes: dict[str, LookupResult] = {}
        self._unverified: set[str] = set()

    @property
    def lookup_authoritative(self) -> bool:
        return self._authoritative

    @lookup_authoritative.setter
    def lookup_authoritative(self, value: bool) -> None:
        self._authoritative = value

    # ---- 注入 ----
    def execute_behavior(self, action_id: str, mode: ExecuteMode) -> None:
        self._execute_modes[action_id] = mode

    def lookup_outcome(self, action_id: str, outcome: LookupResult) -> None:
        self._lookup_outcomes[action_id] = outcome

    def unverified_receipt(self, action_id: str) -> None:
        self._unverified.add(action_id)

    # ---- 查詢 ----
    def execution_count(self, action_id: str) -> int:
        return self.executions[action_id]

    def call_count(self, action_id: str) -> int:
        return sum(1 for c in self.calls if c.action_id == action_id)

    # ---- ToolExecutor 協定 ----
    def execute(self, call: ToolCall) -> ToolReceipt:
        self.calls.append(call)
        mode = self._execute_modes.get(call.action_id, "ok")
        if mode == "lose_before_exec":
            raise ToolResponseLost(f"{call.action_id}: 未執行就失聯")
        existing = self._receipts.get(call.action_id)
        if existing is not None and call.idempotency in ("native", "via_action_id"):
            return existing
        self.crash.hit("tool.before_execute")
        self.executions[call.action_id] += 1
        receipt = ToolReceipt(
            receipt_id=new_id("rcpt"),
            action_id=call.action_id,
            succeeded=mode != "fail",
            result_ref=None if mode == "fail" else f"blob_fake_{call.action_id}",
            recorded_at=datetime.now(UTC),
            source_verified=call.action_id not in self._unverified,
            error_code="FAKE_FAILURE" if mode == "fail" else None,
        )
        self._receipts[call.action_id] = receipt
        self.crash.hit("tool.after_execute")
        if mode == "lose_after_exec":
            raise ToolResponseLost(f"{call.action_id}: 已執行但回覆遺失")
        return receipt

    def lookup_receipt(self, action_id: str) -> ReceiptLookup:
        self.lookups[action_id] += 1
        forced = self._lookup_outcomes.get(action_id)
        receipt = self._receipts.get(action_id)
        if forced == "undetermined":
            return ReceiptLookup("undetermined")
        if forced == "confirmed_not_executed":
            return ReceiptLookup("confirmed_not_executed")
        if forced == "found" and receipt is None:
            raise ValueError(f"{action_id} 尚未執行，無 receipt 可回")
        if receipt is not None:
            return ReceiptLookup("found", receipt)
        return ReceiptLookup("confirmed_not_executed" if self._authoritative else "undetermined")
