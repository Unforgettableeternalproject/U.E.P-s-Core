"""Task Manager：tasks／runs／steps／action_intents／receipts／approvals 的唯一寫入者（契約 §6.6、§7.1）。

- 所有狀態變更與對應的 task.*／run.*／action.*／step.* 事件在**同一交易**內寫入（EventPublisher.record），
  提交後才投遞程序內 Bus（E-1）。
- Task 與 GS／CS 無相依（D8、T-1）：GS／CS 結束或程序重啟都不會觸碰 Task；恢復只讀 DB（``resume_run``）。
- Executor 只回 receipt；ActionIntent.status 由這裡依 receipt／查核結果推進（§6.5）。
- 狀態轉換的合法性先看轉移表，再呼叫可替換的 ``transition_guard``（CC-7 State Coordinator 於 P2 接入）。
- 呼叫 executor 時不持有 DB 交易（避免長交易持鎖）；EXECUTING 先提交，崩潰後由 ``resume_run`` 轉 UNKNOWN。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from uep.adapters.tool import (
    ReceiptLookup,
    ToolCall,
    ToolExecutor,
    ToolReceipt,
    ToolResponseLost,
)
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.publisher import EventPublisher, RecordedEvent
from uep.ids import new_id
from uep.tasks.models import (
    ActionIntentRow,
    ActivityRow,
    ApprovalRow,
    ReceiptRow,
    RunRow,
    StepRow,
    TaskRow,
)
from uep.tasks.states import (
    ACTION_TRANSITIONS,
    APPROVAL_REQUIRED,
    INTERRUPT_STRICTNESS,
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    TERMINAL_STATUSES,
    ActionStatus,
    ActivityKind,
    ActivityStatus,
    InterruptPolicy,
    PauseReason,
    RunStatus,
    StepStatus,
)

logger = logging.getLogger("uep.tasks.manager")

# 預設寫 outbox 的事件類型 -> target（其餘不寫，避免沒人消費的列堆積）
DEFAULT_OUTBOX_TARGETS: dict[str, tuple[str, ...]] = {
    "task.completed": ("notification",),
    "task.failed": ("notification",),
    "task.cancelled": ("notification",),
    "task.input_requested": ("notification",),
}

SOURCE = "core.task_manager"
SYSTEM_ACTOR = "uep:task_manager"

# 契約 §6.4 的 checkpoint 內容鍵
CHECKPOINT_KEYS = frozenset({
    "completed_steps", "input_result_refs", "auth_snapshot", "pending_approvals",
    "consumed_action_ids", "next_step_semantic", "tool_receipts",
})


# ---- 例外 ----

class TaskError(Exception):
    """Task Manager 例外基底。"""


class NotFound(TaskError):
    pass


class IllegalTransition(TaskError):
    pass


class StaleVersion(TaskError):
    """expected_version 與目前版本不符（樂觀併發）。"""


class StaleWorkerEpoch(TaskError):
    """舊 worker_epoch 嘗試產生副作用（T-9）。"""


class PolicyViolation(TaskError):
    pass


class ParamsHashMismatch(TaskError):
    """同 action_id 改了參數；須建立新 action（§6.5）。"""


class ActionBlocked(TaskError):
    """action 目前不可執行（UNKNOWN 待查核、EXECUTING 進行中、Run 非執行中、找不到 executor）。"""


class PresenceRequired(TaskError):
    """presence_bound 的前景 action 在沒有有效 Presence 時不得執行（D12、P-10）；action 狀態不變。"""


class ActiveWorker(TaskError):
    """Run 的 worker 心跳尚未過期，不可接手（L2）。"""


class ApprovalRequired(TaskError):
    """外部副作用缺少 approval；action 維持 INTENDED，Task 進 WAITING_APPROVAL（VC-6-4）。"""


class ApprovalInvalid(TaskError):
    """approval 不存在、已消耗、已過期，或與 params_hash／target／scope 不符（T-4、T-5）。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ---- 資料型別 ----

