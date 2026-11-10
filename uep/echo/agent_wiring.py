"""agent 工具路徑的組裝（切片 C3）：記憶工具 executor、回合結果暫存、``AgentToolTurns`` 與 ``AgentThinkStage`` 的綁定。

呼叫端（組裝層）需要：
1. ``make_memory_executor(memory, roster, audience_ok)`` 取得 executor 與結果暫存，
   並把 executor 以 ``MEMORY_EXECUTOR`` 為鍵放進建構 ``TaskManager`` 的 ``executors``；
2. ``attach_agent_tools(think, tasks, cache)`` 把 ``AgentToolTurns`` 綁到 ``AgentThinkStage``。
"""
from __future__ import annotations

import contextlib
import logging
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from uep.adapters.tool import CAPABILITY_CEILING
from uep.agent.bridge import AgentToolTurns
from uep.agent.budget import LoopBudget
from uep.agent.loop_context import ParentSnapshot
from uep.agent.trigger import LoopTrigger
from uep.decision.capability import (
    HINT_SOURCE_RULE,
    narrow_capability,
    record_narrow,
    route_base,
)
from uep.ids import new_id
from uep.memory.protocol import MemoryPort
from uep.output.delivery import DeliveryStatus
from uep.output.intent import OutputIntent, OutputKind, OutputOrigin
from uep.tasks.manager import TaskManager, TaskPolicy
from uep.tasks.states import InterruptPolicy
from uep.tools.catalog.memory import memory_registry
from uep.tools.executors.memory import MemoryToolExecutor, RosterPort
from uep.tools.gateway import ToolGateway
from uep.tools.outcomes import TurnOutcomeCache
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry
from uep.turn.models import TurnEndFacts
from uep.turn.orchestrator import attempt_label

if TYPE_CHECKING:  # legacy 組裝不得載入 uep.agent.loop（test_loop_wiring 鎖定），所以執行期延後 import
    from uep.agent.loop import AgentLoop


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


# ───────────────────────── production 入口（DL-4 PR4） ─────────────────────────

DEFAULT_LOOP_SYSTEM_TEXT = "你是使用者的長期 AI 助理。需要時使用工具查詢記憶，最後以簡短自然的繁體中文回覆。"
_DELIVERY_REFUSED = frozenset({DeliveryStatus.REJECTED, DeliveryStatus.CANCELLED, DeliveryStatus.FAILED})


def make_capability_tags_for(
    registry: ToolRegistry, metrics: Counter[str] | None = None,
) -> Callable[[Any, Any], frozenset[str]]:
    """``LoopEntry.capability_tags_for`` 的 production 實作：全部交給決策層的 ``narrow_capability``（wiring 不自己算交集）。

    ``narrow_capability(base=route_base(route), hint=proposal.capability_hint, snapshot_tags=registry.enabled_tags(), known=閉集)``。
    ``Proposal`` 目前沒有 hint 來源欄位，一律以 ``rule`` 處理（模型 hint 的 D4-6 丟棄邏輯要等來源欄位才會生效）。
    結果為空（含 route 表外、快照缺失）-> 回傳空集合，``LoopEntry`` 不啟動；降級計數與 reason_code 由 ``record_narrow`` 記錄。"""
    sink: Counter[str] = metrics if metrics is not None else Counter()

    def tags_for(ticket: Any, proposal: Any) -> frozenset[str]:
        route = getattr(proposal, "route", None)
        result = narrow_capability(
            route_base(route), getattr(proposal, "capability_hint", None), registry.enabled_tags(),
            CAPABILITY_CEILING.keys(), route=route, hint_source=HINT_SOURCE_RULE,
        )
        record_narrow(result, sink, ref=getattr(ticket, "turn_id", None))
        return result.tags

    return tags_for


