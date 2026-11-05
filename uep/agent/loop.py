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
- **park**：``ApprovalRequired`` ⇒ 同批其後項不執行、存 child 與 checkpoint、取消 attempt、回 ``awaiting_approval``。
  ``park_run``（釋放 worker）、否決路徑屬切片 E；``resume`` 在這裡只做「重建＋補齊缺的工具結果＋續跑」。
- 不直接發話（AL-4）：最終文字放在 ``LoopOutcome.text``，由呼叫端經 Output Gate 交付；``deliver_result`` 回報是否已交付，
  merge-back 只在已交付時寫入 ``result``。
- ``max_parallel`` 預設 1：TC-3 的 DB 強制（``_check_single_executing``＋部分唯一索引）要到 D1b 才放寬，真實 TaskManager 下
  同波成員不能真的並行；``> 1`` 只能搭配不擋並行的 TaskManager 替身。分波、屏障、序列化語意與並行度無關。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
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
from uep.ids import new_id
from uep.models.port import ChatMessage
from uep.tasks.manager import StaleWorkerEpoch
from uep.tasks.states import RunStatus
from uep.tools.gateway import INVALID_TOOL_ARGS, ToolGateway, visible_tools

logger = logging.getLogger(__name__)

LoopState = Literal["completed", "failed", "awaiting_approval", "cancelled", "stale", "rejected"]
Projector = Callable[[frozenset[str], frozenset[str] | None], Mapping[str, Any]]

TOOL_NOT_IN_SNAPSHOT = "tool_not_in_snapshot"
FINALIZE_NOTE = "預算已用盡：請根據目前已取得的資訊直接收束並回覆，不要再呼叫任何工具。"


@dataclass(frozen=True)
class LoopOutcome:
    state: LoopState
    run_id: str
    reason_code: str | None = None
    text: str = ""  # 最終文字（只放這裡，由呼叫端交付；AL-4）
    steps: int = 0
    tool_calls: int = 0
    pending_action_ids: tuple[str, ...] = ()
    delivered: bool = False


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
    usage_seen: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _End:
    kind: Literal["result", "park", "exhausted", "restart", "fail"]
    text: str = ""
    reason: str = ""
    cap: Mapping[str, Any] | None = None
    pending: tuple[str, ...] = ()


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
        with self._lock:
            return self._data.pop(request.snapshot_ref, [])


