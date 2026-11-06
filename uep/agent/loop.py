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
from uep.agent.core import AgentCore, IntentResult, ScreenedIntent, stable_action_id
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
    TOOL_VERSION_CHANGED,
    IllegalTransition,
    StaleWorkerEpoch,
)
from uep.tasks.states import RunStatus
from uep.tools.catalog.decision import CONSULT_TAG, CONSULT_TOOL, INJECTED_CONSULT
from uep.tools.gateway import INVALID_TOOL_ARGS, ToolGateway, visible_tools

logger = logging.getLogger(__name__)

LoopState = Literal["completed", "failed", "awaiting_approval", "cancelled", "stale", "rejected"]
Projector = Callable[[frozenset[str], frozenset[str] | None], Mapping[str, Any]]

TOOL_NOT_IN_SNAPSHOT = "tool_not_in_snapshot"
RESUME_REQUIRED_KEYS = (
    "child_context_ref", "history_hash", "frozen_tools", "frozen_tags", "visible_tools", "budget_counters", "step_id",
    "frozen_modalities", "frozen_platforms",
)
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
    ) -> None:
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
        pending = frozenset(checkpoint.get("pending_approvals") or ())
        revoked = self._precheck_pending(st, projected, pending)
        if revoked is not None:
            return self._guarded(st, lambda: self._finish(st, "failed", revoked))
        return self._guarded(st, lambda: self._drive_inner(st, pending, approvals or {}))

    def _precheck_pending(self, st: _State, projected: Mapping[str, Any], pending: frozenset[str]) -> LoopReason | None:
        """resume 預檢（只看能力與版本，不看 approval）：尚缺 tool 訊息的 pending 工具必須在凍結集合與當下投影內，且投影版本＝凍結版本。
        不通過回對應原因碼（呼叫端結束 Run、不消耗 approval）；通過回 None。Manager 的 tool_version 檢查保留作最後一道防線。"""
        if not pending:
            return None
        assistant = next((m for m in reversed(st.transcript) if m.role == "assistant" and m.tool_calls), None)
        if assistant is None:
            return None
        done = {m.tool_call_id for m in st.transcript if m.role == "tool"}
        step_id = st.step_id or ""
        current = {t["tool"]: int(t.get("version", 1)) for t in projected.get("tools", ())}
        for ordinal, call in enumerate(assistant.tool_calls):
            if call["id"] in done or stable_action_id(st.run_id, step_id, ordinal) not in pending:
                continue
            name = call["name"]
            if name not in st.frozen_tools or name not in current:
                logger.warning("resume 預檢：pending 工具 %s 不在凍結集合或當下投影，Run 改轉 FAILED：run=%s", name, st.run_id)
                return LoopReason.CAPABILITY_REVOKED
            if current[name] != st.frozen_tools[name]:
                logger.warning("resume 預檢：pending 工具 %s 版本已變，Run 改轉 FAILED：run=%s", name, st.run_id)
                return LoopReason.TOOL_VERSION_CHANGED
        return None

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

    def _snapshot_tags(self, st: _State) -> frozenset[str]:
        """目前快照的能力標籤：當下可見工具的標籤（不比可見集合寬）。"""
        tags: set[str] = set()
        for name in visible_tools(st.cap):
            try:
                tags.add(self._registry.entry(name).spec.capability)
            except Exception:  # noqa: BLE001  查不到的工具不貢獻標籤（保守）
                logger.debug("snapshot 標籤：查不到工具 %s", name)
        return frozenset(tags)

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
        """CN-2 縱深防禦：回傳要 escalate 的缺口碼，或 None。中樞已回 escalate、或 advice 超出「凍結 ∩ 當下快照」都算。"""
        esc = info.get("escalate") or {}
        if esc.get("needed"):
            return str(esc.get("reason_code") or "scope_exceeded")
        advice = info.get("advice")
        if advice is None:
            return None
        sub_kind = str(info.get("sub_kind", ""))
        gap = advice_in_scope(sub_kind, advice, {"modalities": st.frozen_modalities}, self._scope_now())
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

    def _checkpoint(self, st: _State, semantic: str, pending: Sequence[str] = ()) -> None:
        revision, digest = persist_child(self._store, st.child_ref, st.transcript)
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
                run_id=st.run_id, step_id=step_id, ordinal=k, capability_set=cap, tool=calls[k]["name"], args=it.get("args"),
                injected=self._consult_injected(st, step_id, k) if is_consult else None,
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
            for m, res in self._execute_wave(wave, members):
                views[m.ordinal], observed[m.ordinal] = res.view, str(res.view.get("status", "unknown"))
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
                    pending.append(m.action_id)
                elif res.view.get("status") == "unknown" and not self._side_effect_is_read(m.tool):
                    unknown_block = True  # TC-3：未決副作用先解決，同批尚未開始的項不執行

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

    def _execute_wave(self, wave: Wave, members: list[ScreenedIntent]) -> list[tuple[ScreenedIntent, IntentResult]]:
        """波內先全部 create_action，再執行（並行波用執行緒）；單一失敗不影響兄弟。結果依序號排列。"""
        out: dict[int, IntentResult] = {}
        runnable: list[ScreenedIntent] = []
        for m in members:
            early = self._core.create_screened(m)
            if early is not None:
                out[m.ordinal] = early
            else:
                runnable.append(m)
        stale: list[StaleWorkerEpoch] = []

        def run_one(m: ScreenedIntent) -> None:
            try:
                out[m.ordinal] = self._core.execute_screened(m)
            except StaleWorkerEpoch as exc:
                stale.append(exc)

        if wave.parallel and len(runnable) > 1:
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
            self._checkpoint(st, f"await_approval:{action_id}", [action_id])
            self._park(st)
            return LoopOutcome(
                "awaiting_approval", st.run_id, LoopReason.APPROVAL_REQUIRED, steps=st.counters.steps,
                tool_calls=st.counters.tool_calls, pending_action_ids=(action_id,),
            )
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
        | 缺 approval／approval 過期／Presence 不足（可重試） | 再 park（``approval_required``） |
        | 其他 ApprovalInvalid | ``approval_invalid`` |
        | 執行時工具版本已變 | ``tool_version_changed`` |
        | screen／建立被拒（Gateway、PolicyViolation） | ``intent_rejected`` |
        | 參數 hash 不符 | ``params_mismatch`` |
        | action 本身 UNKNOWN／EXECUTING（``block_code=action_unresolved``） | ``unresolved_actions`` |
        | TC-3／其他 ActionBlocked、建立時的基礎設施錯誤 | ``action_blocked`` |
        | UNKNOWN（executor 例外、結果未明） | ``unresolved_actions`` |
        """
        if res.block_code == TOOL_VERSION_CHANGED:
            return LoopReason.TOOL_VERSION_CHANGED
        if res.block_code == ACTION_UNRESOLVED:  # action 本身 UNKNOWN／EXECUTING：副作用可能已發生，不是單純被擋
            return LoopReason.UNRESOLVED_ACTIONS
        if res.approval_code is not None:
            return "repark" if res.approval_code == "expired" else LoopReason.APPROVAL_INVALID
        if res.approval_required or res.record.reason == "PresenceRequired":
            return "repark"
        status = res.view.get("status")
        if res.record.action_id and status in ("ok", "failed"):
            return None
        if status == "unknown":
            return LoopReason.UNRESOLVED_ACTIONS
        if status == "rejected":
            if res.record.reason == "params_hash_mismatch":
                return LoopReason.PARAMS_MISMATCH
            return LoopReason.INTENT_REJECTED
        return LoopReason.ACTION_BLOCKED  # blocked（TC-3 等）或建立時的基礎設施錯誤
