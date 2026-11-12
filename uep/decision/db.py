"""decision_log 資料表（migration 0010_decision_log）、DB sink 與讀取（重放工具用）。

只存決策摘要，不存原文（原文以 ``utterance_refs`` 指回事件）。primary 與 shadow 同表，以 ``role`` 區分，
shadow 列以 ``primary_decision_id`` 指向 primary。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    SmallInteger,
    String,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from uep.config import get_settings
from uep.db.base import Base
from uep.db.engine import make_engine, make_session_factory
from uep.decision.log import COLUMNS, DecisionLogSink, DecisionLogWriter

logger = logging.getLogger("uep.decision.db")


class DecisionLogRow(Base):
    __tablename__ = "decision_log"

    decision_id: Mapped[str] = mapped_column(String, primary_key=True)  # dec_<ULID>
    request_id: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # primary／shadow
    primary_decision_id: Mapped[str | None] = mapped_column(String)  # shadow 列指向 primary
    engine: Mapped[str | None] = mapped_column(String)  # tier：rule／feature_rule／model／llm／fallback
    proposer_id: Mapped[str | None] = mapped_column(String)
    version: Mapped[str | None] = mapped_column(String)
    outcome: Mapped[str | None] = mapped_column(String)  # 例如 reply_mode；shadow 失敗時為 NULL
    reason_code: Mapped[str | None] = mapped_column(String)
    confidence: Mapped[float | None] = mapped_column(Float)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, nullable=False)  # ok／timeout／busy／error／invalid／late
    agree: Mapped[bool | None] = mapped_column(Boolean)  # shadow 列：outcome 是否同 primary
    fallback: Mapped[bool | None] = mapped_column(Boolean)
    candidate_id: Mapped[str | None] = mapped_column(String)
    participant_count: Mapped[int | None] = mapped_column(Integer)
    addressed_hint: Mapped[bool | None] = mapped_column(Boolean)
    utterance_refs: Mapped[list[str] | None] = mapped_column(JSONB)
    sub_kind: Mapped[str | None] = mapped_column(String)  # 例如 loop.consult 的 sub_kind（migration 0014；舊列為 NULL）
    trigger: Mapped[str | None] = mapped_column(String)  # initial／reeval／event／tick
    namespace: Mapped[str | None] = mapped_column(String)  # 決策主體的 namespace ID
    snapshot_version: Mapped[int | None] = mapped_column(Integer)  # 決策依據的 policy 快照版本
    platform_context_version: Mapped[int | None] = mapped_column(Integer)  # 決策當下平台快照版本（migration 0018）
    target: Mapped[str | None] = mapped_column(String)  # 決策指向的目標位置（placement.target_platform；migration 0019）
    chain_id: Mapped[str | None] = mapped_column(String)  # 複合句 chain 的 ID（chain_<ULID>；migration 0020；舊列為 NULL）
    chain_seq: Mapped[int | None] = mapped_column(SmallInteger)  # chain 內序號（0＝母句，1 起為段落；migration 0020）

    __table_args__ = (
        CheckConstraint("role IN ('primary', 'shadow')", name="role"),
        Index("ix_decision_log_kind_created_at", "kind", "created_at"),
        Index("ix_decision_log_created_at", "created_at"),
        Index("ix_decision_log_request_id", "request_id"),
        Index("ix_decision_log_primary_decision_id", "primary_decision_id"),
        Index("ix_decision_log_kind_sub_kind", "kind", "sub_kind"),
        Index("ix_decision_log_chain", "chain_id", "chain_seq"),
    )


class DbSink:
    """批次寫入 decision_log。``decision_id`` 重複（重送）時失敗整批，由寫入器計入 dropped。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def write(self, rows: Sequence[dict[str, Any]]) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(insert(DecisionLogRow), [dict(r) for r in rows])


def load_rows(
    session_factory: sessionmaker[Session], *, kind: str | None = None,
    since: datetime | None = None, until: datetime | None = None, sub_kind: str | None = None,
) -> list[dict[str, Any]]:
    """讀 decision_log（依 kind、sub_kind 與時間範圍 [since, until) 篩選），回傳與 JSONL 同形的 dict 列。

    ``sub_kind`` 為 SQL 等值比對：舊列（該欄為 NULL）不算符合。
    """
    stmt = select(DecisionLogRow).order_by(DecisionLogRow.created_at, DecisionLogRow.decision_id)
    if kind:
        stmt = stmt.where(DecisionLogRow.kind == kind)
    if sub_kind:
        stmt = stmt.where(DecisionLogRow.sub_kind == sub_kind)
    if since is not None:
        stmt = stmt.where(DecisionLogRow.created_at >= since)
    if until is not None:
        stmt = stmt.where(DecisionLogRow.created_at < until)
    with session_factory() as session:
        return [{c: getattr(row, c) for c in COLUMNS} for row in session.scalars(stmt)]


def load_rows_from_url(url: str | None, **filters: Any) -> list[dict[str, Any]]:
    """CLI 用：以連線字串（None = UEP_DATABASE_URL）讀 decision_log。"""
    engine = make_engine(url)
    try:
        return load_rows(make_session_factory(engine), **filters)
    finally:
        engine.dispose()


def create_default_sink() -> DecisionLogSink | None:
    """依環境建立 DB sink：``UEP_DECISION_LOG`` 為 0／off／false，或沒有 ``UEP_DATABASE_URL`` 時回 None（停用）。"""
    if os.environ.get("UEP_DECISION_LOG", "").strip().lower() in {"0", "off", "false", "no"}:
        return None
    if not get_settings().database_url:
        return None
    return DbSink(make_session_factory(make_engine()))


def create_default_writer(**kwargs: Any) -> DecisionLogWriter:
    """預設寫入器：沒有 DB 或被關閉時回一個停用的 writer（``enabled`` 為 False，掛上也不做事）。"""
    return DecisionLogWriter(create_default_sink(), **kwargs)
