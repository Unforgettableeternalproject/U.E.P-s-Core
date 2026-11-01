"""ContextSnapshot 的資料表與存取（migration 0003_context_output）。

快照不可變：DB trigger 禁止 DELETE，且 UPDATE 只允許把 ``sdk_session_ref`` 由 NULL 回填一次。
body 只存引用與計數，不內嵌私人原文（D33）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Computed,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from uep.context.snapshot import ContextSnapshot
from uep.db.base import Base


class StaleRevisionError(ValueError):
    """新快照的 context_revision 未大於同 conversation／segment 的現有最大值。"""


SCOPE_KEY_SQL = (
    "CASE WHEN conversation_id IS NOT NULL OR segment_id IS NOT NULL "
    "THEN 'c:' || coalesce(conversation_id, '') || ':' || coalesce(segment_id, '') "
    "WHEN run_id IS NOT NULL THEN 'r:' || run_id "
    "ELSE 'p:' || persona_id || ':' || purpose END"
)


def scope_key_of(snapshot: ContextSnapshot) -> str:
    """與 SCOPE_KEY_SQL 相同的範圍鍵：revision 在同一範圍內單調遞增（N1）。"""
    if snapshot.conversation_id is not None or snapshot.segment_id is not None:
        return f"c:{snapshot.conversation_id or ''}:{snapshot.segment_id or ''}"
    if snapshot.run_id is not None:
        return f"r:{snapshot.run_id}"
    return f"p:{snapshot.persona_id}:{snapshot.purpose.value}"


class ContextSnapshotRow(Base):
    __tablename__ = "context_snapshots"
    __table_args__ = (
        UniqueConstraint("scope_key", "context_revision", name="uq_context_snapshots_scope_revision"),
        Index("ix_context_snapshots_run_id", "run_id"),
    )

    snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    context_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    persona_id: Mapped[str] = mapped_column(String, nullable=False)
    persona_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String)
    segment_id: Mapped[str | None] = mapped_column(String)
    turn_id: Mapped[str | None] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(String)
    audience_scope: Mapped[str] = mapped_column(String, nullable=False)
    actor_ref: Mapped[str] = mapped_column(String, nullable=False)
    memory_status: Mapped[str] = mapped_column(String, nullable=False)
    history_owner: Mapped[str] = mapped_column(String, nullable=False)
    sdk_session_ref: Mapped[str | None] = mapped_column(String)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # revision 的範圍鍵（由 DB 產生，與 scope_key_of 同步）：
    # 有 conversation／segment 用之；否則退到 run_id；再退到 (persona_id, purpose)
    scope_key: Mapped[str] = mapped_column(String, Computed(SCOPE_KEY_SQL, persisted=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


_COLUMN_FIELDS = (
    "snapshot_id", "context_revision", "purpose", "persona_id", "persona_version", "policy_version",
    "conversation_id", "segment_id", "turn_id", "run_id", "audience_scope", "actor_ref",
    "memory_status", "history_owner", "sdk_session_ref", "built_at", "valid_until",
)
_BODY_FIELDS = (
    "history_window", "history_entries", "memory_slices", "excluded_summary",
    "task_refs", "commitments", "capabilities", "data_egress", "token_budget",
)


def save_snapshot(session: Session, snapshot: ContextSnapshot) -> None:
    """寫入快照；同範圍（``scope_key_of``）的 revision 必須嚴格遞增。

    以交易級 advisory lock 序列化同範圍的寫入，避免並行都讀到舊的 max；
    唯一約束 (scope_key, context_revision) 為最後防線。
    """
    key = scope_key_of(snapshot)
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})
    current = session.scalar(
        select(func.max(ContextSnapshotRow.context_revision)).where(ContextSnapshotRow.scope_key == key)
    )
    if current is not None and snapshot.context_revision <= current:
        raise StaleRevisionError(f"context_revision {snapshot.context_revision} 未大於現有 {current}")
    dumped = snapshot.model_dump(mode="json")
    row = ContextSnapshotRow(
        **{k: getattr(snapshot, k) for k in _COLUMN_FIELDS},
        body={k: dumped[k] for k in _BODY_FIELDS},
    )
    row.purpose = snapshot.purpose.value
    row.memory_status = snapshot.memory_status.value
    row.history_owner = snapshot.history_owner.value
    session.add(row)
    session.flush()


def load_snapshot(session: Session, snapshot_id: str) -> ContextSnapshot | None:
    row = session.get(ContextSnapshotRow, snapshot_id)
    if row is None:
        return None
    data = {k: getattr(row, k) for k in _COLUMN_FIELDS}
    data.update(row.body)
    return ContextSnapshot.model_validate(data)
