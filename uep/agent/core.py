"""Agent Core 最小迴圈（契約 §2.1～2.3、§4.2；SA-1～SA-4、TC-2）。

一個 Step 內：``Runtime.start`` -> 消費 ``stream`` -> 每個 ``tool_intent`` 經 Gateway 驗證、
``create_action``、``execute_action``，把結果組成 D61 內容 ``submit_tool_result`` -> 讀到 ``result`` 事件結束。
同一迴圈天然可處理多個 ``tool_intent``（步內序號遞增），本切片測試只覆蓋單一個。

- ``worker_epoch`` 只在這裡與 TaskManager 之間流動（SA-1）；``RuntimeRequest``／``RuntimeEvent`` 不帶。
- ``capability_set`` 由呼叫端放在 ``RuntimeRequest``，Agent Core 只讀取、不自己決定可見集合。
- ``side_effect_hint`` 只記 debug log，不參與判定（TG-2）。
- ``args_complete=false`` 不建 action、不補值（SA-3、TC-2）。
- 收到 ``StaleWorkerEpoch`` 立即 cancel 並結束（SA-2）。
- 回給 Runtime 的結果只有狀態短碼與項目引用；例外文字與參數內容不外送，log 只記型別或短碼。
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from uep.adapters.runtime import Runtime, RuntimeEvent, RuntimeRequest
from uep.tasks.manager import (
    ActionBlocked,
    ApprovalInvalid,
    ApprovalRequired,
    ParamsHashMismatch,
    PolicyViolation,
    PresenceRequired,
    StaleWorkerEpoch,
    TaskManager,
)
from uep.tools.gateway import GatewayRejected, ResultStatus, ToolGateway

logger = logging.getLogger(__name__)

StepEnd = Literal["completed", "runtime_error", "stale", "incomplete"]
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def stable_action_id(run_id: str, step_id: str, ordinal: int) -> str:
    """SA-4：由 ``run_id``＋``step_id``＋步內序號決定的 ``act_<26 字 Crockford>``（與 ``new_id`` 同格式）。

    同一 attempt 重放、或同步驟重跑，同一個 ``tool_intent`` 序號得到同一個 id。
    """
    digest = hashlib.sha256(f"{run_id}\x1f{step_id}\x1f{ordinal}".encode()).digest()
    number = int.from_bytes(digest[:16], "big")  # 128 位元：首字元 <= 7，與 ULID 同值域
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[number & 31])
        number >>= 5
    return "act_" + "".join(reversed(chars))


@dataclass(frozen=True)
class IntentRecord:
    """一次 ``tool_intent`` 的處理紀錄（供呼叫端與測試核對）。"""

    ordinal: int
    tool: str
    action_id: str | None  # 未建 action（不完整、被拒）為 None
    status: ResultStatus | Literal["incomplete"]
    reason: str | None = None  # 拒絕／阻擋的短碼（只進內部紀錄）


@dataclass(frozen=True)
class StepOutcome:
    end: StepEnd
    attempt_id: str
    intents: tuple[IntentRecord, ...] = ()
    result: Mapping[str, Any] | None = None  # Runtime 的 result 事件內容
    error: Mapping[str, Any] | None = None


class AgentCore:
    def __init__(
        self, runtime: Runtime, tasks: TaskManager, gateway: ToolGateway, *, worker_epoch: int,
    ) -> None:
        self._runtime = runtime
        self._tasks = tasks
        self._gateway = gateway
        self._worker_epoch = worker_epoch

    def run_step(self, *, run_id: str, step_id: str, request: RuntimeRequest) -> StepOutcome:
        """驅動 Runtime 跑完一個 Step。``request.capability_set`` 即當輪可見集合。"""
        attempt_id = self._runtime.start(request)
        intents: list[IntentRecord] = []
        ordinal = 0
        while True:
            delivered = False
            for event in self._runtime.stream(attempt_id):
                if event.kind == "result":
                    return StepOutcome("completed", attempt_id, tuple(intents), result=event.result)
                if event.kind == "error":
                    return StepOutcome("runtime_error", attempt_id, tuple(intents), error=event.error)
                if event.kind != "tool_intent":
                    continue  # text_delta／checkpoint／approval_request 不在本切片範圍
                try:
                    record = self._handle_intent(run_id, step_id, ordinal, attempt_id, request, event)
                except StaleWorkerEpoch:
                    self._runtime.cancel(attempt_id, "stale_worker_epoch")  # SA-2
                    return StepOutcome("stale", attempt_id, tuple(intents))
                ordinal += 1
                intents.append(record)
                delivered = delivered or record.status != "incomplete"
            # stream 結束但沒有 result：剛交過結果時 Runtime 可能暫停等待，再迭代一次續跑；否則結束
            if not delivered:
                return StepOutcome("incomplete", attempt_id, tuple(intents))

    def _handle_intent(
        self, run_id: str, step_id: str, ordinal: int, attempt_id: str, request: RuntimeRequest,
        event: RuntimeEvent,
    ) -> IntentRecord:
        intent = event.tool_intent or {}
        tool = intent["tool"]
        args = intent.get("args", {})
        logger.debug("tool_intent tool=%s side_effect_hint=%s", tool, intent.get("side_effect_hint"))  # TG-2
        if not intent["args_complete"]:
            logger.debug("tool_intent 參數未完成，不建 action：tool=%s ordinal=%d", tool, ordinal)
            return IntentRecord(ordinal, tool, None, "incomplete")  # SA-3／TC-2：等待後續事件

        action_id = stable_action_id(run_id, step_id, ordinal)
        try:
            self._gateway.check_intent(tool, args, request.capability_set)
        except GatewayRejected as exc:
            return self._deliver(attempt_id, action_id, ordinal, tool, "rejected", exc.code, created=False)

        try:
            self._tasks.create_action(
                run_id=run_id, step_id=step_id, tool=tool, params=args, worker_epoch=self._worker_epoch,
                action_id=action_id,
            )
        except StaleWorkerEpoch:
            raise
        except (PolicyViolation, ParamsHashMismatch) as exc:
            code = getattr(exc, "code", None) or type(exc).__name__
            return self._deliver(attempt_id, action_id, ordinal, tool, "rejected", code, created=False)
        except ActionBlocked:
            return self._deliver(attempt_id, action_id, ordinal, tool, "blocked", "action_blocked", created=False)
        except Exception as exc:  # noqa: BLE001  基礎設施錯誤：沒有建出 action，回 failed，只記型別
            logger.error("create_action 失敗：tool=%s error=%s", tool, type(exc).__name__)
            return self._deliver(attempt_id, action_id, ordinal, tool, "failed", type(exc).__name__, created=False)

        try:
            outcome = self._tasks.execute_action(action_id, args, worker_epoch=self._worker_epoch)
        except StaleWorkerEpoch:
            raise
        except ParamsHashMismatch:
            return self._deliver(attempt_id, action_id, ordinal, tool, "rejected", "params_hash_mismatch")
        except (ActionBlocked, PresenceRequired, ApprovalRequired, ApprovalInvalid) as exc:
            # 核准等待屬切片 1（SA-5）：這裡只回「被擋」，不自行建立或消耗 approval
            return self._deliver(attempt_id, action_id, ordinal, tool, "blocked", type(exc).__name__)
        except Exception as exc:  # noqa: BLE001  executor 例外：Manager 已把 action 標 UNKNOWN，不得當成沒執行
            logger.error("execute_action 失敗：action=%s error=%s", action_id, type(exc).__name__)
            return self._deliver(attempt_id, action_id, ordinal, tool, "unknown", type(exc).__name__)

        view = self._gateway.outcome_view(outcome)
        self._runtime.submit_tool_result(attempt_id, action_id, view)
        return IntentRecord(ordinal, tool, action_id, view["status"])

    def _deliver(
        self, attempt_id: str, action_id: str, ordinal: int, tool: str, status: ResultStatus, reason: str,
        *, created: bool = True,
    ) -> IntentRecord:
        """交一個不含項目的狀態結果；``created=False`` 表示沒有 act_ 列（action_id 僅供 Runtime 對應）。"""
        self._runtime.submit_tool_result(attempt_id, action_id, self._gateway.result_view(status))
        return IntentRecord(ordinal, tool, action_id if created else None, status, reason)
