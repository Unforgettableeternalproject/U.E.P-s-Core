"""agent 工具路徑的組裝（切片 C3）：記憶工具 executor、回合結果暫存、``AgentToolTurns`` 與 ``AgentThinkStage`` 的綁定。

呼叫端（組裝層）需要：
1. ``make_memory_executor(memory, roster, audience_ok)`` 取得 executor 與結果暫存，
   並把 executor 以 ``MEMORY_EXECUTOR`` 為鍵放進建構 ``TaskManager`` 的 ``executors``；
2. ``attach_agent_tools(think, tasks, cache)`` 把 ``AgentToolTurns`` 綁到 ``AgentThinkStage``。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from uep.agent.bridge import AgentToolTurns
from uep.agent.trigger import LoopTrigger
from uep.memory.protocol import MemoryPort
from uep.tasks.manager import TaskManager, TaskPolicy
from uep.tasks.states import InterruptPolicy
from uep.tools.catalog.memory import memory_registry
from uep.tools.executors.memory import MemoryToolExecutor, RosterPort
from uep.tools.outcomes import TurnOutcomeCache


def make_memory_executor(
    memory: MemoryPort, roster: RosterPort, audience_ok: Callable[[str], bool] | None = None,
) -> tuple[MemoryToolExecutor, TurnOutcomeCache]:
    cache = TurnOutcomeCache()
    return MemoryToolExecutor(memory, roster, cache=cache, audience_ok=audience_ok), cache


def attach_agent_tools(think: Any, tasks: TaskManager, cache: TurnOutcomeCache, **kwargs: Any) -> AgentToolTurns:
    """把回合工具 Port 綁到 ``AgentThinkStage``；``think`` 不是 agent stage（沒有 ``bind_port``）時拋 TypeError。"""
    bind = getattr(think, "bind_port", None)
    if not callable(bind):
        raise TypeError("think 不是 AgentThinkStage：agent 路徑需要 UEP_TOOL_PATH=agent 組出的 stage")
    turns = AgentToolTurns(tasks, memory_registry(), cache, **kwargs)
    bind(turns)
    return turns


logger = logging.getLogger(__name__)
LOOP_GOAL_USER_TURN = "user_turn_request"


def trigger_from_turn(
    ticket: Any, proposal: Any, *, capability_tags: frozenset[str], policy_ref: str, owner_actor_id: str,
    goal_code: str = LOOP_GOAL_USER_TURN,
) -> LoopTrigger:
    """``route=agent_loop`` 的回合 -> ``LoopTrigger(kind="user_turn")``（§4.2）。``capability_tags`` 是決策層 I-1 已收窄的結果
    （D1 由呼叫端提供）；``owner_actor_id`` 由呼叫端依 D64 決定（非 owner 來源固定為艾斯維爾）。外部文字不進 goal。"""
    return LoopTrigger(
        kind="user_turn", origin_ref=ticket.turn_id, goal_code=goal_code, capability_tags=frozenset(capability_tags),
        scope_ref=ticket.audience_scope, owner_actor_id=owner_actor_id, policy_ref=policy_ref,
        snapshot_ref=f"snapshot_{ticket.snapshot_version}", plan_ref=None,
    )


class LoopEntry:
    """``TurnOrchestrator.bind_loop_entry`` 用的入口：``route=agent_loop`` -> ``LoopTrigger`` -> ``launch``。

    只能在 ``tool_path == "agent"`` 下建立（legacy 下建構即拒絕，所以 legacy 不可達）。``capability_tags_for`` 回傳空集合
    ⇒ 違反介面（決策層應已降為 direct_chat）：記錄後不啟動，回合照 direct_chat 處理。``launch`` 負責非同步化
    （建立 Task／Run、在 worker 執行緒跑 ``AgentLoop``），本入口不阻塞 turn。"""

    def __init__(
        self, tool_path: str, *, capability_tags_for: Callable[[Any, Any], frozenset[str]],
        launch: Callable[[LoopTrigger, Any], object], policy_ref: str, owner_for: Callable[[Any], str],
    ) -> None:
        if tool_path != "agent":
            raise ValueError("LoopEntry 只能在 UEP_TOOL_PATH=agent 下建立（legacy 不可達）")
        self._tags_for = capability_tags_for
        self._launch = launch
        self._policy_ref = policy_ref
        self._owner_for = owner_for

    def __call__(self, ticket: Any, proposal: Any) -> LoopTrigger | None:
        if getattr(proposal, "route", None) != "agent_loop":
            return None
        tags = frozenset(self._tags_for(ticket, proposal))
        if not tags:
            logger.error("route=agent_loop 但 capability_tags 為空（介面違規），不啟動迴圈：turn=%s", ticket.turn_id)
            return None
        trigger = trigger_from_turn(
            ticket, proposal, capability_tags=tags, policy_ref=self._policy_ref, owner_actor_id=self._owner_for(ticket),
        )
        self._launch(trigger, ticket)
        return trigger


def create_loop_run(
    tasks: TaskManager, trigger: LoopTrigger, *, persona_id: str, persona_version: int, policy_version: int,
    interrupt_policy: InterruptPolicy = InterruptPolicy.INTERRUPTIBLE,
) -> tuple[Any, Any]:
    """為 ``LoopTrigger`` 建立 Task 與 Run（RUNNING，worker_epoch 由 Run 帶出），回傳 ``(task, run)``。"""
    task = tasks.create_task(
        persona_id=persona_id, goal=trigger.goal_code, owner_actor_id=trigger.owner_actor_id, scope_ref=trigger.scope_ref,
        policy=TaskPolicy(presence_bound=False, interrupt_policy=interrupt_policy, policy_ref=trigger.policy_ref),
        origin_ref={"kind": trigger.kind, "ref": trigger.origin_ref},
    )
    run = tasks.start_run(task.task_id, persona_version=persona_version, policy_version=policy_version)
    return task, run
