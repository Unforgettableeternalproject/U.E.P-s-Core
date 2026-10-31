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
import math
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
    ToolSpec,
    ToolSpecResolver,
)
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.payload_hash import PayloadHasher, resolve_hasher
from uep.events.publisher import EventPublisher, RecordedEvent
from uep.ids import is_valid_id, new_id
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

# resume_run 預設可自動解除的暫停原因；其餘（user、cooldown、resource_missing）需呼叫端明確傳入
DEFAULT_RESUME_REASONS: frozenset[PauseReason] = frozenset({PauseReason.CORE_SHUTDOWN, PauseReason.PRESENCE_LEFT})

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
    """違反政策；需要機器可判讀的原因時帶 ``code``（例如 params_not_canonical），訊息不得含參數內容。"""

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class ParamsHashMismatch(TaskError):
    """同 action_id 改了參數；須建立新 action（§6.5）。"""


class ActionBlocked(TaskError):
    """action 目前不可執行（UNKNOWN 待查核、EXECUTING 進行中、Run 非執行中、找不到 executor）。"""


class PresenceRequired(TaskError):
    """presence_bound 的前景 action 在沒有有效 Presence 時不得執行（D12、P-10）。

    ``retryable`` 區分兩種情境：階段 1 被擋（True）時 action 維持 INTENDED、核准未消耗，Presence 恢復後可重試；
    階段 1.5（階段 1 提交後、executor 呼叫前）被擋（False）時 action 已轉終態 FAILED，
    須建新 action 與新核准，不可重試同一個 action。
    """

    def __init__(self, message: str = "", *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class ActiveWorker(TaskError):
    """Run 的 worker 心跳尚未過期，不可接手（L2）。"""


class ResumeNotAllowed(TaskError):
    """暫停原因不在 ``allowed_reasons`` 內：不得由 ``resume_run`` 自動解除（例如使用者暫停）。"""


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


@dataclass(frozen=True)
class ParamsLimits:
    """params canonical 化的上限（TG-6 第 6 點，調校起點）；TaskManager 建構時可覆寫。"""

    max_depth: int = 8  # 巢狀容器層數（頂層 dict 算第 1 層）
    max_string_bytes: int = 32 * 1024  # 單一字串（含鍵）UTF-8 位元組數
    max_total_bytes: int = 128 * 1024  # canonical 後總位元組數


DEFAULT_PARAMS_LIMITS = ParamsLimits()
PARAMS_NOT_CANONICAL = "params_not_canonical"
TOOL_VERSION_CHANGED = "tool_version_changed"


def _not_canonical(reason: str) -> PolicyViolation:
    # 訊息只放固定原因，絕不帶參數內容
    return PolicyViolation(f"params 不符合 canonical 規則：{reason}", code=PARAMS_NOT_CANONICAL)


def _check_canonical(value: Any, limits: ParamsLimits, depth: int) -> None:
    """序列化前的結構檢查：深度、鍵型別、字串長度。先擋深度，避免 RecursionError／循環參照。"""
    if isinstance(value, str):
        if len(value) > limits.max_string_bytes:  # 每字元至少 1 位元組：先擋超長，避免編碼巨大字串
            raise _not_canonical("字串過長")
        if len(value.encode("utf-8")) > limits.max_string_bytes:  # lone surrogate 在此拋 UnicodeEncodeError
            raise _not_canonical("字串過長")
        return
    if isinstance(value, Mapping):
        if depth + 1 > limits.max_depth:
            raise _not_canonical("巢狀過深")
        for k, v in value.items():
            if not isinstance(k, str):
                raise _not_canonical("鍵必須是字串")
            _check_canonical(k, limits, depth + 1)
            _check_canonical(v, limits, depth + 1)
    elif isinstance(value, (list, tuple)):
        if depth + 1 > limits.max_depth:
            raise _not_canonical("巢狀過深")
        for item in value:
            _check_canonical(item, limits, depth + 1)


def _dumps(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=_reject
    ).encode("utf-8")


def canonical_params_hash(
    tool: str, version: int, params: Mapping[str, Any], limits: ParamsLimits = DEFAULT_PARAMS_LIMITS
) -> str:
    """action hash 的唯一實作（TG-6、D55）：雜湊輸入是 ``{"tool","v","params"}`` 的 canonical 形式，
    因此同時綁定工具名與版本。上限只量 params 本身；不合規一律拋 PolicyViolation(code=params_not_canonical)。"""
    if not isinstance(tool, str) or not tool:
        raise _not_canonical("工具名必須是非空字串")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _not_canonical("工具版本必須是整數")
    try:
        _check_canonical(params, limits, 0)
        params_raw = _dumps(params)
        if len(params_raw) > limits.max_total_bytes:
            raise _not_canonical("總長過大")
        raw = _dumps({"tool": tool, "v": version, "params": params})
    except PolicyViolation:
        raise
    except (ValueError, TypeError, UnicodeEncodeError, RecursionError, OverflowError):
        raise _not_canonical("無法序列化") from None  # 不串接原例外，避免訊息帶出參數內容
    return hashlib.sha256(raw).hexdigest()


def params_hash(tool: str, version: int, params: Mapping[str, Any]) -> str:
    """action hash（欄位名沿用 params_hash）：以預設上限；規則見 canonical_params_hash。"""
    return canonical_params_hash(tool, version, params)


def _reject(obj: Any) -> Any:
    raise TypeError(f"參數含無法 canonical 化的型別：{type(obj).__name__}")


_SAFE_STR = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")
_MAX_DEPTH = 4


def check_safe_value(value: Any, path: str = "value", depth: int = 0) -> None:
    """值層級白名單（M3）：只允許 ID／數字／短碼，字串限 64 字且字元集受限，擋私人原文。"""
    if depth > _MAX_DEPTH:
        raise PolicyViolation(f"{path} 巢狀過深")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):  # NaN／Infinity 不是合法 JSON，JSONB 入庫會失敗
            raise PolicyViolation(f"{path} 不是有限數值")
        return
    if isinstance(value, str):
        if not _SAFE_STR.fullmatch(value):
            raise PolicyViolation(f"{path} 不是 ID／短碼（不允許自由文字）")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            check_safe_value(item, f"{path}[{i}]", depth + 1)
        return
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str) or not _SAFE_STR.fullmatch(k):
                raise PolicyViolation(f"{path} 含不合法的鍵")
            check_safe_value(v, f"{path}.{k}", depth + 1)
        return
    raise PolicyViolation(f"{path} 型別不允許：{type(value).__name__}")


