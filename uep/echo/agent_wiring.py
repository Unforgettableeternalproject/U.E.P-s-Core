"""agent 工具路徑的組裝（切片 C3）：記憶工具 executor、回合結果暫存、``AgentToolTurns`` 與 ``AgentThinkStage`` 的綁定。

呼叫端（組裝層）需要：
1. ``make_memory_executor(memory, roster, audience_ok)`` 取得 executor 與結果暫存，
   並把 executor 以 ``MEMORY_EXECUTOR`` 為鍵放進建構 ``TaskManager`` 的 ``executors``；
2. ``attach_agent_tools(think, tasks, cache)`` 把 ``AgentToolTurns`` 綁到 ``AgentThinkStage``。
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from uep.adapters.tool import CAPABILITY_CEILING
from uep.agent.bridge import AgentToolTurns
from uep.agent.budget import BudgetPolicyTable, LoopBudget, resolve_budget, tighten
from uep.agent.loop_context import ParentSnapshot
from uep.agent.retry import RetryPolicy
from uep.agent.trigger import BudgetRequest, LoopTrigger
from uep.decision.capability import (
    HINT_SOURCE_RULE,
    narrow_capability,
    record_narrow,
    route_base,
)
from uep.echo.loop_text import LoopTextStore
from uep.echo.resume_return import (
    ResumeCause,
    ResumeDisposition,
    ResumedRunResult,
    ResumeOutcome,
)
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
from uep.turn.orchestrator import REASON_SHUTDOWN, RETRACT_REASONS, attempt_label

if TYPE_CHECKING:  # legacy 組裝不得載入 uep.agent.loop（test_loop_wiring 鎖定），所以執行期延後 import
    from uep.agent.loop import AgentLoop


def make_memory_executor(
    memory: MemoryPort, roster: RosterPort, audience_ok: Callable[[str], bool] | None = None,
) -> tuple[MemoryToolExecutor, TurnOutcomeCache]:
    cache = TurnOutcomeCache()
    return MemoryToolExecutor(memory, roster, cache=cache, audience_ok=audience_ok), cache


def attach_agent_tools(
    think: Any, tasks: TaskManager, cache: TurnOutcomeCache, registry: ToolRegistry | None = None, **kwargs: Any,
) -> AgentToolTurns:
    """把回合工具 Port 綁到 ``AgentThinkStage``；``think`` 不是 agent stage（沒有 ``bind_port``）時拋 TypeError。
    ``registry`` 應為組裝層組出、與 TaskManager 同一份的註冊表；未給時退回 ``tasks.tool_resolver``（是 ToolRegistry 才用），再退回記憶註冊表。"""
    bind = getattr(think, "bind_port", None)
    if not callable(bind):
        raise TypeError("think 不是 AgentThinkStage：agent 路徑需要 UEP_TOOL_PATH=agent 組出的 stage")
    if registry is None:
        resolver = getattr(tasks, "tool_resolver", None)
        registry = resolver if isinstance(resolver, ToolRegistry) else memory_registry()
    kwargs.setdefault("capability_tags", route_base("direct_chat") or frozenset())  # direct_chat 標籤取自決策層 route 表
    turns = AgentToolTurns(tasks, registry, cache, **kwargs)
    bind(turns)
    return turns


logger = logging.getLogger(__name__)
LOOP_GOAL_USER_TURN = "user_turn_request"


DEFAULT_LOOP_POLICY_REF = "pol_agent_loop"
LOOP_GOAL_TASK_REQUEST = "task_request"
# route=task_request 的能力標籤（Chain 契約 PR-R／E3：lore.write、chatroom.post，不含 memory.write）。決策層的 route 表
# （``ROUTE_DEFAULT_CAPABILITIES["task_request"]``）目前仍是空集合，待決策層改表後這份常數即可移除，
# 現在由工具層以自己的 base 呼叫 ``narrow_capability``（仍受註冊表快照與閉集約束，不放寬）
TASK_REQUEST_CAPABILITIES: frozenset[str] = frozenset({"lore.write", "chatroom.post"})
# task_request 的緊預算組：兩步（一次工具、一次收束）、一次工具呼叫、不平行、不 consult；政策表鍵 (policy_ref, "task_request", "lite")
TASK_REQUEST_BUDGET = LoopBudget(max_steps=2, max_tool_calls=1, max_parallel=1, max_consults=0, run_deadline_s=300.0)
TASK_REQUEST_TIER = "lite"

# route -> turn 被取消／結束時 Run 的處置（Agent 提案 §4.5、Chain 契約 2.4）：barge-in 取消的是 turn 的「說」，不必然取消 Run。
# ``cancel``＝Run 隨 turn 取消（現狀）；``continue``＝暫停語音、Run 繼續，結果延後經 Host 入口通知。這是 turn 層的政策，
# 與 Task 的 ``InterruptPolicy``（in-flight action 對 stop 的可中斷程度）無關，後者不變。
ROUTE_INTERRUPT_POLICY: dict[str, str] = {"agent_loop": "cancel", "task_request": "continue"}
# 即使 route 是 continue，這些原因仍取消 Run：撤回歷史類（被拒、撤銷、停止）與 shutdown；其餘（插話讓位、結束）不取消
HARD_CANCEL_REASONS: frozenset[str] = RETRACT_REASONS | {REASON_SHUTDOWN}


def task_request_budget_table(policy_ref: str = DEFAULT_LOOP_POLICY_REF) -> BudgetPolicyTable:
    """assembly 注入 launcher 的政策表：目前只登記 task_request 的緊預算組；agent_loop 查不到列，退回 launcher 預設預算。"""
    return BudgetPolicyTable({(policy_ref, "task_request", TASK_REQUEST_TIER): TASK_REQUEST_BUDGET})


def trigger_from_turn(
    ticket: Any, proposal: Any, *, capability_tags: frozenset[str], policy_ref: str, owner_actor_id: str,
    goal_code: str | None = None, route: str = "agent_loop",
) -> LoopTrigger:
    """``route=agent_loop`` 的回合 -> ``LoopTrigger(kind="user_turn")``（§4.2）；``route=task_request`` -> ``kind="task_request"``、
    預算 tier=lite。``capability_tags`` 是決策層 I-1 已收窄的結果（D1 由呼叫端提供）；``owner_actor_id`` 由呼叫端依 D64 決定
    （非 owner 來源固定為艾斯維爾）。外部文字不進 goal。"""
    task = route == "task_request"
    return LoopTrigger(
        kind="task_request" if task else "user_turn", origin_ref=ticket.turn_id,
        goal_code=goal_code or (LOOP_GOAL_TASK_REQUEST if task else LOOP_GOAL_USER_TURN),
        capability_tags=frozenset(capability_tags), scope_ref=ticket.audience_scope, owner_actor_id=owner_actor_id,
        policy_ref=policy_ref, snapshot_ref=f"snapshot_{ticket.snapshot_version}", plan_ref=None,
        route="task_request" if task else "agent_loop", budget=BudgetRequest(tier=TASK_REQUEST_TIER) if task else None,
    )


class LoopEntry:
    """``TurnOrchestrator.bind_loop_entry`` 用的入口：``route=agent_loop`` -> ``LoopTrigger`` -> ``launch``。

    只能在 ``tool_path == "agent"`` 下建立（legacy 下建構即拒絕，所以 legacy 不可達）。``capability_tags_for`` 回傳空集合
    ⇒ 違反介面（決策層應已降為 direct_chat）：記錄後不啟動，回合照 direct_chat 處理。``launch`` 負責非同步化
    （建立 Task／Run、在 worker 執行緒跑 ``AgentLoop``），本入口不阻塞 turn。"""

    def __init__(
        self, tool_path: str, *, capability_tags_for: Callable[[Any, Any], frozenset[str]],
        launch: Callable[[LoopTrigger, Any], object], policy_ref: str, owner_for: Callable[[Any], str],
        metrics: Counter[str] | None = None,
    ) -> None:
        self._metrics = metrics
        if tool_path != "agent":
            raise ValueError("LoopEntry 只能在 UEP_TOOL_PATH=agent 下建立（legacy 不可達）")
        self._tags_for = capability_tags_for
        self._launch = launch
        self._policy_ref = policy_ref
        self._owner_for = owner_for

    def __call__(self, ticket: Any, proposal: Any) -> LoopTrigger | None:
        route = getattr(proposal, "route", None)
        if route not in ("agent_loop", "task_request"):
            return None
        tags = frozenset(self._tags_for(ticket, proposal))
        if not tags:
            if route == "task_request":
                # 工具層沒有提醒／轉告類工具時的預期情況（降回 direct_chat），不是介面違規
                logger.warning("route=task_request 但 capability_tags 為空，不啟動迴圈：turn=%s", ticket.turn_id)
                if self._metrics is not None:
                    self._metrics["turn.task_request_capability_empty"] += 1
            else:
                logger.error("route=%s 但 capability_tags 為空（介面違規），不啟動迴圈：turn=%s", route, ticket.turn_id)
            return None
        trigger = trigger_from_turn(
            ticket, proposal, capability_tags=tags, policy_ref=self._policy_ref, owner_actor_id=self._owner_for(ticket),
            route=route,
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
        origin_ref={"kind": trigger.kind, "ref": trigger.origin_ref, "route": trigger.route},
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
_GATE_REFUSED = frozenset({DeliveryStatus.REJECTED, DeliveryStatus.CANCELLED})  # Gate 拒絕（turn 世代作廢等）後的終態
_DELIVERY_UNCERTAIN = {
    DeliveryStatus.UNKNOWN: "loop_delivery_unknown", DeliveryStatus.ENQUEUED: "loop_delivery_pending",
}
LOOP_PARKED_REASON = "loop_parked_awaiting_approval"
# loop 失敗時給使用者的固定系統訊息：不經 LLM、不以角色身分發出，只說這次處理失敗（不得編出看似成功的回答）
LOOP_FAILURE_TEXT = "這次處理失敗了，沒有產生回覆。"
# 三段門檻的固定系統訊息（BG-7：程式常數、不經 LLM、origin=SYSTEM、不進 plan 與日誌）
BUDGET_NOTICE_TEXT = "這件事花得比預期多，我還在處理，再多一點就會停下來。"
BUDGET_ABORT_TEXT = "這件事超出了預算，我先停下來了。"
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
        base = TASK_REQUEST_CAPABILITIES if route == "task_request" else route_base(route)
        result = narrow_capability(
            base, getattr(proposal, "capability_hint", None), registry.enabled_tags(),
            CAPABILITY_CEILING.keys(), route=route, hint_source=HINT_SOURCE_RULE,
        )
        record_narrow(result, sink, ref=getattr(ticket, "turn_id", None))
        return result.tags

    return tags_for


def with_extra_capability_tags(
    tags_for: Callable[[Any, Any], frozenset[str]], extra: frozenset[str], registry: ToolRegistry,
) -> Callable[[Any, Any], frozenset[str]]:
    """在決策層收窄**之後**併入額外標籤（測試工具組 ``UEP_TOOL_TESTKIT`` 用；決策層的 route 表不含這些標籤）。

    只在 ``route=agent_loop`` 且收窄結果非空時併入（空即不啟動的 fail-closed 不被繞過）；併入的標籤限 ``CAPABILITY_CEILING`` 閉集
    且必須是註冊表目前有工具的標籤（``registry.enabled_tags()``），所以不會憑空放寬或帶出沒有工具的標籤。"""

    def wrapped(ticket: Any, proposal: Any) -> frozenset[str]:
        tags = frozenset(tags_for(ticket, proposal))
        if not tags or getattr(proposal, "route", None) != "agent_loop":
            return tags
        return tags | (extra & registry.enabled_tags() & CAPABILITY_CEILING.keys())

    return wrapped


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
    budget: Any = None  # 此 Run 的有效預算（建立時凍結；park／resume 沿用）
    activity_id: str | None = None  # Task 的 activity：續跑回覆以它綁定送出（D107）
    cause: ResumeCause = ResumeCause.APPROVED
    route: str = "agent_loop"  # 這個 Run 的 route（abort 的 metric 依 route 分開）
    late_seq: int = 0  # turn 結束後經 Host 入口送的訊息序號（0 留給最終結果）
    failure_notice_sent: bool = False  # 固定失敗訊息只送一次（不論走 Gate 或 Host 入口）
    aborting: bool = False  # 預算 abort 已開始：之後交付的文字是 partial，不可當完整回覆送
    abort_notice_ok: bool = False  # 預算中斷的固定訊息已送出（或結果不確定而不補發）；沒送出時 _conclude 補送失敗訊息
    text: str = ""
    resumed: bool = False  # 核准後續跑的 Run：原 turn 已結束，取消判定不看 turn 終結狀態，結果不再經 Output Gate


@dataclass(frozen=True)
class ParkedLoop:
    """停在 ``WAITING_APPROVAL`` 的 Run：續跑所需的記憶體端狀態（Task／Run 本身與 checkpoint 在 DB）。"""

    ticket: Any
    task_id: str
    trigger: LoopTrigger
    parent_id: str
    budget: Any = None  # 凍結的有效預算（resume 沿用；重新解析更緊時取 min）
    activity_id: str | None = None


class LoopResumeRefused(RuntimeError):
    """``LoopLauncher.resume`` 無法接手（不是 park 中的 Run、關閉中、並發上限、TaskManager 拒絕接手…）。"""


@dataclass(frozen=True)
class LoopResumeResult:
    """續跑的結果摘要（``state`` 同 ``LoopOutcome.state``；``awaiting_approval`` 表示再次 park，Run 仍可續跑）。"""

    run_id: str
    state: str
    text: str = ""
    reason_code: str | None = None
    disposition: ResumeDisposition | None = None  # 續跑結果回 Host 的處置；未接 Host（on_resumed=None）或再次 park 時為 None


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
        memory_store: Callable[[Any, TurnEndFacts], None] | None = None, retry: RetryPolicy | None = None,
        step_timeout_s: float | None = None, budget_policies: BudgetPolicyTable | None = None,
        on_resumed: Callable[[ResumedRunResult], ResumeDisposition] | None = None,
        resume_cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        # 續跑中的 Run 的額外取消條件（Q7）：Host 的 ``resume_should_cancel``（Lease 遺失或 global_stop）；None = 只看 cancel_requested／closing
        self._resume_cancel_check = resume_cancel_check
        # 續跑的 Run 到達終態時回 Host 的入口（``EchoPipelineHost.accept_resumed_result``）；None = 退回舊行為（只回 LoopResumeResult）
        self._on_resumed = on_resumed
        # 預算門檻事件的決策日誌記錄器 (run_id, task_id, kind)；由組裝層在日誌啟用時以 set_budget_recorder 掛上，None = 不記
        self._budget_recorder: Callable[[str, str | None, str], None] | None = None
        self._retry = retry  # None = 不自動重試（組裝層依 UEP_AGENT_* 傳入）
        # 記憶寫入（與 direct_chat 同一條路徑：Host 的 ``store_turn_memory``）；None = 不寫（例如未接記憶的組裝）
        self._memory_store = memory_store
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
                InMemoryLoopContextStore,  # 未注入 store（測試或持久 store 不可用）時的行程內暫存，Run 終態時清理
            )

            store = InMemoryLoopContextStore()
        self._store = store
        self._persona_id = persona_id
        self._persona_version = persona_version
        self._policy_version = policy_version
        self._budget = budget or LoopBudget(run_deadline_s=300.0)
        if step_timeout_s is not None:  # 組裝層依 UEP_AGENT_STEP_TIMEOUT_S 套進預算（其餘欄位沿用）
            self._budget = dataclasses.replace(self._budget, step_timeout_s=step_timeout_s)
        self._budget_policies = budget_policies  # None = 不查政策表，所有 Run 用上面的預設預算（與先前相同）
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
        self._parked: dict[str, ParkedLoop] = {}  # run_id -> park 中的 Run（核准後由 ``resume`` 接手）

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
            effective = self._effective_budget(trigger)
            task, run = create_loop_run(
                self._tasks, trigger, persona_id=self._persona_id, persona_version=self._persona_version,
                policy_version=self._policy_version, run_deadline=self._run_deadline(effective),
            )
        except BaseException:
            self._release()
            raise
        ctx = _RunCtx(
            ticket, task.task_id, run, budget=effective, activity_id=getattr(task, "activity_id", None), route=trigger.route,
        )
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

    def _effective_budget(self, trigger: LoopTrigger) -> LoopBudget:
        """Run 的有效預算：政策表（查不到退回預設）∩ trigger 的 cap，只收窄。Run 建立時凍結。"""
        request = trigger.budget
        out = resolve_budget(
            self._budget_policies, self._budget, policy_ref=trigger.policy_ref, route=trigger.route,
            tier=request.tier if request is not None else None, cap=request.cap if request is not None else None,
            metrics=getattr(self._orchestrator, "metrics", None),
        )
        if trigger.route == "task_request" and (
            self._budget_policies is None
            or self._budget_policies.lookup(trigger.policy_ref, trigger.route, (request.tier if request else None) or "standard") is None
        ):
            # 查不到 task_request 的政策列：不退回寬鬆的預設預算（fail-closed），改取預設與緊預算組的逐維度較緊者
            logger.warning("task_request 查不到預算政策列，改用緊預算組：policy_ref=%s", trigger.policy_ref)
            metrics = getattr(self._orchestrator, "metrics", None)
            if metrics is not None:
                metrics["budget.task_request_policy_missing"] += 1
            try:
                out = tighten(out, TASK_REQUEST_BUDGET)
            except ValueError:
                out = TASK_REQUEST_BUDGET
        if out.step_timeout_s is None and self._budget.step_timeout_s is not None:
            out = dataclasses.replace(out, step_timeout_s=self._budget.step_timeout_s)  # 政策列沒設的 Step 逾時沿用 launcher 的
        return out

    def _run_deadline(self, budget: LoopBudget | None = None) -> datetime:
        budget = budget or self._budget
        seconds = budget.run_deadline_s if budget.run_deadline_s is not None else FALLBACK_RUN_DEADLINE_S
        return datetime.now(UTC) + timedelta(seconds=seconds) + self._deadline_grace

    def _heartbeat(self, ctx: _RunCtx, stop: threading.Event) -> None:
        """背景心跳：等模型回應期間迴圈不會送心跳，Supervisor 的 stale 判定（T-12）需要它。間隔取預算的心跳間隔與
        ``heartbeat_stale_after`` 的三分之一的較小者。worker 結束（或被 force、epoch 變了）就停；讀到取消旗標就通知迴圈。"""
        stale = getattr(self._tasks, "heartbeat_stale_after", timedelta(seconds=60))
        interval = max(0.05, min((ctx.budget or self._budget).heartbeat_interval_s, stale.total_seconds() / 3))
        while not stop.wait(interval) and not self._closing.is_set():
            try:
                signal = self._tasks.heartbeat(ctx.run.run_id, worker_epoch=ctx.run.worker_epoch)
            except StaleWorkerEpoch:
                return
            except Exception:
                logger.exception("AgentLoop 背景心跳失敗：run=%s", ctx.run.run_id)
                continue
            if signal is not None and getattr(signal, "reason", None) != "step_timeout":
                ctx.cancel_requested = True  # step_timeout 旗標只取消卡住的 Step，不取消 Run

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

    def _resume_should_cancel(self) -> bool:
        """續跑中的 Run 是否該取消：Host 回報 Lease 遺失或 global_stop（原 turn 已結束，沒有 live turn 可被 ``cancel_turn`` 取消）。
        TaskManager 的 ``action_guard`` 只在 Runtime STOPPING 或全域停止旗標成立時擋 action 進入，``presence_check`` 只管
        ``presence_bound`` 的 Task／action（loop 的 Task 不是），所以 Lease 遺失不會被 Manager 擋下，要靠這裡在 Step 邊界取消。"""
        check = self._resume_cancel_check
        if check is None:
            return False
        try:
            return bool(check())
        except Exception:
            logger.warning("續跑取消檢查失敗，視為不取消", exc_info=True)
            return False

    def _cancelled(self, turn_id: str, route: str = "agent_loop") -> bool:
        """Run 是否該取消。``agent_loop``（cancel）：turn 結束或被取消即取消（現狀）。``task_request``（continue）：插話讓位等
        軟性結束不取消，只有 closing 與撤回歷史類原因（global_stop／presence_lost／control_stop／被拒）及 shutdown 才取消。"""
        if self._closing.is_set():
            return True
        orch = self._orchestrator
        if ROUTE_INTERRUPT_POLICY.get(route, "cancel") == "continue":
            # turn 結束後 orchestrator 會移除它，cancel_reason_of 回 None，所以硬取消另外看 Host 的 Lease 遺失／global_stop
            # （與續跑中的 Run 同一套判斷）；turn 還在時的撤回歷史類原因照樣取消
            return self._resume_should_cancel() or orch.cancel_reason_of(turn_id) in HARD_CANCEL_REASONS
        return orch.is_ended(turn_id) or orch.cancel_reason_of(turn_id) is not None

    def _cleanup(self, turn_id: str, run_id: str) -> None:
        """Run 終態：回合結果暫存、文字暫存與 child／parent 暫存一併清理，不得無限增長。"""
        if self._cache is not None:
            self._cache.discard_turn(turn_id)
        if self._texts is not None:
            self._texts.discard_turn(turn_id)
        discard = getattr(self._store, "discard_run", None)
        if callable(discard):
            discard(run_id)

    def _work(self, trigger: LoopTrigger, ctx: _RunCtx) -> None:
        ticket, run = ctx.ticket, ctx.run
        turn_id = ticket.turn_id
        facts = TurnEndFacts(status="failed", reason_code="loop_failed")
        parked = False
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
            parked = facts.reason_code == LOOP_PARKED_REASON and facts.status == "failed"
            if parked:  # park 不是終態：保留 child／parent 暫存與回合結果暫存，核准後由 ``resume`` 接手
                self._parked[run.run_id] = ParkedLoop(
                    ticket, ctx.task_id, trigger, ticket.conversation_id or ticket.segment_id, ctx.budget,
                    ctx.activity_id,
                )
        except Exception:
            logger.exception("AgentLoop 收尾失敗：turn=%s", turn_id)
        finally:
            hb_stop.set()
            if not parked:
                self._cleanup(turn_id, run.run_id)
            # 與 direct_chat 一致：被拒／撤銷的輪不寫記憶；須在 on_turn_ended 之前取原因（終結後可能查不到）
            rejected = self._orchestrator.cancel_reason_of(turn_id) in RETRACT_REASONS
            # 名額先釋放、再終結 turn：turn 終結（使用者看到完成）之後立刻到來的新 turn 不該被舊 Run 的名額誤擋；
            # 執行緒追蹤留到最後，shutdown 仍會等 on_turn_ended 完成
            self._release()
            try:
                self._orchestrator.on_turn_ended(turn_id, facts)
            except Exception:
                logger.exception("AgentLoop 結束 turn 失敗：turn=%s", turn_id)
            if not rejected:
                self._store_memory(ticket, facts)
            self._forget(threading.current_thread())

    def _store_memory(self, ticket: Any, facts: TurnEndFacts) -> None:
        """route=agent_loop 的 turn 收尾寫記憶：沿用 Host 的寫入路徑（要求 generation_ok 且有 spoken_text，冪等鍵 ``turn:<id>``）。"""
        if self._memory_store is None or facts.partial:
            return  # 預算中斷的部分結果：記憶 store 沒有 partial 標記，這一輪先不寫（不當成完整回覆）
        try:
            self._memory_store(ticket, facts)
        except Exception:
            logger.exception("AgentLoop 記憶 store 失敗：turn=%s", ticket.turn_id)

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
            # 首跑：回覆就是該回合的結果，ok 看是否送達；continue 政策（task_request）的 turn 可能已被插話結束、結果延後經 Host
            # 入口送出（PENDING／UNCERTAIN），這時 Task 狀態與送達解耦，只看 Run 是否 completed（與 _work_resume 一致）
            if self._continues(ctx):
                ok = not raised and getattr(outcome, "state", None) == "completed"
            else:
                ok = ctx.delivered
            self._settle(ctx, ok=ok, reason=reason)
        if getattr(outcome, "partial", False):
            # 預算中斷（FAILED＋partial）：不是完整回覆，Turn 不記成 completed，保留原因碼；部分結果沒送出（或中斷通知沒送出）時補固定失敗訊息
            # 交付狀態不確定（UNKNOWN／ENQUEUED）時可能已送達，不補送失敗訊息（避免使用者看到兩則）
            needs_notice = not (ctx.delivered and ctx.abort_notice_ok) and ctx.delivery_uncertain is None
            if needs_notice and not self._cancelled(ticket.turn_id, ctx.route):
                self._notify_failure(ctx)
            return TurnEndFacts(
                status="failed", reason_code=reason, generated_len=len(ctx.text) if ctx.delivered else 0,
                spoken_text=ctx.text if ctx.delivered else "", snapshot_version=ticket.snapshot_version, partial=True,
            )
        if ctx.delivered:
            return TurnEndFacts(
                status="completed", generation_ok=True, generated_len=len(ctx.text), spoken_text=ctx.text,
                snapshot_version=ticket.snapshot_version,
            )
        if self._cancelled(ticket.turn_id, ctx.route):
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
            budget=ctx.budget or self._budget, worker_epoch=run.worker_epoch, snapshots=snapshots,
            on_budget=lambda kind: self._on_budget(ctx, kind),
            injected={"actor_id": ticket.actor_id, "audience": ticket.audience_scope, "segment_id": ticket.segment_id},
            cancelled=(
                (lambda: ctx.cancel_requested or self._closing.is_set() or self._resume_should_cancel()) if ctx.resumed
                else (lambda: ctx.cancel_requested or self._cancelled(ticket.turn_id, ctx.route))
            ),
            deliver_result=(
                (lambda text: self._capture(ctx, text)) if ctx.resumed else (lambda text: self._deliver(ctx, text))
            ),
            content_lookup=self._cache.text if self._cache is not None else None,
            run_cleanup=self._cache.discard_about_refs_run if self._cache is not None else None,
            executors=self._executors, retry=self._retry,
        )

    # ---- 交付（只經 Output Gate） ----

    def _deliver(self, ctx: _RunCtx, text: str) -> bool:
        """Run 的最終文字 -> Output Gate。被拒、失敗、結果未明（UNKNOWN）都回 False（Run 結果不標已交付）。"""
        turn_id = ctx.ticket.turn_id
        if self._cancelled(turn_id, ctx.route):
            return False
        if self._turn_over(ctx):
            return self._deliver_after_turn_end(ctx, text)  # 回合已被插話結束、Run 繼續到完成：結果延後經 Host 入口通知
        status = self._dispatch(ctx, text, kind=OutputKind.TEXT, origin=OutputOrigin.PERSONA)
        if status in _DELIVERED:
            ctx.delivered, ctx.text = True, text
            return True
        if self._continues(ctx) and status in _GATE_REFUSED and self._turn_inactive(turn_id):
            return self._deliver_after_turn_end(ctx, text)  # 檢查之後 turn 才被結束（TOCTOU）：Gate 以 turn 世代拒絕，改走 Host 入口
        ctx.delivery_uncertain = _DELIVERY_UNCERTAIN.get(status) if status is not None else None
        return False

    def _continues(self, ctx: _RunCtx) -> bool:
        return ROUTE_INTERRUPT_POLICY.get(ctx.route, "cancel") == "continue"

    def _turn_inactive(self, turn_id: str) -> bool:
        """turn 已結束，或已被取消但還沒 ended（取消已讓 generation 前進，經 Gate 的 turn 綁定輸出同樣必被拒）。"""
        orch = self._orchestrator
        return orch.is_ended(turn_id) or orch.cancel_reason_of(turn_id) is not None

    def _turn_over(self, ctx: _RunCtx) -> bool:
        """continue 政策的 Run，其 turn 已失效（結束或已被取消）：經 Gate 的 turn 綁定輸出必被拒，要改走 Host 入口。"""
        return self._continues(ctx) and self._turn_inactive(ctx.ticket.turn_id)

    def _notice_via_host(self, ctx: _RunCtx, text: str) -> DeliveryStatus | None:
        """turn 結束後的固定系統訊息（預算通知、失敗訊息）經 Host 入口送出（outcome=FAILED -> SYSTEM_NOTICE，不寫記憶）。
        回傳對應的 DeliveryStatus 供呼叫端沿用既有的判斷；無法送（沒有入口或被丟棄）回 None。"""
        if self._on_resumed is None:
            return None
        run = ctx.run
        ctx.late_seq += 1
        result = ResumedRunResult(
            run_id=run.run_id, worker_epoch=run.worker_epoch, task_id=ctx.task_id, activity_id=ctx.activity_id,
            ticket=ctx.ticket, outcome=ResumeOutcome.FAILED, cause=ResumeCause.APPROVED, text=text, seq=ctx.late_seq,
        )
        try:
            disposition = self._on_resumed(result)
        except Exception:
            logger.exception("turn 結束後的系統訊息送出失敗：run=%s", run.run_id)
            return None
        return {
            ResumeDisposition.DELIVERED: DeliveryStatus.SENT, ResumeDisposition.PENDING: DeliveryStatus.ENQUEUED,
            ResumeDisposition.UNCERTAIN: DeliveryStatus.UNKNOWN,
        }.get(disposition)

    def _send_system_notice(self, ctx: _RunCtx, text: str) -> DeliveryStatus | None:
        """固定系統訊息（預算通知、失敗訊息）：一般走 Gate；continue 政策且 turn 已結束（或檢查後才結束，Gate 拒絕）改走 Host 入口。"""
        if self._turn_over(ctx):
            return self._notice_via_host(ctx, text)
        status = self._dispatch(ctx, text, kind=OutputKind.SYSTEM_NOTICE, origin=OutputOrigin.SYSTEM)
        if self._continues(ctx) and status in _GATE_REFUSED and self._turn_inactive(ctx.ticket.turn_id):
            return self._notice_via_host(ctx, text)
        return status

    def _deliver_after_turn_end(self, ctx: _RunCtx, text: str) -> bool:
        """原 turn 已結束，turn 世代作廢、Gate 必拒；改走續跑回覆的 Host 入口（activity 綁定、送出當下重取 Lease，可進 pending）。
        沒有注入入口（``on_resumed`` 為 None）時無處可送，回 False。DELIVERED 才算已交付；UNCERTAIN／PENDING 標記為
        不確定，避免 ``_conclude`` 再補一則失敗訊息。"""
        if self._on_resumed is None:
            return False
        if ctx.aborting:
            # 預算中斷的 partial 不可當完整回覆送（也不寫記憶）：改送固定失敗訊息（outcome=FAILED），只送一次
            if not ctx.failure_notice_sent:
                ctx.failure_notice_sent = True
                if not ctx.abort_notice_ok:  # 中斷通知已送出（或結果不確定）就不再送第二則，使用者最後只收到一則
                    self._notice_via_host(ctx, LOOP_FAILURE_TEXT)
            return False
        run = ctx.run
        result = ResumedRunResult(
            run_id=run.run_id, worker_epoch=run.worker_epoch, task_id=ctx.task_id, activity_id=ctx.activity_id,
            ticket=ctx.ticket, outcome=ResumeOutcome.COMPLETED, cause=ResumeCause.APPROVED, text=text,
        )
        try:
            disposition = self._on_resumed(result)
        except Exception:
            logger.exception("task_request 延後交付失敗：run=%s", run.run_id)
            ctx.delivery_uncertain = "loop_delivery_unknown"
            return False
        if disposition is ResumeDisposition.DELIVERED:
            ctx.delivered, ctx.text = True, text
            return True
        if disposition is ResumeDisposition.PENDING:
            ctx.delivery_uncertain = "loop_delivery_pending"
        elif disposition is ResumeDisposition.UNCERTAIN:
            ctx.delivery_uncertain = "loop_delivery_unknown"
        return False

    @staticmethod
    def _capture(ctx: _RunCtx, text: str) -> bool:
        """核准後續跑的最終文字：原 turn 早已結束，不再經 Output Gate（Gate 以 turn 世代驗證，必被拒）；文字由 ``resume`` 的
        結果回給呼叫端（Console 印出）。送到使用者端的路徑待 Host／平台的非同步回覆通道。"""
        ctx.delivered, ctx.text = True, text
        return True

    # ---- 核准後續跑（提案 §8.2、E1） ----

    def parked_runs(self) -> tuple[str, ...]:
        """目前 park 中、可續跑的 Run。"""
        with self._lock:
            return tuple(self._parked)

    def resume(
        self, run_id: str, approvals: Mapping[str, str] | None = None, *, cause: ResumeCause = ResumeCause.APPROVED,
    ) -> Future[LoopResumeResult]:
        """接手 park 中的 Run 並在背景續跑（Console、日後的 Host 與平台共用的公開入口）。

        ``approvals``：``action_id -> approval_id``（Approval 通道已記錄的核准；缺核准＝再次 park；否決請先
        ``TaskManager.record_rejection`` 再以空 approvals 呼叫，模型會看到 rejected）。接手走 ``TaskManager.resume_run``
        （新 worker_epoch；park 已清心跳所以可立即接手），重驗（tool_version、approval、能力在凍結集合內）全由
        ``AgentLoop.resume`` 與 Manager 判定，這裡不放寬。回傳的 Future 在續跑結束時給 ``LoopResumeResult``；
        再次 park 時 Run 仍留在 ``parked_runs``。無法接手（非 park 中、關閉中、並發上限、Manager 拒絕）立即拋 ``LoopResumeRefused``。"""
        with self._lock:
            entry = self._parked.get(run_id)
            if entry is None:
                raise LoopResumeRefused(f"Run 不在 park 狀態：{run_id}")
            if self._closing.is_set():
                raise LoopResumeRefused("loop_closing")
            if self._active >= self._max:
                raise LoopResumeRefused("loop_busy")
            self._active += 1
        try:
            plan = self._tasks.resume_run(run_id)
        except Exception as exc:
            self._release()
            raise LoopResumeRefused(f"Manager 拒絕接手：{type(exc).__name__}：{exc}") from exc
        frozen = entry.budget or self._budget
        try:
            frozen = tighten(frozen, self._effective_budget(entry.trigger))  # 沿用凍結值；重新解析更緊時取 min（只收窄）
        except ValueError:
            logger.warning("resume 預算收窄失敗，沿用凍結值：run=%s", run_id)
        ctx = _RunCtx(
            entry.ticket, entry.task_id, plan.run, resumed=True, budget=frozen, activity_id=entry.activity_id, cause=cause,
            route=entry.trigger.route,
        )
        future: Future[LoopResumeResult] = Future()
        thread = threading.Thread(
            target=self._work_resume, args=(entry, ctx, dict(plan.checkpoint or {}), dict(approvals or {}), future),
            name=f"uep-agent-resume-{run_id}", daemon=True,
        )
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return future

    def _work_resume(
        self, entry: ParkedLoop, ctx: _RunCtx, checkpoint: dict[str, Any], approvals: dict[str, str],
        future: Future[LoopResumeResult],
    ) -> None:
        run_id, turn_id = ctx.run.run_id, ctx.ticket.turn_id
        hb_stop = threading.Event()
        threading.Thread(target=self._heartbeat, args=(ctx, hb_stop), name=f"uep-loop-hb-{turn_id}", daemon=True).start()
        outcome = None
        raised = False
        state, code = "failed", None
        disposition: ResumeDisposition | None = None
        try:
            scope = self._cache.turn_scope(turn_id) if self._cache is not None else contextlib.nullcontext()
            with scope:
                outcome = self._build_loop(ctx).resume(
                    run_id, checkpoint, trigger=entry.trigger, parent_id=entry.parent_id, approvals=approvals,
                )
        except Exception:
            raised = True
            logger.exception("AgentLoop 續跑失敗：run=%s", run_id)
        finally:
            hb_stop.set()
            if outcome is not None:
                state = str(outcome.state)
                code = getattr(outcome.reason_code, "value", outcome.reason_code)
            try:
                if state != "awaiting_approval":  # 再次 park 時保留；其餘為終態：收尾並清理
                    reason = "loop_exception" if raised else f"loop_{code or state}"
                    # Q4：續跑路徑的 Task 狀態與送達解耦——SUCCEEDED 只代表工作完成（工具副作用已發生）；文字有沒有送達使用者由
                    # disposition 表示。首跑路徑（``_conclude``）仍用 ``ok=ctx.delivered``，兩者刻意不對稱：首跑的回覆就是該回合的結果
                    self._settle(ctx, ok=state == "completed", reason=reason)
                    disposition = self._return_to_host(ctx, state, code, raised)  # 文字暫存清理之前（端點送出時要解析 content_ref）
                    with self._lock:
                        self._parked.pop(run_id, None)
                    self._cleanup(turn_id, run_id)
            finally:
                self._release()
                self._forget(threading.current_thread())
                future.set_result(LoopResumeResult(
                    run_id, state, ctx.text, None if code is None else str(code), disposition,
                ))

    def _return_to_host(self, ctx: _RunCtx, state: str, code: Any, raised: bool) -> ResumeDisposition | None:
        """續跑終態 -> Host 入口（送出與寫記憶都在 Host 內，launcher 不自己拼 OutputIntent）。``on_resumed`` 為 None 回 None。
        冪等鍵 ``(run_id, worker_epoch)`` 由 Host 判定；epoch 已被別人接手（Supervisor／再次 resume）時，舊 worker 不呼叫。"""
        if self._on_resumed is None:
            return None
        run = ctx.run
        try:
            row = self._tasks.get_run(run.run_id)
            if row.worker_epoch != run.worker_epoch:
                return None
        except Exception:
            logger.exception("讀取 Run 失敗，不回 Host：run=%s", run.run_id)
            return None
        if raised or state == "failed":
            outcome = ResumeOutcome.FAILED
        elif state == "completed":
            outcome = ResumeOutcome.COMPLETED
        else:
            outcome = ResumeOutcome.CANCELLED
        # FAILED（含預算中斷的 partial）送固定的失敗訊息（不經 LLM、SYSTEM_NOTICE），部分結果文字不送；CANCELLED 不送
        text = ctx.text if outcome is ResumeOutcome.COMPLETED else (LOOP_FAILURE_TEXT if outcome is ResumeOutcome.FAILED else "")
        result = ResumedRunResult(
            run_id=run.run_id, worker_epoch=run.worker_epoch, task_id=ctx.task_id, activity_id=ctx.activity_id,
            ticket=ctx.ticket, outcome=outcome, cause=ctx.cause, text=text, reason_code=None if code is None else str(code),
        )
        try:
            return self._on_resumed(result)
        except Exception:
            logger.exception("續跑結果回 Host 失敗：run=%s", run.run_id)
            return ResumeDisposition.UNCERTAIN

    def set_budget_recorder(self, recorder: Callable[[str, str | None, str], None] | None) -> None:
        """掛上預算門檻事件記錄器（決策日誌）；launcher 早於日誌寫入器建立，所以用事後掛載。"""
        self._budget_recorder = recorder

    def _on_budget(self, ctx: _RunCtx, kind: str) -> None:
        """三段門檻的通知：warn 只記錄（metric＋log）；notice／abort 送一次固定的 SYSTEM_NOTICE（經 Output Gate，
        不設 ``delivered``；送出結果不確定也不補發）。"""
        metrics = getattr(self._orchestrator, "metrics", None)
        if metrics is not None:
            is_abort = kind.startswith("abort")  # "abort"（2.0 倍輪數／token）與 "abort_tool_calls"（工具次數保險絲）共用通知文字
            metrics["budget.abort" if is_abort else f"budget.{kind}"] += 1
            if kind == "abort_tool_calls":
                metrics["budget.abort_tool_calls"] += 1
            if is_abort and ctx.route == "task_request":
                metrics["budget.abort.task_request"] += 1  # task_budget_exceeded 依 route 分開看
        recorder = self._budget_recorder
        if recorder is not None:  # 決策日誌記錄（只記事實，不含文字）；失敗不影響後續通知
            try:
                recorder(ctx.run.run_id, ctx.task_id, kind)
            except Exception:
                logger.exception("預算事件寫入決策日誌失敗：run=%s kind=%s", ctx.run.run_id, kind)
        if kind == "warn" or ctx.resumed:
            # warn 只記錄；續跑中的 Run 原 turn 已結束，Gate 必拒，只記 metric 不送
            logger.info("預算 %s：run=%s", kind, ctx.run.run_id)
            return
        text = BUDGET_NOTICE_TEXT if kind == "notice" else BUDGET_ABORT_TEXT
        if kind.startswith("abort"):
            ctx.aborting = True  # 之後經 deliver_result 交的是 partial
        try:
            status = self._send_system_notice(ctx, text)
        except Exception:
            logger.exception("預算%s訊息交付失敗：turn=%s", kind, ctx.ticket.turn_id)
            return
        if kind.startswith("abort") and status is not None and (status in _DELIVERED or status in _DELIVERY_UNCERTAIN):
            ctx.abort_notice_ok = True  # 已送出或結果不確定（不補發）；被拒／送不出去則 _conclude 補送失敗訊息

    def _notify_failure(self, ctx: _RunCtx) -> None:
        """失敗時給使用者的固定系統訊息：經同一條 Output Gate；origin=system，不是角色發言，也不編造看似成功的回答。"""
        if ctx.failure_notice_sent:
            return  # 只送一次（延後路徑的 partial 已送過）
        ctx.failure_notice_sent = True
        try:
            self._send_system_notice(ctx, LOOP_FAILURE_TEXT)
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
    policy_ref: str = DEFAULT_LOOP_POLICY_REF, extra_capability_tags: frozenset[str] = frozenset(), **launcher_kwargs: Any,
) -> tuple[LoopEntry, LoopLauncher]:
    """組裝層用：production 的 ``(LoopEntry, LoopLauncher)``（capability 收窄 + 啟動器）。只在 ``tool_path == "agent"`` 可建。
    launcher 另回傳是為了讓 ``CoreAssembly.shutdown`` 能關閉它。``owner_for`` 取 ticket 的 ``actor_id``
    （與 agent 路徑的回合 Task 一致；現有記憶工具不需核准）。
    ``extra_capability_tags``（預設空＝行為不變）：收窄後額外併入的標籤，見 ``with_extra_capability_tags``。"""
    launcher = LoopLauncher(
        tasks=tasks, delivery=delivery, orchestrator=orchestrator, registry=registry, model_port=model_port,
        text_store=text_store, cache=cache, persona_id=persona_id, **launcher_kwargs,
    )
    tags_for = make_capability_tags_for(registry, orchestrator.metrics)
    if extra_capability_tags:
        tags_for = with_extra_capability_tags(tags_for, frozenset(extra_capability_tags), registry)
    entry = LoopEntry(
        tool_path, capability_tags_for=tags_for, launch=launcher,
        policy_ref=policy_ref, owner_for=lambda ticket: ticket.actor_id, metrics=orchestrator.metrics,
    )
    return entry, launcher
