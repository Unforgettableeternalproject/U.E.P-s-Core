"""FakeRuntime：實作 ``uep.adapters.runtime.Runtime`` 協定的可腳本化替身。

腳本是 ``ScriptItem`` 序列（用 ``text``／``tool_intent``／``result`` 等 helper 建立），
由 ``stream`` 依序轉成 RuntimeEvent（seq 由 fake 指派）。

R-2 行為：``cancel`` 之後
- ack=``stopped``：Runtime 真的停止，後續腳本項目不再產生。
- ack=``isolated``：腳本繼續跑（模擬無法立即停止）。``late_policy`` 決定晚到事件的去向：
  * ``emit``（預設，較貼近真實）：晚到事件照常由 ``stream`` 送出，由 Core 負責丟棄（T-15 驗證）；
    件數記在 ``emitted_late[attempt_id]``。
  * ``drop``：fake 自行丟棄 text_delta／result（計入 ``discarded``）與 tool_intent
    （計入 ``dropped_tool_intents``），其他 kind 計入 ``ignored_after_cancel``。
- ``receipt`` 項目（已發生的工具副作用）在兩種策略下都保留於 ``receipts[attempt_id]``，
  且只記 ``{action_id, receipt_id}`` 引用，不依賴 CC-6 型別。

工具結果回饋（D61 選項 A）：``tool_intent(..., await_result=True)`` 之後，``stream`` 在消費者索取下一個事件時
若尚未收到 ``submit_tool_result`` 就暫停（結束這次迭代，attempt 維持 running，不算完成）；收到後再次
迭代 ``stream(attempt_id)``（或同一個產生器繼續）即從暫停處續跑。沒標記的 tool_intent 照舊不暫停。
收到的內容記在 ``tool_results[attempt_id]``（依序的 ``(action_id, result)``）。``result`` 須帶 ``items``、
``status``，可帶布林 ``degraded``／``partial``，不得含 ``excluded_summary``（D61）。

注入點：``cancel_ack``、``late_policy``、``script``／``script_for``、crash 點 ``runtime.before_event``。
計數：``start_calls``、``resume_calls``、``cancel_calls``、``status_calls``。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from uep.adapters.runtime import CancelAck, RuntimeEvent, RuntimeRequest, RuntimeStatus
from uep.fakes.crash import CrashInjector

_LATE_DISCARD = {"text_delta", "result"}
LatePolicy = Literal["emit", "drop"]


@dataclass(frozen=True)
class ScriptItem:
    kind: str  # RuntimeEvent.kind 或 "receipt"
    value: Any
    await_result: bool = False  # 只對 tool_intent：送出後暫停到 submit_tool_result


def text(delta: str) -> ScriptItem:
    return ScriptItem("text_delta", delta)


def tool_intent(
    tool: str, args: Mapping[str, Any] | None = None, *, args_complete: bool = True,
    side_effect_hint: str = "internal_read", await_result: bool = False,
) -> ScriptItem:
    return ScriptItem("tool_intent", {
        "tool": tool, "args_complete": args_complete, "args": dict(args or {}),
        "side_effect_hint": side_effect_hint,
    }, await_result=await_result)


def approval_request(action_hash: str, reason: str = "") -> ScriptItem:
    return ScriptItem("approval_request", {"action_hash": action_hash, "reason": reason})


def checkpoint(content: Mapping[str, Any]) -> ScriptItem:
    return ScriptItem("checkpoint", dict(content))


def result(status: str = "ok", output_ref: str | None = None, usage: Mapping[str, Any] | None = None) -> ScriptItem:
    return ScriptItem("result", {"status": status, "output_ref": output_ref, "usage": dict(usage or {})})


def error(code: str, *, retryable: bool = False, detail: str = "") -> ScriptItem:
    return ScriptItem("error", {"code": code, "retryable": retryable, "detail": detail})


def receipt(action_id: str, receipt_id: str) -> ScriptItem:
    """已發生的工具副作用 receipt 引用（不是 RuntimeEvent；cancel 後仍保留）。"""
    return ScriptItem("receipt", {"action_id": action_id, "receipt_id": receipt_id})


class FakeRuntime:
    def __init__(
        self, *, cancel_ack: CancelAck = "stopped", late_policy: LatePolicy = "emit",
        crash: CrashInjector | None = None,
    ) -> None:
        self.cancel_ack: CancelAck = cancel_ack
        self.late_policy: LatePolicy = late_policy
        self.crash = crash or CrashInjector()
        self.default_script: list[ScriptItem] = []
        self._scripts: dict[str, list[ScriptItem]] = {}
        self._pending: dict[str, list[ScriptItem]] = {}
        self._cancelled: dict[str, CancelAck] = {}
        self._last_seq: Counter[str] = Counter()
        self._finished: set[str] = set()
        self.requests: dict[str, RuntimeRequest] = {}
        self.start_calls = 0
        self.resume_calls = 0
        self.cancel_calls: list[tuple[str, str]] = []
        self.status_calls = 0
        self.discarded: Counter[str] = Counter()  # drop 策略：cancel 後被丟棄的 text_delta／result 件數
        self.dropped_tool_intents: Counter[str] = Counter()  # drop 策略：cancel 後不執行的 tool_intent
        self.ignored_after_cancel: Counter[str] = Counter()  # drop 策略：其他 kind
        self.emitted_late: Counter[str] = Counter()  # emit 策略：cancel 後仍送出的事件件數
        self.receipts: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.tool_results: dict[str, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)  # submit_tool_result 收到的內容
        self._awaiting: set[str] = set()  # 已送出 await_result 的 tool_intent、等結果中
        self._resumable: set[str] = set()  # 等待中的 attempt 已收到結果

    # ---- 注入 ----
    def script(self, items: Sequence[ScriptItem]) -> None:
        """設定之後新 attempt 的預設腳本。"""
        self.default_script = list(items)

    def script_for(self, attempt_id: str, items: Sequence[ScriptItem]) -> None:
        self._scripts[attempt_id] = list(items)

    # ---- Runtime 協定 ----
    def start(self, request: RuntimeRequest) -> str:
        self.start_calls += 1
        return self._begin(request)

    def resume(self, request: RuntimeRequest) -> str:
        if not request.checkpoint_ref:
            raise ValueError("resume 必須帶 checkpoint_ref（R-5）")
        self.resume_calls += 1
        return self._begin(request)

    def _begin(self, request: RuntimeRequest) -> str:
        if request.attempt_id in self.requests:
            raise ValueError(f"attempt_id 重複：{request.attempt_id}（fallback／重試須新建 attempt_id，R-4）")
        self.requests[request.attempt_id] = request
        self._pending[request.attempt_id] = list(self._scripts.get(request.attempt_id, self.default_script))
        return request.attempt_id

    def stream(self, attempt_id: str) -> Iterator[RuntimeEvent]:
        pending = self._pending[attempt_id]
        while True:
            if attempt_id in self._awaiting:
                if attempt_id not in self._resumable:
                    return  # 暫停：等 submit_tool_result 後再迭代 stream 續跑
                self._awaiting.discard(attempt_id)
                self._resumable.discard(attempt_id)
            if not pending:
                break
            item = pending.pop(0)
            cancelled = self._cancelled.get(attempt_id)
            if cancelled == "stopped":
                pending.clear()
                return
            if item.kind == "receipt":
                self.receipts[attempt_id].append(item.value)  # 副作用不可假裝取消
                continue
            if cancelled == "isolated":
                if self.late_policy == "drop":
                    self._drop_late(attempt_id, item)
                    continue
                self.emitted_late[attempt_id] += 1
            self.crash.hit("runtime.before_event")
            self._last_seq[attempt_id] += 1
            if item.await_result:
                self._awaiting.add(attempt_id)  # 先標記再 yield：消費者在迴圈內就能 submit
            yield RuntimeEvent(
                attempt_id=attempt_id, seq=self._last_seq[attempt_id], kind=item.kind,  # type: ignore[arg-type]
                **{item.kind: item.value},
            )
        self._finished.add(attempt_id)

    def submit_tool_result(self, attempt_id: str, action_id: str, result: Mapping[str, Any]) -> None:
        if attempt_id not in self.requests:
            raise ValueError(f"未知的 attempt：{attempt_id}")
        if not isinstance(result.get("items"), (list, tuple)) or not isinstance(result.get("status"), str):
            raise TypeError("result 必須帶 items（清單）與 status（字串）")
        if "excluded_summary" in result:
            raise ValueError("result 不得含 excluded_summary（筆數與原因碼只進稽核，D61）")
        for flag in ("degraded", "partial"):
            if flag in result and not isinstance(result[flag], bool):
                raise ValueError(f"result.{flag} 必須是布林")
        self.tool_results[attempt_id].append((action_id, dict(result)))
        if attempt_id in self._awaiting:
            self._resumable.add(attempt_id)

    def _drop_late(self, attempt_id: str, item: ScriptItem) -> None:
        if item.kind in _LATE_DISCARD:
            self.discarded[attempt_id] += 1
        elif item.kind == "tool_intent":
            self.dropped_tool_intents[attempt_id] += 1
        else:
            self.ignored_after_cancel[attempt_id] += 1

    def cancel(self, attempt_id: str, reason: str) -> CancelAck:
        self.cancel_calls.append((attempt_id, reason))
        # 冪等（含未知 attempt）：第一次的 ack 為準
        return self._cancelled.setdefault(attempt_id, self.cancel_ack)

    def status(self, attempt_id: str) -> RuntimeStatus:
        self.status_calls += 1
        ack = self._cancelled.get(attempt_id)
        if ack == "isolated":
            state = "isolated"
        elif ack == "stopped":
            state = "cancelled"
        elif attempt_id not in self.requests:
            state = "unknown"
        elif attempt_id in self._finished:
            state = "completed"
        else:
            state = "running"
        return RuntimeStatus(attempt_id=attempt_id, state=state, last_seq=self._last_seq[attempt_id])
