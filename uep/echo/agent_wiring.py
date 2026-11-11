"""agent 工具路徑的組裝（切片 C3）：記憶工具 executor、回合結果暫存、``AgentToolTurns`` 與 ``AgentThinkStage`` 的綁定。

呼叫端（組裝層）需要：
1. ``make_memory_executor(memory, roster, audience_ok)`` 取得 executor 與結果暫存，
   並把 executor 以 ``MEMORY_EXECUTOR`` 為鍵放進建構 ``TaskManager`` 的 ``executors``；
2. ``attach_agent_tools(think, tasks, cache)`` 把 ``AgentToolTurns`` 綁到 ``AgentThinkStage``。
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from uep.echo.loop_text import LoopTextStore
from uep.ids import new_id
from uep.memory.protocol import MemoryPort
from uep.output.delivery import DeliveryStatus
from uep.output.intent import OutputIntent, OutputKind, OutputOrigin
from uep.tasks.manager import StaleWorkerEpoch, TaskManager, TaskPolicy
from uep.tasks.states import TERMINAL_STATUSES, InterruptPolicy, RunStatus
from uep.tasks.supervisor import DEFAULT_DEADLINE_GRACE
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
    interrupt_policy: InterruptPolicy = InterruptPolicy.INTERRUPTIBLE, run_deadline: datetime | None = None,
) -> tuple[Any, Any]:
    """為 ``LoopTrigger`` 建立 Task 與 Run（RUNNING，worker_epoch 由 Run 帶出），回傳 ``(task, run)``。
    ``run_deadline`` 給了就是 Supervisor 的收尾期限（worker 消失時由它 force 收尾）；回合 Task（D62）不帶 deadline，與此無關。"""
    task = tasks.create_task(
        persona_id=persona_id, goal=trigger.goal_code, owner_actor_id=trigger.owner_actor_id, scope_ref=trigger.scope_ref,
        policy=TaskPolicy(presence_bound=False, interrupt_policy=interrupt_policy, policy_ref=trigger.policy_ref),
        origin_ref={"kind": trigger.kind, "ref": trigger.origin_ref},
    )
    try:
        extra = {} if run_deadline is None else {"run_deadline": run_deadline}  # 沒給期限就維持原呼叫形狀
        run = tasks.start_run(task.task_id, persona_version=persona_version, policy_version=policy_version, **extra)
    except BaseException:
        # Task 已建、Run 沒建成：回收 Task，不留孤兒（再讓呼叫端看到原例外）
        try:
            tasks.transition_task(task.task_id, RunStatus.FAILED, reason_code="loop_start_failed")
        except Exception:
            logger.exception("回收未啟動的 Task 失敗：task=%s", task.task_id)
        raise
    return task, run


# ───────────────────────── production 入口（DL-4 PR4） ─────────────────────────

DEFAULT_LOOP_SYSTEM_TEXT = "你是使用者的長期 AI 助理。需要時使用工具查詢記憶，最後以簡短自然的繁體中文回覆。"
# 交付白名單：只有「已送出」類狀態算 delivered（端點已收下並渲染；SENT 是 transport_sent 證據）。
# 其餘一律不算：REJECTED／CANCELLED／FAILED／SUPPRESSED（確定沒送出）、GENERATED／SYNTHESIZED（沒進到送出）。
_DELIVERED = frozenset({DeliveryStatus.SENT, DeliveryStatus.ACCEPTED, DeliveryStatus.RENDERED_PLAYED})
# 不確定是否已送達：不補發失敗訊息（避免使用者看到兩則）。UNKNOWN＝送出結果未明；ENQUEUED＝沒搶到送出權
# （``DeliveryManager.dispatch`` 的 ``claim.won`` 為 False，代表另一個送出者持有送出權，它可能正在送），所以同樣不補發。
_DELIVERY_UNCERTAIN = {
    DeliveryStatus.UNKNOWN: "loop_delivery_unknown", DeliveryStatus.ENQUEUED: "loop_delivery_pending",
}
LOOP_PARKED_REASON = "loop_parked_awaiting_approval"
# loop 失敗時給使用者的固定系統訊息：不經 LLM、不以角色身分發出，只說這次處理失敗（不得編出看似成功的回答）
LOOP_FAILURE_TEXT = "這次處理失敗了，沒有產生回覆。"
DEFAULT_MAX_CONCURRENT_LOOPS = 4
DEFAULT_CLOSE_TIMEOUT_S = 5.0
FALLBACK_RUN_DEADLINE_S = 600.0  # 自訂預算沒有牆鐘上限時，Supervisor 收尾期限用的保底值


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


class LoopLaunchRefused(RuntimeError):
    """入口明確拒絕啟動（並發上限、投影為空、關閉中）。orchestrator 看 ``loop_refused``：不算故障，回合退回 direct_chat。"""

    loop_refused = True


@dataclass
class _RunCtx:
    """一個被認領的 turn 對應的 Run 狀態（worker 與 deliver_result 共用）。"""

    ticket: Any
    task_id: str
    run: Any
    delivered: bool = False  # 角色回覆已確實交付（Gate 放行且端點收下）
    delivery_uncertain: str | None = None  # 送出結果不確定時的 reason_code：不補發失敗訊息（避免使用者看到兩則）
    cancel_requested: bool = False  # 心跳讀到 Supervisor 的取消旗標（SV-2）
    text: str = ""


class LoopLauncher:
    """``LoopEntry.launch`` 的 production 實作。

    - 啟動：先檢查並發上限與投影（任一不成立 -> ``LoopLaunchRefused``，回合退回 direct_chat），再同步建立 Task／Run，
      最後起 daemon worker 執行緒跑 ``AgentLoop``（不阻塞 turn）。建 Run 之後的啟動失敗會回收 Task／Run 再上拋。
    - 交付只有一條路：``deliver_result`` -> ``OutputIntent`` -> ``DeliveryManager``（Output Gate）-> 端點依 ``content_ref``
      解析文字並渲染。宿主在 turn 被認領後不啟動 ES 生成，所以同一輸入只有一次對外輸出。
    - 失敗：Run／Task 以同一 ``worker_epoch`` 收尾為 FAILED（原因碼具體）；使用者端經 Output Gate 收到固定的系統訊息
      （``origin=system``、``kind=system_notice``，不經 LLM、不以角色身分發出，只說這次處理失敗）。
    - worker 結束時負責 turn 的終結事件與暫存清理（文字、結果暫存、child context）。
    - ``close()``（組裝層關機時於入口停止段呼叫）設取消旗標並限時等待 worker。"""

    def __init__(
        self, *, tasks: TaskManager, delivery: Any, orchestrator: Any, registry: ToolRegistry, model_port: Any,
        text_store: LoopTextStore | None = None, cache: TurnOutcomeCache | None = None, store: Any | None = None,
        persona_id: str = "uep", persona_version: int = 1, policy_version: int = 1, budget: LoopBudget | None = None,
        system_text: Callable[[Any], str] = lambda ticket: DEFAULT_LOOP_SYSTEM_TEXT,
        max_concurrent: int | None = None, close_timeout_s: float | None = None,
        executors: Mapping[str, Any] | None = None, platform_source: Callable[[], str | None] | None = None,
    ) -> None:
        # 當下 Presence 平台的來源（Host 持有的 Lease／location）；未注入或回 None = 不帶場域，fail-closed 只投 global 工具
        self._platform_source = platform_source
        # executor 名稱 -> ToolExecutor：``AgentLoop`` 對 parallel_one_shot 逾時／取消呼叫 ``executor.cancel`` 用（組裝層已檢查齊備）
        self._executors = dict(executors) if executors is not None else {}
        max_concurrent = DEFAULT_MAX_CONCURRENT_LOOPS if max_concurrent is None else max_concurrent
        if max_concurrent < 1:
            raise ValueError("max_concurrent 必須 >= 1")
        self._tasks = tasks
        self._delivery = delivery
        self._orchestrator = orchestrator
        self._registry = registry
        self._port = model_port
        self._texts = text_store
        self._cache = cache
        if store is None:
            from uep.fakes.loop_context import (
                InMemoryLoopContextStore,  # 尚無 production store（session_store 未接）：行程內暫存，Run 終態時清理
            )

            store = InMemoryLoopContextStore()
        self._store = store
        self._persona_id = persona_id
        self._persona_version = persona_version
        self._policy_version = policy_version
        self._budget = budget or LoopBudget(run_deadline_s=300.0)
        self._system_text = system_text
        self._max = max_concurrent
        self._close_timeout_s = DEFAULT_CLOSE_TIMEOUT_S if close_timeout_s is None else close_timeout_s
        # Supervisor 的收尾期限 = 迴圈自己的牆鐘上限 + grace（grace 取 Supervisor 的預設 deadline_grace）：
        # 正常情況迴圈自己先在 Step 邊界收尾，期限只是 worker 消失時的持久收尾者
        self._deadline_grace = DEFAULT_DEADLINE_GRACE
        self._lock = threading.Lock()
        self._active = 0
        self._threads: set[threading.Thread] = set()
        self._closing = threading.Event()

    # ---- 啟動 ----

    def __call__(self, trigger: LoopTrigger, ticket: Any) -> object:
        with self._lock:
            if self._closing.is_set():
                raise LoopLaunchRefused("loop_closing")
            if self._active >= self._max:
                raise LoopLaunchRefused("loop_busy")
            self._active += 1  # 先占名額，建 Task／Run 之前就會被擋下（不排隊）
        try:
            capability_set = self._registry.project(trigger.capability_tags, self._projection_context(ticket))
            if not capability_set["tools"]:
                raise LoopLaunchRefused("capability_projection_empty")  # 例如全部 warming：不啟動，回 direct_chat
            task, run = create_loop_run(
                self._tasks, trigger, persona_id=self._persona_id, persona_version=self._persona_version,
                policy_version=self._policy_version, run_deadline=self._run_deadline(),
            )
        except BaseException:
            self._release()
            raise
        ctx = _RunCtx(ticket, task.task_id, run)
        thread = threading.Thread(
            target=self._work, args=(trigger, ctx), name=f"uep-agent-loop-{ticket.turn_id}", daemon=True,
        )
        with self._lock:
            self._threads.add(thread)
        try:
            thread.start()
        except BaseException:
            self._settle(ctx, ok=False, reason="loop_start_failed")  # 剛建的 Task／Run 回收，再讓回合退回 direct_chat
            self._forget(thread)
            self._release()
            raise
        return run

    def _run_deadline(self) -> datetime:
        seconds = self._budget.run_deadline_s if self._budget.run_deadline_s is not None else FALLBACK_RUN_DEADLINE_S
        return datetime.now(UTC) + timedelta(seconds=seconds) + self._deadline_grace

    def _heartbeat(self, ctx: _RunCtx, stop: threading.Event) -> None:
        """背景心跳：等模型回應期間迴圈不會送心跳，Supervisor 的 stale 判定（T-12）需要它。間隔取預算的心跳間隔與
        ``heartbeat_stale_after`` 的三分之一的較小者。worker 結束（或被 force、epoch 變了）就停；讀到取消旗標就通知迴圈。"""
        stale = getattr(self._tasks, "heartbeat_stale_after", timedelta(seconds=60))
        interval = max(0.05, min(self._budget.heartbeat_interval_s, stale.total_seconds() / 3))
        while not stop.wait(interval) and not self._closing.is_set():
            try:
                signal = self._tasks.heartbeat(ctx.run.run_id, worker_epoch=ctx.run.worker_epoch)
            except StaleWorkerEpoch:
                return
            except Exception:
                logger.exception("AgentLoop 背景心跳失敗：run=%s", ctx.run.run_id)
                continue
            if signal is not None:
                ctx.cancel_requested = True

    def close(self, timeout: float | None = None) -> None:
        """關機：不再接新 Run、通知進行中的迴圈取消（Step 邊界生效），限時等待 worker 收尾；逾時的 daemon 執行緒不阻擋關機。"""
        self._closing.set()
        deadline = time.monotonic() + (self._close_timeout_s if timeout is None else timeout)
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(t.is_alive() for t in threads):
            logger.warning("AgentLoop worker 在 %.1f 秒內未收尾（daemon，不阻擋關機）", self._close_timeout_s)

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def _release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def _forget(self, thread: threading.Thread) -> None:
        with self._lock:
            self._threads.discard(thread)

    # ---- worker ----

    def _cancelled(self, turn_id: str) -> bool:
        orch = self._orchestrator
        return self._closing.is_set() or orch.is_ended(turn_id) or orch.cancel_reason_of(turn_id) is not None

    def _work(self, trigger: LoopTrigger, ctx: _RunCtx) -> None:
        ticket, run = ctx.ticket, ctx.run
        turn_id = ticket.turn_id
        facts = TurnEndFacts(status="failed", reason_code="loop_failed")
        hb_stop = threading.Event()
        threading.Thread(target=self._heartbeat, args=(ctx, hb_stop), name=f"uep-loop-hb-{turn_id}", daemon=True).start()
        try:
            outcome = None
            raised = False
            try:
                scope = self._cache.turn_scope(turn_id) if self._cache is not None else contextlib.nullcontext()
                with scope:
                    outcome = self._build_loop(ctx).run(
                        trigger, run.run_id,
                        parent=ParentSnapshot(parent_id=ticket.conversation_id or ticket.segment_id, revision=0),
                        system_text=self._system_text(ticket), trigger_text=ticket.user_text,
                        persona_version=self._persona_version,
                    )
            except Exception:
                raised = True
                logger.exception("AgentLoop worker 失敗：turn=%s", turn_id)
            facts = self._conclude(ctx, outcome, raised)
        except Exception:
            logger.exception("AgentLoop 收尾失敗：turn=%s", turn_id)
        finally:
            hb_stop.set()
            if self._cache is not None:
                self._cache.discard_turn(turn_id)
            if self._texts is not None:
                self._texts.discard_turn(turn_id)
            discard = getattr(self._store, "discard_run", None)
            if callable(discard):
                discard(run.run_id)  # Run 終態：child／parent 暫存清理，不得無限增長
            # 名額先釋放、再終結 turn：turn 終結（使用者看到完成）之後立刻到來的新 turn 不該被舊 Run 的名額誤擋；
            # 執行緒追蹤留到最後，shutdown 仍會等 on_turn_ended 完成
            self._release()
            try:
                self._orchestrator.on_turn_ended(turn_id, facts)
            except Exception:
                logger.exception("AgentLoop 結束 turn 失敗：turn=%s", turn_id)
            self._forget(threading.current_thread())

    def _conclude(self, ctx: _RunCtx, outcome: Any, raised: bool) -> TurnEndFacts:
        """Run 收尾（Run／Task 狀態、使用者端的最後一則訊息）-> turn 終結事實。"""
        ticket = ctx.ticket
        parked = outcome is not None and getattr(outcome, "state", None) == "awaiting_approval" and not ctx.delivered
        if parked:
            # park 的 Run 保留（核准路徑屬切片 E），不在這裡收掉；目前 park 後沒有交付路徑，切片 E 之後補，所以不發失敗訊息
            reason = LOOP_PARKED_REASON
        else:
            code = getattr(outcome, "reason_code", None)
            code = getattr(code, "value", code)
            reason = "loop_exception" if raised else f"loop_{code or getattr(outcome, 'state', 'failed')}"
            self._settle(ctx, ok=ctx.delivered, reason=reason)
        if ctx.delivered:
            return TurnEndFacts(
                status="completed", generation_ok=True, generated_len=len(ctx.text), spoken_text=ctx.text,
                snapshot_version=ticket.snapshot_version,
            )
        if self._cancelled(ticket.turn_id):
            return TurnEndFacts(
                status="cancelled", reason_code=self._orchestrator.cancel_reason_of(ticket.turn_id) or "loop_cancelled",
            )
        if parked:
            return TurnEndFacts(status="failed", reason_code=LOOP_PARKED_REASON)
        if ctx.delivery_uncertain is not None:
            return TurnEndFacts(status="failed", reason_code=ctx.delivery_uncertain)  # 可能已送達：不再補發
        self._notify_failure(ctx)
        return TurnEndFacts(status="failed", reason_code=reason)

    def _settle(self, ctx: _RunCtx, *, ok: bool, reason: str) -> None:
        """用同一 ``worker_epoch`` 把未終態的 Run 轉 FAILED，並終結 Task（reason_code 具體）。
        epoch 已變（Supervisor／resume 接手）-> 不動，交給接手者。每一步獨立容錯。"""
        run = ctx.run
        try:
            row = self._tasks.get_run(run.run_id)
        except Exception:
            logger.exception("讀取 Run 失敗：run=%s", run.run_id)
            return
        if row.worker_epoch != run.worker_epoch:
            return
        status = row.status
        terminal = {s.value for s in TERMINAL_STATUSES}
        if status not in terminal:
            try:
                self._tasks.transition_run(run.run_id, RunStatus.FAILED, worker_epoch=run.worker_epoch)
                status = RunStatus.FAILED.value
            except Exception:
                logger.exception("Run 轉 FAILED 失敗：run=%s", run.run_id)
        succeeded = ok and status == RunStatus.SUCCEEDED.value
        try:
            self._tasks.transition_task(
                ctx.task_id, RunStatus.SUCCEEDED if succeeded else RunStatus.FAILED,
                reason_code=None if succeeded else reason[:64],
            )
        except Exception:
            logger.exception("Task 終結失敗：task=%s", ctx.task_id)

    def _projection_context(self, ticket: Any) -> ProjectionContext:
        """投影脈絡：audience 取自 ticket，platform 取自 Core 持有的 Presence 來源（不取自模型輸入）；來源失敗 fail-closed 為 None。"""
        platform: str | None = None
        if self._platform_source is not None:
            try:
                platform = self._platform_source()
            except Exception:
                logger.warning("讀取 Presence 平台失敗，投影不帶場域（fail-closed）", exc_info=True)
        return ProjectionContext.for_platform(platform, audience=getattr(ticket, "audience_scope", "private"))

    def _build_loop(self, ctx: _RunCtx) -> AgentLoop:
        from uep.agent.loop import AgentLoop, LoopSnapshots
        from uep.agent.runtime import ModelPortRuntime

        ticket, run = ctx.ticket, ctx.run
        snapshots = LoopSnapshots()
        registry = self._registry

        def projector(tags: frozenset[str], prior: frozenset[str] | None) -> Any:
            # 每一步重取平台：Loop 執行期間 Presence 轉移時，下一步的投影不再含原平台的限定工具
            return registry.project(tags, self._projection_context(ticket), prior=prior)

        return AgentLoop(
            runtime=ModelPortRuntime(self._port, snapshots.take, continue_after_tools=True, max_rounds=50),
            tasks=self._tasks, gateway=ToolGateway(registry), registry=registry, store=self._store, projector=projector,
            budget=self._budget, worker_epoch=run.worker_epoch, snapshots=snapshots,
            injected={"actor_id": ticket.actor_id, "audience": ticket.audience_scope, "segment_id": ticket.segment_id},
            cancelled=lambda: ctx.cancel_requested or self._cancelled(ticket.turn_id),
            deliver_result=lambda text: self._deliver(ctx, text),
            content_lookup=self._cache.text if self._cache is not None else None,
            run_cleanup=self._cache.discard_about_refs_run if self._cache is not None else None,
            executors=self._executors,
        )

    # ---- 交付（只經 Output Gate） ----

    def _deliver(self, ctx: _RunCtx, text: str) -> bool:
        """Run 的最終文字 -> Output Gate。被拒、失敗、結果未明（UNKNOWN）都回 False（Run 結果不標已交付）。"""
        if self._cancelled(ctx.ticket.turn_id):
            return False
        status = self._dispatch(ctx, text, kind=OutputKind.TEXT, origin=OutputOrigin.PERSONA)
        if status in _DELIVERED:
            ctx.delivered, ctx.text = True, text
            return True
        ctx.delivery_uncertain = _DELIVERY_UNCERTAIN.get(status) if status is not None else None
        return False

    def _notify_failure(self, ctx: _RunCtx) -> None:
        """失敗時給使用者的固定系統訊息：經同一條 Output Gate；origin=system，不是角色發言，也不編造看似成功的回答。"""
        try:
            self._dispatch(ctx, LOOP_FAILURE_TEXT, kind=OutputKind.SYSTEM_NOTICE, origin=OutputOrigin.SYSTEM)
        except Exception:
            logger.exception("AgentLoop 失敗訊息交付失敗：turn=%s", ctx.ticket.turn_id)

    def _dispatch(self, ctx: _RunCtx, text: str, *, kind: OutputKind, origin: OutputOrigin) -> DeliveryStatus | None:
        ticket, run = ctx.ticket, ctx.run
        if self._texts is None:
            logger.error("LoopLauncher 沒有文字暫存，無法交付：turn=%s", ticket.turn_id)
            return None
        a = ticket.auth
        output_id = new_id("out")
        ref = f"loop:{ticket.turn_id}:{run.run_id}:{output_id}"
        self._texts.put(ref, ticket.turn_id, text)
        intent = OutputIntent(
            output_id=output_id, sequence=0, kind=kind, origin=origin,
            persona_id=a.persona_id, endpoint_id=a.endpoint_id, lease_id=a.lease_id, fencing_epoch=a.fencing_epoch,
            core_epoch=a.core_epoch, presence_generation=a.presence_generation, turn_id=ticket.turn_id,
            turn_generation=ticket.turn_generation, attempt_id=attempt_label(1), audience_scope=ticket.audience_scope,
            content_ref=ref, valid_until=ticket.valid_until,
        )
        try:
            self._delivery.prepare(intent)
            record = self._delivery.dispatch(intent)
        except Exception:
            logger.exception("AgentLoop 交付失敗：turn=%s", ticket.turn_id)
            return None
        return record.status


def build_loop_entry(
    *, tool_path: str, tasks: TaskManager, delivery: Any, orchestrator: Any, registry: ToolRegistry, model_port: Any,
    cache: TurnOutcomeCache | None, text_store: LoopTextStore | None, persona_id: str = "uep",
    policy_ref: str = "pol_agent_loop", **launcher_kwargs: Any,
) -> tuple[LoopEntry, LoopLauncher]:
    """組裝層用：production 的 ``(LoopEntry, LoopLauncher)``（capability 收窄 + 啟動器）。只在 ``tool_path == "agent"`` 可建。
    launcher 另回傳是為了讓 ``CoreAssembly.shutdown`` 能關閉它。``owner_for`` 取 ticket 的 ``actor_id``
    （與 agent 路徑的回合 Task 一致；現有記憶工具不需核准）。"""
    launcher = LoopLauncher(
        tasks=tasks, delivery=delivery, orchestrator=orchestrator, registry=registry, model_port=model_port,
        text_store=text_store, cache=cache, persona_id=persona_id, **launcher_kwargs,
    )
    entry = LoopEntry(
        tool_path, capability_tags_for=make_capability_tags_for(registry, orchestrator.metrics), launch=launcher,
        policy_ref=policy_ref, owner_for=lambda ticket: ticket.actor_id,
    )
    return entry, launcher
