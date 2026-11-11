"""測試用工具組的 ``ToolExecutor``（``UEP_TOOL_TESTKIT``；宣告見 ``uep.tools.catalog.testkit``）。

- 全部在行程內執行，沒有 I/O、沒有外部效果。結果文字放進回合範圍的 ``TurnOutcomeCache``（D33；與記憶工具共用同一個實例，
  loop 的 ``content_lookup`` 才讀得到），receipt 只帶 ``result_ref`` 與 ``meta["status"]``。
- ``test_sleep`` 實作 ``cancel``（D69、RA-5）：用每個 action 一個 ``threading.Event`` 喚醒睡眠，被取消的 ``execute`` 簽回
  失敗 receipt（``error_code="cancelled"``，屬 loop 的 ``CANCEL_ERROR_CODES``），不拋例外。``cancel`` 先於 ``execute`` 到達時
  也記下，之後的 ``execute`` 立即以取消收尾。回傳 ``stopped`` 表示該 action 不會再睡下去（對未知或已結束的 action 同樣是 ``stopped``）。
- ``lookup_authoritative=False``：結果暫存只在記憶體，沒有 receipt 時只能回 ``undetermined``。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from uep.adapters.tool import CancelOutcome, ReceiptLookup, ToolCall, ToolReceipt
from uep.ids import new_id
from uep.tools.catalog.testkit import (
    ECHO_TEXT_MAX,
    SLEEP_MAX_S,
    TOOL_APPROVAL_ECHO,
    TOOL_CLOCK,
    TOOL_ECHO,
    TOOL_SLEEP,
)
from uep.tools.outcomes import TurnOutcomeCache

logger = logging.getLogger("uep.tools.executors.testkit")

CANCELLED = "cancelled"
APPROVAL_NOTE = "測試工具，沒有任何外部效果。"
_MAX_PENDING_CANCELS = 256  # 先於 execute 到達的 cancel 紀錄上限（超過就丟最舊的，只影響極端的測試競態）


def _bad_args(tool: str) -> dict[str, Any]:
    return {"success": False, "result": f"Invalid arguments for {tool!r}.", "code": "failed"}


class TestkitToolExecutor:
    """滿足 ``uep.adapters.tool.CancellableToolExecutor``。"""

    __test__ = False  # 名稱以 Test 開頭，避免被 pytest 當成測試類別收集

    def __init__(
        self, *, cache: TurnOutcomeCache | None = None, clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.cache = cache if cache is not None else TurnOutcomeCache()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._sleeping: dict[str, threading.Event] = {}  # 進行中的 test_sleep：action_id -> 取消旗標
        self._pending_cancels: dict[str, None] = {}  # 先於 execute 到達的 cancel（有序，供淘汰最舊）
        self.executions: list[str] = []  # 真正執行過的 action_id（測試與診斷用；重放不重複記）

    @property
    def lookup_authoritative(self) -> bool:
        return False

    def lookup_receipt(self, action_id: str) -> ReceiptLookup:
        receipt = self.cache.get_receipt(action_id)
        return ReceiptLookup("found", receipt) if receipt is not None else ReceiptLookup("undetermined")

    def cancel(self, action_id: str) -> CancelOutcome:
        with self._lock:
            event = self._sleeping.get(action_id)
            if event is None:
                self._pending_cancels[action_id] = None
                while len(self._pending_cancels) > _MAX_PENDING_CANCELS:
                    self._pending_cancels.pop(next(iter(self._pending_cancels)))
            else:
                event.set()
        return "stopped"

    def execute(self, call: ToolCall) -> ToolReceipt:
        existing = self.cache.get_receipt(call.action_id)
        if existing is not None and call.idempotency in ("native", "via_action_id"):
            return existing
        params = call.params if isinstance(call.params, Mapping) else {}
        if call.tool == TOOL_SLEEP:
            outcome, status = self._sleep(call.action_id, params)
        else:
            outcome = self._run(call.tool, params)
            status = "executed" if outcome.get("success") else "failed"
        code = outcome.pop("code", None)
        self.cache.put(call.action_id, outcome)
        self.executions.append(call.action_id)
        succeeded = bool(outcome.get("success"))
        receipt = ToolReceipt(
            receipt_id=new_id("rcpt"), action_id=call.action_id, succeeded=succeeded,
            result_ref=f"blob_{call.action_id}", recorded_at=datetime.now(UTC), source_verified=True,
            error_code=None if succeeded else str(code or status), meta={"status": status},
        )
        self.cache.put_receipt(call.action_id, receipt)
        return receipt

    # ---- 內部 ----

    @staticmethod
    def _json(payload: Mapping[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _run(self, tool: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if tool == TOOL_ECHO:
            text = params.get("text")
            if not isinstance(text, str) or len(text) > ECHO_TEXT_MAX:
                return _bad_args(tool)
            return {"success": True, "result": self._json({"echo": text, "length": len(text)})}
        if tool == TOOL_CLOCK:
            now = self._clock()
            local = now.astimezone()
            return {"success": True, "result": self._json({
                "utc": now.astimezone(UTC).isoformat(), "local": local.isoformat(), "timezone": str(local.tzname() or ""),
            })}
        if tool == TOOL_APPROVAL_ECHO:
            message = params.get("message")
            if not isinstance(message, str) or len(message) > ECHO_TEXT_MAX:
                return _bad_args(tool)
            return {"success": True, "result": self._json({"echo": message, "note": APPROVAL_NOTE})}
        return {"success": False, "result": f"Tool {tool!r} is not available.", "code": "denied"}

    def _sleep(self, action_id: str, params: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        seconds = params.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= SLEEP_MAX_S:
            return _bad_args(TOOL_SLEEP), "failed"
        requested = float(seconds)
        event = threading.Event()
        with self._lock:
            if action_id in self._pending_cancels:  # cancel 先到：不睡，直接以取消收尾
                del self._pending_cancels[action_id]
                event.set()
            self._sleeping[action_id] = event
        started = time.monotonic()
        try:
            cancelled = event.wait(requested) if not event.is_set() else True
        finally:
            with self._lock:
                self._sleeping.pop(action_id, None)
        slept = round(time.monotonic() - started, 3)
        if cancelled:
            return {"success": False, "result": "cancelled", "code": CANCELLED}, CANCELLED
        return {"success": True, "result": self._json({
            "status": "completed", "requested_seconds": requested, "slept_seconds": slept,
        })}, "executed"
