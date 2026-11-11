"""``AgentToolTurns``：宿主對話迴圈的工具呼叫 -> ActionIntent（切片 C3，D62）。

實作 ``uep.stream.adapters.think_agent.ToolTurnPort``。模型呼叫仍在宿主的對話迴圈（``LLMThinkStage``）；這裡只負責
「一個工具呼叫」的 Core 端：

- 回合內第一次工具呼叫時 ``get_or_create_turn_task``（回合 Task 與唯一 Run），並開一個 Step；
- 每次呼叫走 ``AgentCore.run_intent``：Gateway（capability／schema／PD-12 注入）-> ``create_action``（``act_`` 確定性 id）->
  ``execute_action``（TC-3、receipt）-> 視圖；結果文字只從回合範圍的 ``TurnOutcomeCache`` 取（D33）；
- ``finish_turn``：先關閉回合（之後的呼叫一律拒絕），**有上限地**等在途的 ``run_tool`` 結束；逾時就把仍 EXECUTING 的
  action 走 TaskManager 的正規轉移標成 UNKNOWN，再 ``finish_turn_task`` 收尾（有未決 action 以 FAILED／
  ``unresolved_actions``），最後整批清掉該回合的結果暫存。

``action.*`` 事件由 TaskManager 發出（payload 沿用舊 Host 補記的 kind／status／ref_id，另有 action_id）。
鎖：全域鎖只保護回合表（不做 DB）；每個回合各一把鎖保護該回合的 DB 狀態與計數。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
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
DEFAULT_FINISH_TIMEOUT_S = 2.0  # 回合收尾等在途工具的上限（沿用工具層的短逾時；逾時即標 UNKNOWN）


@dataclass
class _Turn:
    turn_id: str
    cond: threading.Condition = field(default_factory=threading.Condition)  # 保護 inflight／closed／db
    inflight: int = 0
    closed: bool = False
    db: _TurnDb | None = None
    init_lock: threading.Lock = field(default_factory=threading.Lock)  # 每個回合一把：DB 交易不在全域鎖內


@dataclass
class _TurnDb:
    task_id: str
    run_id: str
    step_id: str
    epoch: int
    ordinal: int = 0
    action_ids: list[str] = field(default_factory=list)


class AgentToolTurns:
    def __init__(
        self, tasks: TaskManager, registry: ToolRegistry, cache: TurnOutcomeCache, *, persona_id: str = "uep",
        policy_ref: str = "pol_turn_tools", persona_version: int = 1, policy_version: int = 1,
        capability_tags: Iterable[str] = DIRECT_CHAT_TAGS, finish_timeout_s: float = DEFAULT_FINISH_TIMEOUT_S,
        platform_source: Callable[[], str | None] | None = None,
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
        self._finish_timeout_s = finish_timeout_s
        # 當下 Presence 平台的來源（Host 持有的 Lease／location）；未注入或回 None = 不帶場域，fail-closed 只投 global 工具
        self._platform_source = platform_source
        self._turns: dict[str, _Turn] = {}
        self._lock = threading.Lock()  # 只保護 _turns 這個 dict

    # ---- ToolTurnPort ----
    def run_tool(self, name: str, args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        turn_id = context.get("ticket_ref")
        if not isinstance(turn_id, str) or not turn_id:
            return {"success": False, "result": f"Tool {name!r} is not available."}
        with self._lock:
            turn = self._turns.setdefault(turn_id, _Turn(turn_id))
        with turn.cond:
            if turn.closed:  # 回合已結束（含收尾逾時之後才回來的呼叫）：拒絕，不建 action
                logger.warning("回合已收尾，拒絕工具呼叫：%s", name)
                return {"success": False, "result": "tool rejected: turn closed"}
            turn.inflight += 1
        try:
            return self._run_tool(turn, name, args, context)
        except Exception as exc:  # noqa: BLE001 - Port 永不拋；只記型別，不記內容
            logger.error("回合工具呼叫失敗：%s %s", name, type(exc).__name__)
            return {"success": False, "result": "tool failed"}
        finally:
            with turn.cond:
                turn.inflight -= 1
                turn.cond.notify_all()
                late = turn.closed
            if late:  # 收尾逾時之後才回來的呼叫：它寫進暫存的東西也要清掉
                self._cache.discard_turn(turn.turn_id)
                if turn.db is not None:
                    self._cache.discard_about_refs_run(turn.db.run_id)

    def finish_turn(self, turn_id: str) -> None:
        with self._lock:
            turn = self._turns.pop(turn_id, None)
        if turn is None:
            return  # 回合內沒有工具呼叫：不建 Task（懶建立）
        with turn.cond:
            turn.closed = True
            drained = turn.cond.wait_for(lambda: turn.inflight == 0, timeout=self._finish_timeout_s)
            db = turn.db
        if not drained:
            logger.error("回合收尾等不到在途工具（%.1fs），將其標為 UNKNOWN：turn=%s", self._finish_timeout_s, turn_id)
        try:
            if db is not None:
                try:
                    self._tasks.complete_step(db.step_id, worker_epoch=db.epoch, ok=drained)
                except Exception as exc:  # noqa: BLE001
                    logger.error("結束 Step 失敗：%s", type(exc).__name__)
                self._tasks.finish_turn_task(db.task_id, abandon_executing=not drained)
        finally:
            self._cache.discard_turn(turn_id)  # 整批清除：結果暫存與 receipt 查核表
            if db is not None:
                self._cache.discard_about_refs_run(db.run_id)  # Run（回合）終態：旁證編號一併清除

    # ---- 內部 ----
    def _db(self, turn: _Turn, context: dict[str, Any]) -> _TurnDb:
        with turn.init_lock:  # 該回合的 DB 初始化只做一次；其他回合不受影響
            if turn.db is not None:
                return turn.db
            audience = context.get("audience")
            task, run = self._tasks.get_or_create_turn_task(
                turn.turn_id, persona_id=self._persona_id,
                owner_actor_id=str(context.get("actor_id") or "unknown"),
                scope_ref=str(context.get("event_scope") or audience),
                policy_ref=self._policy_ref, persona_version=self._persona_version,
                policy_version=self._policy_version,
                conversation_id=context["conversation_id"] if isinstance(context.get("conversation_id"), str) else None,
                segment_id=context["segment_id"] if isinstance(context.get("segment_id"), str) else None,
            )
            step = self._tasks.begin_step(run.run_id, "tools", worker_epoch=run.worker_epoch)
            db = _TurnDb(task.task_id, run.run_id, step.step_id, run.worker_epoch)
            with turn.cond:
                turn.db = db
            return db

    def _current_platform(self) -> str | None:
        """當下 Presence 平台；來源失敗 fail-closed 為 None（不信任模型輸入或 context 以外的值）。"""
        if self._platform_source is None:
            return None
        try:
            return self._platform_source()
        except Exception:
            logger.warning("讀取 Presence 平台失敗，投影不帶場域（fail-closed）", exc_info=True)
            return None

    def _run_tool(self, turn: _Turn, name: str, args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        audience = context.get("audience")
        if not isinstance(audience, str) or not audience:
            return {"success": False, "result": f"Tool {name!r} is not available."}
        db = self._db(turn, context)
        available = frozenset({GUILD_ROSTER_CONTEXT}) if context.get("guild_id") else frozenset()
        capability_set = self._registry.project(
            self._tags, ProjectionContext.for_platform(
                self._current_platform(), available_context=available, audience=audience,
            ),
        )
        injected = {k: context[k] for k in INJECTED_CONTEXT_KEYS if k in context}
        injected["run_id"] = db.run_id  # 旁證編號的範圍（Run）；經注入值帶入，不靠執行緒區域
        core = AgentCore(
            None, self._tasks, self._gateway, worker_epoch=db.epoch, content_lookup=self._cache.text,
        )
        with turn.cond:
            ordinal = db.ordinal
            db.ordinal += 1
        with self._cache.turn_scope(turn.turn_id):  # executor 在本執行緒內同步執行，結果依 turn 索引
            record, view = core.run_intent(
                run_id=db.run_id, step_id=db.step_id, ordinal=ordinal, capability_set=capability_set,
                tool=name, args=args, injected=injected,
            )
        if record.action_id:
            with turn.cond:
                if record.action_id not in db.action_ids:
                    db.action_ids.append(record.action_id)
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