# 引用字串（result_ref、error_code）：URI 路徑安全字元、無空白、長度上限 256（N4）。
# 不含 @ % = +：這些能夾帶 email、百分比編碼與 base64 之類的私人內容
_SAFE_REF = re.compile(r"[A-Za-z0-9_.:/\-~]{1,256}")
# 脫敏時 Core 自己寫入 meta 的保留鍵；executor 帶這些鍵的 meta 一律視為不合格
_RESERVED_META_KEYS = frozenset({"redacted", "redactions"})


def _canonical_bytes(value: Any) -> bytes:
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return raw.encode("utf-8")


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
        tool_resolver: ToolSpecResolver,
        clock: Callable[[], datetime] = _utcnow,
        transition_guard: TransitionGuard | None = None,
        crash_hook: Callable[[str], None] | None = None,
        approvers: Callable[[TaskRow], frozenset[str]] | None = None,
        heartbeat_stale_after: timedelta = timedelta(seconds=60),
        outbox_targets: Mapping[str, Sequence[str]] | None = None,
        presence_check: Callable[[str], bool] | None = None,
        action_guard: Callable[[Session], str | None] | None = None,
        payload_hasher: PayloadHasher | None = None,
        params_limits: ParamsLimits = DEFAULT_PARAMS_LIMITS,
    ) -> None:
        if tool_resolver is None:  # TG-1：分類只能來自註冊表，缺則建構失敗
            raise TypeError("TaskManager 需要 tool_resolver（TG-1）")
        self._sf = session_factory
        self._tool_resolver = tool_resolver
        self._params_limits = params_limits  # params canonical 化上限（TG-6）
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
        # CC-7／全域停止的 action 層檢查（T-10）：回非 None 的原因碼即拒絕「新」工具執行；
        # 只在 execute_action 階段 1 生效，resolve_unknown 與階段 3 的 receipt 記錄不擋。
        # 以階段 1 的交易 session 呼叫（鎖順序 task -> run -> action -> 旗標列），讓旗標共享鎖
        # 持有到階段 1 提交：engage 的 FOR UPDATE 會等階段 1 提交，不會有「讀旗標後才 engage」的窗口
        self._action_guard = action_guard
        # receipt 脫敏雜湊用的 HMAC 雜湊器；None 時於需要時才讀設定，沒有金鑰就 fail-closed（與 payload blob 一致）
        self._payload_hasher = payload_hasher
        self.redaction_count = 0  # receipt 欄位被脫敏的累計次數（N4）

    @property
    def tool_resolver(self) -> ToolSpecResolver:
        return self._tool_resolver

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
        if to is RunStatus.SUCCEEDED:
            # T-13：Task 完成前，所有 Run（含先前失敗或取消的）都不得留有結果未明的 action
            all_runs = list(s.scalars(select(RunRow.run_id).where(RunRow.task_id == task.task_id)))
            self._assert_no_inflight(s, all_runs)
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
            payload: dict[str, Any] = {"status": to.value, "run_id": run.run_id}
            if to is not RunStatus.SUCCEEDED:
                # Run 以 FAILED／CANCELLED 結束時，EXECUTING／UNKNOWN 的 action 仍待查核：留下可查的標記
                leftover = list(s.scalars(
                    select(ActionIntentRow.action_id).where(
                        ActionIntentRow.run_id == run.run_id,
                        ActionIntentRow.status.in_([ActionStatus.EXECUTING.value, ActionStatus.UNKNOWN.value]),
                    ).order_by(ActionIntentRow.created_at)
                ))
                if leftover:
                    payload.update(reason_code="unresolved_actions", count=len(leftover), action_id=leftover[0])
                    logger.warning("Run 結束時仍有結果未明的 action：run=%s actions=%s", run.run_id, leftover)
            self._emit(
                s, pending, "run.ended", task, aggregate=("run", run.run_id, run.version),
                run_id=run.run_id, payload=payload,
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
            if to not in TERMINAL_STATUSES:
                # Task 轉非終態時，未終態 Run 連動轉到同狀態與 pause_reason，避免 Task 與 Run 脫鉤
                # （終態由 _set_task_status 負責收尾）
                for run in self._live_runs(s, task_id):
                    if run.status != to.value:
                        self._set_run_status(s, pending, task, run, to, pause_reason)
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
            if current is RunStatus.PAUSED:
                raise IllegalTransition("task 為 PAUSED，須先 resume 才可啟動 Run")
            if self._guard is not None:  # 新 Run 直接以 RUNNING 建立，同樣要過 CC-7 裁決（全域停止期間不可啟動）
                self._guard(TransitionRequest("run", f"new:{task_id}", RunStatus.PENDING, RunStatus.RUNNING))
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
        self, run_id: str, to: RunStatus, *, worker_epoch: int, expected_version: int | None = None,
        pause_reason: PauseReason | None = None,
    ) -> RunRow:
        """worker 的 Run 狀態轉移；``worker_epoch`` 必填。帶舊 worker_epoch 的回報只記錄、不改變狀態（T-9）。

        Supervisor／Core 要不論 epoch 強制轉移時，改用 ``force_transition_run``。
        """
        with self._tx() as (s, pending):
            task, run = self._lock_run(s, run_id)
            if worker_epoch != run.worker_epoch:
                logger.warning("忽略舊 worker_epoch 的 Run 轉移：run=%s epoch=%s", run_id, worker_epoch)
                return run
            if expected_version is not None and run.version != expected_version:
                raise StaleVersion(f"run {run_id} 版本 {run.version}，預期 {expected_version}")
            self._set_run_status(s, pending, task, run, to, pause_reason)
            return run

    def force_transition_run(
        self, run_id: str, to: RunStatus, *, reason: str, expected_version: int | None = None,
        pause_reason: PauseReason | None = None,
    ) -> RunRow:
        """Supervisor／Core 專用：不驗 worker_epoch 的 Run 轉移（仍走轉移表與 guard）。必須附原因並記 log。"""
        if not reason or not _SAFE_STR.fullmatch(reason):
            raise PolicyViolation("force_transition_run 必須附上短碼 reason")
        with self._tx() as (s, pending):
            task, run = self._lock_run(s, run_id)
            if expected_version is not None and run.version != expected_version:
                raise StaleVersion(f"run {run_id} 版本 {run.version}，預期 {expected_version}")
            logger.warning(
                "強制轉移 Run：run=%s %s -> %s reason=%s", run_id, run.status, to.value, reason
            )
            self._set_run_status(s, pending, task, run, to, pause_reason)
            return run

    @property
    def action_guard_configured(self) -> bool:
        return self._action_guard is not None

    @property
    def transition_guard_configured(self) -> bool:
        return self._guard is not None

    @property
    def presence_check_configured(self) -> bool:
        """是否注入了 Presence 檢查（組裝層用來斷言 R1：presence_check 必填）。"""
        return self._presence_check is not None

    def pause_presence_bound(
        self, persona_id: str, reason: PauseReason = PauseReason.PRESENCE_LEFT
    ) -> list[str]:
        """presence_bound 的未終態 Task 於離場時暫停（P-10、T-6）；回傳本次實際轉入 PAUSED 的 task_id。

        背景（presence_bound=false）Task 不受影響。已 PAUSED 者冪等略過。暫停只改 Task／Run 狀態：
        已提交且不可中斷的 EXECUTING action 仍由其 worker 完成並記 receipt，不假裝取消成功；
        轉移表不允許暫停的狀態（如 UNKNOWN）只記 warning，留待查核。單一 Task 失敗不影響其餘。
        """
        with self._sf() as s:
            candidates = list(s.scalars(
                select(TaskRow.task_id)
                .join(ActivityRow, ActivityRow.activity_id == TaskRow.activity_id)
                .where(
                    ActivityRow.persona_id == persona_id, TaskRow.presence_bound.is_(True),
                    TaskRow.status.notin_([x.value for x in TERMINAL_STATUSES] + [RunStatus.PAUSED.value]),
                )
            ))
        paused: list[str] = []
        for task_id in candidates:
            try:
                task = self.pause_task(task_id, reason)
            except IllegalTransition as exc:
                logger.warning("presence 離場無法暫停 task=%s：%s", task_id, exc)
                continue
            except Exception:
                logger.exception("presence 離場暫停 task=%s 失敗", task_id)
                continue
            if task.status == RunStatus.PAUSED.value:
                paused.append(task_id)
        return paused

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
            if RunStatus(run.status) in TERMINAL_STATUSES:
                raise IllegalTransition(f"run 已是終態 {run.status}，不可新增 Step")
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
        """結束 Step。舊 worker_epoch 拋 StaleWorkerEpoch、不改變狀態（T-9）。已結束者冪等。"""
        with self._tx() as (s, pending):
            run_id = self._need(s.scalar(select(StepRow.run_id).where(StepRow.step_id == step_id)), "step", step_id)
            task, run = self._lock_run(s, run_id)
            step = self._need(
                s.get(StepRow, step_id, with_for_update=True, populate_existing=True), "step", step_id
            )
            self._require_epoch(run, worker_epoch)
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
        """寫入 §6.4 checkpoint（只含 ID／引用）。舊 worker_epoch 拋 StaleWorkerEpoch、不寫入。"""
        extra = sorted(set(checkpoint) - CHECKPOINT_KEYS)
        if extra:
            raise PolicyViolation(f"checkpoint 含契約外的鍵：{extra}")
        check_safe_value(dict(checkpoint), "checkpoint")
        data = json.loads(json.dumps(dict(checkpoint)))  # 確認可序列化，並與呼叫者物件脫鉤
        with self._tx() as (s, _pending):
            _task, run = self._lock_run(s, run_id)
            self._require_epoch(run, worker_epoch)
            run.checkpoint = data
            run.checkpoint_ref = checkpoint_ref
            self._bump(run)
            return run

    @staticmethod
    def _require_epoch(run: RunRow, worker_epoch: int) -> None:
        if worker_epoch != run.worker_epoch:
            raise StaleWorkerEpoch(f"run {run.run_id} 目前 epoch={run.worker_epoch}，收到 {worker_epoch}")

    def heartbeat(self, run_id: str, *, worker_epoch: int) -> None:
        """worker 心跳；舊 epoch 拋 StaleWorkerEpoch（讓被取代的 worker 得知自己該停手）。"""
        with self._tx() as (s, _pending):
            _task, run = self._lock_run(s, run_id)
            self._require_epoch(run, worker_epoch)
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
    def params_hash(tool: str, version: int, params: Mapping[str, Any]) -> str:
        return params_hash(tool, version, params)

    @staticmethod
    def _assert_same_action(
        existing: ActionIntentRow, run_id: str, tool: str, spec: ToolSpec, digest: str
    ) -> None:
        """同 action_id 重呼叫只在 run、tool、註冊表解析出的 side_effect_class、action hash 全相同時冪等回傳既有列。"""
        if existing.run_id != run_id or existing.tool != tool or existing.side_effect_class != spec.side_effect_class:
            raise PolicyViolation("同 action_id 已屬於不同的 run／tool／side_effect_class，須建立新 action")
        if existing.params_hash != digest:
            raise ParamsHashMismatch("同 action_id 的參數已變更，須建立新 action")

    def create_action(
        self, *, run_id: str, step_id: str, tool: str, params: Mapping[str, Any], worker_epoch: int,
        presence_bound: bool | None = None, action_id: str | None = None,
    ) -> ActionIntentRow:
        """建立 INTENDED 的 ActionIntent。同 action_id 重呼叫回傳既有列；參數改變則拒絕（須新 action）。

        executor／side_effect_class／idempotency／tool_version 一律來自 ``tool_resolver.resolve(tool)``（TG-1）；
        ``presence_bound`` 只能收緊（TG-3）：註冊表為真時不可傳 False。
        """
        spec = self._tool_resolver.resolve(tool)  # 解析不到拋 PolicyViolation；在交易之前
        if presence_bound is False and spec.presence_bound:
            raise PolicyViolation("presence_bound 只能收緊，不可放寬註冊表的宣告", code="presence_bound_loosened")
        digest = canonical_params_hash(tool, spec.tool_version, params, self._params_limits)  # 不合規不產生 act_ 列
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
                    self._assert_same_action(existing, run_id, tool, spec, digest)
                    return existing
            action = ActionIntentRow(
                action_id=action_id or new_id("act"), run_id=run_id, step_id=step_id, tool=tool,
                executor=spec.executor, params_hash=digest, side_effect_class=spec.side_effect_class,
                idempotency=spec.idempotency, tool_version=spec.tool_version,
                presence_bound=spec.presence_bound or bool(presence_bound),
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
                self._assert_same_action(existing, run_id, tool, spec, digest)
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

    def _hasher(self) -> PayloadHasher:
        """脫敏雜湊器：明確注入優先，否則讀設定；沒有金鑰即 PayloadHashKeyMissing（fail-closed，D42）。"""
        return resolve_hasher(self._payload_hasher)

    def _record_receipt(
        self, s: Session, action_id: str, receipt: ToolReceipt, worker_epoch: int | None = None
    ) -> ReceiptRow:
        """receipt 入庫（每個 action 至多一筆）。值層級不合格的 receipt_id／meta／error_code／result_ref
        以脫敏值取代而不拒收，因為副作用已發生，證據不能丟（M3）。脫敏摘要用 HMAC（D42），沒有金鑰時
        拒絕（與 payload blob 一致）。已有未驗證 receipt 而新的已驗證時，升級為已驗證。"""
        meta = dict(receipt.meta)
        receipt_id = receipt.receipt_id
        redactions: dict[str, dict[str, str]] = {}

        def redact(field_name: str, original: Any, reason: str) -> None:
            redactions[field_name] = {"sha256": self._hasher().hash(_canonical_bytes(original)), "reason": reason}

        if not isinstance(receipt_id, str) or not is_valid_id(receipt_id, "rcpt"):
            redact("receipt_id", receipt_id, "not_rcpt_ulid")
            receipt_id = new_id("rcpt")
        if meta and (_RESERVED_META_KEYS & set(meta)):
            redact("meta", meta, "meta_reserved_key")  # 保留鍵只有 Core 能寫，executor 自帶者不可信
            meta = {}
        elif meta and not _is_safe(meta):
            redact("meta", meta, "meta_not_id_or_short_code")
            meta = {}
        error_code, result_ref = receipt.error_code, receipt.result_ref
        if error_code is not None and not _SAFE_REF.fullmatch(error_code):
            redact("error_code", error_code, "not_uri_safe_reference")
            error_code = "redacted"
        if result_ref is not None and not _SAFE_REF.fullmatch(result_ref):
            redact("result_ref", result_ref, "not_uri_safe_reference")
            result_ref = "redacted"
        if redactions:
            self.redaction_count += len(redactions)
            logger.warning("receipt 欄位已脫敏：action=%s fields=%s", action_id, sorted(redactions))
            meta = {**meta, "redacted": True, "redactions": redactions}
        values = {
            "receipt_id": receipt_id, "action_id": action_id, "succeeded": receipt.succeeded,
            "result_ref": result_ref, "error_code": error_code,
            "source_verified": receipt.source_verified, "meta": meta or None,
            "worker_epoch": worker_epoch, "recorded_at": receipt.recorded_at,
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
        receipt: ToolReceipt, worker_epoch: int | None = None,
    ) -> ReceiptRow:
        """以實際入庫的那筆 receipt 推進 action；未驗證來源者維持（或轉為）UNKNOWN（M7）。"""
        stored = self._record_receipt(s, action.action_id, receipt, worker_epoch)
        if not stored.source_verified:
            if action.status == ActionStatus.EXECUTING.value:
                self._set_action_status(s, pending, task, action, ActionStatus.UNKNOWN, "action.unknown")
            return stored
        to = ActionStatus.SUCCEEDED if stored.succeeded else ActionStatus.FAILED
        self._set_action_status(s, pending, task, action, to, "action.executed")
        return stored

    def _force_unknown(self, action_id: str) -> None:
        """階段 3 入庫失敗的保底：在全新交易內把仍為 EXECUTING 的 action 轉 UNKNOWN 並落地。"""
        with self._tx() as (s, pending):
            task, _run, action = self._lock_action(s, action_id)
            if ActionStatus(action.status) is ActionStatus.EXECUTING:
                self._set_action_status(s, pending, task, action, ActionStatus.UNKNOWN, "action.unknown")

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
        - 階段 1.5（階段 1 提交後、executor 呼叫前）重檢：epoch 已變或 action 不在 EXECUTING 拋 StaleWorkerEpoch、
          不呼叫 executor；全域停止或 Presence 失效則 action 轉終態 FAILED 並拋 ActionBlocked／PresenceRequired。
          階段 1.5 的 PresenceRequired 帶 ``retryable=False``：action 已是終態，須建新 action 與新核准；
          階段 1 的 PresenceRequired（``retryable=True``）則 action 維持 INTENDED、核准未消耗，可於 Presence 恢復後重試。
        """
        deferred: TaskError | None = None
        call: ToolCall | None = None
        executor: ToolExecutor | None = None

        # 階段 1：檢查、核准、標 EXECUTING 並提交（崩潰後可由恢復流程轉 UNKNOWN）
        with self._tx() as (s, pending):
            task, run, action = self._lock_action(s, action_id)
            self._require_epoch(run, worker_epoch)
            # hash 域含工具名與版本（D55）：以 action 建立時的 tool／tool_version 計算
            digest = canonical_params_hash(action.tool, action.tool_version, params, self._params_limits)
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
            if task.status not in (RunStatus.RUNNING.value, RunStatus.WAITING_APPROVAL.value):
                # Task 為 PAUSED（含使用者暫停）等狀態時不得執行，即使 Run 狀態與之脫鉤
                raise ActionBlocked(f"task 狀態為 {task.status}，不可執行 action")
            if run.status not in (RunStatus.RUNNING.value, RunStatus.WAITING_APPROVAL.value):
                raise ActionBlocked(f"run 狀態為 {run.status}，不可執行 action")
            if self._tool_resolver.resolve(action.tool).tool_version != action.tool_version:
                # 註冊表升版後 fail-closed：須以新 action 與新 approval 重來，不自動沿用舊版
                raise ActionBlocked(f"{TOOL_VERSION_CHANGED}：{action.tool} 已升版，須建立新 action")
            executor = self._executors.get(action.executor)
            if executor is None:
                raise ActionBlocked(f"找不到 executor：{action.executor}")
            if self._presence_check is not None and task.presence_bound:
                activity = s.get(ActivityRow, task.activity_id)
                if activity is None:
                    raise NotFound(f"找不到 activity：{task.activity_id}")
                persona_id = activity.persona_id
                if not self._presence_check(persona_id):
                    raise PresenceRequired(
                        "前景 action 需要有效 Presence；請待 Presence 恢復後再執行", retryable=True
                    )
            if self._action_guard is not None:
                blocked = self._action_guard(s)
                if blocked:  # 拋出使交易回滾：核准不被消耗、不轉 EXECUTING
                    raise ActionBlocked(blocked)

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
                    approval_id=action.approval_ref, tool_version=action.tool_version,
                )
        if deferred is not None:
            raise deferred
        self._crash("action.after_executing_commit")

        # 階段 1.5：階段 1 提交到呼叫 executor 之間的殘餘窗口（全域停止、Presence 失效）再驗一次；
        # 被擋時確定未呼叫 executor，action 轉 FAILED（不是 UNKNOWN）後上拋
        blocked_exc = self._recheck_before_call(action_id, worker_epoch)
        if blocked_exc is not None:
            raise blocked_exc

        # 階段 2：呼叫 executor（不持有 DB 交易）
        receipt_in: ToolReceipt | None = None
        failure: Exception | None = None
        try:
            receipt_in = executor.execute(call)
        except ToolResponseLost:
            pass
        except Exception as exc:  # noqa: BLE001  結果未知：標 UNKNOWN 後再上拋，不重試
            failure = exc

        # 階段 3：依 receipt 或遺失結果推進。入庫失敗（脫敏金鑰缺失、DB 錯誤、receipt_id 衝突等）時，
        # 副作用已發生，保證 action 轉 UNKNOWN 並於新交易落地，再把原例外上拋，不留在 EXECUTING
        try:
            outcome, failure = self._finish_execution(action_id, worker_epoch, receipt_in, failure)
        except Exception as exc:
            # 只記型別，不記訊息全文（可能含私人內容）；原 executor 結果／例外型別一併留線索
            logger.error(
                "階段 3 入庫失敗，強制轉 UNKNOWN：action=%s error=%s executor_result=%s",
                action_id, type(exc).__name__,
                type(failure).__name__ if failure is not None
                else ("receipt" if receipt_in is not None else "response_lost"),
            )
            try:
                self._force_unknown(action_id)
            except Exception:
                logger.exception("階段 3 失敗後轉 UNKNOWN 也失敗：action=%s", action_id)
            raise
        if failure is not None:
            raise failure
        return outcome

    def _recheck_before_call(self, action_id: str, worker_epoch: int) -> TaskError | None:
        """階段 1 提交後、executor 呼叫前的重檢（新短交易）：再呼叫一次 presence_check 與 action_guard。

        被擋時 executor 尚未被呼叫，副作用確定沒發生：action 由 EXECUTING 轉 FAILED，並寫一筆 Core 自行簽發的
        receipt（error_code ``stopped_before_call`` 或 ``presence_lost_before_call``），回傳要上拋的例外。
        epoch 已變或 action 不在 EXECUTING（Run 已被接手、action 已轉 UNKNOWN 等）時，殭屍 worker 不得呼叫 executor：
        拋 StaleWorkerEpoch，不寫 receipt、不改狀態（交 resolve_unknown 查核）。
        """
        exc: TaskError | None = None
        with self._tx() as (s, pending):
            task, run, action = self._lock_action(s, action_id)
            if run.worker_epoch != worker_epoch or ActionStatus(action.status) is not ActionStatus.EXECUTING:
                raise StaleWorkerEpoch(
                    f"run {run.run_id} 在 executor 呼叫前已被接手或 action 不再 EXECUTING；本 worker 不得執行"
                )
            if self._presence_check is None and self._action_guard is None:
                return None
            code = ""
            if self._presence_check is not None and task.presence_bound:
                activity = s.get(ActivityRow, task.activity_id)
                if activity is None or not self._presence_check(activity.persona_id):
                    code = "presence_lost_before_call"
                    exc = PresenceRequired(
                        "呼叫 executor 前 Presence 已失效；action 未執行且已轉終態 FAILED，"
                        "階段 1.5 被擋為終態不可重試，須建新 action 與新核准", retryable=False,
                    )
            if exc is None and self._action_guard is not None:
                blocked = self._action_guard(s)
                if blocked:
                    code = "stopped_before_call"
                    exc = ActionBlocked(blocked)
            if exc is None:
                return None
            self._record_receipt(s, action_id, ToolReceipt(
                receipt_id=new_id("rcpt"), action_id=action_id, succeeded=False, result_ref=None,
                recorded_at=self._clock(), source_verified=True, error_code=code,
                meta={"origin": "core_pre_call"},
            ), worker_epoch)
            self._set_action_status(s, pending, task, action, ActionStatus.FAILED, "action.executed")
        return exc

    def _finish_execution(
        self, action_id: str, worker_epoch: int, receipt_in: ToolReceipt | None, failure: Exception | None
    ) -> tuple[ActionOutcome, Exception | None]:
        with self._tx("action.before_receipt_commit") as (s, pending):
            task, run, action = self._lock_action(s, action_id)
            matched = receipt_in is not None and receipt_in.action_id == action_id
            if run.worker_epoch != worker_epoch:
                # T-9：舊 epoch 的回報只記錄證據，不推進狀態；狀態交 resolve_unknown 查核
                stored = self._record_receipt(s, action_id, receipt_in, worker_epoch) if matched else None
                return ActionOutcome(action_id, ActionStatus(action.status), True, stored), failure
            if ActionStatus(action.status) is not ActionStatus.EXECUTING:
                return ActionOutcome(action_id, ActionStatus(action.status), True, None), failure
            if matched:
                rec = self._apply_receipt(s, pending, task, action, receipt_in, worker_epoch)
                outcome = ActionOutcome(action_id, ActionStatus(action.status), True, rec)
            else:
                self._set_action_status(s, pending, task, action, ActionStatus.UNKNOWN, "action.unknown")
                if receipt_in is not None:
                    failure = failure or TaskError("executor 回傳的 receipt.action_id 與呼叫不符")
                outcome = ActionOutcome(action_id, ActionStatus.UNKNOWN, True, None)
        return outcome, failure

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
            # 已入庫的已驗證 receipt 是比 executor 回報更強的證據：不再向 executor 查核
            has_verified = s.scalar(
                select(ReceiptRow.receipt_id).where(
                    ReceiptRow.action_id == action_id, ReceiptRow.source_verified.is_(True)
                )
            ) is not None
        if executor is None and not has_verified:
            raise ActionBlocked(f"找不到 executor：{action.executor}")

        lookup, authoritative = ReceiptLookup("undetermined"), False
        if not has_verified:
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
            stored = s.scalar(
                select(ReceiptRow).where(ReceiptRow.action_id == action_id, ReceiptRow.source_verified.is_(True))
                .execution_options(populate_existing=True)
            )
            if stored is not None:
                # 已有已驗證 receipt：依其結果推進，無論 executor 怎麼說都不可退回 INTENDED
                to = ActionStatus.SUCCEEDED if stored.succeeded else ActionStatus.FAILED
                self._set_action_status(s, pending, task, action, to, "action.executed")
                return ActionOutcome(action_id, to, False, stored)
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
        """所有未終態 Run（其 Task 亦未終態）。程序重啟後由此取回，與 GS／CS 無關。

        回傳的 RunRow 帶 ``pause_reason``（PAUSED 者），呼叫端據此決定是否傳 ``allowed_reasons`` 恢復。"""
        done = [x.value for x in TERMINAL_STATUSES]
        with self._sf() as s:
            stmt = (
                select(RunRow).join(TaskRow, TaskRow.task_id == RunRow.task_id)
                .where(RunRow.status.notin_(done), TaskRow.status.notin_(done))
                .order_by(RunRow.created_at)
            )
            return list(s.scalars(stmt))

    def resume_run(
        self, run_id: str, *, stale_after: timedelta | None = None,
        allowed_reasons: frozenset[PauseReason] = DEFAULT_RESUME_REASONS,
    ) -> ResumePlan:
        """以新 worker_epoch 接手 Run：舊 epoch 失效；EXECUTING 的 action 轉 UNKNOWN 待 ``resolve_unknown``；
        PAUSED 的 Run／Task 回 RUNNING。已有 receipt 的 action 不會被重做。

        非 PAUSED 的 Run 仍可能有 worker 在跑：須 worker_heartbeat_at 已超過 ``stale_after`` 才可接手（L2）。
        PAUSED 的 Run／Task 只在 pause_reason 屬於 ``allowed_reasons`` 時才自動解除（預設只有 core_shutdown、
        presence_left）；使用者暫停、cooldown、resource_missing 等需呼叫端明確傳入，否則拋 ResumeNotAllowed。
        T-7 的重新驗證（權限、裝置、檔案變更、批准過期）由呼叫端或 ``transition_guard`` 負責。
        """
        with self._tx() as (s, pending):
            task, run = self._lock_run(s, run_id)
            if RunStatus(run.status) in TERMINAL_STATUSES or RunStatus(task.status) in TERMINAL_STATUSES:
                raise IllegalTransition("Run 或 Task 已是終態，無法恢復")
            for pausing in (run, task):
                if pausing.status == RunStatus.PAUSED.value and PauseReason(pausing.pause_reason) not in allowed_reasons:
                    raise ResumeNotAllowed(f"暫停原因 {pausing.pause_reason} 不在允許自動恢復的範圍內")
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