class AgentLoop:
    def __init__(
        self, *, runtime: Runtime, tasks: Any, gateway: ToolGateway, registry: EntryLookup,
        store: LoopContextStore, projector: Projector, budget: LoopBudget, worker_epoch: int,
        snapshots: _SnapshotSink | None = None, injected: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic, cancelled: Callable[[], bool] = lambda: False,
        deliver_result: Callable[[str], bool] | None = None,
        content_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        self._runtime = runtime
        self._tasks = tasks
        self._gateway = gateway
        self._registry = registry
        self._store = store
        self._projector = projector
        self._budget = budget
        self._epoch = worker_epoch
        self._snapshots = snapshots
        self._clock = clock
        self._cancelled = cancelled
        self._deliver_result = deliver_result
        self._core = AgentCore(runtime, tasks, gateway, worker_epoch=worker_epoch, injected=injected, content_lookup=content_lookup)

    # ───────────────────────── 入口 ─────────────────────────

    def run(
        self, trigger: LoopTrigger, run_id: str, *, parent: ParentSnapshot, system_text: str, trigger_text: str = "",
        persona_version: int = 0,
    ) -> LoopOutcome:
        """啟動一個 Run 的迴圈（Run 已由呼叫端建立並進入 RUNNING）。``capability_tags`` 為空或投影為空 ⇒ 拒絕（介面違規）。"""
        if not trigger.capability_tags:
            logger.error("LoopTrigger 的 capability_tags 為空，拒絕啟動：run=%s", run_id)
            return LoopOutcome("rejected", run_id, "capability_empty")
        cap = self._projector(trigger.capability_tags, None)
        frozen = {t["tool"]: int(t.get("version", 1)) for t in cap.get("tools", ())}
        if not frozen:
            logger.error("能力投影為空，拒絕啟動：run=%s", run_id)
            return LoopOutcome("rejected", run_id, "capability_empty")
        child = fork_child(
            self._store, run_id, parent=parent, system_text=system_text, goal_code=trigger.goal_code,
            trigger_text=trigger_text, persona_version=persona_version, scope_ref=trigger.scope_ref,
        )
        st = self._new_state(run_id, trigger, parent.parent_id, self._narrow(cap, frozen, None), frozen,
                             trigger.capability_tags, list(child.messages), child.ref, BudgetCounters())
        return self._drive(st, resume_fill=None)

    def resume(self, run_id: str, checkpoint: Mapping[str, Any], *, trigger: LoopTrigger, parent_id: str) -> LoopOutcome:
        """核准後的續行（呼叫端已 ``resume_run`` 取得新 epoch 並建構本物件）。重建 child 並驗 hash（失敗 ⇒ FAILED(context_lost)），
        能力＝凍結集合 ∩ 當下投影（只收窄，AL-3），補齊缺少的工具結果後以新 attempt 續跑。"""
        frozen = {str(k): int(v) for k, v in dict(checkpoint.get("frozen_tools") or {}).items()}
        tags = frozenset(str(t) for t in checkpoint.get("frozen_tags") or ())
        counters = BudgetCounters.from_checkpoint(checkpoint.get("budget_counters"))
        try:
            messages, _revision = load_verified(
                self._store, str(checkpoint.get("child_context_ref", "")), str(checkpoint.get("history_hash", "")),
            )
        except ContextLost:
            logger.error("child context 無法重建：run=%s", run_id)
            st = self._new_state(run_id, trigger, parent_id, {"tools": []}, frozen, tags, [],
                                 str(checkpoint.get("child_context_ref", "")), counters)
            return self._guarded(st, lambda: self._finish(st, "failed", "context_lost"))
        cap = self._narrow(self._projector(tags, frozenset(frozen)), frozen, frozenset(frozen))
        st = self._new_state(run_id, trigger, parent_id, cap, frozen, tags, messages, str(checkpoint["child_context_ref"]), counters)
        st.step_id = checkpoint.get("step_id") if isinstance(checkpoint.get("step_id"), str) else None
        st.consumed = list(checkpoint.get("consumed_action_ids") or ())
        return self._drive(st, resume_fill=frozenset(checkpoint.get("pending_approvals") or ()))

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
        return self._guarded(st, lambda: self._drive_inner(st, resume_fill))

    def _finalize_stop(self, st: _State, outcome: LoopOutcome) -> LoopOutcome:
        if st.attempt_id:
            self._runtime.cancel(st.attempt_id, outcome.reason_code or outcome.state)
        if st.open_step:
            self._end_step(st, ok=False)
        if outcome.state == "failed":
            return self._finish(st, "failed", outcome.reason_code or "failed")
        merge_back(self._store, run_id=st.run_id, parent_id=st.parent_id, scope_ref=st.trigger.scope_ref,
                   state=outcome.state, reason_code=outcome.reason_code)
        return outcome

    def _finish(self, st: _State, state: LoopState, reason: str | None, text: str = "") -> LoopOutcome:
        """終態：Run 轉移、最後 checkpoint、merge-back。"""
        if st.transcript:  # context_lost 時沒有可寫的 history
            self._checkpoint(st, f"end:{reason or state}")
        if state == "completed":
            self._tasks.transition_run(st.run_id, RunStatus.SUCCEEDED, worker_epoch=self._epoch)
        elif state == "failed":
            self._tasks.transition_run(st.run_id, RunStatus.FAILED, worker_epoch=self._epoch)
        delivered = False
        if state == "completed" and self._deliver_result is not None and text:
            delivered = bool(self._deliver_result(text))
        merge_back(
            self._store, run_id=st.run_id, parent_id=st.parent_id, scope_ref=st.trigger.scope_ref, state=state,
            reason_code=reason, summary=text, result=text, delivered=delivered, trail_ref=f"trail:{st.run_id}",
        )
        return LoopOutcome(
            state, st.run_id, reason, text, st.counters.steps, st.counters.tool_calls, delivered=delivered,
        )

    def _checkpoint(self, st: _State, semantic: str, pending: Sequence[str] = ()) -> None:
        revision, digest = persist_child(self._store, st.child_ref, st.transcript)
        st.counters.elapsed_s = self._elapsed(st)
        cp: dict[str, Any] = {
            "next_step_semantic": semantic, "pending_approvals": list(pending),
            "consumed_action_ids": list(st.consumed), "child_context_ref": st.child_ref, "child_revision": revision,
            "history_hash": digest, "frozen_tools": dict(st.frozen_tools), "frozen_tags": sorted(st.frozen_tags),
            "budget_counters": st.counters.to_checkpoint(),
        }
        if st.step_id:
            cp["step_id"] = st.step_id
        self._tasks.save_checkpoint(st.run_id, cp, worker_epoch=self._epoch)

    # ───────────────────────── 內部：主迴圈 ─────────────────────────

    def _drive_inner(self, st: _State, resume_fill: frozenset[str] | None) -> LoopOutcome:
        if resume_fill is not None:
            parked = self._fill_missing(st, resume_fill)
            if parked is not None:
                return parked
        while True:
            dim = exhausted(self._budget, st.counters, elapsed_s=self._elapsed(st))
            if dim is not None:
                end = _End("exhausted", reason=dim)
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
            mark_parked(self._store, run_id=st.run_id, parent_id=st.parent_id)
            return LoopOutcome(
                "awaiting_approval", st.run_id, "approval_required", steps=st.counters.steps,
                tool_calls=st.counters.tool_calls, pending_action_ids=end.pending,
            )
        if end.kind == "exhausted":
            if end.reason not in ("run_deadline",) and self._budget.finalize_on_exhausted:
                final = self._run_attempt(st, {"tools": []}, finalize=True)
                if final.kind == "result":
                    st.transcript.append(ChatMessage("assistant", final.text))
                    return self._finish(st, "completed", f"budget_finalized_{end.reason}", final.text)
            return self._finish(st, "failed", "budget_exhausted")
        return self._finish(st, "failed", end.reason or "failed")

    def _gate(self, st: _State) -> None:
        """heartbeat／取消／牆鐘／Step 逾時。舊 epoch 的 StaleWorkerEpoch 直接往外拋。"""
        if self._cancelled():
            raise _Stop(LoopOutcome("cancelled", st.run_id, "cancelled", steps=st.counters.steps))
        if self._budget.run_deadline_s is not None and self._elapsed(st) >= self._budget.run_deadline_s:
            raise _Stop(LoopOutcome("failed", st.run_id, "run_deadline"))
        now = self._clock()
        if self._budget.step_timeout_s is not None and st.open_step and now - st.step_started >= self._budget.step_timeout_s:
            raise _Stop(LoopOutcome("failed", st.run_id, "step_timeout"))
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
        attempt_id = self._start_attempt(st, cap, finalize)
        events = iter(self._runtime.stream(attempt_id))
        texts: list[str] = []
        batch: list[Mapping[str, Any]] = []

        def stop(end: _End, *, ok: bool = False) -> _End:
            self._runtime.cancel(attempt_id, end.reason or end.kind)
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
                return stop(_End("fail", reason="runtime_stream_ended"))
            if event.kind == "text_delta":
                texts.append(event.text_delta or "")
            elif event.kind == "tool_intent":
                batch.append(event.tool_intent or {})
                if len(batch) < int((event.tool_intent or {}).get("batch_size", 1)):
                    continue
                if finalize:
                    return stop(_End("fail", reason="finalize_tool_intent"))
                intents, batch = batch, []
                text, texts = "".join(texts), []
                end = self._handle_batch(st, cap, attempt_id, intents, text)
                if end is not None:
                    return end
                dim = exhausted(self._budget, st.counters, elapsed_s=self._elapsed(st))
                if dim is not None:
                    self._runtime.cancel(attempt_id, "budget_exhausted")
                    return _End("exhausted", reason=dim)
                prior = visible_tools(cap)
                narrowed = self._narrow(self._projector(st.frozen_tags, prior), st.frozen_tools, prior)
                if visible_tools(narrowed) != prior:  # AL-3：只可能變窄；變窄就以較窄的集合重開 attempt
                    self._runtime.cancel(attempt_id, "capability_narrowed")
                    return _End("restart", cap=narrowed)
            elif event.kind == "result":
                self._account_usage(st, attempt_id)
                self._end_step(st, ok=True)
                status = (event.result or {}).get("status", "ok")
                if status == "max_rounds":
                    return _End("exhausted", reason="rounds")
                return _End("result", text="".join(texts))
            elif event.kind == "error":
                code = str((event.error or {}).get("code", "UNKNOWN")).lower()
                self._end_step(st, ok=False)
                return _End("fail", reason=f"runtime_{code}")

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
        for k in kept:
            it = intents[k]
            if not it.get("args_complete"):
                views[k] = self._gateway.result_view("rejected", reason_code=INVALID_TOOL_ARGS)
                observed[k] = "rejected"
                continue
            res = self._core.screen_intent(
                run_id=st.run_id, step_id=step_id, ordinal=k, capability_set=cap, tool=calls[k]["name"], args=it.get("args"),
            )
            if isinstance(res, IntentResult):
                views[k], observed[k] = res.view, "rejected"
            else:
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
        pending: list[str] = []
        unknown_block = False
        for wave in waves:
            members = [by_ordinal[i.index] for i in wave.items]
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
                if res.record.action_id and not res.approval_required:
                    st.consumed.append(res.record.action_id)
                if res.approval_required and parked_at is None:
                    parked_at = m.ordinal
                    pending.append(m.action_id)
                elif res.view.get("status") == "unknown" and not self._side_effect_is_read(m.tool):
                    unknown_block = True  # TC-3：未決副作用先解決，同批尚未開始的項不執行

        no_progress: str | None = None
        for k in sorted(observed):
            reason = observe_call(self._budget, st.counters, calls[k]["name"], calls[k]["args"], observed[k])
            no_progress = no_progress or reason

        # history：assistant 訊息帶整批呼叫；tool 訊息依序號回餵。park 時只回餵 park 之前的連續前綴，
        # 其餘由 resume 補齊（每個 tool_call 最終都要有對應的 tool 訊息）
        st.transcript.append(ChatMessage("assistant", text, tool_calls=tuple(calls)))
        limit = n if parked_at is None else parked_at
        for k in range(limit):
            st.transcript.append(ChatMessage("tool", result_text(views[k]), tool_call_id=calls[k]["id"]))
        self._account_usage(st, attempt_id)
        self._end_step(st, ok=True)

        if parked_at is not None:
            self._checkpoint(st, f"await_approval:{pending[0]}", pending)
            self._runtime.cancel(attempt_id, "park")
            return _End("park", pending=tuple(pending))
        if no_progress is not None:
            self._runtime.cancel(attempt_id, no_progress)
            return _End("fail", reason="no_progress")
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

    def _fill_missing(self, st: _State, pending: frozenset[str]) -> LoopOutcome | None:
        """為最後一則 assistant 訊息中缺少 tool 訊息的呼叫補上結果：已核准的 pending 項經完整流程執行（冪等，
        不重做已有 receipt 者）；凍結集合外 ⇒ blocked(tool_not_in_snapshot)；其餘同批後續項 ⇒ blocked(deferred_by_approval_wait)。
        pending 項仍缺核准 ⇒ 再次 park（回 awaiting_approval）。"""
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
            if call["name"] not in allowed:
                view = self._blocked(TOOL_NOT_IN_SNAPSHOT)
            elif action_id in pending:
                screened = self._core.screen_intent(
                    run_id=st.run_id, step_id=step_id, ordinal=ordinal, capability_set=st.cap, tool=call["name"], args=call["args"],
                )
                if isinstance(screened, IntentResult):
                    view = screened.view
                else:
                    res = self._core.create_screened(screened) or self._core.execute_screened(screened)
                    if res.approval_required:
                        self._checkpoint(st, f"await_approval:{action_id}", [action_id])
                        mark_parked(self._store, run_id=st.run_id, parent_id=st.parent_id)
                        return LoopOutcome(
                            "awaiting_approval", st.run_id, "approval_required", steps=st.counters.steps,
                            tool_calls=st.counters.tool_calls, pending_action_ids=(action_id,),
                        )
                    st.counters.tool_calls += 1
                    if res.record.action_id:
                        st.consumed.append(res.record.action_id)
                    view = res.view
            else:
                view = self._blocked(DEFERRED_BY_APPROVAL_WAIT)
            st.transcript.append(ChatMessage("tool", result_text(view), tool_call_id=call["id"]))
        return None
