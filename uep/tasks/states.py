"""CC-6 的列舉與狀態機轉移表（契約 §6.2、§6.3、§6.5；D30、D31）。

Task 與 Run 共用同一組狀態與轉移表（§6.3「同 Task」）。READY 併入 PENDING，BLOCKED 併入
PAUSED＋pause_reason（D30）。
"""
from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


# Task 與 Run 狀態集合相同；另設別名方便閱讀
TaskStatus = RunStatus


class PauseReason(StrEnum):
    USER = "user"
    COOLDOWN = "cooldown"
    RESOURCE_MISSING = "resource_missing"
    PRESENCE_LEFT = "presence_left"
    CORE_SHUTDOWN = "core_shutdown"


class InterruptPolicy(StrEnum):
    INTERRUPTIBLE = "interruptible"
    CHECKPOINT_ONLY = "checkpoint_only"
    NON_INTERRUPTIBLE = "non_interruptible"


# 嚴格程度：數字越大越不可中斷。建立者只能收緊（D31）。
INTERRUPT_STRICTNESS = {
    InterruptPolicy.INTERRUPTIBLE: 0,
    InterruptPolicy.CHECKPOINT_ONLY: 1,
    InterruptPolicy.NON_INTERRUPTIBLE: 2,
}


class ActivityKind(StrEnum):
    INTERACTION_EPISODE = "interaction_episode"
    TASK = "task"
    MAINTENANCE = "maintenance"


class ActivityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ActionStatus(StrEnum):
    INTENDED = "INTENDED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


S = RunStatus

TERMINAL_STATUSES = frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED})

# Task／Run 合法轉移（終態無出邊）
RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    S.PENDING: frozenset({S.RUNNING, S.PAUSED, S.CANCELLED, S.FAILED}),
    S.RUNNING: frozenset({
        S.WAITING_INPUT, S.WAITING_APPROVAL, S.PAUSED,
        S.SUCCEEDED, S.FAILED, S.CANCELLED, S.UNKNOWN,
    }),
    S.WAITING_INPUT: frozenset({S.RUNNING, S.PAUSED, S.CANCELLED, S.FAILED}),
    S.WAITING_APPROVAL: frozenset({S.RUNNING, S.PAUSED, S.CANCELLED, S.FAILED}),
    S.PAUSED: frozenset({S.RUNNING, S.CANCELLED, S.FAILED}),
    S.UNKNOWN: frozenset({S.RUNNING, S.SUCCEEDED, S.FAILED, S.CANCELLED}),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}
TASK_TRANSITIONS = RUN_TRANSITIONS

A = ActionStatus

# ActionIntent 合法轉移。UNKNOWN -> INTENDED 只在 executor 確認「未執行」後發生（T-3）。
ACTION_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    A.INTENDED: frozenset({A.APPROVED}),
    A.APPROVED: frozenset({A.EXECUTING}),
    A.EXECUTING: frozenset({A.SUCCEEDED, A.FAILED, A.UNKNOWN}),
    A.UNKNOWN: frozenset({A.SUCCEEDED, A.FAILED, A.INTENDED}),
    A.SUCCEEDED: frozenset(),
    A.FAILED: frozenset(),
}

# 需有效 approval 的副作用類別（§6.5：外部副作用／高影響必填；T-4）
APPROVAL_REQUIRED = frozenset({"external_effect", "high_impact"})


def values(enum_cls: type[StrEnum]) -> list[str]:
    return [m.value for m in enum_cls]
