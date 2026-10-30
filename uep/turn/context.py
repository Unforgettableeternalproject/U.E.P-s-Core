"""D72 (a)：ES Think 是 ``conversation_turn`` 的 Context Adapter，Core 事後登記快照。

``history_owner = sdk_store``：歷史由 ES 的 Think 單一擁有（X-3），Core 不另寫歷史注入；這裡只在回合結束後
登記一份稽核用快照（不含私人原文，只帶範圍、狀態與 ID）。``history_entries`` 的投影（從 ``delivery.reported``
與 ``turn.*`` 事件）留待 S4。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from uep.context.models import ContextSnapshotRow, StaleRevisionError, save_snapshot
from uep.context.snapshot import (
    ContextSnapshot,
    HistoryOwner,
    HistoryWindow,
    MemoryStatus,
    SnapshotPurpose,
)
from uep.ids import new_id
from uep.turn.models import CoreTicket

logger = logging.getLogger("uep.turn.context")


class ContextRegistrar:
    def __init__(
        self, session_factory: sessionmaker[Session], *, persona_id: str = "uep",
        persona_version: int = 1, policy_version: int = 1,
    ) -> None:
        self._sf = session_factory
        self._persona = persona_id
        self._persona_version = persona_version
        self._policy_version = policy_version

    def register(self, ticket: CoreTicket, *, memory_status: MemoryStatus = MemoryStatus.OK) -> str | None:
        """登記一份 conversation_turn 快照；回傳 snapshot_id。範圍內 revision 衝突時重試一次。"""
        for _attempt in range(2):
            try:
                with self._sf.begin() as s:
                    snapshot = ContextSnapshot(
                        snapshot_id=new_id("snap"), context_revision=self._next_revision(s, ticket),
                        purpose=SnapshotPurpose.CONVERSATION_TURN, persona_id=self._persona,
                        persona_version=self._persona_version, policy_version=self._policy_version,
                        conversation_id=ticket.conversation_id, segment_id=ticket.segment_id,
                        turn_id=ticket.turn_id, audience_scope=ticket.audience_scope, actor_ref=ticket.actor_id,
                        history_window=HistoryWindow(), memory_status=memory_status, excluded_summary={},
                        capabilities={"context_adapter": "echo_stream_think"},
                        data_egress={"providers": []}, token_budget={},
                        history_owner=HistoryOwner.SDK_STORE, sdk_session_ref=ticket.segment_id,
                        built_at=datetime.now(UTC),
                    )
                    save_snapshot(s, snapshot)
                    return snapshot.snapshot_id
            except StaleRevisionError:
                continue
            except Exception:
                logger.exception("登記 ContextSnapshot 失敗")
                return None
        logger.warning("登記 ContextSnapshot：revision 競爭，放棄")
        return None

    @staticmethod
    def _next_revision(s: Session, ticket: CoreTicket) -> int:
        key = f"c:{ticket.conversation_id}:{ticket.segment_id}"
        current = s.scalar(
            select(func.max(ContextSnapshotRow.context_revision)).where(ContextSnapshotRow.scope_key == key)
        )
        return (current or 0) + 1
