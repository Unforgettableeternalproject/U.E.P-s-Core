"""CC-7 資料表：state_projection、state_requests（migration 0005_state_stop）。

``state_projection`` 是各軸投影的唯一權威（契約 6A.3），只由 State Coordinator 寫入。
Health 軸每個服務一列（``subject`` = 服務名）；其餘軸 ``subject`` 為空字串。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from uep.db.base import Base

AXES = ("Runtime", "Presence", "Interaction", "Task", "Behavior", "Health")
DECISIONS = (
    "ACCEPTED", "REJECTED_ILLEGAL", "REJECTED_VERSION", "REJECTED_UNAUTHORIZED", "REJECTED_EXPIRED", "DEFERRED",
)


def in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class StateProjectionRow(Base):
    __tablename__ = "state_projection"
    __table_args__ = (CheckConstraint(in_list("axis", AXES), name="axis"),)

    persona_id: Mapped[str] = mapped_column(String, primary_key=True)
    axis: Mapped[str] = mapped_column(String, primary_key=True)
    subject: Mapped[str] = mapped_column(String, primary_key=True, default="", server_default="")
    state: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 投影來源事件的 aggregate_version：晚到或重播的舊事件不得讓投影回退（S-6）
    source_version: Mapped[int | None] = mapped_column(BigInteger)
    cause_event_id: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StateRequestRow(Base):
    """轉換請求與裁決紀錄；``request_id`` 為冪等鍵（6A.2）。追加式，不可修改。"""

    __tablename__ = "state_requests"
    __table_args__ = (
        CheckConstraint(in_list("decision", DECISIONS), name="decision"),
        Index("ix_state_requests_persona_axis", "persona_id", "axis"),
    )

    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    persona_id: Mapped[str] = mapped_column(String, nullable=False)
    axis: Mapped[str] = mapped_column(String, nullable=False)  # 不加 CHECK：非法軸字串也要留下被拒紀錄
    proposer: Mapped[str] = mapped_column(String, nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str] = mapped_column(String, nullable=False)
    to_state: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    scope_ref: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str] = mapped_column(String, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String)
    new_version: Mapped[int | None] = mapped_column(Integer)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cause_event_id: Mapped[str | None] = mapped_column(String)
