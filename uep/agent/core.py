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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from uep.adapters.runtime import Runtime, RuntimeRequest
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


@dataclass(frozen=True)
class ScreenedIntent:
    """通過 Gateway 篩選的 tool_intent（切片 D1）：``args`` 是驗證並注入後的參數，``action_id`` 由 ``stable_action_id`` 決定。"""

    run_id: str
    step_id: str
    ordinal: int
    tool: str
    args: Mapping[str, Any]
    action_id: str


@dataclass(frozen=True)
class IntentResult:
    """一個 tool_intent 的處理結果：紀錄、要交給模型的 D61 視圖，以及是否因需核准而停住（呼叫端 park）。"""

    record: IntentRecord
    view: Mapping[str, Any]
    approval_required: bool = False
    # 以下兩個欄位只在 resume 帶 approval 執行時才可能有值（加性、預設 None，legacy 輸出不變）
    approval_code: str | None = None  # ``ApprovalInvalid.code``（not_found／consumed／expired／hash_mismatch…）
    block_code: str | None = None  # ``ActionBlocked.code``（目前只有 ``tool_version_changed``）
    retryable: bool | None = None  # ``PresenceRequired.retryable``：False ⇒ 階段 1.5 被擋，action 已 FAILED、核准已消耗


class AgentCore:
    def __init__(
        self, runtime: Runtime | None, tasks: TaskManager, gateway: ToolGateway, *, worker_epoch: int,
        injected: Mapping[str, Any] | None = None, content_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        # ``runtime`` 只在 ``run_step`` 需要；只用 ``run_intent`` 的呼叫端（模型呼叫在別處）可以傳 None
        self._runtime = runtime
        self._content_lookup = content_lookup  # internal_read 結果文字的回合範圍來源（D33：不進 receipt／DB）
        self._tasks = tasks
        self._gateway = gateway
        self._worker_epoch = worker_epoch
        self._injected = injected  # Core 持有的注入值（venue 等），只經 Gateway 進 params，LLM 看不到

    def run_step(self, *, run_id: str, step_id: str, request: RuntimeRequest) -> StepOutcome:
        """驅動 Runtime 跑完一個 Step。``request.capability_set`` 即當輪可見集合。"""
        runtime = self._require_runtime()
        attempt_id = runtime.start(request)
        intents: list[IntentRecord] = []
        ordinal = 0
        while True:
            delivered = False
            for event in runtime.stream(attempt_id):
                if event.kind == "result":
                    return StepOutcome("completed", attempt_id, tuple(intents), result=event.result)
                if event.kind == "error":
                    return StepOutcome("runtime_error", attempt_id, tuple(intents), error=event.error)
                if event.kind != "tool_intent":
                    continue  # text_delta／checkpoint／approval_request 不在本切片範圍
                intent = event.tool_intent or {}

                def deliver(action_id: str, view: Mapping[str, Any], attempt_id: str = attempt_id) -> None:
                    runtime.submit_tool_result(attempt_id, action_id, view)

                try:
                    record = self._handle_intent(
                        run_id, step_id, ordinal, request.capability_set, intent, deliver, self._injected,
                    )
                except StaleWorkerEpoch:
                    runtime.cancel(attempt_id, "stale_worker_epoch")  # SA-2
                    return StepOutcome("stale", attempt_id, tuple(intents))
                ordinal += 1
                intents.append(record)
                delivered = delivered or record.status != "incomplete"
            # stream 結束但沒有 result：剛交過結果時 Runtime 可能暫停等待，再迭代一次續跑；否則結束
            if not delivered:
                return StepOutcome("incomplete", attempt_id, tuple(intents))

    def _require_runtime(self) -> Runtime:
        if self._runtime is None:
            raise RuntimeError("AgentCore 沒有 Runtime：只能使用 run_intent")
        return self._runtime

    def run_intent(
        self, *, run_id: str, step_id: str, ordinal: int, capability_set: Mapping[str, Any], tool: str,
        args: Any, injected: Mapping[str, Any] | None = None,
    ) -> tuple[IntentRecord, Mapping[str, Any] | None]:
        """處理一個已完整的 ``tool_intent``（Gateway -> create_action -> execute_action），回傳紀錄與結果視圖。

        與 ``run_step`` 同一條路徑，但視圖交給呼叫端而不是 ``submit_tool_result``：模型呼叫不在這個 Core 內時使用
        （例如宿主的對話迴圈已持有模型串流，只把工具執行交給 ActionIntent）。``injected`` 為本輪 Core 持有的
        注入值（PD-12），沒給則用建構子的值。視圖在沒有交付時（參數未完成）為 None。"""
        delivered: list[Mapping[str, Any]] = []
        record = self._handle_intent(
            run_id, step_id, ordinal, capability_set,
            {"tool": tool, "args": args, "args_complete": True},
            lambda _action_id, view: delivered.append(view),
            self._injected if injected is None else injected,
        )
        return record, (delivered[-1] if delivered else None)

    def _handle_intent(
        self, run_id: str, step_id: str, ordinal: int, capability_set: Mapping[str, Any],
        intent: Mapping[str, Any], deliver: Callable[[str, Mapping[str, Any]], None],
        injected: Mapping[str, Any] | None,
    ) -> IntentRecord:
        tool = intent["tool"]
        args = intent.get("args", {})
        logger.debug("tool_intent tool=%s side_effect_hint=%s", tool, intent.get("side_effect_hint"))  # TG-2
        if not intent["args_complete"]:
            logger.debug("tool_intent 參數未完成，不建 action：tool=%s ordinal=%d", tool, ordinal)
            return IntentRecord(ordinal, tool, None, "incomplete")  # SA-3／TC-2：等待後續事件

        action_id = stable_action_id(run_id, step_id, ordinal)
        try:
            # 用 Gateway 回傳的（驗證並注入後的）參數建 action 與執行：注入發生在 params_hash 之前
            args = self._gateway.check_intent(tool, args, capability_set, injected=injected)
        except GatewayRejected as exc:
            deliver(action_id, self._gateway.rejection_view(exc))
            return IntentRecord(ordinal, tool, None, "rejected", exc.code)

        try:
            self._tasks.create_action(
                run_id=run_id, step_id=step_id, tool=tool, params=args, worker_epoch=self._worker_epoch,
                action_id=action_id,
            )
        except StaleWorkerEpoch:
            raise
        except (PolicyViolation, ParamsHashMismatch) as exc:
            code = getattr(exc, "code", None) or type(exc).__name__
            return self._deliver(deliver, action_id, ordinal, tool, "rejected", code, created=False)
        except ActionBlocked:
            return self._deliver(deliver, action_id, ordinal, tool, "blocked", "action_blocked", created=False)
        except Exception as exc:  # noqa: BLE001  基礎設施錯誤：沒有建出 action，回 failed，只記型別
            logger.error("create_action 失敗：tool=%s error=%s", tool, type(exc).__name__)
            return self._deliver(deliver, action_id, ordinal, tool, "failed", type(exc).__name__, created=False)

        try:
            outcome = self._tasks.execute_action(action_id, args, worker_epoch=self._worker_epoch)
        except StaleWorkerEpoch:
            raise
        except ParamsHashMismatch:
            return self._deliver(deliver, action_id, ordinal, tool, "rejected", "params_hash_mismatch")
        except (ActionBlocked, PresenceRequired, ApprovalRequired, ApprovalInvalid) as exc:
            # 核准等待屬切片 1（SA-5）：這裡只回「被擋」，不自行建立或消耗 approval
            return self._deliver(deliver, action_id, ordinal, tool, "blocked", type(exc).__name__)
        except Exception as exc:  # noqa: BLE001  executor 例外：Manager 已把 action 標 UNKNOWN，不得當成沒執行
            logger.error("execute_action 失敗：action=%s error=%s", action_id, type(exc).__name__)
            return self._deliver(deliver, action_id, ordinal, tool, "unknown", type(exc).__name__)

        view = self._gateway.outcome_view(outcome, content_lookup=self._content_lookup)
        deliver(action_id, view)
        return IntentRecord(ordinal, tool, action_id, view["status"])

    def _deliver(
        self, deliver: Callable[[str, Mapping[str, Any]], None], action_id: str, ordinal: int, tool: str,
        status: ResultStatus, reason: str, *, created: bool = True,
    ) -> IntentRecord:
        """交一個不含項目的狀態結果；``created=False`` 表示沒有 act_ 列（action_id 僅供 Runtime 對應）。"""
        deliver(action_id, self._gateway.result_view(status))
        return IntentRecord(ordinal, tool, action_id if created else None, status, reason)

    # ---- 切片 D1：AgentLoop 的分段介面（篩選／建立／執行分開，平行排程器才能先建立整波再並行執行）----
    # 與 ``_handle_intent`` 同一套判定與例外對映；``_handle_intent``／``run_step``（legacy 與切片 C 路徑）不經過這裡。

    def screen_intent(
        self, *, run_id: str, step_id: str, ordinal: int, capability_set: Mapping[str, Any], tool: str, args: Any,
        injected: Mapping[str, Any] | None = None,
    ) -> ScreenedIntent | IntentResult:
        """Gateway 篩選（capability／schema／注入）。通過回 ``ScreenedIntent``（含驗證並注入後的參數與穩定 action_id），
        被拒回帶 ``rejected`` 視圖的 ``IntentResult``（不建 action）。"""
        action_id = stable_action_id(run_id, step_id, ordinal)
        try:
            checked = self._gateway.check_intent(
                tool, args, capability_set, injected=self._injected if injected is None else injected,
            )
        except GatewayRejected as exc:
            return IntentResult(
                IntentRecord(ordinal, tool, None, "rejected", exc.code), self._gateway.rejection_view(exc),
            )
        return ScreenedIntent(run_id, step_id, ordinal, tool, checked, action_id)

    def create_screened(self, item: ScreenedIntent) -> IntentResult | None:
        """建立 INTENDED 的 action（同 id 冪等）。成功回 None；被擋回對應狀態的 ``IntentResult``。"""
        try:
            self._tasks.create_action(
                run_id=item.run_id, step_id=item.step_id, tool=item.tool, params=item.args,
                worker_epoch=self._worker_epoch, action_id=item.action_id,
            )
        except StaleWorkerEpoch:
            raise
        except (PolicyViolation, ParamsHashMismatch) as exc:
            code = getattr(exc, "code", None) or type(exc).__name__
            return self._status_result(item, "rejected", code, created=False)
        except ActionBlocked as exc:
            result = self._status_result(item, "blocked", "action_blocked", created=False)
            return IntentResult(result.record, result.view, block_code=exc.code)
        except Exception as exc:  # noqa: BLE001  基礎設施錯誤：沒有建出 action
            logger.error("create_action 失敗：tool=%s error=%s", item.tool, type(exc).__name__)
            return self._status_result(item, "failed", type(exc).__name__, created=False)
        return None

    def execute_screened(self, item: ScreenedIntent, *, approval_id: str | None = None) -> IntentResult:
        """執行已建立的 action。``ApprovalRequired`` 以 ``approval_required=True`` 回報（呼叫端 park），不自行建立或消耗 approval。

        ``approval_id`` 是呼叫端（resume）提供的既有核准；是否與 action 的 params_hash／target／scope 吻合一律由
        Manager 判定，這裡原樣轉交，不預判。``ApprovalInvalid`` 的 code 與「工具版本已變」放在 ``IntentResult`` 的
        ``approval_code``／``block_code``。沒給 ``approval_id`` 時呼叫形式與先前完全相同。"""
        extra = {} if approval_id is None else {"approval_id": approval_id}
        try:
            outcome = self._tasks.execute_action(item.action_id, item.args, worker_epoch=self._worker_epoch, **extra)
        except StaleWorkerEpoch:
            raise
        except ParamsHashMismatch:
            return self._status_result(item, "rejected", "params_hash_mismatch")
        except ApprovalRequired:
            result = self._status_result(item, "blocked", "ApprovalRequired")
            return IntentResult(result.record, result.view, approval_required=True)
        except ApprovalInvalid as exc:
            result = self._status_result(item, "blocked", type(exc).__name__)
            return IntentResult(result.record, result.view, approval_code=exc.code)
        except ActionBlocked as exc:
            result = self._status_result(item, "blocked", type(exc).__name__)
            return IntentResult(result.record, result.view, block_code=exc.code)
        except PresenceRequired as exc:
            result = self._status_result(item, "blocked", type(exc).__name__)
            return IntentResult(result.record, result.view, retryable=exc.retryable)
        except Exception as exc:  # noqa: BLE001  executor 例外：Manager 已把 action 標 UNKNOWN
            logger.error("execute_action 失敗：action=%s error=%s", item.action_id, type(exc).__name__)
            return self._status_result(item, "unknown", type(exc).__name__)
        view = self._gateway.outcome_view(outcome, content_lookup=self._content_lookup)
        return IntentResult(IntentRecord(item.ordinal, item.tool, item.action_id, view["status"]), view)

    def _status_result(
        self, item: ScreenedIntent, status: ResultStatus, reason: str, *, created: bool = True,
    ) -> IntentResult:
        return IntentResult(
            IntentRecord(item.ordinal, item.tool, item.action_id if created else None, status, reason),
            self._gateway.result_view(status),
        )
