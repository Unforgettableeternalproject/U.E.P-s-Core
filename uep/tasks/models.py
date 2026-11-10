"""CC-6 資料表：activities、tasks、runs、steps、action_intents、receipts、approvals。

migration 手寫於 ``uep/db/migrations/versions/0002_task_model.py``，此處須與之保持一致。
這些表只由 TaskManager 寫入（契約 §6.6、§7.1）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Interval,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from uep.db.base import Base
from uep.tasks.states import (
    ActionStatus,
    ActivityKind,
    ActivityStatus,
    CancelReason,
    InterruptPolicy,
    PauseReason,
    RunStatus,
    StepStatus,
    values,
)


def in_list(column: str, enum_cls) -> str:
    """產生 CHECK 用的 ``col IN (...)``；migration 重用同一函式，避免兩邊漂移。"""
    return f"{column} IN ({', '.join(repr(v) for v in values(enum_cls))})"


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ActivityRow(Base):
    """前景／背景活動的共同索引（§6.1a）。不是生命週期容器。

    寫入者只有 TaskManager；唯一例外是 ``uep.memory.activity.DreamActivityRecorder``（記憶做夢的 maintenance 紀錄）。
    """

    __tablename__ = "activities"
    __table_args__ = (
        CheckConstraint(in_list("kind", ActivityKind), name="kind"),
        CheckConstraint(in_list("status", ActivityStatus), name="status"),
    )

    activity_id: Mapped[str] = mapped_column(String, primary_key=True)
    persona_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    presence_bound: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    origin_ref: Mapped[dict | None] = mapped_column(JSONB)
    legacy_ref: Mapped[dict | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _ts()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(in_list("status", RunStatus), name="status"),
        CheckConstraint(in_list("interrupt_policy", InterruptPolicy), name="interrupt_policy"),
        CheckConstraint(
            "pause_reason IS NULL OR " + in_list("pause_reason", PauseReason), name="pause_reason"
        ),
        # PAUSED 時 pause_reason 必填；非 PAUSED 時必須為空
        CheckConstraint(
            "(status = 'PAUSED') = (pause_reason IS NOT NULL)", name="pause_reason_iff_paused"
        ),
        Index("ix_tasks_status", "status"),
    )

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    activity_id: Mapped[str] = mapped_column(
        ForeignKey("activities.activity_id"), nullable=False, unique=True
    )
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.task_id"))
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    owner_actor_id: Mapped[str] = mapped_column(String, nullable=False)
    scope_ref: Mapped[str] = mapped_column(String, nullable=False)
    presence_bound: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    pause_reason: Mapped[str | None] = mapped_column(String)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    interrupt_policy: Mapped[str] = mapped_column(String, nullable=False)
    origin_ref: Mapped[dict | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    policy_ref: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt", name="uq_runs_task_attempt"),
        CheckConstraint(in_list("status", RunStatus), name="status"),
        CheckConstraint(
            "pause_reason IS NULL OR " + in_list("pause_reason", PauseReason), name="pause_reason"
        ),
        CheckConstraint(
            "(status = 'PAUSED') = (pause_reason IS NOT NULL)", name="pause_reason_iff_paused"
        ),
        CheckConstraint(
            "cancel_reason IS NULL OR " + in_list("cancel_reason", CancelReason), name="cancel_reason"
        ),
        Index("ix_runs_task_id", "task_id"),
        # 每個 Task 同時至多一個未終態 Run
        Index(
            "uq_runs_one_active_per_task", "task_id", unique=True,
            postgresql_where=text("status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')"),
        ),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(String, nullable=False)
    pause_reason: Mapped[str | None] = mapped_column(String)
    run_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    step_timeout: Mapped[timedelta | None] = mapped_column(Interval)
    approval_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Supervisor（SV-2，0013）：取消旗標與「已通知的 approval_expiry 值」（不是通知時間）
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String)
    approval_expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint_ref: Mapped[str | None] = mapped_column(String)
    # §6.4 內容；只存 ID／引用，不存私人原文或不可序列化物件
    checkpoint: Mapped[dict | None] = mapped_column(JSONB)
    persona_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class StepRow(Base):
    __tablename__ = "steps"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_steps_run_seq"),
        CheckConstraint(in_list("status", StepStatus), name="status"),
    )

    step_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)  # 步驟語意名稱（非私人原文）
    status: Mapped[str] = mapped_column(String, nullable=False)
    result_ref: Mapped[str | None] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _ts()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalRow(Base):
    """人類經 Approval 通道產生的核准（§6.5）。單次消耗，綁 canonical params hash。"""

    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String, primary_key=True)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    action_hash: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by_action_id: Mapped[str | None] = mapped_column(String)
    # 撤銷（migration 0016、E2）：人類否決 action 時，作廢「此 action 可消耗」的核准；撤銷後一律判無效
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String)
    revoked_reason: Mapped[str | None] = mapped_column(String)
    revoked_by_action_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = _ts()


_SIDE_EFFECTS = "side_effect_class IN ('internal_read', 'reversible_local', 'external_effect', 'high_impact')"
_IDEMPOTENCY = "idempotency IN ('native', 'via_action_id', 'none')"
_CONCURRENCY = "concurrency IN ('parallel_safe', 'parallel_one_shot', 'serial')"


class ActionIntentRow(Base):
    __tablename__ = "action_intents"
    __table_args__ = (
        CheckConstraint(in_list("status", ActionStatus), name="status"),
        CheckConstraint(_SIDE_EFFECTS, name="side_effect_class"),
        CheckConstraint(_IDEMPOTENCY, name="idempotency"),
        CheckConstraint(_CONCURRENCY, name="concurrency"),
        Index("ix_action_intents_run_id", "run_id"),
        Index("ix_action_intents_step_id", "step_id"),
        # retry_of 一對一（SA-11）：一個 action 只能被重試一次；並發第二筆由唯一索引擋下
        Index("uq_action_intents_retry_of", "retry_of", unique=True, postgresql_where=text("retry_of IS NOT NULL")),
        # D62／TC-3（D1b 修訂）：一 Run 的 EXECUTING 集合不再由部分唯一索引限制；改由 Manager 在持有 Run 鎖的
        # 同一交易內檢查（``TaskManager._check_single_executing``）
    )

    action_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False)
    step_id: Mapped[str] = mapped_column(ForeignKey("steps.step_id"), nullable=False)
    tool: Mapped[str] = mapped_column(String, nullable=False)
    executor: Mapped[str] = mapped_column(String, nullable=False)
    params_hash: Mapped[str] = mapped_column(String, nullable=False)
    side_effect_class: Mapped[str] = mapped_column(String, nullable=False)
    idempotency: Mapped[str] = mapped_column(String, nullable=False)
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")  # 建立時由註冊表寫入（D55）
    approval_ref: Mapped[str | None] = mapped_column(ForeignKey("approvals.approval_id"))
    presence_bound: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # 建立時由註冊表快照（TG-1），建立後不可改（TaskManager 沒有任何更新路徑）；fail-closed 預設 serial
    concurrency: Mapped[str] = mapped_column(String, nullable=False, server_default="serial")
    # 重試來源（CC-6 §6.5、SA-11）：指向被重試的 FAILED action（含人工標記 retry 的 UNKNOWN）；migration 0015
    retry_of: Mapped[str | None] = mapped_column(ForeignKey("action_intents.action_id"))
    status: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class ReceiptRow(Base):
    """Executor 簽回、Core 記錄的執行證據；每個 action 至多一筆。"""

    __tablename__ = "receipts"

    receipt_id: Mapped[str] = mapped_column(String, primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("action_intents.action_id"), nullable=False, unique=True
    )
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result_ref: Mapped[str | None] = mapped_column(String)
    error_code: Mapped[str | None] = mapped_column(String)
    source_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    # 取得此 receipt 時呼叫端的 worker_epoch（migration 0008）；查核取得者為 NULL
    worker_epoch: Mapped[int | None] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
