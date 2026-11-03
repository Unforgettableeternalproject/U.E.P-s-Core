"""``AgentToolTurns``：宿主對話迴圈的工具呼叫 -> ActionIntent（切片 C3，D62）。

實作 ``uep.stream.adapters.think_agent.ToolTurnPort``。模型呼叫仍在宿主的對話迴圈（``LLMThinkStage``）；這裡只負責
「一個工具呼叫」的 Core 端：

- 回合內第一次工具呼叫時 ``get_or_create_turn_task``（回合 Task 與唯一 Run），並開一個 Step；
- 每次呼叫走 ``AgentCore.run_intent``：Gateway（capability／schema／PD-12 注入）-> ``create_action``（``act_`` 確定性 id）->
  ``execute_action``（TC-3、receipt）-> 視圖；結果文字只從回合範圍的 ``TurnOutcomeCache`` 取（D33）；
- ``finish_turn``：結束 Step、``finish_turn_task``（有 UNKNOWN 以 FAILED 收尾）並清掉該回合的結果暫存。

``action.*`` 事件由 TaskManager 發出（payload 沿用舊 Host 補記的 kind／status／ref_id，另有 action_id）。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from uep.agent.core import AgentCore
from uep.tasks.manager import TaskManager
from uep.tools.catalog.memory import GUILD_ROSTER_CONTEXT
from uep.tools.gateway import ToolGateway
from uep.tools.outcomes import TurnOutcomeCache
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

INJECTED_CONTEXT_KEYS = ("actor_id", "audience", "segment_id", "guild_id")
DIRECT_CHAT_TAGS = frozenset({"memory.read"})  # Q-E：direct_chat 僅唯讀


@dataclass
class _TurnState:
    task_id: str
    run_id: str
    step_id: str
    epoch: int
    ordinal: int = 0
    action_ids: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class AgentToolTurns:
    def __init__(
        self, tasks: TaskManager, registry: ToolRegistry, cache: TurnOutcomeCache, *, persona_id: str = "uep",
        policy_ref: str = "pol_turn_tools", persona_version: int = 1, policy_version: int = 1,
        capability_tags: Iterable[str] = DIRECT_CHAT_TAGS,
    ) -> None:
        self._tasks = tasks
        self._registry = registry
        self._gateway = ToolGateway(registry)
        self._cache = cache
        self._persona_id = persona_id
        self._policy_ref = policy_ref
        self._persona_version = persona_version
        self._policy_version = policy_version
        self._tags = frozenset(capability_tags)
        self._turns: dict[str, _TurnState] = {}
        self._lock = threading.Lock()

    # ---- ToolTurnPort ----
    def run_tool(self, name: str, args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._run_tool(name, args, context)
        except Exception as exc:  # noqa: BLE001 - Port 永不拋；只記型別，不記內容
            logger.error("回合工具呼叫失敗：%s %s", name, type(exc).__name__)
            return {"success": False, "result": "tool failed"}

    def finish_turn(self, turn_id: str) -> None:
        with self._lock:
            state = self._turns.pop(turn_id, None)
        if state is None:
            return  # 回合內沒有工具呼叫：不建 Task（懶建立）
        try:
            self._tasks.complete_step(state.step_id, worker_epoch=state.epoch, ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("結束 Step 失敗：%s", type(exc).__name__)
        try:
            self._tasks.finish_turn_task(state.task_id)
        finally:
            self._cache.discard(state.action_ids)

    # ---- 內部 ----
    def _state(self, turn_id: str, context: dict[str, Any]) -> _TurnState:
        with self._lock:
            state = self._turns.get(turn_id)
            if state is not None:
                return state
            audience = context.get("audience")
            task, run = self._tasks.get_or_create_turn_task(
                turn_id, persona_id=self._persona_id,
                owner_actor_id=str(context.get("actor_id") or "unknown"),
                scope_ref=str(context.get("event_scope") or audience),
                policy_ref=self._policy_ref, persona_version=self._persona_version,
                policy_version=self._policy_version,
                conversation_id=context.get("conversation_id") if isinstance(context.get("conversation_id"), str) else None,
                segment_id=context.get("segment_id") if isinstance(context.get("segment_id"), str) else None,
            )
            step = self._tasks.begin_step(run.run_id, "tools", worker_epoch=run.worker_epoch)
            state = _TurnState(task.task_id, run.run_id, step.step_id, run.worker_epoch)
            self._turns[turn_id] = state
            return state

    def _run_tool(self, name: str, args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        turn_id = context.get("ticket_ref")
        audience = context.get("audience")
        if not isinstance(turn_id, str) or not isinstance(audience, str) or not audience:
            return {"success": False, "result": f"Tool {name!r} is not available."}
        state = self._state(turn_id, context)
        available = frozenset({GUILD_ROSTER_CONTEXT}) if context.get("guild_id") else frozenset()
        capability_set = self._registry.project(
            self._tags, ProjectionContext(available_context=available, audience=audience)
        )
        injected = {k: context[k] for k in INJECTED_CONTEXT_KEYS if k in context}
        core = AgentCore(
            None, self._tasks, self._gateway, worker_epoch=state.epoch, content_lookup=self._cache.text,
        )
        with state.lock:
            ordinal = state.ordinal
            state.ordinal += 1
        record, view = core.run_intent(
            run_id=state.run_id, step_id=state.step_id, ordinal=ordinal, capability_set=capability_set,
            tool=name, args=args, injected=injected,
        )
        if record.action_id and record.action_id not in state.action_ids:
            state.action_ids.append(record.action_id)
        if view is None:
            return {"success": False, "result": "tool incomplete"}
        status = view.get("status")
        cached = self._cache.get(record.action_id) if record.action_id else None
        if status == "ok":
            text = "\n".join(
                str(item["content"]) for item in view.get("items", ()) if "content" in item
            )
            outcome: dict[str, Any] = {"success": True, "result": text}
            if cached and cached.get("shared_ids"):
                outcome["shared_ids"] = list(cached["shared_ids"])
            return outcome
        if status == "failed" and cached is not None:
            return {"success": False, "result": str(cached.get("result", "tool failed"))}
        reason = view.get("reason_code")
        return {"success": False, "result": f"tool {status}" + (f": {reason}" if isinstance(reason, str) else "")}
