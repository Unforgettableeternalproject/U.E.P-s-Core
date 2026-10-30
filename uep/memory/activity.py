"""做夢的 Activity 記錄（P3接入方案 §3.1「做夢的記錄」、CC-6 §6.1a）。

dream 由記憶端內建排程驅動（D75 (c)），Host 只能事後從 ``memory.dream_completed`` 得知，因此只補記「已結束」的
``kind=maintenance``、``presence_bound=False`` Activity；dream 為「重做不復原」，不升級成 Task。

這是繞過 TaskManager 直接寫 ``ActivityRow`` 的**唯一例外**（Activity 沒有獨立建立 API，只在
``TaskManager.create_task`` 內寫）；``origin_ref`` 只含 component、namespace、觸發來源與結果碼，不含記憶內容。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from uep.ids import new_id
from uep.tasks.models import ActivityRow
from uep.tasks.states import ActivityKind, ActivityStatus

COMPONENT = "memory.dream"


class DreamActivityRecorder:
    def __init__(
        self, session_factory: sessionmaker[Session], *, persona_id: str = "uep",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._persona_id = persona_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def record_finished(self, namespace_id: str, triggered_by: str, outcome: str) -> str:
        """單一交易直接寫成 ENDED（不經 ACTIVE，崩潰時不會留下殘列）。"""
        activity_id = new_id("actv")
        now = self._clock()
        with self._session_factory() as session, session.begin():
            session.add(ActivityRow(
                activity_id=activity_id, persona_id=self._persona_id, kind=ActivityKind.MAINTENANCE.value,
                presence_bound=False, status=ActivityStatus.ENDED.value, version=1, ended_at=now,
                origin_ref={
                    "component": COMPONENT, "namespace_id": namespace_id, "triggered_by": triggered_by,
                    "outcome": outcome,
                },
            ))
        return activity_id

    def sweep_stale(self) -> int:
        """啟動時清掃殘留的 ACTIVE maintenance 列（舊版或崩潰遺留）：標成 ENDED、outcome=interrupted。
        只動本 persona 且 origin_ref.component 為 memory.dream 的列。回傳清掃筆數。"""
        with self._session_factory() as session, session.begin():
            rows = session.query(ActivityRow).filter(
                ActivityRow.persona_id == self._persona_id,
                ActivityRow.kind == ActivityKind.MAINTENANCE.value,
                ActivityRow.status == ActivityStatus.ACTIVE.value,
                ActivityRow.origin_ref["component"].as_string() == COMPONENT,
            ).all()
            now = self._clock()
            for row in rows:
                session.execute(
                    update(ActivityRow).where(ActivityRow.activity_id == row.activity_id).values(
                        status=ActivityStatus.ENDED.value, ended_at=now, version=row.version + 1,
                        origin_ref={**(row.origin_ref or {}), "outcome": "interrupted"},
                    )
                )
            return len(rows)
