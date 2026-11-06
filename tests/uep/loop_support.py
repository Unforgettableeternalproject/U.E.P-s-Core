"""AgentLoop 離線測試的共用替身（切片 D1）：不需要 DB。

- ``FakeTasks``：in-memory 的 TaskManager 子集（begin_step／complete_step／save_checkpoint／heartbeat／transition_run／
  create_action／execute_action）。對齊真實 TaskManager 的守衛（各自有 DB 測試對照，見 ``test_agent_loop_db.py``）：
  ``save_checkpoint`` 套用真實的 ``CHECKPOINT_KEYS`` 與 ``check_safe_value``；execute_action 以 action_id 冪等；需核准的工具
  一律 ``ApprovalRequired``（沒有 approval_id 的執行路徑，核准後執行屬切片 E）；executor 例外 ⇒ action 持久化為 UNKNOWN，
  同 id 重放回 ``ActionBlocked``；有 UNKNOWN／EXECUTING 時 ``transition_run(SUCCEEDED)`` 拋 ``IllegalTransition``；
  舊 epoch 的 ``transition_run`` 記 warning 後忽略。
  TC-3（D1b）：與真實 ``_check_single_executing`` 共用 ``executing_set_allowed``——同 Run 的 EXECUTING 集合要麼恰好一個
  serial、要麼全部 parallel_safe（``parallel_one_shot`` 視同 serial），另有非 internal_read 的 UNKNOWN 時一律 ``ActionBlocked``。
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
    ApprovalRequired,
    IllegalTransition,
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
           capability="memory.read") -> ToolEntry:
    spec = ToolSpec(
        executor="fake", side_effect_class=side, idempotency="native", presence_bound=False, tool_version=1,
        capability=capability, approval_level=approval,
    )
    return ToolEntry(
        name=name, spec=spec, descriptor=ToolDescriptor(f"{name} 說明", _IN), output_schema=_OUT, concurrency=concurrency,
        conflict_keys=tuple(keys) if concurrency != "serial" else (), timeout_s=1.0 if concurrency != "serial" else None,
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
        self.log: list[str] = []  # 寫入類操作的順序（checkpoint／transition），驗證耐久順序用
        self.stale_on_heartbeat = False
        self.run_status: list[RunStatus] = []
        self.actions: dict[str, dict[str, Any]] = {}
        self.outcomes: dict[str, ActionOutcome] = {}
        self.contents: dict[str, str] = {}
        self.executors: dict[str, Any] = {}  # tool -> 真正的 ToolExecutor（例如 decision.consult 的 executor）；沒設定就用 behaviors
        self.unknown: set[str] = set()  # executor 失聯而持久化為 UNKNOWN 的 action
        self.executing: dict[str, str] = {}  # 進行中的 action_id -> concurrency（TC-3 守衛用）
        self.executions: list[tuple[str, str]] = []  # (action_id, tool)：真正執行副作用的紀錄
        self.fail_tools: set[str] = set()  # 這些工具的 action 以 FAILED 收尾（receipt succeeded=False）

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

    def heartbeat(self, run_id: str, *, worker_epoch: int) -> None:
        with self.lock:
            if self.stale_on_heartbeat:
                raise StaleWorkerEpoch("heartbeat")
            self._check(worker_epoch)
            self.heartbeats += 1

    def transition_run(self, run_id: str, to: RunStatus, *, worker_epoch: int, **_: Any):
        with self.lock:
            if worker_epoch != self.epoch:
                logger.warning("忽略舊 worker_epoch 的 Run 轉移")  # 與真實 transition_run 相同：只記錄、不拋例外
                return
            if to is RunStatus.SUCCEEDED and self.unknown:
                raise IllegalTransition("仍有結果未明的 action，不可轉 SUCCEEDED")
            self.run_status.append(to)
            self.log.append(f"transition:{to.value}")

    def create_action(self, *, run_id, step_id, tool, params, worker_epoch, presence_bound=None, action_id=None):
        with self.lock:
            self._check(worker_epoch)
            assert action_id
            self.actions.setdefault(action_id, {"run_id": run_id, "step_id": step_id, "tool": tool, "params": dict(params)})

    def execute_action(self, action_id: str, params: Mapping[str, Any], *, worker_epoch: int) -> ActionOutcome:
        with self.lock:
            self._check(worker_epoch)
            if action_id in self.outcomes:
                return self.outcomes[action_id]  # 冪等：已有 receipt 不重做
            if action_id in self.unknown:
                raise ActionBlocked(f"action {action_id} 結果未明（UNKNOWN），不可重做")
            tool = self.actions[action_id]["tool"]
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
                raise ApprovalRequired(action_id)  # 沒有 approval_id 的執行路徑（核准後執行屬切片 E）
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
        except Exception:
            with self.lock:
                self.unknown.add(action_id)  # 真實 Manager：executor 例外 ⇒ action 標 UNKNOWN 並重新拋出
            raise
        with self.lock:
            self.executions.append((action_id, tool))
            self.contents[action_id] = text
            ok = tool not in self.fail_tools
            outcome = ActionOutcome(
                action_id, ActionStatus.SUCCEEDED if ok else ActionStatus.FAILED, True,
                SimpleNamespace(result_ref=f"ref:{action_id}", source_verified=True),
            )
            self.outcomes[action_id] = outcome
            return outcome


@dataclass
class Round:
    """Runtime 的一個回合（一則模型訊息）：文字＋一批工具呼叫；沒有呼叫 ⇒ 最終文字。"""

    text: str = ""
    calls: tuple[tuple[str, Any], ...] = ()  # (tool, args)；args 為 None 表示參數不完整
    usage: int = 0
    error: str | None = None


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
                yield self._ev(attempt_id, "error", error={"code": rnd.error, "retryable": False, "detail": ""})
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
