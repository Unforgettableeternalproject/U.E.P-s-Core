"""AgentLoop 離線測試的共用替身（切片 D1）：不需要 DB。

- ``FakeTasks``：in-memory 的 TaskManager 子集（begin_step／complete_step／save_checkpoint／heartbeat／transition_run／
  create_action／execute_action）。``save_checkpoint`` 套用真實的 ``CHECKPOINT_KEYS`` 與 ``check_safe_value``，擴鍵或值格式錯誤
  在這裡就會被抓到；execute_action 以 action_id 冪等（重放不重做副作用）。
- ``ScriptedRuntime``：依「回合腳本」產生 RuntimeEvent 的 Runtime（遵守 D61 A 路徑：送出整批 tool_intent 後暫停，
  等 submit_tool_result 到齊才續跑下一回合）。
- ``make_registry``：含並行／序列／需核准工具的註冊表。
"""
from __future__ import annotations

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
    ActionOutcome,
    ApprovalRequired,
    StaleWorkerEpoch,
    check_safe_value,
)
from uep.tasks.states import ActionStatus, RunStatus
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry

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
    def __init__(self, registry: ToolRegistry, behaviors: Mapping[str, Behavior] | None = None) -> None:
        self.registry = registry
        self.behaviors: dict[str, Behavior] = dict(behaviors or {})
        self.epoch = 1
        self.lock = threading.RLock()
        self.steps: list[SimpleNamespace] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.heartbeats = 0
        self.stale_on_heartbeat = False
        self.run_status: list[RunStatus] = []
        self.actions: dict[str, dict[str, Any]] = {}
        self.outcomes: dict[str, ActionOutcome] = {}
        self.contents: dict[str, str] = {}
        self.approved: set[str] = set()
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

    def heartbeat(self, run_id: str, *, worker_epoch: int) -> None:
        with self.lock:
            if self.stale_on_heartbeat:
                raise StaleWorkerEpoch("heartbeat")
            self._check(worker_epoch)
            self.heartbeats += 1

    def transition_run(self, run_id: str, to: RunStatus, *, worker_epoch: int, **_: Any):
        with self.lock:
            self._check(worker_epoch)
            self.run_status.append(to)

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
            tool = self.actions[action_id]["tool"]
            if self.registry.entry(tool).spec.approval_level != "none" and action_id not in self.approved:
                raise ApprovalRequired(action_id)
        behavior = self.behaviors.get(tool, lambda args: f"{tool}:{args.get('path')}")
        text = behavior(dict(params))  # 在鎖外執行，讓並行波能真的並行
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
