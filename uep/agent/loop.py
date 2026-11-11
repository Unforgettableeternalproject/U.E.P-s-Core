"""``AgentLoop``：Run 的外層迴圈（Agent 架構提案 §4.1～4.4、§4.10、§6.4、§8.2；切片 D1）。

包住 ``AgentCore`` 的單項處理（``screen_intent``／``create_screened``／``execute_screened``），不改 ``run_step``：
外層 Run 迴圈、預算、heartbeat、checkpoint、無進展偵測、平行 context、park 與 resume。

- **Step ＝ 一則模型訊息**：``begin_step`` -> 消費 Runtime 事件 -> 收齊整批 ``tool_intent``（以 ``batch_size`` 標記，缺省為 1）
  -> 分波執行 -> 依 index 回餵（D61 A 路徑：同一個 attempt 續跑）-> ``complete_step`` ＋ ``save_checkpoint``。
- **history 由迴圈擁有**：child context（``loop_context``）是權威；Runtime 內部的 history 與它逐則相等（測試鎖定），
  checkpoint 只存 ref／revision／hash。能力縮減或 resume 時另開 attempt（B 路徑），以 child 的 history 起頭。
- **AL-3**：每個 Step 邊界重新投影，結果再與「凍結集合（名稱＋版本）∩ 前一步集合」取交集，只會變窄。
  變窄時取消目前 attempt、以較窄的集合重開。模型或工具結果「要求」更大能力沒有任何通道（Gateway 以 capability_missing 拒絕）。
- **預算**（AL-2）：步數／工具次數／token／牆鐘；耗盡先 finalize（``tools=[]`` 要求收束），仍吐 tool_intent 或關閉 finalize ⇒
  ``FAILED(budget_exhausted)``。無進展 ⇒ ``FAILED(no_progress)``，不再喚醒模型。
- **park**：``ApprovalRequired`` ⇒ 同批其後項不執行、存 child 與 checkpoint、取消 attempt、``park_run``（釋放 worker，
  清心跳讓 ``resume_run`` 可立即接手）、回 ``awaiting_approval``。順序固定：checkpoint -> park_run -> mark_parked／回傳。
  ``resume(approvals=...)`` 重建＋預檢能力與版本＋補齊缺的工具結果（pending 項帶呼叫端提供的 approval_id 經 Manager 執行）＋續跑；
  重驗失敗（approval 無效、版本已變、能力撤銷）一律 ``FAILED``（WAITING_APPROVAL 不能轉 SUCCEEDED）。否決路徑（E2）尚未實作。
- 不直接發話（AL-4）：最終文字放在 ``LoopOutcome.text``，由呼叫端經 Output Gate 交付；``deliver_result`` 回報是否已交付，
  merge-back 只在已交付時寫入 ``result``。
- ``max_parallel`` 預設 1（序列，行為與並行度無關的部分不變）。D1b 起真實 TaskManager 的 TC-3 放寬為同 Run 內
  「全部 parallel_safe 或恰好一個 serial」，``> 1`` 時同波的 parallel_safe 成員可真的並行。分波、屏障、序列化語意與並行度無關。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from uep.adapters.runtime import Runtime, RuntimeRequest
from uep.agent.budget import BudgetCounters, LoopBudget, exhausted, observe_call
from uep.agent.core import (
    AgentCore,
    IntentRecord,
    IntentResult,
    ScreenedIntent,
    stable_action_id,
)
from uep.agent.loop_context import (
    ContextLost,
    LoopContextStore,
    ParentSnapshot,
    fork_child,
    load_verified,
    mark_parked,
    merge_back,
    persist_child,
)
from uep.agent.reasons import LoopReason, runtime_reason
from uep.agent.runtime import result_text
from uep.agent.scheduler import (
    DEFERRED_BATCH_LIMIT,
    DEFERRED_BUDGET,
    DEFERRED_BY_APPROVAL_WAIT,
    UNKNOWN_UNRESOLVED,
    EntryLookup,
    SchedItem,
    Wave,
    defer_tail,
    plan_waves,
)
from uep.agent.trigger import LoopTrigger
from uep.decision.loop_consult import advice_in_scope
from uep.ids import new_id
from uep.models.port import ChatMessage
from uep.tasks.manager import (
    ACTION_UNRESOLVED,
    APPROVAL_DENIED,
    TOOL_VERSION_CHANGED,
    IllegalTransition,
    PolicyViolation,
    StaleWorkerEpoch,
)
from uep.tasks.states import ActionStatus, RunStatus
from uep.tools.cancel import cancel_executors
from uep.tools.catalog.decision import CONSULT_TAG, CONSULT_TOOL, INJECTED_CONSULT
from uep.tools.composite import (
    COMPOSITE_STEP_SKIPPED,
    CompositePathError,
    resolve_params,
)
from uep.tools.gateway import (
    INVALID_TOOL_ARGS,
    TOOL_NOT_READY,
    TOOL_TIMEOUT,
    ToolGateway,
    visible_tools,
)

logger = logging.getLogger(__name__)

LoopState = Literal["completed", "failed", "awaiting_approval", "cancelled", "stale", "rejected"]
Projector = Callable[[frozenset[str], frozenset[str] | None], Mapping[str, Any]]

TOOL_NOT_IN_SNAPSHOT = "tool_not_in_snapshot"
CANCEL_ERROR_CODES = frozenset({"timeout", "cancelled"})  # executor 因 cancel 而失敗時簽回的 receipt error_code（§6.4 stopped）
RESUME_REQUIRED_KEYS = (
    "child_context_ref", "history_hash", "frozen_tools", "frozen_tags", "visible_tools", "budget_counters", "step_id",
    "frozen_modalities", "frozen_platforms",
)
# composite 逐步狀態列整體 status 的嚴重度（取最差者）：unknown 最差（副作用可能已發生），其次 failed、rejected、blocked
_SEVERITY: dict[str, int] = {"ok": 0, "blocked": 1, "rejected": 2, "failed": 3, "unknown": 4}
FINALIZE_NOTE = "預算已用盡：請根據目前已取得的資訊直接收束並回覆，不要再呼叫任何工具。"


@dataclass(frozen=True)
class LoopOutcome:
    state: LoopState
    run_id: str
    reason_code: str | None = None  # 只能是 ``LoopReason`` 的值（固定集合）
    text: str = ""  # 最終文字（只放這裡，由呼叫端交付；AL-4）
    steps: int = 0
    tool_calls: int = 0
    pending_action_ids: tuple[str, ...] = ()
    delivered: bool = False

    def __post_init__(self) -> None:
        if self.reason_code is not None:
            LoopReason(self.reason_code)  # 不在固定集合內即 ValueError


@dataclass(frozen=True)
class EscalationCandidate:
    """consult 回 escalate 時發給決策層的候選事件（CN-3）：缺口類別＋``decision_id`` 與一份候選 ``LoopTrigger``。

    候選觸發只帶**原本凍結**的能力標籤（迴圈自己絕不擴大，AL-3）；是否發出新的 ``LoopTrigger``、範圍如何重裁定，
    由決策中樞的 ``loop.reeval`` 決定（不在本層）。"""

    run_id: str
    decision_id: str
    sub_kind: str
    gap_code: str
    trigger: LoopTrigger


class _ConsultStore(Protocol):
    def get(self, action_id: str) -> Mapping[str, Any] | None:
        ...

    def content(self, action_id: str) -> str | None:
        ...

    def discard_run(self, run_id: str) -> None:
        ...


class _Stop(Exception):
    """內部用：需要立刻結束本次執行的終局（取消、牆鐘）。"""

    def __init__(self, outcome: LoopOutcome) -> None:
        super().__init__(outcome.state)
        self.outcome = outcome


@dataclass
class _State:
    run_id: str
    trigger: LoopTrigger
    parent_id: str
    frozen_tools: dict[str, int]
    frozen_tags: frozenset[str]
    cap: Mapping[str, Any]
    transcript: list[ChatMessage]
    child_ref: str
    counters: BudgetCounters
    seg_start: float
    base_elapsed: float = 0.0
    last_hb: float = 0.0
    step_id: str | None = None
    open_step: str | None = None
    step_started: float = 0.0
    attempt_id: str | None = None
    consumed: list[str] = field(default_factory=list)
    frozen_modalities: frozenset[str] = frozenset()  # consult 的凍結範圍（呼叫端提供，Run 內不變）
    frozen_platforms: frozenset[str] = frozenset()
    last_decision_id: str = ""  # CN-9：最近一次 consult 的 decision_id（進 checkpoint）
    escalate_gap: str = ""  # escalate 的缺口碼（進終態 checkpoint，崩潰後仍可重送候選事件）
    unresolved: bool = False  # 本次執行中出現過 UNKNOWN 的 action：Run 不可宣稱 SUCCEEDED
    usage_seen: dict[str, int] = field(default_factory=dict)
    composite_cursor: dict[str, Any] | None = None  # 進行中的 composite（§5.7.4）：進 checkpoint，完成即清除
    batch: _BatchCtx | None = None  # 目前批次的呼叫／文字／已得結果（composite 逐步 checkpoint 重建 history 用）


@dataclass
class _BatchCtx:
    calls: list[dict[str, Any]]
    text: str
    views: dict[int, Mapping[str, Any]]


@dataclass(frozen=True)
class _Halt:
    """resume 時 composite 的待決步驟不能續跑：``"repark"``（缺／過期核准、暫時非 ready）或 ``LoopReason``（結束 Run）。"""

    stop: LoopReason | Literal["repark"]
    action_id: str


@dataclass(frozen=True)
class _End:
    kind: Literal["result", "park", "exhausted", "restart", "fail", "escalate"]
    text: str = ""
    reason: str = ""
    dim: str = ""  # exhausted 時耗盡的維度（steps／tool_calls／tokens／run_deadline／rounds）
    cap: Mapping[str, Any] | None = None
    pending: tuple[str, ...] = ()
    info: Mapping[str, Any] | None = None  # escalate 時的 consult 結果


class _SnapshotSink(Protocol):
    def put(self, ref: str, messages: Sequence[ChatMessage]) -> None:
        ...


class LoopSnapshots:
    """attempt 的起始 history 暫存：``AgentLoop`` 放入，``ModelPortRuntime(messages_for=snapshots.take)`` 取出。"""

    def __init__(self) -> None:
        self._data: dict[str, list[ChatMessage]] = {}
        self._lock = threading.Lock()

    def put(self, ref: str, messages: Sequence[ChatMessage]) -> None:
        with self._lock:
            self._data[ref] = list(messages)

    def take(self, request: RuntimeRequest) -> list[ChatMessage]:
        """找不到就 ``KeyError``（絕不以空 history 呼叫模型）；``AgentLoop`` 會把它轉成 ``snapshot_missing`` 失敗。"""
        with self._lock:
            return self._data.pop(request.snapshot_ref)


class AgentLoop:
    def __init__(
        self, *, runtime: Runtime, tasks: Any, gateway: ToolGateway, registry: EntryLookup,
        store: LoopContextStore, projector: Projector, budget: LoopBudget, worker_epoch: int,
        snapshots: _SnapshotSink | None = None, injected: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic, cancelled: Callable[[], bool] = lambda: False,
        deliver_result: Callable[[str], bool] | None = None,
        content_lookup: Callable[[str], str | None] | None = None, consults: _ConsultStore | None = None,
        consult_scope: Callable[[], Mapping[str, Collection[str]]] | None = None,
        on_escalate: Callable[[EscalationCandidate], None] | None = None,
        run_cleanup: Callable[[str], None] | None = None,
        executors: Mapping[str, Any] | None = None, cancel_timeout_s: float = 5.0, cancel_grace_s: float = 2.0,
        step_output: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        # ``step_output``（sub action_id -> 結構化輸出欄位）：composite 的 ``$.steps[k].output.x`` 路徑來源（§5.7.4）。
        # 沒給時，輸出欄位取自回合範圍的結果來源（``content_lookup`` ⇒ ``text``）與 receipt 的 ``result_ref``
        self._step_output = step_output
        # ``executors``（executor 名稱 -> ToolExecutor）只用於 ``parallel_one_shot`` 的逾時／取消路徑呼叫 ``executor.cancel``；
        # 沒給或該 executor 不支援 cancel ⇒ 逾時視為結果未明（unknown），不推定已停止（§6.4）
        self._executors = dict(executors) if executors is not None else {}
        self._cancel_timeout_s = cancel_timeout_s
        self._cancel_grace_s = cancel_grace_s
        self._run_cleanup = run_cleanup  # Run 終態（非 park）時清除以 run_id 為鍵的回合範圍暫存（例如旁證編號）
        self._consults = consults
        self._consult_scope = consult_scope
        self._on_escalate = on_escalate
        self._base_injected = dict(injected or {})
        self._runtime = runtime
        self._tasks = tasks
        self._gateway = gateway
        self._registry = registry
        self._store = store
        self._projector = projector
        self._budget = budget
        self._epoch = worker_epoch
        if budget.max_parallel > 1 and not getattr(tasks, "supports_parallel_execution", False):
            # 並行需要 tasks 端的 TC-3 放寬（D1b：_check_single_executing 的新規則）；明示支援才允許
            raise ValueError("max_parallel > 1 需要 tasks.supports_parallel_execution（tasks 端須實作 TC-3 的 Run 內並行規則）")
        self._snapshots = snapshots
        self._clock = clock
        self._cancelled = cancelled
        self._deliver_result = deliver_result
        def with_consults(action_id: str) -> str | None:
            # consult 結果文字（無使用者原文）優先從 consult 暫存取
            return (consults.content(action_id) if consults is not None else None) or (
                content_lookup(action_id) if content_lookup is not None else None
            )

        lookup = with_consults if consults is not None else content_lookup
        self._content_lookup = lookup
        self._core = AgentCore(runtime, tasks, gateway, worker_epoch=worker_epoch, injected=injected, content_lookup=lookup)

    # ───────────────────────── 入口 ─────────────────────────

    def run(
        self, trigger: LoopTrigger, run_id: str, *, parent: ParentSnapshot, system_text: str, trigger_text: str = "",
        persona_version: int = 0,
    ) -> LoopOutcome:
        """啟動一個 Run 的迴圈（Run 已由呼叫端建立並進入 RUNNING）。``capability_tags`` 為空或投影為空 ⇒ 拒絕（介面違規）。"""
        if not trigger.capability_tags:
            logger.error("LoopTrigger 的 capability_tags 為空，拒絕啟動：run=%s", run_id)
            return LoopOutcome("rejected", run_id, LoopReason.CAPABILITY_EMPTY)
        cap = self._projector(trigger.capability_tags, None)
        frozen = {t["tool"]: int(t.get("version", 1)) for t in cap.get("tools", ())}
        if not frozen:
            logger.error("能力投影為空，拒絕啟動：run=%s", run_id)
            return LoopOutcome("rejected", run_id, LoopReason.CAPABILITY_EMPTY)
        child = fork_child(
            self._store, run_id, parent=parent, system_text=system_text, goal_code=trigger.goal_code,
            trigger_text=trigger_text, persona_version=persona_version, scope_ref=trigger.scope_ref,
        )
        st = self._new_state(run_id, trigger, parent.parent_id, self._narrow(cap, frozen, None), frozen,
                             trigger.capability_tags, list(child.messages), child.ref, BudgetCounters())
        scope = self._scope_now()
        st.frozen_modalities, st.frozen_platforms = scope["modalities"], scope["platforms"]
        st.cap = self._offer(st, st.cap)
        return self._drive(st, resume_fill=None)

    def resume(
        self, run_id: str, checkpoint: Mapping[str, Any], *, trigger: LoopTrigger, parent_id: str,
        approvals: Mapping[str, str] | None = None,
    ) -> LoopOutcome:
        """核准後的續行（呼叫端已 ``resume_run`` 取得新 epoch 並建構本物件）。重建 child 並驗 hash（失敗 ⇒ FAILED(context_lost)），
        能力＝凍結集合 ∩ 當下投影（只收窄，AL-3），補齊缺少的工具結果後以新 attempt 續跑。

        ``approvals``：``action_id -> approval_id``，由呼叫端（Approval 通道的結果）提供，只是「宣稱的對應」：approval 與 action 的
        params_hash／target／scope、過期、是否已消耗一律交給 Manager 判定，迴圈不預判也不放寬。預檢只看能力與版本（不看 approval）：
        pending 工具不在凍結集合或當下投影 ⇒ ``FAILED(capability_revoked)``；投影版本與凍結版本不同 ⇒ ``FAILED(tool_version_changed)``；
        兩者都不續跑、不消耗 approval。pending 項缺 approval 或 approval 已過期 ⇒ 再次 park；其他 approval 問題 ⇒ ``FAILED(approval_invalid)``。"""
        semantic = checkpoint.get("next_step_semantic")
        if isinstance(semantic, str) and semantic.startswith("end:"):
            return self._resume_terminal(run_id, semantic, checkpoint, trigger, parent_id)
        missing = [k for k in RESUME_REQUIRED_KEYS if checkpoint.get(k) is None]
        if missing:
            # 舊版或殘缺的 checkpoint：不以預設值補，fail-closed
            logger.error("checkpoint 缺少必要的鍵，不續跑：run=%s keys=%s", run_id, missing)
            st = self._new_state(run_id, trigger, parent_id, {"tools": []}, {}, frozenset(), [], "", BudgetCounters())
            return self._guarded(st, lambda: self._finish(st, "failed", LoopReason.CHECKPOINT_INCOMPLETE))
        frozen = {str(k): int(v) for k, v in dict(checkpoint["frozen_tools"]).items()}
        tags = frozenset(str(t) for t in checkpoint["frozen_tags"])
        visible = frozenset(str(t) for t in checkpoint["visible_tools"])  # park 當下的可見集合：resume 的 prior（單調收窄）
        counters = BudgetCounters.from_checkpoint(checkpoint["budget_counters"])
        try:
            messages, _revision = load_verified(
                self._store, str(checkpoint["child_context_ref"]), str(checkpoint["history_hash"]),
            )
        except ContextLost:
            logger.error("child context 無法重建：run=%s", run_id)
            st = self._new_state(run_id, trigger, parent_id, {"tools": []}, frozen, tags, [],
                                 str(checkpoint["child_context_ref"]), counters)
            return self._guarded(st, lambda: self._finish(st, "failed", LoopReason.CONTEXT_LOST))
        prior = visible & frozenset(frozen)
        projected = self._projector(tags, prior)  # 保留原始投影（_narrow 之前）供版本預檢
        cap = self._narrow(projected, frozen, prior)
        st = self._new_state(run_id, trigger, parent_id, cap, frozen, tags, messages, str(checkpoint["child_context_ref"]), counters)
        st.step_id = str(checkpoint["step_id"])
        st.frozen_modalities = frozenset(str(m) for m in checkpoint["frozen_modalities"])
        st.frozen_platforms = frozenset(str(m) for m in checkpoint["frozen_platforms"])
        st.cap = self._offer(st, st.cap)
        st.consumed = list(checkpoint.get("consumed_action_ids") or ())
        if checkpoint.get("composite_cursor") is not None:
            st.composite_cursor = self._valid_cursor(checkpoint["composite_cursor"])
            if st.composite_cursor is None:  # 形狀不合：不猜、不從頭重播已成功的步驟
                logger.error("composite_cursor 形狀不合，不續跑：run=%s", run_id)
                return self._guarded(st, lambda: self._finish(st, "failed", LoopReason.CHECKPOINT_INCOMPLETE))
        pending = frozenset(checkpoint.get("pending_approvals") or ())
        verdict = self._precheck_pending(st, projected, pending)
        if isinstance(verdict, LoopReason):
            return self._guarded(st, lambda: self._finish(st, "failed", verdict))
        if verdict is not None:  # (action_id,)：pending 工具暫時非 ready ⇒ 再 park，不丟掉核准
            return self._guarded(st, lambda: self._repark(st, verdict[0], write_checkpoint=False))
        return self._guarded(st, lambda: self._drive_inner(st, pending, approvals or {}))

    def _precheck_pending(
        self, st: _State, projected: Mapping[str, Any], pending: frozenset[str],
    ) -> LoopReason | tuple[str] | None:
        """resume 預檢（只看能力、readiness 與版本，不看 approval），對尚缺 tool 訊息的 pending 工具依序判斷：

        1. 不在凍結集合 ⇒ ``capability_revoked``。
        2. 在凍結集合、但不在當下投影：registry 有 ``readiness`` 與該條目，且 ``readiness != "ready"``（warming／unavailable，
           含 coordinator 不認得的模組，RA-15）⇒ **再 park**（回 ``(action_id,)``；暫時性，核准沒被消耗，不丟掉人類的核准）；
           沒有 ``readiness``、沒有條目、或 readiness 是 ready 卻仍不在投影（scope／平台／policy 收窄）⇒ ``capability_revoked``。
        3. 在投影內但版本不同 ⇒ ``tool_version_changed``。

        只判斷 readiness，不放寬能力（AL-3）：再 park 不改寫 checkpoint（``visible_tools`` 保持原樣），下一次 resume 仍從頭跑完整預檢。
        不通過回原因碼（呼叫端結束 Run、不消耗 approval）；通過回 None。Manager 的 tool_version 檢查保留作最後一道防線。"""
        if not pending:
            return None
        assistant = next((m for m in reversed(st.transcript) if m.role == "assistant" and m.tool_calls), None)
        if assistant is None:
            return None
        done = {m.tool_call_id for m in st.transcript if m.role == "tool"}
        step_id = st.step_id or ""
        current = {t["tool"]: int(t.get("version", 1)) for t in projected.get("tools", ())}
        for ordinal, call in enumerate(assistant.tool_calls):
            name, action_id = call["name"], stable_action_id(st.run_id, step_id, ordinal)
            cursor = st.composite_cursor
            if cursor is not None and cursor["ordinal"] == ordinal and call["id"] not in done:
                # composite：待決的是子步驟（cursor.next），能力與版本檢查對的是子步驟工具
                entry = self._composite_entry(name)
                if entry is not None and cursor["next"] < len(entry.composite.steps):
                    name = entry.composite.steps[cursor["next"]].tool
                    action_id = stable_action_id(st.run_id, step_id, ordinal, cursor["next"])
            if call["id"] in done or action_id not in pending:
                continue
            if name not in st.frozen_tools:
                logger.warning("resume 預檢：pending 工具 %s 不在凍結集合，Run 改轉 FAILED：run=%s", name, st.run_id)
                return LoopReason.CAPABILITY_REVOKED
            if name not in current:
                if self._temporarily_not_ready(name):
                    logger.warning("resume 預檢：pending 工具 %s 暫時非 ready，再 park（不消耗核准）：run=%s", name, st.run_id)
                    return (action_id,)
                logger.warning("resume 預檢：pending 工具 %s 不在當下投影，Run 改轉 FAILED：run=%s", name, st.run_id)
                return LoopReason.CAPABILITY_REVOKED
            if current[name] != st.frozen_tools[name]:
                logger.warning("resume 預檢：pending 工具 %s 版本已變，Run 改轉 FAILED：run=%s", name, st.run_id)
                return LoopReason.TOOL_VERSION_CHANGED
        return None

    def _temporarily_not_ready(self, tool: str) -> bool:
        """registry 有 ``readiness`` 與該條目、且 readiness 不是 ready。其餘（沒有 readiness、沒有條目）一律 False（fail-closed）。"""
        readiness = getattr(self._registry, "readiness", None)
        if not callable(readiness):
            return False
        try:
            return readiness(tool) != "ready"
        except PolicyViolation:
            return False

    def _repark(self, st: _State, action_id: str, *, write_checkpoint: bool = True) -> LoopOutcome:
        """再次 park：checkpoint -> park_run -> 回 ``awaiting_approval``。``write_checkpoint=False`` 用於預檢（transcript 與
        pending 都沒變，既有 checkpoint 已是正確的 park 狀態；重寫會把暫時被投影排除的工具從 ``visible_tools`` 永久移掉）。"""
        if write_checkpoint:
            self._checkpoint(st, f"await_approval:{action_id}", [action_id])
        self._park(st)
        return LoopOutcome(
            "awaiting_approval", st.run_id, LoopReason.APPROVAL_REQUIRED, steps=st.counters.steps,
            tool_calls=st.counters.tool_calls, pending_action_ids=(action_id,),
        )

    def _resume_terminal(
        self, run_id: str, semantic: str, checkpoint: Mapping[str, Any], trigger: LoopTrigger, parent_id: str,
    ) -> LoopOutcome:
        """終態 checkpoint（``end:<reason>[:<維度>]``）已寫入、轉移可能還沒提交就崩潰：只補完缺少的轉移，不喚醒模型（0 次呼叫）。

        - 失敗類原因（含 ``escalated``）⇒ 轉 ``FAILED`` 並帶原因碼回傳；``on_escalate`` 不在這裡重送（屬切片 F 的恢復掃描器）。
        - 完成類（``completed``／``budget_finalized``／``consult_finalized``）：成功轉移是在寫 checkpoint 之前提交的，Run 已是終態，不再動作。
        - 解析不出合法的 ``LoopReason`` ⇒ ``FAILED(checkpoint_incomplete)``。"""
        st = self._new_state(run_id, trigger, parent_id, {"tools": []}, {}, frozenset(), [], "",
                             BudgetCounters.from_checkpoint(checkpoint.get("budget_counters")))
        raw = semantic.split(":")[1] if semantic.count(":") >= 1 else ""
        if raw == "completed":
            return LoopOutcome("completed", run_id, steps=st.counters.steps, tool_calls=st.counters.tool_calls)
        try:
            reason = LoopReason(raw)
        except ValueError:
            logger.error("終態 checkpoint 的原因碼不合法，不續跑：run=%s", run_id)
            return self._guarded(st, lambda: self._finish(st, "failed", LoopReason.CHECKPOINT_INCOMPLETE))
        if reason in (LoopReason.BUDGET_FINALIZED, LoopReason.CONSULT_FINALIZED):
            return LoopOutcome("completed", run_id, reason, steps=st.counters.steps, tool_calls=st.counters.tool_calls)
        logger.warning("Run 已有終態 checkpoint（%s），只補完 FAILED 轉移：run=%s", reason.value, run_id)
        return self._guarded(st, lambda: self._finish(st, "failed", reason))

    # ───────────────────────── 內部：狀態與收尾 ─────────────────────────

    def _new_state(self, run_id, trigger, parent_id, cap, frozen, tags, transcript, child_ref, counters) -> _State:
        now = self._clock()
        return _State(
            run_id=run_id, trigger=trigger, parent_id=parent_id, frozen_tools=dict(frozen), frozen_tags=frozenset(tags),
            cap=cap, transcript=list(transcript), child_ref=child_ref, counters=counters, seg_start=now,
            base_elapsed=counters.elapsed_s, last_hb=now,
        )

    def _elapsed(self, st: _State) -> float:
        return st.base_elapsed + (self._clock() - st.seg_start)

    @staticmethod
    def _narrow(cap: Mapping[str, Any], frozen: Mapping[str, int], prior: frozenset[str] | None) -> dict[str, Any]:
        """AL-3：只保留「凍結集合（名稱與版本都相同）∩ 前一步集合」內的工具；結果永遠不比輸入寬。"""
        tools = [
            dict(t) for t in cap.get("tools", ())
            if t["tool"] in frozen and int(t.get("version", 1)) == frozen[t["tool"]] and (prior is None or t["tool"] in prior)
        ]
        return {"tools": tools}

    # ───────────────────────── 內部：consult（§4.9） ─────────────────────────

    def _scope_now(self) -> dict[str, frozenset[str]]:
        raw = self._consult_scope() if self._consult_scope is not None else {}
        return {k: frozenset(str(v) for v in raw.get(k, ())) for k in ("modalities", "platforms")}

    def _cap_tags(self, cap: Mapping[str, Any]) -> frozenset[str]:
        """某個 capability_set 的能力標籤：可見工具的標籤（不比可見集合寬）。"""
        tags: set[str] = set()
        for name in visible_tools(cap):
            try:
                tags.add(self._registry.entry(name).spec.capability)
            except Exception:  # noqa: BLE001  查不到的工具不貢獻標籤（保守）
                logger.debug("snapshot 標籤：查不到工具 %s", name)
        return frozenset(tags)

    def _snapshot_tags(self, st: _State) -> frozenset[str]:
        """目前快照的能力標籤：Run 目前持有（已過 ``_offer``）的可見工具的標籤。注入給中樞用。"""
        return self._cap_tags(st.cap)

    def _projection_tags(self, st: _State) -> frozenset[str]:
        """「當下投影」的能力標籤：重算 AL-3 投影（純記憶體、無副作用），**不經 ``_offer``**。

        與 ``_snapshot_tags`` 來源不同：後者是 ``st.cap``（含 consult 次數／無進展上限造成的移除，屬預算）；
        這裡只反映真正的能力收窄（投影器不再給、凍結集合或前一步集合之外），供 CN-2 再驗使用。"""
        prior = visible_tools(st.cap)
        return self._cap_tags(self._narrow(self._projector(st.frozen_tags, prior), st.frozen_tools, prior))

    def _no_progress_count(self, st: _State) -> int:
        # 連續無進展的次數：同一呼叫「重複」的次數（第一次不算）與連續被拒次數，取大者
        return max(st.counters.repeat_count - 1, st.counters.rejected_streak, 0)

    def _consult_allowed(self, st: _State) -> bool:
        """是否還投出 consult：凍結標籤含 ``decision.consult``、有結果暫存可讀、consults 與 no_progress 兩個上限都沒到。"""
        budget = self._budget
        return (
            CONSULT_TAG in st.frozen_tags and self._consults is not None and budget.max_consults > 0
            and st.counters.consults < budget.max_consults
            and self._no_progress_count(st) < budget.consult_no_progress_limit
        )

    def _offer(self, st: _State, cap: Mapping[str, Any]) -> dict[str, Any]:
        """CN-8／AL-3：不允許時把 consult 從集合中拿掉（只會變窄）。"""
        if self._consult_allowed(st):
            return dict(cap)
        return {"tools": [dict(t) for t in cap.get("tools", ()) if t["tool"] != CONSULT_TOOL]}

    def _run_injected(self, st: _State) -> dict[str, Any]:
        """一般工具的注入值：Core 持有的基礎值加上 ``run_id``（旁證編號等以 Run 為範圍的暫存用；顯式帶入，不靠執行緒區域）。"""
        return {**self._base_injected, "run_id": st.run_id}

    def _consult_injected(self, st: _State, step_id: str, ordinal: int) -> dict[str, Any]:
        now = self._scope_now()
        values: dict[str, Any] = {
            "run_id": st.run_id, "step_id": step_id, "ordinal": ordinal,
            "frozen_capability_tags": sorted(st.frozen_tags), "frozen_modalities": sorted(st.frozen_modalities),
            "frozen_platforms": sorted(st.frozen_platforms),
            "snapshot_capability_tags": sorted(self._snapshot_tags(st)), "snapshot_modalities": sorted(now["modalities"]),
            "snapshot_platforms": sorted(now["platforms"]),
            "consults_used": st.counters.consults, "no_progress_count": self._no_progress_count(st),
        }
        if set(values) != set(INJECTED_CONSULT):  # 組裝錯誤：注入欄位與條目宣告不一致
            raise RuntimeError("consult 注入欄位與 decision.consult 條目的 injected_params 不一致")
        return {**self._base_injected, **values}

    def _consult_info(self, st: _State, action_id: str | None) -> Mapping[str, Any] | None:
        if self._consults is None or not action_id:
            return None
        info = self._consults.get(action_id)
        if info is not None and isinstance(info.get("decision_id"), str):
            st.last_decision_id = info["decision_id"]
        return info

    def _recheck_escalation(self, st: _State, info: Mapping[str, Any]) -> str | None:
        """CN-2 縱深防禦：回傳要 escalate 的缺口碼，或 None。中樞已回 escalate、或 advice 超出「凍結 ∩ 當下快照」都算。

        再驗的標籤快照是「當下投影」（``_projection_tags``，不經 ``_offer``），不是注入給中樞的 ``_snapshot_tags``：
        consult 因預算被 ``_offer`` 拿掉不算能力收窄，投影器真的不再給 consult 才算。"""
        esc = info.get("escalate") or {}
        if esc.get("needed"):
            return str(esc.get("reason_code") or "scope_exceeded")
        advice = info.get("advice")
        if advice is None:
            return None
        sub_kind = str(info.get("sub_kind", ""))
        gap = advice_in_scope(
            sub_kind, advice, {"modalities": st.frozen_modalities, "capability_tags": st.frozen_tags},
            {**self._scope_now(), "capability_tags": self._projection_tags(st)},
        )
        if gap is not None:
            logger.warning("consult 的 advice 形狀不合或超出凍結範圍 ∩ 快照，改當 escalate 處理（稽核）：sub_kind=%s", sub_kind)
        return gap

    def _guarded(self, st: _State, action: Callable[[], LoopOutcome]) -> LoopOutcome:
        try:
            return action()
        except _Stop as stop:
            return self._finalize_stop(st, stop.outcome)
        except StaleWorkerEpoch:
            logger.warning("worker_epoch 已被取代，停止迴圈：run=%s", st.run_id)
            if st.attempt_id:
                self._runtime.cancel(st.attempt_id, "stale_worker_epoch")  # SA-2
            return LoopOutcome("stale", st.run_id, steps=st.counters.steps, tool_calls=st.counters.tool_calls)

    def _drive(self, st: _State, resume_fill: frozenset[str] | None) -> LoopOutcome:
        return self._guarded(st, lambda: self._drive_inner(st, resume_fill, {}))

    def _finalize_stop(self, st: _State, outcome: LoopOutcome) -> LoopOutcome:
        if st.attempt_id:
            self._runtime.cancel(st.attempt_id, outcome.reason_code or outcome.state)
        if st.open_step:
            self._end_step(st, ok=False)
        if outcome.state == "failed":
            return self._finish(st, "failed", LoopReason(outcome.reason_code or LoopReason.NO_PROGRESS))
        merge_back(self._store, run_id=st.run_id, parent_id=st.parent_id, scope_ref=st.trigger.scope_ref,
                   state=outcome.state, reason_code=outcome.reason_code)
        if outcome.state == "cancelled":
            self._discard_consults(st)
        return outcome

    def _finish(
        self, st: _State, state: LoopState, reason: LoopReason | None, text: str = "", detail: str = "",
    ) -> LoopOutcome:
        """終態：Run 轉移、最後 checkpoint、merge-back。``completed`` 在最終文字為空、或 Run 內仍有結果未明（UNKNOWN／EXECUTING）
        的 action 時改走 ``FAILED``（真實 TaskManager 的 ``_assert_no_inflight`` 也會拒絕轉 SUCCEEDED）。"""
        if state == "completed" and not text.strip():
            state, reason, detail = "failed", LoopReason.EMPTY_RESULT, ""
        if state == "completed":
            if st.unresolved:
                state, reason, detail = "failed", LoopReason.UNRESOLVED_ACTIONS, ""
            else:
                try:
                    self._tasks.transition_run(st.run_id, RunStatus.SUCCEEDED, worker_epoch=self._epoch)
                except IllegalTransition:
                    logger.warning("Run 仍有結果未明的 action，改轉 FAILED：run=%s", st.run_id)
                    state, reason, detail = "failed", LoopReason.UNRESOLVED_ACTIONS, ""
        semantic = f"end:{reason or state}{f':{detail}' if detail else ''}"
        if state == "failed":
            # 終態 checkpoint（含 last_decision_id／escalate_gap）先於轉移：兩者之間崩潰也不會丟失缺口紀錄
            if st.transcript:  # context_lost／checkpoint_incomplete 時沒有可寫的 history
                self._checkpoint(st, semantic)
            self._tasks.transition_run(st.run_id, RunStatus.FAILED, worker_epoch=self._epoch)
        elif st.transcript:
            self._checkpoint(st, semantic)
        self._discard_consults(st)
        delivered = False
        if state == "completed" and self._deliver_result is not None:
            delivered = bool(self._deliver_result(text))
        merge_back(
            self._store, run_id=st.run_id, parent_id=st.parent_id, scope_ref=st.trigger.scope_ref, state=state,
            reason_code=reason, summary=text, result=text, delivered=delivered, trail_ref=f"trail:{st.run_id}",
        )
        return LoopOutcome(
            state, st.run_id, reason, text, st.counters.steps, st.counters.tool_calls, delivered=delivered,
        )

    def _discard_consults(self, st: _State) -> None:
        """Run 終態：清掉該 Run 的 consult 結果暫存（park 不是終態，不清）。"""
        discard = getattr(self._consults, "discard_run", None)
        if callable(discard):
            discard(st.run_id)
        if self._run_cleanup is not None:
            try:
                self._run_cleanup(st.run_id)
            except Exception as exc:  # noqa: BLE001 - 清理失敗不影響 Run 終態
                logger.warning("Run 終態清理失敗：%s", type(exc).__name__)

    def _checkpoint(
        self, st: _State, semantic: str, pending: Sequence[str] = (), *, transcript: Sequence[ChatMessage] | None = None,
    ) -> None:
        revision, digest = persist_child(self._store, st.child_ref, st.transcript if transcript is None else transcript)
        st.counters.elapsed_s = self._elapsed(st)
        cp: dict[str, Any] = {
            "next_step_semantic": semantic, "pending_approvals": list(pending),
            "consumed_action_ids": list(st.consumed), "child_context_ref": st.child_ref, "child_revision": revision,
            "history_hash": digest, "frozen_tools": dict(st.frozen_tools), "frozen_tags": sorted(st.frozen_tags),
            "budget_counters": st.counters.to_checkpoint(), "visible_tools": sorted(visible_tools(st.cap)),
            "frozen_modalities": sorted(st.frozen_modalities), "frozen_platforms": sorted(st.frozen_platforms),
        }
        if st.last_decision_id:
            cp["last_decision_id"] = st.last_decision_id
        if st.escalate_gap:
            cp["escalate_gap"] = st.escalate_gap
        if st.composite_cursor is not None:
            cp["composite_cursor"] = st.composite_cursor
        if st.step_id:
            cp["step_id"] = st.step_id
        self._tasks.save_checkpoint(st.run_id, cp, worker_epoch=self._epoch)

    # ───────────────────────── 內部：主迴圈 ─────────────────────────

    def _drive_inner(self, st: _State, resume_fill: frozenset[str] | None, approvals: Mapping[str, str]) -> LoopOutcome:
        if resume_fill is not None:
            parked = self._fill_missing(st, resume_fill, approvals)
            if parked is not None:
                return parked
        while True:
            dim = exhausted(self._budget, st.counters, elapsed_s=self._elapsed(st))
            if dim is not None:
                end = _End("exhausted", dim=dim)
            else:
                end = self._run_attempt(st, st.cap, finalize=False)
                if end.kind == "restart":
                    st.cap = end.cap or {"tools": []}
                    continue
            return self._conclude(st, end)

    def _conclude(self, st: _State, end: _End) -> LoopOutcome:
        if end.kind == "result":
            st.transcript.append(ChatMessage("assistant", end.text))
            return self._finish(st, "completed", None, end.text)
        if end.kind == "park":
            self._park(st)
            return LoopOutcome(
                "awaiting_approval", st.run_id, LoopReason.APPROVAL_REQUIRED, steps=st.counters.steps,
                tool_calls=st.counters.tool_calls, pending_action_ids=end.pending,
            )
        if end.kind == "escalate":
            return self._escalate(st, end)
        if end.kind == "exhausted":
            by_consult = end.dim == "consult"  # 中樞建議 finalize：不是預算耗盡
            if end.dim != "run_deadline" and self._budget.finalize_on_exhausted:
                final = self._run_attempt(st, {"tools": []}, finalize=True)
                if final.kind == "result":
                    st.transcript.append(ChatMessage("assistant", final.text))
                    reason = LoopReason.CONSULT_FINALIZED if by_consult else LoopReason.BUDGET_FINALIZED
                    return self._finish(st, "completed", reason, final.text, "" if by_consult else end.dim)
            if by_consult:
                return self._finish(st, "failed", LoopReason.FINALIZE_FAILED)
            return self._finish(st, "failed", LoopReason.BUDGET_EXHAUSTED, detail=end.dim)
        return self._finish(st, "failed", LoopReason(end.reason or LoopReason.NO_PROGRESS))

    def _park(self, st: _State) -> None:
        """park 出口（checkpoint 已寫入之後）：``park_run`` 釋放 worker，再 mark_parked。回傳的 Run 若已不是本 epoch
        （park 之前已被接手），拋 ``StaleWorkerEpoch`` 交給 ``_guarded``，不再標記 parked。"""
        run = self._tasks.park_run(st.run_id, worker_epoch=self._epoch)
        if run.worker_epoch != self._epoch:
            raise StaleWorkerEpoch(f"run {st.run_id} 在 park 時已被接手")
        mark_parked(self._store, run_id=st.run_id, parent_id=st.parent_id)

    def _escalate(self, st: _State, end: _End) -> LoopOutcome:
        """CN-3：結束本 Run（``FAILED(escalated)``，不回饋模型、不在同一個 Run 續做），並送出候選事件。"""
        info = end.info or {}
        gap = end.reason if re.fullmatch(r"[a-z0-9_.:\-]{1,64}", end.reason or "") else "scope_exceeded"
        if isinstance(info.get("decision_id"), str):
            st.last_decision_id = info["decision_id"]
        st.escalate_gap = gap  # 進終態 checkpoint（耐久紀錄；恢復掃描器的重送屬切片 F）
        outcome = self._finish(st, "failed", LoopReason.ESCALATED)
        if self._on_escalate is not None:
            origin = st.trigger
            candidate = EscalationCandidate(
                run_id=st.run_id, decision_id=str(info.get("decision_id", "")), sub_kind=str(info.get("sub_kind", "")),
                gap_code=gap,
                trigger=LoopTrigger(
                    kind="external_event", origin_ref=str(info.get("decision_id", "")) or origin.origin_ref,
                    goal_code=origin.goal_code, capability_tags=origin.capability_tags,  # 只帶原本凍結的標籤，不擴大
                    scope_ref=origin.scope_ref, owner_actor_id=origin.owner_actor_id, policy_ref=origin.policy_ref,
                    snapshot_ref=origin.snapshot_ref, plan_ref=origin.plan_ref,
                ),
            )
            try:
                self._on_escalate(candidate)
            except Exception:
                logger.exception("escalate 候選事件送出失敗：run=%s", st.run_id)
        return outcome

    def _active_consult_due(self, st: _State, cap: Mapping[str, Any]) -> bool:
        """CN-8：觸發點由 Policy 決定（``LoopBudget.consult_loop_end_margin``，None ⇒ 關閉），不寫死在迴圈。
        剩餘步數 <= margin 且 >= 2（consult 自己要用掉一步，不吃掉最後一步）才諮詢；剩餘工具次數也要 >= 2。凍結集合沒有標籤、
        或目前集合不含 consult 時不得呼叫。"""
        margin = self._budget.consult_loop_end_margin
        if margin is None or CONSULT_TOOL not in visible_tools(cap) or not self._consult_allowed(st):
            return False
        steps_left = self._budget.max_steps - st.counters.steps
        calls_left = self._budget.max_tool_calls - st.counters.tool_calls
        return 2 <= steps_left <= margin and calls_left >= 2

    def _active_consult(self, st: _State, cap: Mapping[str, Any]) -> _End | None:
        """預算將盡時問中樞 ``loop_end``：自己的 Step（計入步數、工具次數與 consults），走 ActionIntent。
        回 ``escalate`` ⇒ 結束本 Run；建議 ``finalize`` ⇒ 改走 finalize；其餘照常。"""
        self._begin_step(st, "consult")
        step_id = st.step_id or ""
        args = {"sub_kind": "loop_end", "goal_satisfied": False, "made_progress": self._no_progress_count(st) == 0}
        screened = self._core.screen_intent(
            run_id=st.run_id, step_id=step_id, ordinal=0, capability_set=cap, tool=CONSULT_TOOL, args=args,
            injected=self._consult_injected(st, step_id, 0),
        )
        if isinstance(screened, IntentResult):
            self._end_step(st, ok=True)
            return None
        res = self._core.create_screened(screened) or self._core.execute_screened(screened)
        st.counters.tool_calls += 1
        st.counters.consults += 1
        if res.view.get("status") == "unknown":
            st.unresolved = True
        if res.record.action_id:
            st.consumed.append(res.record.action_id)
        self._end_step(st, ok=True)
        info = self._consult_info(st, screened.action_id) if res.view.get("status") == "ok" else None
        if info is not None:
            gap = self._recheck_escalation(st, info)
            if gap is not None:
                return _End("escalate", reason=gap, info=info)
            outcome = ((info.get("advice") or {}).get("loop") or {}).get("outcome")
            if outcome == "finalize" and self._budget.finalize_on_exhausted:  # 不繞過 finalize 的政策開關
                return _End("exhausted", dim="consult")
        self._checkpoint(st, "think")
        return None

    def _gate(self, st: _State) -> None:
        """heartbeat／取消／牆鐘／Step 逾時。舊 epoch 的 StaleWorkerEpoch 直接往外拋。"""
        if self._cancelled():
            raise _Stop(LoopOutcome("cancelled", st.run_id, LoopReason.CANCELLED, steps=st.counters.steps))
        if self._budget.run_deadline_s is not None and self._elapsed(st) >= self._budget.run_deadline_s:
            raise _Stop(LoopOutcome("failed", st.run_id, LoopReason.RUN_DEADLINE))
        now = self._clock()
        if self._budget.step_timeout_s is not None and st.open_step and now - st.step_started >= self._budget.step_timeout_s:
            raise _Stop(LoopOutcome("failed", st.run_id, LoopReason.STEP_TIMEOUT))
        if now - st.last_hb >= self._budget.heartbeat_interval_s:
            self._tasks.heartbeat(st.run_id, worker_epoch=self._epoch)
            st.last_hb = now

    def _begin_step(self, st: _State, label: str) -> None:
        step = self._tasks.begin_step(st.run_id, label, worker_epoch=self._epoch)
        st.step_id, st.open_step, st.step_started = step.step_id, step.step_id, self._clock()
        self._tasks.heartbeat(st.run_id, worker_epoch=self._epoch)
        st.last_hb = self._clock()

    def _end_step(self, st: _State, *, ok: bool) -> None:
        if st.open_step:
            self._tasks.complete_step(st.open_step, worker_epoch=self._epoch, ok=ok)
            st.open_step = None
        if ok:
            st.counters.steps += 1

    def _account_usage(self, st: _State, attempt_id: str) -> None:
        meta = self._runtime.status(attempt_id).meta or {}
        usage = meta.get("usage") or {}
        total = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        st.counters.tokens += max(total - st.usage_seen.get(attempt_id, 0), 0)
        st.usage_seen[attempt_id] = total

    def _start_attempt(self, st: _State, cap: Mapping[str, Any], finalize: bool) -> str:
        attempt_id = new_id("att")
        ref = f"snap_{attempt_id}"
        messages = list(st.transcript)
        if finalize:
            messages.append(ChatMessage("system", FINALIZE_NOTE))
        if self._snapshots is not None:
            self._snapshots.put(ref, messages)
        request = RuntimeRequest(
            run_id=st.run_id, attempt_id=attempt_id, snapshot_ref=ref, capability_set=cap,
            cancel_scope={"task_run": st.run_id}, deadline=datetime.now(UTC) + timedelta(minutes=30), mode="agent_loop",
        )
        self._runtime.start(request)
        st.attempt_id = attempt_id
        return attempt_id

    def _run_attempt(self, st: _State, cap: Mapping[str, Any], *, finalize: bool) -> _End:
        try:
            attempt_id = self._start_attempt(st, cap, finalize)
        except KeyError:
            logger.error("attempt 的起始 history 遺失，不呼叫模型：run=%s", st.run_id)
            return _End("fail", reason=LoopReason.SNAPSHOT_MISSING)
        events = iter(self._runtime.stream(attempt_id))
        texts: list[str] = []
        batch: list[Mapping[str, Any]] = []

        def stop(end: _End, *, ok: bool = False) -> _End:
            self._runtime.cancel(attempt_id, end.reason or end.dim or end.kind)
            self._end_step(st, ok=ok)
            return end

        while True:
            if st.open_step is None:
                self._begin_step(st, "finalize" if finalize else "think")
            try:
                self._gate(st)
            except _Stop:
                self._end_step(st, ok=False)
                raise
            event = next(events, None)
            if event is None:
                return stop(_End("fail", reason=LoopReason.STREAM_ENDED))
            if event.kind == "text_delta":
                texts.append(event.text_delta or "")
            elif event.kind == "tool_intent":
                batch.append(event.tool_intent or {})
                if len(batch) < int((event.tool_intent or {}).get("batch_size", 1)):
                    continue
                if finalize:
                    return stop(_End("fail", reason=LoopReason.FINALIZE_TOOL_INTENT))
                intents, batch = batch, []
                text, texts = "".join(texts), []
                end = self._handle_batch(st, cap, attempt_id, intents, text)
                if end is not None:
                    return end
                dim = exhausted(self._budget, st.counters, elapsed_s=self._elapsed(st))
                if dim is not None:
                    self._runtime.cancel(attempt_id, "budget_exhausted")
                    return _End("exhausted", dim=dim)
                if self._active_consult_due(st, cap):
                    active = self._active_consult(st, cap)
                    if active is not None:
                        self._runtime.cancel(attempt_id, active.reason or active.dim or active.kind)
                        return active
                    dim = exhausted(self._budget, st.counters, elapsed_s=self._elapsed(st))  # consult 也消耗步數
                    if dim is not None:
                        self._runtime.cancel(attempt_id, "budget_exhausted")
                        return _End("exhausted", dim=dim)
                prior = visible_tools(cap)
                narrowed = self._offer(st, self._narrow(self._projector(st.frozen_tags, prior), st.frozen_tools, prior))
                if visible_tools(narrowed) != prior:  # AL-3：只可能變窄；變窄就以較窄的集合重開 attempt
                    self._runtime.cancel(attempt_id, "capability_narrowed")
                    return _End("restart", cap=narrowed)
            elif event.kind == "result":
                self._account_usage(st, attempt_id)
                self._end_step(st, ok=True)
                status = (event.result or {}).get("status", "ok")
                if status == "max_rounds":
                    return _End("exhausted", dim="rounds")
                return _End("result", text="".join(texts))
            elif event.kind == "error":
                self._end_step(st, ok=False)
                return _End("fail", reason=runtime_reason(str((event.error or {}).get("code", "PROVIDER_UNAVAILABLE"))))

    # ───────────────────────── 內部：批次執行 ─────────────────────────

    def _blocked(self, reason: str) -> dict[str, Any]:
        return self._gateway.result_view("blocked", reason_code=reason)

    def _side_effect_is_read(self, tool: str) -> bool:
        try:
            return self._registry.entry(tool).spec.side_effect_class == "internal_read"
        except Exception:  # noqa: BLE001  查不到就當非唯讀（保守）
            return False

    def _handle_batch(
        self, st: _State, cap: Mapping[str, Any], attempt_id: str, intents: list[Mapping[str, Any]], text: str,
    ) -> _End | None:
        """處理一整批 tool_intent。回 None 表示續跑；否則回 park／fail 的終局（attempt 已取消、Step 已結束）。"""
        step_id = st.step_id or ""
        n = len(intents)
        calls = [
            {"id": str(it.get("call_id") or f"{attempt_id}:{k}"), "name": str(it["tool"]), "args": dict(it.get("args") or {})}
            for k, it in enumerate(intents)
        ]
        views: dict[int, Mapping[str, Any]] = {}
        observed: dict[int, str] = {}  # 序號 -> 狀態（無進展偵測用，依序號處理）

        kept, deferred = defer_tail(list(range(n)), self._budget.max_batch, DEFERRED_BATCH_LIMIT)
        for k, reason in deferred:
            views[k] = self._blocked(reason)
        screened: list[ScreenedIntent] = []
        consult_left = max(self._budget.max_consults - st.counters.consults, 0)
        for k in kept:
            it = intents[k]
            if not it.get("args_complete"):
                views[k] = self._gateway.result_view("rejected", reason_code=INVALID_TOOL_ARGS)
                observed[k] = "rejected"
                continue
            is_consult = calls[k]["name"] == CONSULT_TOOL
            res = self._core.screen_intent(
                run_id=st.run_id, step_id=step_id, ordinal=k, capability_set=cap, tool=calls[k]["name"],
                args=self._prefill(calls[k]["name"], it.get("args")),
                injected=self._consult_injected(st, step_id, k) if is_consult else self._run_injected(st),
            )
            if isinstance(res, IntentResult):
                views[k], observed[k] = res.view, "rejected"
            elif is_consult and consult_left <= 0:  # 同批多個 consult：超出 max_consults 的不執行
                views[k] = self._blocked(DEFERRED_BUDGET)
            else:
                consult_left -= 1 if is_consult else 0
                screened.append(res)
        remaining = self._budget.max_tool_calls - st.counters.tool_calls
        runnable, over = defer_tail(screened, remaining, DEFERRED_BUDGET)
        for item, reason in over:
            views[item.ordinal] = self._blocked(reason)

        by_ordinal = {s.ordinal: s for s in runnable}
        waves = plan_waves(
            [SchedItem(s.ordinal, s.tool, s.args) for s in runnable], self._registry, max_parallel=self._budget.max_parallel,
        )
        parked_at: int | None = None
        escalated: tuple[int, str, Mapping[str, Any]] | None = None  # (序號, 缺口碼, consult 結果)
        pending: list[str] = []
        unknown_block = False
        consult_digest: dict[int, dict[str, Any]] = {}
        st.batch = _BatchCtx(calls, text, views)
        for wave in waves:
            members = [by_ordinal[i.index] for i in wave.items]
            if escalated is not None:
                continue  # escalate 後同批其餘項不執行、不回饋
            if parked_at is not None:
                for m in members:
                    views[m.ordinal] = self._blocked(DEFERRED_BY_APPROVAL_WAIT)
                continue
            if unknown_block:
                for m in members:
                    views[m.ordinal] = self._blocked(UNKNOWN_UNRESOLVED)
                continue
            self._gate(st)
            for m, res in self._execute_wave(wave, members, st):
                views[m.ordinal], observed[m.ordinal] = res.view, str(res.view.get("status", "unknown"))
                if self._composite_entry(m.tool) is None:  # composite 的子步驟已各自計次（_execute_composite）
                    st.counters.tool_calls += 1
                if res.view.get("status") == "unknown":
                    st.unresolved = True
                if res.record.action_id and not res.approval_required:
                    st.consumed.append(res.record.action_id)
                if m.tool == CONSULT_TOOL:
                    st.counters.consults += 1
                    info = self._consult_info(st, m.action_id) if res.view.get("status") == "ok" else None
                    if info is not None:
                        # 無進展偵測以「sub_kind＋結果」為指紋（args 判不出同 kind 同結果，§4.4）
                        consult_digest[m.ordinal] = {
                            "sub_kind": info.get("sub_kind"), "reason": info.get("reason_code"),
                            "escalate": bool((info.get("escalate") or {}).get("needed")),
                        }
                        gap = self._recheck_escalation(st, info)
                        if gap is not None and escalated is None:
                            escalated = (m.ordinal, gap, info)
                if res.approval_required and parked_at is None:
                    parked_at = m.ordinal
                    pending.append(res.record.action_id or m.action_id)  # composite：待核准的是子步驟的 action
                elif res.view.get("status") == "unknown" and not self._side_effect_is_read(m.tool):
                    unknown_block = True  # TC-3：未決副作用先解決，同批尚未開始的項不執行

        st.batch = None
        no_progress: str | None = None
        for k in sorted(observed):
            args_for_fp = consult_digest.get(k, calls[k]["args"])
            reason = observe_call(self._budget, st.counters, calls[k]["name"], args_for_fp, observed[k])
            no_progress = no_progress or reason

        # history：assistant 訊息帶整批呼叫；tool 訊息依序號回餵。park 時只回餵 park 之前的連續前綴，
        # 其餘由 resume 補齊（每個 tool_call 最終都要有對應的 tool 訊息）
        st.transcript.append(ChatMessage("assistant", text, tool_calls=tuple(calls)))
        limit = n
        if parked_at is not None:
            limit = parked_at
        if escalated is not None:
            limit = min(limit, escalated[0])  # escalate 的結果與其後各項都不進 history（不回饋模型）
        for k in range(limit):
            st.transcript.append(ChatMessage("tool", result_text(views[k]), tool_call_id=calls[k]["id"]))
        self._account_usage(st, attempt_id)
        self._end_step(st, ok=True)

        if escalated is not None:
            self._runtime.cancel(attempt_id, "escalated")
            return _End("escalate", reason=escalated[1], info=escalated[2])
        if parked_at is not None:
            self._checkpoint(st, f"await_approval:{pending[0]}", pending)
            self._runtime.cancel(attempt_id, "park")
            return _End("park", pending=tuple(pending))
        if no_progress is not None:
            self._runtime.cancel(attempt_id, no_progress)
            return _End("fail", reason=LoopReason.NO_PROGRESS)
        for k in range(n):
            self._runtime.submit_tool_result(attempt_id, stable_action_id(st.run_id, step_id, k), views[k])
        self._checkpoint(st, "think")
        return None

    def _execute_wave(
        self, wave: Wave, members: list[ScreenedIntent], st: _State,
    ) -> list[tuple[ScreenedIntent, IntentResult]]:
        """波內先全部 create_action，再執行（並行波用執行緒）；單一失敗不影響兄弟。結果依序號排列。

        波內有 ``parallel_one_shot`` 項時改走 ``_run_watched``：每項有自己的逾時，逾時／取消／StaleWorkerEpoch 時對在途的
        one_shot 項呼叫 ``executor.cancel``（§6.4）。其餘波（只有 ``parallel_safe`` 或 serial）行為不變。"""
        out: dict[int, IntentResult] = {}
        runnable: list[ScreenedIntent] = []
        for m in members:
            early = None if self._composite_entry(m.tool) is not None else self._core.create_screened(m)  # composite 不建 action
            if early is not None:
                out[m.ordinal] = early
            else:
                runnable.append(m)
        stale: list[StaleWorkerEpoch] = []

        def run_one(m: ScreenedIntent) -> None:
            try:
                entry = self._composite_entry(m.tool)
                if entry is None:
                    out[m.ordinal] = self._core.execute_screened(m)
                else:  # composite 恆為 serial 單獨一波（scheduler）；全新執行沒有 cursor，不會有 _Halt
                    result = self._execute_composite(st, m, entry)
                    assert isinstance(result, IntentResult)
                    out[m.ordinal] = result
            except StaleWorkerEpoch as exc:
                stale.append(exc)

        timeouts = {m.ordinal: t for m in runnable if wave.parallel and (t := self._one_shot_timeout(m.tool)) is not None}
        if timeouts:
            self._run_watched(runnable, timeouts, out, stale, st)
        elif wave.parallel and len(runnable) > 1:
            with ThreadPoolExecutor(max_workers=len(runnable)) as pool:
                list(pool.map(run_one, runnable))
        else:
            for m in runnable:
                run_one(m)
                if stale:
                    break
        if stale:
            raise stale[0]
        return [(m, out[m.ordinal]) for m in members if m.ordinal in out]

    # ───────────────────────── 內部：composite 的線性展開（§5.7.4） ─────────────────────────

    def _composite_entry(self, tool: str) -> Any | None:
        """composite 條目（``ToolEntry.composite`` 非 None）；一般工具與查不到的工具回 None。"""
        try:
            entry = self._registry.entry(tool)
        except Exception:  # noqa: BLE001  查不到就當一般工具（screen 階段已有 tool_unknown 處理）
            return None
        return entry if getattr(entry, "composite", None) is not None else None

    def _prefill(self, tool: str, args: Any) -> Any:
        """``initial_params`` 預填：只補模型沒給的欄位（先於 Gateway 驗證，使本來要詢問的必填欄位不必再問）。"""
        entry = self._composite_entry(tool)
        if entry is None or not entry.composite.initial_params or not isinstance(args, Mapping):
            return args
        return {**{k: v for k, v in entry.composite.initial_params.items() if k not in args}, **args}

    @staticmethod
    def _valid_cursor(raw: Any) -> dict[str, Any] | None:
        """checkpoint 的 ``composite_cursor`` 形狀檢查；不合回 None（呼叫端 fail-closed）。"""
        if not isinstance(raw, Mapping):
            return None
        rows, ordinal, nxt, tool = raw.get("rows"), raw.get("ordinal"), raw.get("next"), raw.get("tool")
        if not (isinstance(rows, list) and isinstance(ordinal, int) and isinstance(nxt, int) and isinstance(tool, str)):
            return None
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping) or row.get("step") != index or row.get("status") not in _SEVERITY:
                return None
            if not isinstance(row.get("tool"), str):
                return None
        if nxt != len(rows):
            return None
        return {"ordinal": ordinal, "tool": tool, "next": nxt, "rows": [dict(r) for r in rows]}

    @staticmethod
    def _composite_inputs(entry: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        """``$.input`` 的來源：驗證並預填後的輸入，缺的欄位補 schema 的 ``default``。"""
        values = dict(args)
        for name, prop in (entry.descriptor.input_schema.get("properties") or {}).items():
            if name not in values and isinstance(prop, Mapping) and "default" in prop:
                values[name] = prop["default"]
        return values

    def _step_outputs(self, action_id: str) -> dict[str, Any]:
        """某個已成功子步驟的輸出欄位：回合範圍結果的 ``text``、receipt 的 ``result_ref``，再疊上注入的 ``step_output``。"""
        out: dict[str, Any] = {}
        text = self._content_lookup(action_id) if self._content_lookup is not None else None
        if text is not None:
            out["text"] = text
        try:
            receipt = self._tasks.get_receipt(action_id)
        except Exception:  # noqa: BLE001  取不到 receipt 不影響其他欄位
            receipt = None
        ref = getattr(receipt, "result_ref", None)
        if ref:
            out["result_ref"] = ref
        if self._step_output is not None:
            extra = self._step_output(action_id)
            if isinstance(extra, Mapping):
                out.update(extra)
        return out

    @staticmethod
    def _row(step: int, tool: str, status: str, **extra: Any) -> dict[str, Any]:
        return {"step": step, "tool": tool, "status": status, **{k: v for k, v in extra.items() if v}}

    def _row_of(self, step: int, tool: str, res: IntentResult) -> dict[str, Any]:
        view = res.view
        return self._row(
            step, tool, str(view.get("status", "unknown")), action_id=res.record.action_id, reason_code=view.get("reason_code"),
            degraded=bool(view.get("degraded")), fields=list(view.get("fields") or ()),
        )

    @staticmethod
    def _cursor_of(ordinal: int, tool: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """進 checkpoint 的 cursor：逐步狀態短碼（``fields`` 是巢狀清單且只對當次回饋有用，不存）。"""
        return {
            "ordinal": ordinal, "tool": tool, "next": len(rows),
            "rows": [{k: v for k, v in row.items() if k != "fields"} for row in rows],
        }

    def _composite_progress(self, st: _State, m: ScreenedIntent, entry: Any, rows: Sequence[Mapping[str, Any]]) -> None:
        """每完成一步就寫 checkpoint（崩潰後從 cursor 續跑、不重播已成功的步驟）。批次執行中 history 尚未寫入，所以在這裡
        暫時補上 assistant 訊息與前序呼叫的 tool 訊息（與 park 的形狀相同：其餘呼叫的結果由 resume 補齊）。"""
        st.composite_cursor = self._cursor_of(m.ordinal, entry.name, rows)
        transcript = None
        if st.batch is not None:
            b = st.batch
            transcript = [
                *st.transcript, ChatMessage("assistant", b.text, tool_calls=tuple(b.calls)),
                *(ChatMessage("tool", result_text(b.views[k]), tool_call_id=b.calls[k]["id"]) for k in range(m.ordinal) if k in b.views),
            ]
        self._checkpoint(st, "composite", transcript=transcript)

    def _execute_composite(
        self, st: _State, m: ScreenedIntent, entry: Any, *, cursor: Mapping[str, Any] | None = None,
        approvals: Mapping[str, str] | None = None, pending: frozenset[str] = frozenset(),
    ) -> IntentResult | _Halt:
        """依序展開 composite 的子步驟；composite 本身不建 ActionIntent，每個子步驟走完整的 screen／create／execute。

        回傳彙整結果（``IntentResult``）；子步驟需核准時回 ``approval_required=True`` 的結果（``record.action_id`` 是子 action，
        ``st.composite_cursor`` 已更新、由呼叫端 park）。``cursor`` 只在 resume 時給：已成功的步驟不重播，從 ``cursor.next`` 續跑，
        該步（待決的那一個）的結果依 ``_pending_stop`` 判定，不能續跑時回 ``_Halt``。``pending``：checkpoint 的待核准 action
        （Run 此時是 WAITING_APPROVAL）；待決步驟連執行階段都到不了（能力、路徑、screen、建立被拒）也一樣回 ``_Halt``，
        不回 rejected／blocked 視圖喚醒模型（同 ``_resume_pending``）。"""
        steps = entry.composite.steps
        rows = [dict(r) for r in cursor["rows"]] if cursor is not None else []
        inputs = self._composite_inputs(entry, m.args)
        sub_ids = [stable_action_id(m.run_id, m.step_id, m.ordinal, j) for j in range(len(steps))]
        outputs: dict[int, dict[str, Any]] = {}

        def output_of(k: int) -> Mapping[str, Any] | None:
            if k >= len(rows) or rows[k]["status"] != "ok":
                return None
            if k not in outputs:
                schema = self._registry.entry(steps[k].tool).output_schema or {}
                declared = set(schema.get("properties") or {})  # 受 output_schema 約束：沒宣告的欄位不可引用
                outputs[k] = {key: v for key, v in self._step_outputs(sub_ids[k]).items() if key in declared}
            return outputs[k]

        resumed = cursor is not None  # 只有續跑的第一步是「待決步驟」
        while len(rows) < len(steps) and all(r["status"] == "ok" for r in rows):
            j = len(rows)
            self._gate(st)
            if not resumed and st.counters.tool_calls >= self._budget.max_tool_calls:
                step_result: dict[str, Any] | IntentResult | _Halt = self._row(j, steps[j].tool, "blocked", reason_code=DEFERRED_BUDGET)
            else:
                step_result = self._composite_step(
                    st, m, j, steps[j].tool, steps[j].params, inputs, output_of, sub_ids[j], (approvals or {}).get(sub_ids[j]),
                    resumed, resumed and sub_ids[j] in pending,
                )
            resumed = False
            if isinstance(step_result, _Halt):
                return step_result
            if isinstance(step_result, IntentResult):  # 子步驟需核准：記下進度並停住（呼叫端 park）
                st.composite_cursor = self._cursor_of(m.ordinal, entry.name, rows)
                return step_result
            rows.append(step_result)
            self._composite_progress(st, m, entry, rows)

        st.composite_cursor = None
        if any(r["status"] == "unknown" for r in rows):
            st.unresolved = True
        return self._composite_result(m, steps, rows)

    def _composite_step(
        self, st: _State, m: ScreenedIntent, j: int, tool: str, params: Mapping[str, Any], inputs: Mapping[str, Any],
        output_of: Callable[[int], Mapping[str, Any] | None], sub_id: str, approval_id: str | None, resumed: bool,
        pending_step: bool = False,
    ) -> dict[str, Any] | IntentResult | _Halt:
        """一個子步驟：能力範圍 -> 路徑解析 -> screen -> create -> execute。回逐步狀態列（dict）、需核准的結果、或 ``_Halt``。

        ``pending_step``：這一步是 resume 時有待核准 action 的步驟（Run 在 WAITING_APPROVAL）；它在抵達 execute 之前被擋下
        一律回 ``_Halt``（原因碼對照 ``_pending_stop``），否則 composite 會回 rejected／blocked 視圖喚醒模型。"""
        # AL-3：子步驟只能在 Run 凍結的能力範圍內（且當下仍可見）；不在範圍 ⇒ 這一步 blocked，composite 失敗
        if tool not in st.frozen_tools or tool not in visible_tools(st.cap):
            if pending_step:
                return _Halt(LoopReason.CAPABILITY_REVOKED, sub_id)
            return self._row(j, tool, "blocked", reason_code=TOOL_NOT_IN_SNAPSHOT)
        try:
            resolved = resolve_params(params, inputs=inputs, output_of=output_of)
        except CompositePathError:
            if pending_step:  # 引用的前步輸出已不可得（例如重啟後回合範圍暫存已清）：核准的參數無從重建
                return _Halt(LoopReason.INTENT_REJECTED, sub_id)
            return self._row(j, tool, "rejected", reason_code=INVALID_TOOL_ARGS, fields=[f"/steps/{j}/params"])
        screened = self._core.screen_intent(
            run_id=m.run_id, step_id=m.step_id, ordinal=m.ordinal, sub_index=j, capability_set=st.cap, tool=tool, args=resolved,
            injected=self._run_injected(st),
        )
        if isinstance(screened, IntentResult):
            stop = self._pending_stop(screened) if pending_step else None
            return _Halt(stop, sub_id) if stop is not None else self._row_of(j, tool, screened)
        early = self._core.create_screened(screened)
        if early is not None:
            stop = self._pending_stop(early) if pending_step else None
            return _Halt(stop, sub_id) if stop is not None else self._row_of(j, tool, early)
        res = self._core.execute_screened(screened, approval_id=approval_id)
        if res.approval_required and not resumed:
            return res
        if resumed:
            if res.record.reason == "PresenceRequired" and res.retryable is False:
                st.counters.tool_calls += 1  # 階段 1.5：核准已消耗、action 已 FAILED ⇒ 等同已執行且失敗
                st.consumed.append(sub_id)
                return self._row(j, tool, "failed", action_id=sub_id)
            stop = self._pending_stop(res)
            if stop is not None:
                return _Halt(stop, sub_id)
        if not res.approval_required:
            st.counters.tool_calls += 1
            if res.record.action_id:
                st.consumed.append(res.record.action_id)
        return self._row_of(j, tool, res)

    def _composite_result(self, m: ScreenedIntent, steps: Sequence[Any], rows: Sequence[Mapping[str, Any]]) -> IntentResult:
        """彙整結果：逐步狀態列為 ``items``（沒執行的步驟列為 blocked(composite_step_skipped)），整體 ``status`` 取最差者；
        全部成功時，對外內容取最後一步的結果文字（若有）。經 Gateway 的 ``result_view`` 組成，不含例外文字與參數內容。"""
        items = [dict(r) for r in rows]
        items += [self._row(j, steps[j].tool, "blocked", reason_code=COMPOSITE_STEP_SKIPPED) for j in range(len(rows), len(steps))]
        worst = max((str(r["status"]) for r in rows), key=_SEVERITY.__getitem__)
        bad = next((r for r in rows if r["status"] != "ok"), {})
        if worst == "ok" and len(rows) == len(steps) and self._content_lookup is not None:
            content = self._content_lookup(str(rows[-1].get("action_id", "")))
            if content is not None:
                items[-1]["content"] = content
        view = self._gateway.result_view(
            worst, items=items, degraded=any(r.get("degraded") for r in rows), reason_code=bad.get("reason_code"),
            fields=bad.get("fields") or (),
        )
        return IntentResult(IntentRecord(m.ordinal, m.tool, None, view["status"], bad.get("reason_code")), view)

    def _resume_composite(
        self, st: _State, ordinal: int, call: Mapping[str, Any], cursor: Mapping[str, Any], step_id: str,
        pending: frozenset[str], allowed: frozenset[str], approvals: Mapping[str, str],
    ) -> LoopOutcome | Mapping[str, Any]:
        """resume（核准後或崩潰後）接續 composite：從 ``cursor.next`` 續跑，已成功的步驟不重播。回終局（再 park／結束 Run）或
        要補進 history 的彙整視圖。待決子步驟的判定同 ``_resume_pending``（缺／過期核准 ⇒ 再 park；其餘依 ``_pending_stop``）。"""
        entry = self._composite_entry(call["name"])
        sub_id = stable_action_id(st.run_id, step_id, ordinal, cursor["next"])
        if call["name"] not in allowed:
            if sub_id in pending:  # 與一般 pending 項相同：能力已被撤銷，核准不消耗
                logger.warning("resume：composite %s 不在當下可見集合，Run 改轉 FAILED(capability_revoked)：run=%s", call["name"], st.run_id)
                return self._finish(st, "failed", LoopReason.CAPABILITY_REVOKED)
            st.composite_cursor = None
            return self._blocked(TOOL_NOT_IN_SNAPSHOT)
        screened = self._core.screen_intent(
            run_id=st.run_id, step_id=step_id, ordinal=ordinal, capability_set=st.cap, tool=call["name"],
            args=self._prefill(call["name"], call["args"]), injected=self._run_injected(st),
        )
        if isinstance(screened, IntentResult):
            logger.warning("resume：composite %s 重新篩選被拒，Run 改轉 FAILED(intent_rejected)：run=%s", call["name"], st.run_id)
            return self._finish(st, "failed", LoopReason.INTENT_REJECTED)
        result = self._execute_composite(st, screened, entry, cursor=cursor, approvals=approvals, pending=pending)
        if isinstance(result, _Halt):
            if result.stop == "repark":
                return self._repark(st, result.action_id)
            logger.warning("resume：composite 待決步驟沒有執行，Run 改轉 FAILED(%s)：action=%s", result.stop.value, result.action_id)
            return self._finish(st, "failed", result.stop)
        if result.approval_required:  # 後面的步驟也需要核准：再次 park（cursor 已更新）
            return self._repark(st, str(result.record.action_id))
        return result.view

    # ───────────────────────── 內部：parallel_one_shot 的逾時與取消（§6.4） ─────────────────────────

    def _one_shot_timeout(self, tool: str) -> float | None:
        """``parallel_one_shot`` 工具的 ``timeout_s``；其他工具（含查不到）回 None。parallel_safe 的 ``timeout_s`` 本期不強制。"""
        try:
            entry = self._registry.entry(tool)
        except Exception:  # noqa: BLE001  查不到就當一般項
            return None
        if entry.concurrency != "parallel_one_shot" or not entry.timeout_s:
            return None
        return float(entry.timeout_s)

    def _cancel_targets(self, items: Sequence[ScreenedIntent]) -> dict[str, str]:
        """對在途 one_shot 項呼叫 ``executor.cancel``（平行、有總逾時）；回傳 action_id -> 結果。"""
        targets = []
        for m in items:
            executor = self._executors.get(self._registry.entry(m.tool).spec.executor)
            if executor is not None:
                targets.append((m.action_id, executor))
        return dict(cancel_executors(targets, timeout_s=self._cancel_timeout_s)) if targets else {}

    def _run_watched(
        self, runnable: list[ScreenedIntent], timeouts: Mapping[int, float], out: dict[int, IntentResult],
        stale: list[StaleWorkerEpoch], st: _State,
    ) -> None:
        """並行執行整波（daemon 執行緒，不 join 卡住的 worker），one_shot 項各有 ``timeout_s`` 期限。

        - 逾時：``_settle_timeouts``（同時到期的項一起處理，總耗時有上限）。同波兄弟照常。
        - 任一項 ``StaleWorkerEpoch``：對其餘在途 one_shot 項 cancel 後結束等待，由呼叫端拋出。
        - Run 被取消（``cancelled()``）：對在途 one_shot 項 cancel，限時等它們收尾（已簽回的 receipt 照常入庫，R-2）；
          寬限後仍在途的項（含同波 ``parallel_safe``）一律轉 UNKNOWN（``run_cancelled``），不留在 EXECUTING；然後以
          ``_Stop(cancelled)`` 結束。"""
        lock = threading.Lock()
        wake = threading.Event()
        by = {m.ordinal: m for m in runnable}
        finished = {o: threading.Event() for o in by}
        settled: set[int] = set()  # 已由逾時／取消路徑接手的序號：晚到的結果與 StaleWorkerEpoch 都不再寫入
        errors: list[BaseException] = []

        def work(m: ScreenedIntent) -> None:
            try:
                res = self._core.execute_screened(m)
                with lock:
                    if m.ordinal not in settled:
                        out[m.ordinal] = res
            except StaleWorkerEpoch as exc:
                with lock:
                    if m.ordinal not in settled:
                        stale.append(exc)
            except Exception as exc:  # noqa: BLE001  與 pool.map 相同：其他例外在等待結束後上拋
                with lock:
                    errors.append(exc)
            finally:
                finished[m.ordinal].set()
                wake.set()

        start = time.monotonic()
        deadline = {o: start + t for o, t in timeouts.items()}
        for m in runnable:
            threading.Thread(target=work, args=(m,), name="tool-wave", daemon=True).start()
        pending = set(by)
        halted: Literal["stale", "cancel"] | None = None
        while pending:
            wake.wait(0.02)
            wake.clear()
            pending = {o for o in pending if not finished[o].is_set()}
            with lock:
                has_stale = bool(stale)
            if has_stale:
                halted = "stale"
                break
            if not pending:
                break
            if self._cancelled():
                halted = "cancel"
                break
            now = time.monotonic()
            due = sorted(o for o in pending if o in deadline and now >= deadline[o])
            if due:
                self._settle_timeouts([by[o] for o in due], finished, out, stale, lock, settled)
                pending.difference_update(due)
        if halted is not None:
            inflight = [by[o] for o in sorted(pending) if o in timeouts]
            results = self._cancel_targets(inflight)
            logger.info("波內中止（%s），對 %d 個在途 one_shot 項呼叫 cancel：%s", halted, len(inflight), results)
            if halted == "cancel":
                grace_end = time.monotonic() + self._cancel_grace_s
                for o in sorted(pending):
                    finished[o].wait(max(0.0, grace_end - time.monotonic()))
                for o in sorted(o for o in pending if not finished[o].is_set()):
                    with lock:
                        settled.add(o)
                    self._tasks.mark_action_unknown(by[o].action_id, worker_epoch=self._epoch, reason="run_cancelled")
                raise _Stop(LoopOutcome("cancelled", st.run_id, LoopReason.CANCELLED, steps=st.counters.steps))
        if errors and not stale:
            raise errors[0]

    def _settle_timeouts(
        self, items: list[ScreenedIntent], finished: Mapping[int, threading.Event], out: dict[int, IntentResult],
        stale: list[StaleWorkerEpoch], lock: threading.Lock, settled: set[int],
    ) -> None:
        """one_shot 項逾時：先 ``executor.cancel``（同時到期的項一起 cancel、共用寬限，總耗時 ≤ cancel 逾時＋寬限）。

        - execute 已收尾（逾時當下或 cancel 期間）⇒ 一律以真實結果為準。``stopped`` 且該項確實因取消而失敗（receipt 的
          ``error_code`` 是 ``timeout``／``cancelled``）⇒ 回餵加 ``reason_code=timeout``；其他真實錯誤保留原樣。
        - 仍未收尾（``isolated``／例外／cancel 逾時／無可用 cancel／``stopped`` 卻沒收尾）⇒ ``mark_action_unknown(timeout)``：
          回 True 才回報 ``unknown``＋``reason_code=timeout``，並嘗試一次 ``resolve_unknown``；回 False 代表 action 已不是 EXECUTING
          （execute 剛好收尾），改用真實結果。"""
        due = [m for m in items if not finished[m.ordinal].is_set()]
        if not due:
            return
        results = self._cancel_targets(due)
        grace_end = time.monotonic() + self._cancel_grace_s
        for m in due:
            outcome = results.get(m.action_id, "skipped")
            logger.info("one_shot 逾時：action=%s tool=%s cancel=%s", m.action_id, m.tool, outcome)
            done = finished[m.ordinal]
            if outcome == "stopped":
                done.wait(max(0.0, grace_end - time.monotonic()))
            try:
                if done.is_set():
                    self._overlay_timeout(m, out, lock, only_if_stopped=outcome == "stopped")
                else:
                    self._unknown_after_timeout(m, done, out, lock, settled, grace_end)
            except StaleWorkerEpoch as exc:
                with lock:
                    stale.append(exc)

    def _is_cancel_failure(self, m: ScreenedIntent) -> bool:
        receipt = self._tasks.get_receipt(m.action_id)
        return receipt is not None and receipt.error_code in CANCEL_ERROR_CODES

    def _overlay_timeout(self, m: ScreenedIntent, out: dict[int, IntentResult], lock: threading.Lock, *, only_if_stopped: bool) -> None:
        with lock:
            res = out.get(m.ordinal)
        if not only_if_stopped or res is None or res.view.get("status") != "failed" or not self._is_cancel_failure(m):
            return  # 真實結果（含真實錯誤）原樣保留；res 為 None 是 StaleWorkerEpoch（已記入 stale）
        with lock:
            out[m.ordinal] = IntentResult(
                IntentRecord(m.ordinal, m.tool, m.action_id, "failed", TOOL_TIMEOUT), {**res.view, "reason_code": TOOL_TIMEOUT},
            )

    def _unknown_after_timeout(
        self, m: ScreenedIntent, done: threading.Event, out: dict[int, IntentResult], lock: threading.Lock,
        settled: set[int], grace_end: float,
    ) -> None:
        marked = self._tasks.mark_action_unknown(m.action_id, worker_epoch=self._epoch, reason="timeout")
        if not marked:
            # action 已不是 EXECUTING：execute 剛好收尾，等它把真實結果交出來
            done.wait(max(0.0, grace_end - time.monotonic()))
            with lock:
                res = out.get(m.ordinal)
                if res is None and not done.is_set():
                    settled.add(m.ordinal)
            if res is not None:
                return
            logger.warning("逾時後 action 已非 EXECUTING 但結果未到：action=%s status=%s", m.action_id, self._tasks.get_action(m.action_id).status)
            if done.is_set():
                return  # StaleWorkerEpoch
        unknown = IntentResult(
            IntentRecord(m.ordinal, m.tool, m.action_id, "unknown", TOOL_TIMEOUT),
            self._gateway.result_view("unknown", reason_code=TOOL_TIMEOUT),
        )
        with lock:
            settled.add(m.ordinal)
            out[m.ordinal] = unknown
        self._try_resolve(m, out, lock)

    def _try_resolve(self, m: ScreenedIntent, out: dict[int, IntentResult], lock: threading.Lock) -> None:
        """轉 UNKNOWN 後試一次 ``resolve_unknown``，**僅在已有已驗證 receipt 時**（此時不會呼叫 executor 查核）；
        沒有 receipt 就維持 UNKNOWN。不在沒有證據時呼叫：``resolve_unknown`` 對 ``confirmed_not_executed`` 會把 action 退回
        INTENDED，而逾時的 execute 可能仍在跑，退回會有重送風險。"""
        try:
            receipt = self._tasks.get_receipt(m.action_id)
            if receipt is None or not receipt.source_verified:
                return
            outcome = self._tasks.resolve_unknown(m.action_id)
            if outcome.status not in (ActionStatus.SUCCEEDED, ActionStatus.FAILED):
                return
            res = self._core.outcome_result(m, outcome)
            if res.view.get("status") == "failed" and receipt.error_code in CANCEL_ERROR_CODES:
                res = IntentResult(
                    IntentRecord(m.ordinal, m.tool, m.action_id, "failed", TOOL_TIMEOUT), {**res.view, "reason_code": TOOL_TIMEOUT},
                )
        except Exception:  # 解決失敗就維持 UNKNOWN
            logger.exception("逾時後 resolve_unknown 失敗，維持 UNKNOWN：action=%s", m.action_id)
            return
        with lock:
            out[m.ordinal] = res

    # ───────────────────────── 內部：resume 補齊 ─────────────────────────

    def _fill_missing(
        self, st: _State, pending: frozenset[str], approvals: Mapping[str, str] | None = None,
    ) -> LoopOutcome | None:
        """為最後一則 assistant 訊息中缺少 tool 訊息的呼叫補上結果：已核准的 pending 項經完整流程執行（冪等，
        不重做已有 receipt 者）；pending 項在凍結集合外 ⇒ ``FAILED(capability_revoked)``；非 pending 的後續項在凍結集合外 ⇒
        blocked(tool_not_in_snapshot)，其餘 ⇒ blocked(deferred_by_approval_wait)。
        pending 項仍缺核准、或其 approval 已過期 ⇒ 再次 park（回 awaiting_approval，Run 維持 WAITING_APPROVAL）；
        approval 有其他問題（不存在、已消耗、hash／target／scope 不符…）⇒ ``FAILED(approval_invalid)``；執行時工具版本已變 ⇒
        ``FAILED(tool_version_changed)``。重驗失敗一律結束 Run：WAITING_APPROVAL 不能轉 SUCCEEDED。

        **pending 最多一個**：需核准的工具是屏障（§6.4），批次裡第一個 ``approval_required`` 之後的項目全部 deferred，
        所以 ``pending`` 至多一個 action，pending 項的終局（再 park／結束 Run）不會漏掉其他 pending。pending 項的各種
        結果與原因碼見 ``_resume_pending``／``_pending_stop``。"""
        assistant = next((m for m in reversed(st.transcript) if m.role == "assistant" and m.tool_calls), None)
        if assistant is None:
            return None
        done = {m.tool_call_id for m in st.transcript if m.role == "tool"}
        step_id = st.step_id or ""
        allowed = visible_tools(st.cap)
        for ordinal, call in enumerate(assistant.tool_calls):
            if call["id"] in done:
                continue
            action_id = stable_action_id(st.run_id, step_id, ordinal)
            cursor = st.composite_cursor
            if cursor is not None and cursor["ordinal"] == ordinal and self._composite_entry(call["name"]) is not None:
                outcome = self._resume_composite(st, ordinal, call, cursor, step_id, pending, allowed, approvals or {})
                if isinstance(outcome, LoopOutcome):
                    return outcome
                st.transcript.append(ChatMessage("tool", result_text(outcome), tool_call_id=call["id"]))
                continue
            if action_id in pending and call["name"] not in allowed:
                # pending 項的能力已被撤銷或收窄（預檢理應已攔下，這裡是保險）：Run 此時仍是 WAITING_APPROVAL
                # （這個 action 還沒執行，Run 尚未回 RUNNING），該狀態不能轉 SUCCEEDED（states.py），繼續跑只會在收尾
                # 被拒並誤判成 unresolved_actions ⇒ 直接結束 Run。只限 pending 項：非 pending 的後續項是在 pending 項
                # 執行（Run 已回 RUNNING、副作用可能已發生）之後才處理，被收窄時照舊回 blocked(tool_not_in_snapshot)
                logger.warning("resume：pending 工具 %s 不在當下可見集合，Run 改轉 FAILED(capability_revoked)：run=%s", call["name"], st.run_id)
                return self._finish(st, "failed", LoopReason.CAPABILITY_REVOKED)
            elif call["name"] not in allowed:
                view = self._blocked(TOOL_NOT_IN_SNAPSHOT)
            elif action_id in pending:
                outcome = self._resume_pending(st, ordinal, call, action_id, step_id, approvals or {})
                if isinstance(outcome, LoopOutcome):
                    return outcome
                view = outcome
            else:
                view = self._blocked(DEFERRED_BY_APPROVAL_WAIT)
            st.transcript.append(ChatMessage("tool", result_text(view), tool_call_id=call["id"]))
        return None

    def _resume_pending(
        self, st: _State, ordinal: int, call: Mapping[str, Any], action_id: str, step_id: str, approvals: Mapping[str, str],
    ) -> LoopOutcome | Mapping[str, Any]:
        """resume 時處理一個 pending 項：已執行（成功或失敗，有 receipt）⇒ 回 tool 視圖讓迴圈續跑；其餘一律不續跑，回終局。

        不變量：**pending 最多一個**。需核准的工具是屏障（§6.4）：批次裡第一個 ``approval_required`` 之後的項目全部 deferred，
        所以 checkpoint 的 ``pending_approvals`` 只會有那一個 action；因此這裡的終局（再 park／結束 Run）不會漏掉其他 pending。

        沒有真的執行的結果（screen 被拒、建立被拒、參數 hash 不符、Presence 不足、TC-3 被擋、UNKNOWN）若放行續跑，Run 仍是
        WAITING_APPROVAL，白喚醒一次模型，最後還會被補成 ``unresolved_actions``；所以直接結束 Run（原因碼見 ``_pending_stop``），
        可重試的 ``PresenceRequired`` 與缺／過期核准一樣再次 park；不可重試的（階段 1.5，action 已 FAILED、核准已消耗）視為已執行且失敗。"""
        screened = self._core.screen_intent(
            run_id=st.run_id, step_id=step_id, ordinal=ordinal, capability_set=st.cap, tool=call["name"], args=call["args"],
            injected=self._run_injected(st),
        )
        if isinstance(screened, IntentResult):
            res = screened
        else:
            res = self._core.create_screened(screened) or self._core.execute_screened(
                screened, approval_id=approvals.get(action_id),
            )
        if res.record.reason == "PresenceRequired" and res.retryable is False:
            # 階段 1.5 被擋（不可重試）：action 已是終態 FAILED、核准已消耗、Run 已回 RUNNING ⇒ 等同「已執行且失敗」，
            # 回 failed 視圖續跑（與工具失敗一致），不再 park（再 park 會讓已消耗核准的 action 永遠等不到下一份核准）
            st.counters.tool_calls += 1
            st.consumed.append(action_id)
            return self._gateway.result_view("failed")
        stop = self._pending_stop(res)
        if stop is None:  # 已執行（成功或失敗）：沿用既有計數續跑
            st.counters.tool_calls += 1
            st.consumed.append(action_id)
            return res.view
        if stop == "repark":
            return self._repark(st, action_id)
        logger.warning(
            "resume：pending 項沒有執行，Run 改轉 FAILED(%s)：action=%s status=%s reason=%s",
            stop.value, action_id, res.view.get("status"), res.record.reason,
        )
        return self._finish(st, "failed", stop)

    @staticmethod
    def _pending_stop(res: IntentResult) -> LoopReason | Literal["repark"] | None:
        """pending 項的結果 -> 下一步：None＝已執行（續跑）、``"repark"``＝再 park、``LoopReason``＝結束 Run 的原因碼。

        | 結果 | 原因碼 |
        |---|---|
        | 缺 approval／approval 過期／Presence 不足（可重試）／``tool_not_ready``（Gateway 擋下，未建 action） | 再 park（``approval_required``） |
        | 其他 ApprovalInvalid | ``approval_invalid`` |
        | 執行時工具版本已變 | ``tool_version_changed`` |
        | screen／建立被拒（Gateway、PolicyViolation） | ``intent_rejected`` |
        | 參數 hash 不符 | ``params_mismatch`` |
        | action 本身 UNKNOWN／EXECUTING（``block_code=action_unresolved``） | ``unresolved_actions`` |
        | TC-3／其他 ActionBlocked、建立時的基礎設施錯誤 | ``action_blocked`` |
        | UNKNOWN（executor 例外、結果未明） | ``unresolved_actions`` |
        | 人類否決（action 已 FAILED、receipt ``approval_denied``） | None（回 rejected 視圖續跑，E2） |
        """
        if res.block_code == TOOL_VERSION_CHANGED:
            return LoopReason.TOOL_VERSION_CHANGED
        if res.block_code == ACTION_UNRESOLVED:  # action 本身 UNKNOWN／EXECUTING：副作用可能已發生，不是單純被擋
            return LoopReason.UNRESOLVED_ACTIONS
        if res.approval_code is not None:
            return "repark" if res.approval_code == "expired" else LoopReason.APPROVAL_INVALID
        if res.approval_required or res.record.reason in ("PresenceRequired", TOOL_NOT_READY):
            return "repark"  # 暫時性（Presence／模組暖機）：核准沒被消耗，恢復後同一份核准可再用
        status = res.view.get("status")
        if res.record.action_id and status in ("ok", "failed"):
            return None
        if status == "rejected" and res.view.get("reason_code") == APPROVAL_DENIED:
            return None  # 人類否決（E2）：action 已是終態 FAILED，回 rejected 視圖讓迴圈續跑，不結束 Run
        if status == "unknown":
            return LoopReason.UNRESOLVED_ACTIONS
        if status == "rejected":
            if res.record.reason == "params_hash_mismatch":
                return LoopReason.PARAMS_MISMATCH
            return LoopReason.INTENT_REJECTED
        return LoopReason.ACTION_BLOCKED  # blocked（TC-3 等）或建立時的基礎設施錯誤