class LoopLauncher:
    """``LoopEntry.launch`` 的 production 實作：同步建立 Task／Run（失敗 -> 例外 -> 回合退回 direct_chat），
    再把 ``AgentLoop.run`` 丟到 worker 執行緒（不阻塞 turn）。

    交付只有一條路：``AgentLoop`` 的 ``deliver_result`` -> ``OutputIntent(kind=text)`` -> ``DeliveryManager``（Output Gate）。
    宿主在 turn 被認領（``orchestrator.loop_claimed``）後不啟動 ES 生成，所以同一輸入只會有一次對外輸出。
    worker 結束時負責 turn 的終結事件（completed／cancelled／failed），否則 turn 與 Interaction 軸會殘留。"""

    def __init__(
        self, *, tasks: TaskManager, delivery: Any, orchestrator: Any, registry: ToolRegistry, model_port: Any,
        cache: TurnOutcomeCache | None = None, store: Any | None = None, persona_id: str = "uep",
        persona_version: int = 1, policy_version: int = 1, budget: LoopBudget | None = None,
        system_text: Callable[[Any], str] = lambda ticket: DEFAULT_LOOP_SYSTEM_TEXT,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._tasks = tasks
        self._delivery = delivery
        self._orchestrator = orchestrator
        self._registry = registry
        self._port = model_port
        self._cache = cache
        if store is None:
            from uep.fakes.loop_context import (
                InMemoryLoopContextStore,  # 尚無 production store（session_store 未接）：行程內暫存
            )

            store = InMemoryLoopContextStore()
        self._store = store
        self._persona_id = persona_id
        self._persona_version = persona_version
        self._policy_version = policy_version
        self._budget = budget or LoopBudget(run_deadline_s=300.0)
        self._system_text = system_text
        self._pool = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="uep-agent-loop")

    def __call__(self, trigger: LoopTrigger, ticket: Any) -> object:
        _task, run = create_loop_run(
            self._tasks, trigger, persona_id=self._persona_id, persona_version=self._persona_version,
            policy_version=self._policy_version,
        )
        self._pool.submit(self._work, trigger, ticket, run)
        return run

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---- worker ----

    def _work(self, trigger: LoopTrigger, ticket: Any, run: Any) -> None:
        turn_id = ticket.turn_id
        facts = TurnEndFacts(status="failed", reason_code="loop_failed")
        try:
            scope = self._cache.turn_scope(turn_id) if self._cache is not None else contextlib.nullcontext()
            with scope:
                outcome = self._build_loop(ticket, run).run(
                    trigger, run.run_id,
                    parent=ParentSnapshot(parent_id=ticket.conversation_id or ticket.segment_id, revision=0),
                    system_text=self._system_text(ticket), trigger_text=ticket.user_text,
                    persona_version=self._persona_version,
                )
            cancel = self._orchestrator.cancel_reason_of(turn_id)
            if outcome.delivered:
                facts = TurnEndFacts(
                    status="completed", generation_ok=True, generated_len=len(outcome.text), spoken_text=outcome.text,
                    snapshot_version=ticket.snapshot_version,
                )
            elif cancel is not None or outcome.state == "cancelled":
                facts = TurnEndFacts(status="cancelled", reason_code=cancel or "loop_cancelled")
            else:
                facts = TurnEndFacts(status="failed", reason_code=f"loop_{outcome.reason_code or outcome.state}")
        except Exception:
            logger.exception("AgentLoop worker 失敗：turn=%s", turn_id)
        finally:
            if self._cache is not None:
                self._cache.discard_turn(turn_id)
            try:
                self._orchestrator.on_turn_ended(turn_id, facts)
            except Exception:
                logger.exception("AgentLoop 結束 turn 失敗：turn=%s", turn_id)

    def _build_loop(self, ticket: Any, run: Any) -> AgentLoop:
        from uep.agent.loop import AgentLoop, LoopSnapshots
        from uep.agent.runtime import ModelPortRuntime

        snapshots = LoopSnapshots()
        registry = self._registry
        context = ProjectionContext(audience=ticket.audience_scope)

        def projector(tags: frozenset[str], prior: frozenset[str] | None) -> Any:
            return registry.project(tags, context, prior=prior)

        return AgentLoop(
            runtime=ModelPortRuntime(self._port, snapshots.take, continue_after_tools=True, max_rounds=50),
            tasks=self._tasks, gateway=ToolGateway(registry), registry=registry, store=self._store, projector=projector,
            budget=self._budget, worker_epoch=run.worker_epoch, snapshots=snapshots,
            injected={"actor_id": ticket.actor_id, "audience": ticket.audience_scope, "segment_id": ticket.segment_id},
            cancelled=lambda: self._orchestrator.cancel_reason_of(ticket.turn_id) is not None,
            deliver_result=lambda text: self._deliver(ticket, run, text),
            content_lookup=self._cache.text if self._cache is not None else None,
            run_cleanup=self._cache.discard_about_refs_run if self._cache is not None else None,
        )

    def _deliver(self, ticket: Any, run: Any, text: str) -> bool:
        """Run 的最終文字 -> Output Gate（``DeliveryManager``）。被拒、失敗都回 False（Run 結果不標已交付）。"""
        a = ticket.auth
        intent = OutputIntent(
            output_id=new_id("out"), sequence=0, kind=OutputKind.TEXT, origin=OutputOrigin.PERSONA,
            persona_id=a.persona_id, endpoint_id=a.endpoint_id, lease_id=a.lease_id, fencing_epoch=a.fencing_epoch,
            core_epoch=a.core_epoch, presence_generation=a.presence_generation, turn_id=ticket.turn_id,
            turn_generation=ticket.turn_generation, attempt_id=attempt_label(1), audience_scope=ticket.audience_scope,
            content_ref=f"loop:{ticket.turn_id}:{run.run_id}", valid_until=ticket.valid_until,
        )
        try:
            self._delivery.prepare(intent)
            record = self._delivery.dispatch(intent)
        except Exception:
            logger.exception("AgentLoop 交付失敗：turn=%s", ticket.turn_id)
            return False
        return record.status not in _DELIVERY_REFUSED


def build_loop_entry(
    *, tool_path: str, tasks: TaskManager, delivery: Any, orchestrator: Any, registry: ToolRegistry, model_port: Any,
    cache: TurnOutcomeCache | None, persona_id: str = "uep", policy_ref: str = "pol_agent_loop", **launcher_kwargs: Any,
) -> LoopEntry:
    """組裝層用：production 的 ``LoopEntry``（capability 收窄 + ``LoopLauncher``）。只在 ``tool_path == "agent"`` 可建。
    ``owner_for`` 取 ticket 的 ``actor_id``（與 agent 路徑的回合 Task 一致；現有記憶工具不需核准）。"""
    launcher = LoopLauncher(
        tasks=tasks, delivery=delivery, orchestrator=orchestrator, registry=registry, model_port=model_port,
        cache=cache, persona_id=persona_id, **launcher_kwargs,
    )
    return LoopEntry(
        tool_path, capability_tags_for=make_capability_tags_for(registry, orchestrator.metrics), launch=launcher,
        policy_ref=policy_ref, owner_for=lambda ticket: ticket.actor_id,
    )