@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Gateway／Approval 通道認證後的身分。record_approval 只收這個型別（M2）。"""

    actor_id: str
    kind: Literal["human", "agent", "system"]
    verified: bool


@dataclass(frozen=True)
class TaskPolicy:
    """Policy 對 Task 的決定（presence_bound、interrupt_policy 由 Policy 依工具分類給定）。"""

    presence_bound: bool
    interrupt_policy: InterruptPolicy
    policy_ref: str


@dataclass(frozen=True)
class TransitionRequest:
    """交給 ``transition_guard`` 的合法性檢查輸入（CC-7 接入點）。"""

    entity: str  # "task" | "run"
    entity_id: str
    from_status: RunStatus
    to_status: RunStatus
    pause_reason: PauseReason | None = None


TransitionGuard = Callable[[TransitionRequest], None]


@dataclass(frozen=True)
class ActionOutcome:
    action_id: str
    status: ActionStatus
    executed: bool  # 本次呼叫是否真的呼叫了 executor.execute
    receipt: ReceiptRow | None = None


@dataclass
class ResumePlan:
    """恢復時交給 worker 的資料：只含 DB 內容，不含 Python 物件。"""

    task: TaskRow
    run: RunRow
    checkpoint: dict[str, Any] | None
    open_steps: list[StepRow] = field(default_factory=list)
    open_actions: list[ActionIntentRow] = field(default_factory=list)
    unknown_action_ids: list[str] = field(default_factory=list)


def params_hash(params: Mapping[str, Any]) -> str:
    """canonical 參數雜湊：鍵排序、無空白的 JSON 後取 sha256。"""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_reject)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reject(obj: Any) -> Any:
    raise TypeError(f"參數含無法 canonical 化的型別：{type(obj).__name__}")


_SAFE_STR = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_MAX_DEPTH = 4


def check_safe_value(value: Any, path: str = "value", depth: int = 0) -> None:
    """值層級白名單（M3）：只允許 ID／數字／短碼，字串限 64 字且字元集受限，擋私人原文。"""
    if depth > _MAX_DEPTH:
        raise PolicyViolation(f"{path} 巢狀過深")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if not _SAFE_STR.match(value):
            raise PolicyViolation(f"{path} 不是 ID／短碼（不允許自由文字）")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            check_safe_value(item, f"{path}[{i}]", depth + 1)
        return
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str) or not _SAFE_STR.match(k):
                raise PolicyViolation(f"{path} 含不合法的鍵")
            check_safe_value(v, f"{path}.{k}", depth + 1)
        return
    raise PolicyViolation(f"{path} 型別不允許：{type(value).__name__}")


# 引用字串（result_ref、error_code）：URI 安全字元、無空白、長度上限 256（N4）
_SAFE_REF = re.compile(r"^[A-Za-z0-9_.:/\-+=%@~]{1,256}$")


def _sha256_of(value: Any) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_safe(value: Any) -> bool:
    try:
        check_safe_value(value)
    except PolicyViolation:
        return False
    return True


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        publisher: EventPublisher,
        executors: Mapping[str, ToolExecutor],
        *,
        clock: Callable[[], datetime] = _utcnow,
        transition_guard: TransitionGuard | None = None,
        crash_hook: Callable[[str], None] | None = None,
        approvers: Callable[[TaskRow], frozenset[str]] | None = None,
        heartbeat_stale_after: timedelta = timedelta(seconds=60),
        outbox_targets: Mapping[str, Sequence[str]] | None = None,
        presence_check: Callable[[str], bool] | None = None,
    ) -> None:
        self._sf = session_factory
        self._publisher = publisher
        self._executors = dict(executors)
        self._clock = clock
        self._guard = transition_guard
        # 崩潰注入點（T-15 用）：action.after_executing_commit、action.before_receipt_commit、
        # resolve.before_commit、events.before_dispatch；預設 no-op
        self._crash = crash_hook or (lambda _point: None)
        # P1：核准者只認 Task 的 owner；之後由 Policy 指定
        self._approvers = approvers or (lambda task: frozenset({task.owner_actor_id}))
        self._stale_after = heartbeat_stale_after
        # 事件類型（或 "task.*" 這類前綴鍵）-> outbox target；與狀態變更同交易寫入（VC-1-2、T-8）
        self._outbox_targets = dict(DEFAULT_OUTBOX_TARGETS if outbox_targets is None else outbox_targets)
        # 選用的 Presence 檢查點（D12）：以 persona_id 查是否有有效 Presence；None 表示不檢查。
        # 只套用於 presence_bound 的前景 Task，背景 Task 例外。
        self._presence_check = presence_check
        self.redaction_count = 0  # receipt 欄位被脫敏的累計次數（N4）

    # ---- 交易與事件 ----

    @contextmanager
    def _tx(self, before_commit: str | None = None) -> Iterator[tuple[Session, list[RecordedEvent]]]:
        """單一交易；提交成功後才把本交易記錄的事件投遞到 Bus（投遞失敗只記 log）。"""
        pending: list[RecordedEvent] = []
        with self._sf() as session, session.begin():
            yield session, pending
            if before_commit:
                self._crash(before_commit)
        if pending:
            self._crash("events.before_dispatch")
        for recorded in pending:
            try:
                self._publisher.dispatch(recorded)
            except Exception:
                logger.exception("事件投遞失敗：%s", recorded.event.event_id)

    def _emit(
        self,
        session: Session,
        pending: list[RecordedEvent],
        event_type: str,
        task: TaskRow,
        *,
        aggregate: tuple[str, str, int],
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        activity = session.get(ActivityRow, task.activity_id)
        draft = EventDraft(
            event_type=event_type,
            activity_id=task.activity_id,
            task_id=task.task_id,
            run_id=run_id,
            aggregate_type=aggregate[0],
            aggregate_id=aggregate[1],
            aggregate_version=aggregate[2],
            payload={"task_id": task.task_id, **(payload or {})},
        )
        trusted = TrustedContext(
            source=SOURCE,
            actor_id=SYSTEM_ACTOR,
            scope_ref=task.scope_ref,
            persona_id=activity.persona_id if activity else "uep",
        )
        pending.append(
            self._publisher.record(session, draft, trusted, outbox_targets=self._targets_for(event_type))
        )

    def _targets_for(self, event_type: str) -> list[str]:
        targets: list[str] = list(self._outbox_targets.get(event_type, ()))
        for key, value in self._outbox_targets.items():
            if key.endswith(".*") and event_type.startswith(key[:-1]):
                targets.extend(value)
        return list(dict.fromkeys(targets))

    def _bump(self, row: TaskRow | RunRow | ActionIntentRow | StepRow) -> None:
        row.version += 1
        if hasattr(row, "updated_at"):
            row.updated_at = self._clock()

    # ---- 查詢 ----

    def get_task(self, task_id: str) -> TaskRow:
        with self._sf() as s:
            return self._need(s.get(TaskRow, task_id), "task", task_id)

    def get_run(self, run_id: str) -> RunRow:
        with self._sf() as s:
            return self._need(s.get(RunRow, run_id), "run", run_id)

    def get_action(self, action_id: str) -> ActionIntentRow:
        with self._sf() as s:
            return self._need(s.get(ActionIntentRow, action_id), "action", action_id)

    def get_receipt(self, action_id: str) -> ReceiptRow | None:
        with self._sf() as s:
            return s.scalar(select(ReceiptRow).where(ReceiptRow.action_id == action_id))

    @staticmethod
    def _need(row, kind: str, ident: str):
        if row is None:
            raise NotFound(f"{kind} 不存在：{ident}")
        return row

    # ---- Task ----

    def create_task(
        self,
        *,
        persona_id: str,
        goal: str,
        owner_actor_id: str,
        scope_ref: str,
        policy: TaskPolicy,
        requested_interrupt: InterruptPolicy | None = None,
        deadline: datetime | None = None,
        origin_ref: dict[str, Any] | None = None,
        parent_task_id: str | None = None,
    ) -> TaskRow:
        """建立 Task 與其 ``kind=task`` Activity（同一交易，§6.1）。

        interrupt_policy 由 Policy 決定，建立者只能收緊（D31）；要求比 Policy 寬鬆者拒絕。
        """
        effective = policy.interrupt_policy
        if requested_interrupt is not None:
            if INTERRUPT_STRICTNESS[requested_interrupt] < INTERRUPT_STRICTNESS[policy.interrupt_policy]:
                raise PolicyViolation("建立者不可放寬 interrupt_policy")
            effective = requested_interrupt
        with self._tx() as (s, pending):
            if parent_task_id is not None:
                self._need(s.get(TaskRow, parent_task_id), "task", parent_task_id)
            activity = ActivityRow(
                activity_id=new_id("actv"), persona_id=persona_id, kind=ActivityKind.TASK.value,
                presence_bound=policy.presence_bound, status=ActivityStatus.ACTIVE.value,
                origin_ref=origin_ref, version=1,
            )
            s.add(activity)
            s.flush()
            task = TaskRow(
                task_id=new_id("task"), activity_id=activity.activity_id, parent_task_id=parent_task_id,
                goal=goal, owner_actor_id=owner_actor_id, scope_ref=scope_ref,
                presence_bound=policy.presence_bound, status=RunStatus.PENDING.value,
                deadline=deadline, interrupt_policy=effective.value, origin_ref=origin_ref,
                version=1, policy_ref=policy.policy_ref,
            )
            s.add(task)
            s.flush()
            self._emit(
                s, pending, "task.created", task, aggregate=("task", task.task_id, task.version),
                payload={"status": task.status},
            )
            return task

    def _check_transition(
        self, kind: str, row: TaskRow | RunRow, to: RunStatus, pause_reason: PauseReason | None
    ) -> RunStatus:
        table = TASK_TRANSITIONS if kind == "task" else RUN_TRANSITIONS
        current = RunStatus(row.status)
        if to not in table[current]:
            raise IllegalTransition(f"{kind} 不允許 {current.value} -> {to.value}")
        if (to is RunStatus.PAUSED) != (pause_reason is not None):
            raise IllegalTransition("pause_reason 只在（且必須在）轉入 PAUSED 時提供")
        if self._guard is not None:
            ident = row.task_id if kind == "task" else row.run_id
            self._guard(TransitionRequest(kind, ident, current, to, pause_reason))
        return current

    def _live_runs(self, s: Session, task_id: str) -> list[RunRow]:
        done = [x.value for x in TERMINAL_STATUSES]
        stmt = (
            select(RunRow).where(RunRow.task_id == task_id, RunRow.status.notin_(done))
            .with_for_update().execution_options(populate_existing=True)
        )
        return list(s.scalars(stmt))

    @staticmethod
    def _assert_no_inflight(s: Session, run_ids: list[str]) -> None:
        """轉 SUCCEEDED 前不可有 EXECUTING／UNKNOWN 的 action（結果未明不得宣稱完成）。"""
        if not run_ids:
            return
        bad = s.scalar(
            select(ActionIntentRow.action_id).where(
                ActionIntentRow.run_id.in_(run_ids),
                ActionIntentRow.status.in_([ActionStatus.EXECUTING.value, ActionStatus.UNKNOWN.value]),
            ).limit(1)
        )
        if bad is not None:
            raise IllegalTransition(f"仍有結果未明的 action（{bad}），不可轉 SUCCEEDED")

    def _set_task_status(
        self, s: Session, pending: list[RecordedEvent], task: TaskRow, to: RunStatus,
        pause_reason: PauseReason | None,
    ) -> None:
        current = self._check_transition("task", task, to, pause_reason)
        if to in TERMINAL_STATUSES:
            # Task 終態時同交易把未終態 Run 一併收尾（H4）；不合法的 Run 轉移會讓整筆失敗
            for run in self._live_runs(s, task.task_id):
                self._set_run_status(s, pending, task, run, to, None)
        task.status = to.value
        task.pause_reason = pause_reason.value if pause_reason else None
        self._bump(task)
        event_type = _TASK_EVENT.get((current, to)) or _TASK_EVENT.get((None, to))
        if event_type:
            self._emit(
                s, pending, event_type, task, aggregate=("task", task.task_id, task.version),
                payload={"status": to.value},
            )
        if to in TERMINAL_STATUSES:
            # kind=task 的 Activity 在 Task 到達終態後才結束（§6.1a）
            activity = s.get(ActivityRow, task.activity_id)
            if activity and activity.status != ActivityStatus.ENDED.value:
                activity.status = ActivityStatus.ENDED.value
                activity.ended_at = self._clock()
                activity.version += 1

    def _set_run_status(
        self, s: Session, pending: list[RecordedEvent], task: TaskRow, run: RunRow, to: RunStatus,
        pause_reason: PauseReason | None,
    ) -> None:
        self._check_transition("run", run, to, pause_reason)
        if to is RunStatus.SUCCEEDED:
            self._assert_no_inflight(s, [run.run_id])
        run.status = to.value
        run.pause_reason = pause_reason.value if pause_reason else None
        run.paused_at = self._clock() if to is RunStatus.PAUSED else None
        self._bump(run)
        if to in TERMINAL_STATUSES:
            self._emit(
                s, pending, "run.ended", task, aggregate=("run", run.run_id, run.version),
                run_id=run.run_id, payload={"status": to.value, "run_id": run.run_id},
            )

    def _lock_task(self, s: Session, task_id: str, expected_version: int | None) -> TaskRow:
        task = self._need(
            s.get(TaskRow, task_id, with_for_update=True, populate_existing=True), "task", task_id
        )
        if expected_version is not None and task.version != expected_version:
            raise StaleVersion(f"task {task_id} 版本 {task.version}，預期 {expected_version}")
        return task

    def _lock_run(self, s: Session, run_id: str) -> tuple[TaskRow, RunRow]:
        """鎖定順序固定為 task -> run；先查純量 id，鎖定後才載入 entity，避免讀到鎖前舊值（H2）。"""
        task_id = self._need(s.scalar(select(RunRow.task_id).where(RunRow.run_id == run_id)), "run", run_id)
        task = self._lock_task(s, task_id, None)
        run = self._need(
            s.get(RunRow, run_id, with_for_update=True, populate_existing=True), "run", run_id
        )
        return task, run

    def transition_task(
        self, task_id: str, to: RunStatus, *, expected_version: int | None = None,
        pause_reason: PauseReason | None = None,
    ) -> TaskRow:
        """Task 狀態轉移（轉移表＋guard）。終態不可再轉；Task 結束只能經這裡（T-1）。"""
        with self._tx() as (s, pending):
            task = self._lock_task(s, task_id, expected_version)
            self._set_task_status(s, pending, task, to, pause_reason)
            return task

    # ---- Run ----

    def start_run(
        self, task_id: str, *, persona_version: int, policy_version: int,
        run_deadline: datetime | None = None, step_timeout: timedelta | None = None,
        approval_expiry: datetime | None = None,
    ) -> RunRow:
        """建立新 attempt 的 Run 並使 Task 進入 RUNNING。同一 Task 同時至多一個未終態 Run。"""
        with self._tx() as (s, pending):
            task = self._lock_task(s, task_id, None)
            current = RunStatus(task.status)
            if current in TERMINAL_STATUSES:
                raise IllegalTransition(f"task 已是終態 {current.value}，不可再啟動 Run")
            active = s.scalar(
                select(RunRow.run_id).where(
                    RunRow.task_id == task_id, RunRow.status.notin_([x.value for x in TERMINAL_STATUSES])
                )
            )
            if active is not None:
                raise IllegalTransition(f"task 已有未結束的 Run：{active}")
            attempt = (s.scalar(select(func.max(RunRow.attempt)).where(RunRow.task_id == task_id)) or 0) + 1
            run = RunRow(
                run_id=new_id("run"), task_id=task_id, attempt=attempt, worker_epoch=1,
                status=RunStatus.RUNNING.value, run_deadline=run_deadline, step_timeout=step_timeout,
                approval_expiry=approval_expiry, worker_heartbeat_at=self._clock(),
                persona_version=persona_version, policy_version=policy_version, version=1,
            )
            s.add(run)
            s.flush()
            if current is not RunStatus.RUNNING:
                self._set_task_status(s, pending, task, RunStatus.RUNNING, None)
            self._emit(
                s, pending, "run.started", task, aggregate=("run", run.run_id, run.version),
                run_id=run.run_id, payload={"status": run.status, "run_id": run.run_id, "count": attempt},
            )
            return run

    def transition_run(
        self, run_id: str, to: RunStatus, *, expected_version: int | None = None,
        pause_reason: PauseReason | None = None, worker_epoch: int | None = None,
    ) -> RunRow:
        """Run 狀態轉移。帶舊 worker_epoch 的回報只記錄、不改變狀態（T-9）。"""
        with self._tx() as (s, pending):
            task, run = self._lock_run(s, run_id)
            if worker_epoch is not None and worker_epoch != run.worker_epoch:
                logger.warning("忽略舊 worker_epoch 的 Run 轉移：run=%s epoch=%s", run_id, worker_epoch)
                return run
            if expected_version is not None and run.version != expected_version:
                raise StaleVersion(f"run {run_id} 版本 {run.version}，預期 {expected_version}")
            self._set_run_status(s, pending, task, run, to, pause_reason)
            return run

    def pause_task(self, task_id: str, reason: PauseReason) -> TaskRow:
        """Task 與其進行中的 Run 一併轉 PAUSED（presence 離場、core 關機等；T-6、T-11）。已 PAUSED 者冪等。"""
        with self._tx() as (s, pending):
            task = self._lock_task(s, task_id, None)
            for run in self._live_runs(s, task_id):
                if run.status != RunStatus.PAUSED.value:
                    self._set_run_status(s, pending, task, run, RunStatus.PAUSED, reason)
            if task.status != RunStatus.PAUSED.value:
                self._set_task_status(s, pending, task, RunStatus.PAUSED, reason)
            return task

    # ---- Step ----

    def begin_step(self, run_id: str, label: str, *, worker_epoch: int) -> StepRow:
        check_safe_value(label, "step.label")
        with self._tx() as (s, _pending):
            _task, run = self._lock_run(s, run_id)
            self._require_epoch(run, worker_epoch)
            seq = (s.scalar(select(func.max(StepRow.seq)).where(StepRow.run_id == run_id)) or 0) + 1
            step = StepRow(
                step_id=new_id("step"), run_id=run_id, seq=seq, label=label,
                status=StepStatus.RUNNING.value, version=1,
            )
            s.add(step)
            s.flush()
            return step

    def complete_step(
        self, step_id: str, *, worker_epoch: int, result_ref: str | None = None, ok: bool = True
    ) -> StepRow:
        """結束 Step。舊 worker_epoch 的回報不改變狀態（T-9）。已結束者冪等。"""
        with self._tx() as (s, pending):
            run_id = self._need(s.scalar(select(StepRow.run_id).where(StepRow.step_id == step_id)), "step", step_id)
            task, run = self._lock_run(s, run_id)
            step = self._need(
                s.get(StepRow, step_id, with_for_update=True, populate_existing=True), "step", step_id
            )
            if worker_epoch != run.worker_epoch:
                logger.warning("忽略舊 worker_epoch 的 Step 回報：step=%s epoch=%s", step_id, worker_epoch)
                return step
            if step.status != StepStatus.RUNNING.value:
                return step
            step.status = (StepStatus.SUCCEEDED if ok else StepStatus.FAILED).value
            step.result_ref = result_ref
            step.ended_at = self._clock()
            step.version += 1
            if ok:
                self._emit(
                    s, pending, "step.completed", task, aggregate=("step", step.step_id, step.version),
                    run_id=run.run_id, payload={"step_id": step.step_id, "status": step.status},
                )
            return step

    def save_checkpoint(
        self, run_id: str, checkpoint: Mapping[str, Any], *, worker_epoch: int,
        checkpoint_ref: str | None = None,
    ) -> RunRow:
        """寫入 §6.4 checkpoint（只含 ID／引用）。舊 worker_epoch 不寫入。"""
        extra = sorted(set(checkpoint) - CHECKPOINT_KEYS)
        if extra:
            raise PolicyViolation(f"checkpoint 含契約外的鍵：{extra}")
        check_safe_value(dict(checkpoint), "checkpoint")
        data = json.loads(json.dumps(dict(checkpoint)))  # 確認可序列化，並與呼叫者物件脫鉤
        with self._tx() as (s, _pending):
            _task, run = self._lock_run(s, run_id)
            if worker_epoch != run.worker_epoch:
                logger.warning("忽略舊 worker_epoch 的 checkpoint：run=%s epoch=%s", run_id, worker_epoch)
                return run
            run.checkpoint = data
            run.checkpoint_ref = checkpoint_ref
            self._bump(run)
            return run

    @staticmethod
    def _require_epoch(run: RunRow, worker_epoch: int) -> None:
        if worker_epoch != run.worker_epoch:
            raise StaleWorkerEpoch(f"run {run.run_id} 目前 epoch={run.worker_epoch}，收到 {worker_epoch}")

    def heartbeat(self, run_id: str, *, worker_epoch: int) -> None:
        """worker 心跳；舊 epoch 的心跳忽略。"""
        with self._tx() as (s, _pending):
            _task, run = self._lock_run(s, run_id)
            if worker_epoch == run.worker_epoch:
                run.worker_heartbeat_at = self._clock()

    # ---- Approval ----

    def record_approval(
        self, *, principal: AuthenticatedPrincipal, action_hash: str, target: str, scope: str,
        expiry: datetime, approval_id: str | None = None,
    ) -> ApprovalRow:
        """記錄人類經 Approval 通道產生的核准。只收已認證的人類身分，拒絕裸字串（M2、T-5）。"""
        if not isinstance(principal, AuthenticatedPrincipal):
            raise PolicyViolation("approval 必須由 AuthenticatedPrincipal 建立")
        if principal.kind != "human" or not principal.verified:
            raise PolicyViolation("approval 只接受已驗證的人類身分")
        with self._tx() as (s, _pending):
            row = ApprovalRow(
                approval_id=approval_id or new_id("appr"), actor_id=principal.actor_id,
                action_hash=action_hash, target=target, scope=scope, expiry=expiry,
            )
            s.add(row)
            s.flush()
            return row

    def _approval_problem(self, approval: ApprovalRow | None, action: ActionIntentRow, task: TaskRow) -> str | None:
        """回傳不合格原因碼；合格回 None。"""
        if approval is None:
            return "not_found"
        if approval.consumed_at is not None:
            return "consumed"
        if approval.expiry <= self._clock():
            return "expired"
        if approval.actor_id not in self._approvers(task):
            return "unauthorized_approver"
        if approval.action_hash != action.params_hash:
            return "hash_mismatch"
        if approval.target != action.tool:
            return "target_mismatch"
        if approval.scope != task.scope_ref:
            return "scope_mismatch"
        return None

    # ---- Action ----

    @staticmethod
    def params_hash(params: Mapping[str, Any]) -> str:
        return params_hash(params)

    def create_action(
        self, *, run_id: str, step_id: str, tool: str, executor: str, params: Mapping[str, Any],
        side_effect_class: str, idempotency: str, worker_epoch: int,
        presence_bound: bool | None = None, action_id: str | None = None,
    ) -> ActionIntentRow:
        """建立 INTENDED 的 ActionIntent。同 action_id 重呼叫回傳既有列；參數改變則拒絕（須新 action）。"""
        digest = params_hash(params)
        with self._tx() as (s, pending):
            task, run = self._lock_run(s, run_id)
            self._require_epoch(run, worker_epoch)
            if RunStatus(task.status) in TERMINAL_STATUSES:
                raise ActionBlocked("task 已是終態，不可建立 action")
            step = self._need(s.get(StepRow, step_id), "step", step_id)
            if step.run_id != run_id:
                raise PolicyViolation("step 不屬於此 run")
            if action_id is not None:
                existing = s.get(ActionIntentRow, action_id, with_for_update=True, populate_existing=True)
                if existing is not None:
                    if existing.params_hash != digest:
                        raise ParamsHashMismatch("同 action_id 的參數已變更，須建立新 action")
                    return existing
            action = ActionIntentRow(
                action_id=action_id or new_id("act"), run_id=run_id, step_id=step_id, tool=tool,
                executor=executor, params_hash=digest, side_effect_class=side_effect_class,
                idempotency=idempotency,
                presence_bound=task.presence_bound if presence_bound is None else presence_bound,
                status=ActionStatus.INTENDED.value, version=1,
            )
            try:
                with s.begin_nested():  # 唯一約束衝突只回滾這個 savepoint
                    s.add(action)
                    s.flush()
            except IntegrityError:
                existing = s.get(ActionIntentRow, action.action_id, populate_existing=True)
                if existing is None:
                    raise
                if existing.params_hash != digest:
                    raise ParamsHashMismatch("同 action_id 的參數已變更，須建立新 action") from None
                return existing
            self._emit(
                s, pending, "action.intended", task, aggregate=("action", action.action_id, action.version),
                run_id=run_id, payload={"action_id": action.action_id, "status": action.status},
            )
            return action

    def _lock_action(self, s: Session, action_id: str) -> tuple[TaskRow, RunRow, ActionIntentRow]:
        """鎖定順序 task -> run -> action；鎖前只取純量 id（H2）。"""
        run_id = self._need(
            s.scalar(select(ActionIntentRow.run_id).where(ActionIntentRow.action_id == action_id)),
            "action", action_id,
        )
        task, run = self._lock_run(s, run_id)
        action = self._need(
            s.get(ActionIntentRow, action_id, with_for_update=True, populate_existing=True),
            "action", action_id,
        )
        return task, run, action

    def _set_action_status(
        self, s: Session, pending: list[RecordedEvent], task: TaskRow, action: ActionIntentRow,
        to: ActionStatus, event_type: str | None,
    ) -> None:
        current = ActionStatus(action.status)
        if to not in ACTION_TRANSITIONS[current]:
            raise IllegalTransition(f"action 不允許 {current.value} -> {to.value}")
        action.status = to.value
        self._bump(action)
        if event_type:
            self._emit(
                s, pending, event_type, task, aggregate=("action", action.action_id, action.version),
                run_id=action.run_id, payload={"action_id": action.action_id, "status": to.value},
            )

    def _record_receipt(self, s: Session, action_id: str, receipt: ToolReceipt) -> ReceiptRow:
        """receipt 入庫（每個 action 至多一筆）。值層級不合格的 meta／error_code 以脫敏值取代而不拒收，
        因為副作用已發生，證據不能丟（M3）。已有未驗證 receipt 而新的已驗證時，升級為已驗證。"""
        meta = dict(receipt.meta)
        redactions: dict[str, dict[str, str]] = {}

        def redact(field_name: str, original: Any, reason: str) -> None:
            redactions[field_name] = {"sha256": _sha256_of(original), "reason": reason}

        if meta and not _is_safe(meta):
            redact("meta", meta, "meta_not_id_or_short_code")
            meta = {}
        error_code, result_ref = receipt.error_code, receipt.result_ref
        if error_code is not None and not _SAFE_REF.match(error_code):
            redact("error_code", error_code, "not_uri_safe_reference")
            error_code = "redacted"
        if result_ref is not None and not _SAFE_REF.match(result_ref):
            redact("result_ref", result_ref, "not_uri_safe_reference")
            result_ref = "redacted"
        if redactions:
            self.redaction_count += len(redactions)
            logger.warning("receipt 欄位已脫敏：action=%s fields=%s", action_id, sorted(redactions))
            meta = {**meta, "redacted": True, "redactions": redactions}
        values = {
            "receipt_id": receipt.receipt_id, "action_id": action_id, "succeeded": receipt.succeeded,
            "result_ref": result_ref, "error_code": error_code,
            "source_verified": receipt.source_verified, "meta": meta or None,
            "recorded_at": receipt.recorded_at,
        }
        s.execute(pg_insert(ReceiptRow).values(**values).on_conflict_do_nothing())
        stored = s.scalar(
            select(ReceiptRow).where(ReceiptRow.action_id == action_id)
            .execution_options(populate_existing=True)
        )
        if stored is None:
            raise TaskError("receipt 入庫失敗（receipt_id 與其他 action 衝突）")
        if not stored.source_verified and receipt.source_verified:  # 同 action_id 的已驗證 receipt 取代未驗證者
            for key, value in values.items():
                setattr(stored, key, value)
        return stored

    def _apply_receipt(
        self, s: Session, pending: list[RecordedEvent], task: TaskRow, action: ActionIntentRow,
        receipt: ToolReceipt,
    ) -> ReceiptRow:
        """以實際入庫的那筆 receipt 推進 action；未驗證來源者維持（或轉為）UNKNOWN（M7）。"""
        stored = self._record_receipt(s, action.action_id, receipt)
        if not stored.source_verified:
            if action.status == ActionStatus.EXECUTING.value:
                self._set_action_status(s, pending, task, action, ActionStatus.UNKNOWN, "action.unknown")
            return stored
        to = ActionStatus.SUCCEEDED if stored.succeeded else ActionStatus.FAILED
        self._set_action_status(s, pending, task, action, to, "action.executed")
        return stored

    def _to_waiting_approval(self, s: Session, pending: list[RecordedEvent], task: TaskRow, run: RunRow) -> None:
        if run.status == RunStatus.RUNNING.value:
            self._set_run_status(s, pending, task, run, RunStatus.WAITING_APPROVAL, None)
        if task.status == RunStatus.RUNNING.value:
            self._set_task_status(s, pending, task, RunStatus.WAITING_APPROVAL, None)

    def _back_to_running(self, s: Session, pending: list[RecordedEvent], task: TaskRow, run: RunRow) -> None:
        if run.status == RunStatus.WAITING_APPROVAL.value:
            self._set_run_status(s, pending, task, run, RunStatus.RUNNING, None)
        if task.status == RunStatus.WAITING_APPROVAL.value:
            self._set_task_status(s, pending, task, RunStatus.RUNNING, None)

    def execute_action(
        self, action_id: str, params: Mapping[str, Any], *, worker_epoch: int,
        approval_id: str | None = None,
    ) -> ActionOutcome:
        """執行 action（T-2、T-3、T-4、T-5）。

        - 已 SUCCEEDED／FAILED：回傳既有結果，不呼叫 executor。
        - UNKNOWN／EXECUTING：拒絕（ActionBlocked），UNKNOWN 須經 ``resolve_unknown``。
        - external_effect／high_impact 需有效 approval（單次消耗，綁 params_hash）；缺少或無效時
          action 維持 INTENDED、Task／Run 進 WAITING_APPROVAL，並拋 ApprovalRequired／ApprovalInvalid。
        - 階段 3 以階段 1 的 worker_epoch 重驗；epoch 已變只記 receipt、不推進狀態（T-9）。
        """
        digest = params_hash(params)
        deferred: TaskError | None = None
        call: ToolCall | None = None
        executor: ToolExecutor | None = None

        # 階段 1：檢查、核准、標 EXECUTING 並提交（崩潰後可由恢復流程轉 UNKNOWN）
        with self._tx() as (s, pending):
            task, run, action = self._lock_action(s, action_id)
            self._require_epoch(run, worker_epoch)
            if digest != action.params_hash:
                raise ParamsHashMismatch("參數與 action 的 params_hash 不符，須建立新 action")
            status = ActionStatus(action.status)
            if status in (ActionStatus.SUCCEEDED, ActionStatus.FAILED):
                receipt = s.scalar(select(ReceiptRow).where(ReceiptRow.action_id == action_id))
                return ActionOutcome(action_id, status, False, receipt)
            if status is ActionStatus.UNKNOWN:
                raise ActionBlocked("action 為 UNKNOWN，須先 resolve_unknown 查核，禁止盲目重試")
            if status is ActionStatus.EXECUTING:
                raise ActionBlocked("action 正在執行中或結果未明；崩潰後請走 resume_run")
            if RunStatus(task.status) in TERMINAL_STATUSES:
                raise ActionBlocked(f"task 已是終態 {task.status}，不可執行 action")
            if run.status not in (RunStatus.RUNNING.value, RunStatus.WAITING_APPROVAL.value):
                raise ActionBlocked(f"run 狀態為 {run.status}，不可執行 action")
            executor = self._executors.get(action.executor)
            if executor is None:
                raise ActionBlocked(f"找不到 executor：{action.executor}")
            if self._presence_check is not None and task.presence_bound:
                activity = s.get(ActivityRow, task.activity_id)
                if activity is None:
                    raise NotFound(f"找不到 activity：{task.activity_id}")
                persona_id = activity.persona_id
                if not self._presence_check(persona_id):
                    raise PresenceRequired("前景 action 需要有效 Presence；請待 Presence 恢復後再執行")

            if status is ActionStatus.INTENDED:
                if action.side_effect_class in APPROVAL_REQUIRED:
                    if approval_id is None:
                        deferred = ApprovalRequired("外部副作用需要有效 approval")
                    else:
                        approval = s.get(
                            ApprovalRow, approval_id, with_for_update=True, populate_existing=True
                        )
                        problem = self._approval_problem(approval, action, task)
                        if problem:
                            deferred = ApprovalInvalid(problem)
                        else:
                            approval.consumed_at = self._clock()
                            approval.consumed_by_action_id = action_id
                            action.approval_ref = approval_id
                    if deferred is not None:
                        self._to_waiting_approval(s, pending, task, run)
                if deferred is None:
                    self._set_action_status(s, pending, task, action, ActionStatus.APPROVED, "action.approved")
                    self._back_to_running(s, pending, task, run)
            if deferred is None:
                self._set_action_status(s, pending, task, action, ActionStatus.EXECUTING, None)
                call = ToolCall(
                    action_id=action_id, tool=action.tool, params=dict(params), params_hash=action.params_hash,
                    side_effect_class=action.side_effect_class, idempotency=action.idempotency,
                    approval_id=action.approval_ref,
                )
        if deferred is not None:
            raise deferred
        self._crash("action.after_executing_commit")

        # 階段 2：呼叫 executor（不持有 DB 交易）
        receipt_in: ToolReceipt | None = None
        failure: Exception | None = None
        try:
            receipt_in = executor.execute(call)
        except ToolResponseLost:
            pass
        except Exception as exc:  # noqa: BLE001  結果未知：標 UNKNOWN 後再上拋，不重試
            failure = exc

        # 階段 3：依 receipt 或遺失結果推進
        with self._tx("action.before_receipt_commit") as (s, pending):
            task, run, action = self._lock_action(s, action_id)
            matched = receipt_in is not None and receipt_in.action_id == action_id
            if run.worker_epoch != worker_epoch:
                # T-9：舊 epoch 的回報只記錄證據，不推進狀態；狀態交 resolve_unknown 查核
                stored = self._record_receipt(s, action_id, receipt_in) if matched else None
                return ActionOutcome(action_id, ActionStatus(action.status), True, stored)
            if ActionStatus(action.status) is not ActionStatus.EXECUTING:
                return ActionOutcome(action_id, ActionStatus(action.status), True, None)
            if matched:
                rec = self._apply_receipt(s, pending, task, action, receipt_in)
                outcome = ActionOutcome(action_id, ActionStatus(action.status), True, rec)
            else:
                self._set_action_status(s, pending, task, action, ActionStatus.UNKNOWN, "action.unknown")
                if receipt_in is not None:
                    failure = failure or TaskError("executor 回傳的 receipt.action_id 與呼叫不符")
                outcome = ActionOutcome(action_id, ActionStatus.UNKNOWN, True, None)
        if failure is not None:
            raise failure
        return outcome

    def resolve_unknown(self, action_id: str) -> ActionOutcome:
        """UNKNOWN 的唯一出口：向 executor 以 action_id 查核（T-3）。永遠不呼叫 ``execute``。

        - found：依 receipt 推進（來源未驗證者維持 UNKNOWN）。
        - confirmed_not_executed：僅當 executor 宣告 ``lookup_authoritative`` 且 action 的 idempotency
          為 native／via_action_id 才退回 INTENDED（舊 approval 已消耗，需核准者須取得新的）。
        - 其餘（undetermined、idempotency=none、非權威 executor）一律維持 UNKNOWN，交規則或人工。
        """
        with self._sf() as s:
            action = self._need(s.get(ActionIntentRow, action_id), "action", action_id)
            if ActionStatus(action.status) is not ActionStatus.UNKNOWN:
                receipt = s.scalar(select(ReceiptRow).where(ReceiptRow.action_id == action_id))
                return ActionOutcome(action_id, ActionStatus(action.status), False, receipt)
            executor = self._executors.get(action.executor)
            idempotency = action.idempotency
        if executor is None:
            raise ActionBlocked(f"找不到 executor：{action.executor}")

        try:
            lookup = executor.lookup_receipt(action_id)
            authoritative = bool(executor.lookup_authoritative)
        except Exception:
            logger.exception("lookup_receipt 失敗，視為 undetermined：action=%s", action_id)
            lookup, authoritative = ReceiptLookup("undetermined"), False
        if lookup.outcome == "found" and (lookup.receipt is None or lookup.receipt.action_id != action_id):
            lookup = ReceiptLookup("undetermined")
        can_retry = (
            lookup.outcome == "confirmed_not_executed"
            and authoritative
            and idempotency in ("native", "via_action_id")
        )

        with self._tx("resolve.before_commit") as (s, pending):
            task, _run, action = self._lock_action(s, action_id)
            if ActionStatus(action.status) is not ActionStatus.UNKNOWN:
                return ActionOutcome(action_id, ActionStatus(action.status), False, None)
            if lookup.outcome == "found":
                rec = self._apply_receipt(s, pending, task, action, lookup.receipt)
                return ActionOutcome(action_id, ActionStatus(action.status), False, rec)
            if can_retry:
                action.approval_ref = None
                self._set_action_status(s, pending, task, action, ActionStatus.INTENDED, "action.intended")
                return ActionOutcome(action_id, ActionStatus.INTENDED, False, None)
            return ActionOutcome(action_id, ActionStatus.UNKNOWN, False, None)

    # ---- 恢復（T-1、T-7、T-11） ----

    def list_resumable_runs(self) -> list[RunRow]:
        """所有未終態 Run（其 Task 亦未終態）。程序重啟後由此取回，與 GS／CS 無關。"""
        done = [x.value for x in TERMINAL_STATUSES]
        with self._sf() as s:
            stmt = (
                select(RunRow).join(TaskRow, TaskRow.task_id == RunRow.task_id)
                .where(RunRow.status.notin_(done), TaskRow.status.notin_(done))
                .order_by(RunRow.created_at)
            )
            return list(s.scalars(stmt))

    def resume_run(self, run_id: str, *, stale_after: timedelta | None = None) -> ResumePlan:
        """以新 worker_epoch 接手 Run：舊 epoch 失效；EXECUTING 的 action 轉 UNKNOWN 待 ``resolve_unknown``；
        PAUSED 的 Run／Task 回 RUNNING。已有 receipt 的 action 不會被重做。

        非 PAUSED 的 Run 仍可能有 worker 在跑：須 worker_heartbeat_at 已超過 ``stale_after`` 才可接手（L2）。
        T-7 的重新驗證（權限、裝置、檔案變更、批准過期）由呼叫端或 ``transition_guard`` 負責。
        """
        with self._tx() as (s, pending):
            task, run = self._lock_run(s, run_id)
            if RunStatus(run.status) in TERMINAL_STATUSES or RunStatus(task.status) in TERMINAL_STATUSES:
                raise IllegalTransition("Run 或 Task 已是終態，無法恢復")
            limit = self._stale_after if stale_after is None else stale_after
            beat = run.worker_heartbeat_at
            paused = run.status == RunStatus.PAUSED.value
            if paused:
                # PAUSED 時 worker 可能仍在收尾進行中的 action：有 EXECUTING 就同樣要等心跳（含暫停時間）過期
                executing = s.scalar(
                    select(ActionIntentRow.action_id).where(
                        ActionIntentRow.run_id == run_id,
                        ActionIntentRow.status == ActionStatus.EXECUTING.value,
                    ).limit(1)
                )
                marks = [m for m in (beat, run.paused_at) if m is not None]
                beat = max(marks) if marks and executing is not None else None
            if beat is not None and self._clock() - beat < limit:
                raise ActiveWorker(f"run {run_id} 的 worker 心跳未過期，不可接手")
            run.worker_epoch += 1
            run.worker_heartbeat_at = self._clock()
            self._bump(run)
            actions = list(
                s.scalars(
                    select(ActionIntentRow).where(ActionIntentRow.run_id == run_id)
                    .order_by(ActionIntentRow.created_at).with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            for action in actions:
                if action.status == ActionStatus.EXECUTING.value:
                    self._set_action_status(s, pending, task, action, ActionStatus.UNKNOWN, "action.unknown")
            if run.status == RunStatus.PAUSED.value:
                self._set_run_status(s, pending, task, run, RunStatus.RUNNING, None)
            if task.status == RunStatus.PAUSED.value:
                self._set_task_status(s, pending, task, RunStatus.RUNNING, None)
            steps = list(
                s.scalars(
                    select(StepRow).where(
                        StepRow.run_id == run_id, StepRow.status.in_(
                            [StepStatus.PENDING.value, StepStatus.RUNNING.value]
                        )
                    ).order_by(StepRow.seq)
                )
            )
            done = {ActionStatus.SUCCEEDED.value, ActionStatus.FAILED.value}
            return ResumePlan(
                task=task, run=run, checkpoint=run.checkpoint, open_steps=steps,
                open_actions=[a for a in actions if a.status not in done],
                unknown_action_ids=[a.action_id for a in actions if a.status == ActionStatus.UNKNOWN.value],
            )


# (來源狀態或 None=任意, 目標狀態) -> 事件類型；只列契約 §1.3 已註冊者
_TASK_EVENT: dict[tuple[RunStatus | None, RunStatus], str] = {
    (None, RunStatus.SUCCEEDED): "task.completed",
    (None, RunStatus.FAILED): "task.failed",
    (None, RunStatus.CANCELLED): "task.cancelled",
    (None, RunStatus.WAITING_INPUT): "task.input_requested",
    (RunStatus.WAITING_INPUT, RunStatus.RUNNING): "task.input_provided",
}
