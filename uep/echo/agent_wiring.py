"""agent 工具路徑的組裝（切片 C3）：記憶工具 executor、回合結果暫存、``AgentToolTurns`` 與 ``AgentThinkStage`` 的綁定。

呼叫端（組裝層）需要：
1. ``make_memory_executor(memory, roster, audience_ok)`` 取得 executor 與結果暫存，
   並把 executor 以 ``MEMORY_EXECUTOR`` 為鍵放進建構 ``TaskManager`` 的 ``executors``；
2. ``attach_agent_tools(think, tasks, cache)`` 把 ``AgentToolTurns`` 綁到 ``AgentThinkStage``。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from uep.agent.bridge import AgentToolTurns
from uep.memory.protocol import MemoryPort
from uep.tasks.manager import TaskManager
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
