"""做夢的 Activity 記錄（P3接入方案 §3.1「做夢的記錄」、CC-6 §6.1a）。

每次 dream 以 ``kind=maintenance``、``presence_bound=False`` 的 Activity 承載；dream 為「重做不復原」，
不升級成 Task。``ActivityRow`` 沒有獨立建立 API（只在 ``TaskManager.create_task`` 內寫），這裡自開交易直接寫；
Activity 只是索引，結果放在 ``origin_ref``（只含 namespace、觸發來源與結果碼，不含記憶內容）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from uep.ids import new_id
from uep.tasks.models import ActivityRow
from uep.tasks.states import ActivityKind, ActivityStatus

logger = logging.getLogger("uep.memory.activity")


class DreamActivityRecorder:
    def __init__(
        self, session_factory: sessionmaker[Session], *, persona_id: str = "uep",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._persona_id = persona_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def begin(self, namespace_id: str, triggered_by: str) -> str:
        activity_id = new_id("actv")
        with self._session_factory() as session, session.begin():
            session.add(ActivityRow(
                activity_id=activity_id, persona_id=self._persona_id, kind=ActivityKind.MAINTENANCE.value,
                presence_bound=False, status=ActivityStatus.ACTIVE.value, version=1,
                origin_ref={"component": "memory.dream", "namespace_id": namespace_id, "triggered_by": triggered_by},
            ))
        return activity_id

    def end(self, activity_id: str, outcome: str) -> None:
        """結束 Activity；``outcome`` 為結果碼（completed／skipped_lease／failed 等）。"""
        with self._session_factory() as session, session.begin():
            row = session.get(ActivityRow, activity_id)
            if row is None or row.status == ActivityStatus.ENDED.value:
                return
            row.origin_ref = {**(row.origin_ref or {}), "outcome": outcome}
            row.status = ActivityStatus.ENDED.value
            row.ended_at = self._clock()
            row.version += 1

    def record_finished(self, namespace_id: str, triggered_by: str, outcome: str) -> str:
        """已結束的 dream（例如排程觸發、事後才從事件得知）補一筆 Activity。"""
        activity_id = self.begin(namespace_id, triggered_by)
        self.end(activity_id, outcome)
        return activity_id
