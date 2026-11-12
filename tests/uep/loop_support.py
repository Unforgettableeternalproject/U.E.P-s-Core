"""AgentLoop 離線測試的共用替身（切片 D1）：不需要 DB。

- ``FakeTasks``：in-memory 的 TaskManager 子集（begin_step／complete_step／save_checkpoint／heartbeat／transition_run／
  create_action／execute_action）。對齊真實 TaskManager 的守衛（各自有 DB 測試對照，見 ``test_agent_loop_db.py``）：
  ``save_checkpoint`` 套用真實的 ``CHECKPOINT_KEYS`` 與 ``check_safe_value``；execute_action 以 action_id 冪等；需核准的工具
  沒有 approval_id 一律 ``ApprovalRequired``；帶 approval_id 時依 ``grant`` 登記的核准判定（見下）；executor 例外 ⇒ action 持久化為 UNKNOWN，
  同 id 重放回 ``ActionBlocked``；有 UNKNOWN／EXECUTING 時 ``transition_run(SUCCEEDED)`` 拋 ``IllegalTransition``；
  舊 epoch 的 ``transition_run`` 記 warning 後忽略。
  TC-3（D1b）：與真實 ``_check_single_executing`` 共用 ``executing_set_allowed``——同 Run 的 EXECUTING 集合要麼恰好一個
  serial、要麼全部 parallel_safe（``parallel_one_shot`` 視同 serial），另有非 internal_read 的 UNKNOWN 時一律 ``ActionBlocked``。
  切片 E1：``grant(approval_id, action_id, state=...)`` 登記核准（``valid``／``consumed``／``expired``／``hash_mismatch``／
  ``unauthorized_approver``…；沒登記的 id 即 ``not_found``；綁定的 action 與執行的不同 ⇒ ``hash_mismatch``），不合格拋
  ``ApprovalInvalid(code)`` 且不消耗；合格則單次消耗（``consumed_by``）。``bump_version(tool)`` 模擬註冊表升版 ⇒
  ``ActionBlocked(code=tool_version_changed)``。``park_run`` 記錄呼叫（舊 epoch 只記 warning）。fake 不建立任何核准（TG-7）。
  **fake 沒有**：``params_hash`` 驗證、presence／CC-7 檢查、跨執行緒的 Run 鎖（以 ``self.lock`` 序列化檢查與登記）。
- ``ScriptedRuntime``：依「回合腳本」產生 RuntimeEvent 的 Runtime（遵守 D61 A 路徑：送出整批 tool_intent 後暫停，
  等 submit_tool_result 到齊才續跑下一回合）。
- ``make_registry``：含並行／序列／需核准工具的註冊表。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from uep.adapters.runtime import CancelAck, RuntimeEvent, RuntimeRequest, RuntimeStatus
from uep.adapters.tool import ToolDescriptor, ToolEntry, ToolSpec
from uep.tasks.manager import (
    CHECKPOINT_KEYS,
    ActionBlocked,
    ActionOutcome,
    ApprovalInvalid,
    ApprovalRequired,
    IllegalTransition,
    ParamsHashMismatch,
    PresenceRequired,
    StaleWorkerEpoch,
    check_safe_value,
    executing_set_allowed,
)
from uep.tasks.states import ActionStatus, RunStatus
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)
_IN: dict[str, Any] = {
    "type": "object", "properties": {"path": {"type": "string"}, "n": {"type": "integer"}}, "required": ["path"],
}
_OUT: dict[str, Any] = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
TAGS = frozenset({"memory.read", "memory.write", "chatroom.post"})


def _entry(name: str, *, concurrency="serial", keys=("file:{path}",), approval="none", side="internal_read",
           capability="memory.read", timeout_s=1.0) -> ToolEntry:
    spec = ToolSpec(
        executor="fake", side_effect_class=side, idempotency="native", presence_bound=False, tool_version=1,
        capability=capability, approval_level=approval,
    )
    return ToolEntry(
        name=name, spec=spec, descriptor=ToolDescriptor(f"{name} 說明", _IN), output_schema=_OUT, concurrency=concurrency,
        conflict_keys=tuple(keys) if concurrency != "serial" else (), timeout_s=timeout_s if concurrency != "serial" else None,
        cancellable=concurrency == "parallel_one_shot",
    )


def make_registry(*extra: ToolEntry) -> ToolRegistry:
    return ToolRegistry.build([
        _entry("read.a", concurrency="parallel_safe"),
        _entry("read.b", concurrency="parallel_safe"),
        _entry("read.lone"),  # internal_read 但 serial
        _entry("write.s", side="reversible_local", capability="memory.write"),
        _entry("post.ask", approval="per_action", side="external_effect", capability="chatroom.post"),
        *extra,
    ])


def projector_for(registry: ToolRegistry) -> Callable[[frozenset[str], frozenset[str] | None], Mapping[str, Any]]:
    def project(tags: frozenset[str], prior: frozenset[str] | None) -> Mapping[str, Any]:
        return registry.project(tags, ProjectionContext(), prior=prior)

    return project


class StoppedExecution(Exception):
    """behavior 專用：模擬 executor 被 ``cancel`` 後簽回失敗 receipt（action 以 FAILED 收尾，不是 UNKNOWN）。"""


Behavior = Callable[[dict[str, Any]], str]  # 回傳結果文字；拋例外 ⇒ executor 失聯（UNKNOWN）


class FakeTasks:
    supports_parallel_execution = True  # D1b 起真實 TaskManager 同為 True

    def __init__(self, registry: ToolRegistry, behaviors: Mapping[str, Behavior] | None = None) -> None:
        self.registry = registry
        self.behaviors: dict[str, Behavior] = dict(behaviors or {})
        self.epoch = 1
        self.lock = threading.RLock()
        self.steps: list[SimpleNamespace] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.heartbeats = 0
        self.cancel_signal = None  # 測試可設成 CancelSignal（SV-2）；AgentLoop 目前忽略回傳值（F-4 才讀）
        self.log: list[str] = []  # 寫入類操作的順序（checkpoint／transition），驗證耐久順序用
        self.stale_on_heartbeat = False
        self.run_status: list[RunStatus] = []
        self.actions: dict[str, dict[str, Any]] = {}
        self.outcomes: dict[str, ActionOutcome] = {}
        self.contents: dict[str, str] = {}
        self.executors: dict[str, Any] = {}  # tool -> 真正的 ToolExecutor（例如 decision.consult 的 executor）；沒設定就用 behaviors
        self.unknown: set[str] = set()  # executor 失聯而持久化為 UNKNOWN 的 action
        self.waiting_approval = False  # 真實 Run 在 ApprovalRequired 後為 WAITING_APPROVAL；有 action 成功進入執行才回 RUNNING
        self.approvals: dict[str, FakeApproval] = {}  # approval_id -> 核准（只能由測試 grant，fake 不自建）
        self.versions: dict[str, int] = {}  # tool -> 目前註冊表版本（預設取註冊表條目的 tool_version）
        self.created_versions: dict[str, int] = {}  # action_id -> 建立時的 tool_version
        self.park_calls: list[tuple[str, int]] = []
        self.presence_missing = False  # True ⇒ presence_bound_tools 內的工具因 Presence 不足被擋（見 presence_retryable）
        self.presence_bound_tools: set[str] = set()  # 宣告 presence_bound 的工具；真實 Manager 只對 presence_bound 的 Task／action 檢查 Presence
        self.presence_retryable = True  # True：階段 1 被擋（action 維持 INTENDED、核准未消耗）；False：階段 1.5（核准已消耗、action FAILED）
        self.executing: dict[str, str] = {}  # 進行中的 action_id -> concurrency（TC-3 守衛用）
        self.executions: list[tuple[str, str]] = []  # (action_id, tool)：真正執行副作用的紀錄
        self.fail_codes: dict[str, str] = {}  # fail_tools 的 receipt error_code（預設 FAKE_FAILURE）
        self.fail_tools: set[str] = set()  # 這些工具的 action 以 FAILED 收尾（receipt succeeded=False）
        self.marked_unknown: list[str] = []  # 逾時後經 mark_action_unknown 轉 UNKNOWN 的 action
        self.mark_reasons: dict[str, str] = {}
        self.late_receipts: dict[str, Any] = {}  # 測試預置：UNKNOWN 之後「查得到」的已驗證 receipt（resolve_unknown 用）
        self.resolved: list[str] = []
        self.retries: dict[str, str] = {}  # 重試 action_id -> retry_of

    def mark_action_unknown(self, action_id: str, *, worker_epoch: int, reason: str) -> bool:
        """對照真實 ``TaskManager.mark_action_unknown``：仍在執行中、concurrency 符合 ``reason`` 的 action 轉 UNKNOWN；其餘不動作。"""
        allowed = {"timeout": {"parallel_one_shot"}, "run_cancelled": {"parallel_safe", "parallel_one_shot"}}[reason]
        with self.lock:
            self._check(worker_epoch)
            if action_id not in self.executing or self.executing[action_id] not in allowed:
                return False
            self.unknown.add(action_id)
            self.marked_unknown.append(action_id)
            self.mark_reasons[action_id] = reason
            return True

    def get_receipt(self, action_id: str):
        outcome = self.outcomes.get(action_id)
        if outcome is None or outcome.receipt is None:
            return self.late_receipts.get(action_id)
        return outcome.receipt

    def get_action(self, action_id: str):
        status = ActionStatus.UNKNOWN if action_id in self.unknown else (
            self.outcomes[action_id].status if action_id in self.outcomes else ActionStatus.EXECUTING
        )
        return SimpleNamespace(status=status.value)

    def resolve_unknown(self, action_id: str) -> ActionOutcome:
        """對照真實 resolve_unknown 的「已有已驗證 receipt」分支：依 ``late_receipts`` 解決，否則維持 UNKNOWN。"""
        receipt = self.late_receipts.get(action_id)
        if receipt is None:
            return ActionOutcome(action_id, ActionStatus.UNKNOWN, False, None)
        self.unknown.discard(action_id)
        outcome = ActionOutcome(
            action_id, ActionStatus.SUCCEEDED if receipt.succeeded else ActionStatus.FAILED, False, receipt,
        )
        self.outcomes[action_id] = outcome
        self.resolved.append(action_id)
        if receipt.succeeded:
            self.contents.setdefault(action_id, f"resolved:{action_id}")
        return outcome

    def _check(self, epoch: int) -> None:
        if epoch != self.epoch:
            raise StaleWorkerEpoch(f"epoch={self.epoch}，收到 {epoch}")

    def begin_step(self, run_id: str, label: str, *, worker_epoch: int):
        with self.lock:
            self._check(worker_epoch)
            step = SimpleNamespace(step_id=f"step_{len(self.steps) + 1}", label=label, status="RUNNING")
            self.steps.append(step)
            return step

    def complete_step(self, step_id: str, *, worker_epoch: int, result_ref=None, ok: bool = True):
        with self.lock:
            self._check(worker_epoch)
            step = next(s for s in self.steps if s.step_id == step_id)
            if step.status == "RUNNING":
                step.status = "SUCCEEDED" if ok else "FAILED"
            return step

    def save_checkpoint(self, run_id: str, checkpoint: Mapping[str, Any], *, worker_epoch: int, checkpoint_ref=None):
        with self.lock:
            self._check(worker_epoch)
            extra = sorted(set(checkpoint) - CHECKPOINT_KEYS)
            assert not extra, f"checkpoint 含契約外的鍵：{extra}"
            check_safe_value(dict(checkpoint), "checkpoint")
            self.checkpoints.append(dict(checkpoint))
            self.log.append("checkpoint")

    def heartbeat(self, run_id: str, *, worker_epoch: int):
        with self.lock:
            if self.stale_on_heartbeat:
                raise StaleWorkerEpoch("heartbeat")
            self._check(worker_epoch)
            self.heartbeats += 1
            return self.cancel_signal  # 與真實 Manager 對齊：Supervisor 設了取消旗標時回傳 CancelSignal，否則 None

    def transition_run(self, run_id: str, to: RunStatus, *, worker_epoch: int, **_: Any):
        with self.lock:
            if worker_epoch != self.epoch:
                logger.warning("忽略舊 worker_epoch 的 Run 轉移")  # 與真實 transition_run 相同：只記錄、不拋例外
                return
            if to is RunStatus.SUCCEEDED and self.waiting_approval:
                raise IllegalTransition("Run 為 WAITING_APPROVAL，不可轉 SUCCEEDED（states.py）")
            if to is RunStatus.SUCCEEDED and self.unknown:
                raise IllegalTransition("仍有結果未明的 action，不可轉 SUCCEEDED")
            self.run_status.append(to)
            self.log.append(f"transition:{to.value}")

    def _version(self, tool: str) -> int:
        return self.versions.get(tool, self.registry.entry(tool).spec.tool_version)

    def bump_version(self, tool: str) -> None:
        """模擬註冊表升版：既有 action 之後的 create／execute 都回 ``ActionBlocked(code=tool_version_changed)``。"""
        self.versions[tool] = self._version(tool) + 1

    def grant(self, approval_id: str, action_id: str, *, state: str = "valid") -> FakeApproval:
        """登記一筆核准（模擬 Approval 通道已產生）；``state`` 非 valid 時模擬對應的不合格原因。"""
        self.approvals[approval_id] = FakeApproval(action_id, state)
        return self.approvals[approval_id]

    def park_run(self, run_id: str, *, worker_epoch: int):
        with self.lock:
            self.park_calls.append((run_id, worker_epoch))
            self.log.append("park")
            if worker_epoch != self.epoch:
                logger.warning("忽略舊 worker_epoch 的 park")
            return SimpleNamespace(run_id=run_id, worker_epoch=self.epoch)

    def create_action(self, *, run_id, step_id, tool, params, worker_epoch, presence_bound=None, action_id=None, retry_of=None):
        with self.lock:
            self._check(worker_epoch)
            assert action_id
            if action_id in self.actions and self.created_versions[action_id] != self._version(tool):
                raise ActionBlocked("tool_version_changed", code="tool_version_changed")
            self.created_versions.setdefault(action_id, self._version(tool))
            self.actions.setdefault(action_id, {"run_id": run_id, "step_id": step_id, "tool": tool, "params": dict(params)})
            if retry_of is not None:
                self.retries[action_id] = retry_of

    def execute_action(
        self, action_id: str, params: Mapping[str, Any], *, worker_epoch: int, approval_id: str | None = None,
    ) -> ActionOutcome:
        with self.lock:
            self._check(worker_epoch)
            if dict(params) != self.actions[action_id]["params"]:  # 真實 Manager 以 params_hash 判定（此處簡化為參數相等）
                raise ParamsHashMismatch("參數與 action 不符")
            if action_id in self.outcomes:
                return self.outcomes[action_id]  # 冪等：已有 receipt 不重做
            if action_id in self.unknown:
                raise ActionBlocked(f"action {action_id} 結果未明（UNKNOWN），不可重做", code="action_unresolved")
            tool = self.actions[action_id]["tool"]
            if self.created_versions[action_id] != self._version(tool):  # 真實 Manager：版本檢查早於 TC-3 與核准
                raise ActionBlocked("tool_version_changed", code="tool_version_changed")
            if self.presence_missing and self.presence_retryable and tool in self.presence_bound_tools:
                raise PresenceRequired("前景 action 需要有效 Presence", retryable=True)
            blocked = next((
                u for u in self.unknown
                if u != action_id and self.registry.entry(self.actions[u]["tool"]).spec.side_effect_class != "internal_read"
            ), None)
            if blocked is not None:
                raise ActionBlocked(f"同一 Run 已有 UNKNOWN 的 action（{blocked}），不可再進入執行（TC-3）")
            concurrency = self.registry.entry(tool).concurrency
            if not executing_set_allowed(concurrency, list(self.executing.values())):
                raise ActionBlocked(f"同一 Run 已有 EXECUTING 的 action，不可再進入執行（TC-3，{concurrency}）")
            # 與真實 Manager 同序：先 TC-3 檢查，再判斷核准（被 TC-3 擋下時核准不被消耗）
            if self.registry.entry(tool).spec.approval_level != "none":
                self.waiting_approval = True  # 缺核准或核准不合格：Run 都進 WAITING_APPROVAL（真實 _to_waiting_approval）
                if approval_id is None:
                    raise ApprovalRequired(action_id)
                approval = self.approvals.get(approval_id)
                if approval is None:
                    raise ApprovalInvalid("not_found")
                if approval.state != "valid":
                    raise ApprovalInvalid(approval.state)
                if approval.action_id != action_id:
                    raise ApprovalInvalid("hash_mismatch")  # 核准綁 params_hash：對錯 action
                approval.state, approval.consumed_by = "consumed", action_id  # 單次消耗
            self.waiting_approval = False  # 真實 Manager：進入執行時 _back_to_running
            if self.presence_missing and not self.presence_retryable and tool in self.presence_bound_tools:
                # 階段 1.5：階段 1 已提交（核准已消耗、Run 回 RUNNING），executor 呼叫前 Presence 失效 ⇒ action 終態 FAILED
                self.outcomes[action_id] = ActionOutcome(
                    action_id, ActionStatus.FAILED, True, SimpleNamespace(result_ref=None, source_verified=True),
                )
                raise PresenceRequired("呼叫 executor 前 Presence 已失效", retryable=False)
            self.executing[action_id] = concurrency
        try:
            return self._run_action(action_id, tool, params)
        finally:
            with self.lock:
                self.executing.pop(action_id, None)

    def _run_action(self, action_id: str, tool: str, params: Mapping[str, Any]) -> ActionOutcome:
        behavior = self.behaviors.get(tool, lambda args: f"{tool}:{args.get('path')}")
        try:
            if tool in self.executors:
                from uep.adapters.tool import ToolCall

                self.executors[tool].execute(ToolCall(
                    action_id=action_id, tool=tool, params=dict(params), params_hash="h",
                    side_effect_class="internal_read", idempotency="native",
                ))
                text = f"{tool}:executor"
            else:
                text = behavior(dict(params))  # 在鎖外執行，讓並行波能真的並行
        except StoppedExecution:
            with self.lock:  # executor 被 cancel 且簽回失敗 receipt ⇒ action FAILED（沒有副作用、沒有內容）
                outcome = ActionOutcome(
                    action_id, ActionStatus.FAILED, True,
                    SimpleNamespace(result_ref=None, source_verified=True, error_code="timeout"),
                )
                self.outcomes[action_id] = outcome
                return outcome
        except Exception:
            with self.lock:
                self.unknown.add(action_id)  # 真實 Manager：executor 例外 ⇒ action 標 UNKNOWN 並重新拋出
            raise
        with self.lock:
            if action_id in self.unknown:
                return ActionOutcome(action_id, ActionStatus.UNKNOWN, True, None)  # 逾時已轉 UNKNOWN：晚到結果不推進
            self.executions.append((action_id, tool))
            self.contents[action_id] = text
            ok = tool not in self.fail_tools
            outcome = ActionOutcome(
                action_id, ActionStatus.SUCCEEDED if ok else ActionStatus.FAILED, True,
                SimpleNamespace(result_ref=f"ref:{action_id}", source_verified=True, error_code=None if ok else self.fail_codes.get(tool, "FAKE_FAILURE")),
            )
            self.outcomes[action_id] = outcome
            return outcome


@dataclass
class FakeApproval:
    """FakeTasks 的一筆核准：綁定的 action、狀態（valid／consumed／expired／hash_mismatch…）與消耗者。"""

    action_id: str
    state: str = "valid"
    consumed_by: str | None = None


@dataclass
class Round:
    """Runtime 的一個回合（一則模型訊息）：文字＋一批工具呼叫；沒有呼叫 ⇒ 最終文字。"""

    text: str = ""
    calls: tuple[tuple[str, Any], ...] = ()  # (tool, args)；args 為 None 表示參數不完整
    usage: int = 0
    error: str | None = None
    retryable: bool = False  # error 事件的 retryable 旗標


class ScriptedRuntime:
    """遵守 D61 A 路徑：整批 tool_intent 送出後暫停，等 submit_tool_result 到齊才續跑下一回合。回合腳本由所有 attempt 共用。"""

    def __init__(self, rounds: list[Round]) -> None:
        self.rounds: deque[Round] = deque(rounds)
        self.requests: list[RuntimeRequest] = []
        self.cancels: list[tuple[str, str]] = []
        self.submitted: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
        self.round_caps: list[list[str]] = []  # 每個回合開始時 attempt 的工具名稱
        self._usage: dict[str, int] = {}
        self._cancelled: set[str] = set()
        self._seq: dict[str, int] = {}

    def start(self, request: RuntimeRequest) -> str:
        self.requests.append(request)
        self.submitted[request.attempt_id] = []
        self._usage[request.attempt_id] = 0
        return request.attempt_id

    def resume(self, request: RuntimeRequest) -> str:
        raise NotImplementedError

    def cancel(self, attempt_id: str, reason: str) -> CancelAck:
        self.cancels.append((attempt_id, reason))
        self._cancelled.add(attempt_id)
        return "stopped"

    def status(self, attempt_id: str) -> RuntimeStatus:
        return RuntimeStatus(attempt_id, "running", meta={"usage": {"input_tokens": self._usage.get(attempt_id, 0)}})

    def submit_tool_result(self, attempt_id: str, action_id: str, result: Mapping[str, Any]) -> None:
        self.submitted[attempt_id].append((action_id, result))

    def _request(self, attempt_id: str) -> RuntimeRequest:
        return next(r for r in self.requests if r.attempt_id == attempt_id)

    def stream(self, attempt_id: str) -> Iterator[RuntimeEvent]:
        names = [t["tool"] for t in self._request(attempt_id).capability_set.get("tools", ())]
        while self.rounds:
            if attempt_id in self._cancelled:
                return
            rnd = self.rounds.popleft()
            self.round_caps.append(names)
            self._usage[attempt_id] += rnd.usage
            if rnd.error:
                yield self._ev(attempt_id, "error", error={"code": rnd.error, "retryable": rnd.retryable, "detail": ""})
                return
            if rnd.text:
                yield self._ev(attempt_id, "text_delta", text_delta=rnd.text)
            if not rnd.calls:
                yield self._ev(attempt_id, "result", result={"status": "ok", "usage": {}})
                return
            for index, (tool, args) in enumerate(rnd.calls):
                yield self._ev(attempt_id, "tool_intent", tool_intent={
                    "tool": tool, "args": dict(args or {}), "args_complete": args is not None,
                    "call_id": f"{attempt_id}:{len(self.requests)}:{index}:{tool}", "index": index,
                    "side_effect_hint": None, "batch_size": len(rnd.calls),
                })
            # 到這裡 AgentLoop 已收齊整批、執行並回餵（它在回餵之前不會要下一個事件）
        yield self._ev(attempt_id, "result", result={"status": "ok", "usage": {}})

    def _ev(self, attempt_id: str, kind: str, **fields: Any) -> RuntimeEvent:
        self._seq[attempt_id] = self._seq.get(attempt_id, 0) + 1
        return RuntimeEvent(attempt_id=attempt_id, seq=self._seq[attempt_id], kind=kind, **fields)  # type: ignore[arg-type]


@dataclass
class Rig:
    """把 AgentLoop 需要的東西裝在一起。"""

    registry: ToolRegistry
    tasks: FakeTasks
    runtime: ScriptedRuntime
    extra: dict[str, Any] = field(default_factory=dict)
