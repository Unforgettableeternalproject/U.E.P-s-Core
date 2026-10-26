"""Presence -> Task 橋接（P-10、T-6、VC-6-5）：離場時 presence_bound Task 於安全 checkpoint 暫停。

消費 ``presence.revoked``／``presence.recovering``／``presence.recovered``：
- 這三種事件發生時 Presence 都不是 ACTIVE（撤銷、釋放、過期、Core 重啟、恢復完成後為 NONE），
  橋接呼叫 ``TaskManager.pause_presence_bound``；背景 Task 不受影響。
- 處理前先向 Presence 權威讀取目前狀態：已重新 ACTIVE 時（晚到的舊事件）不暫停，避免誤傷新的 Lease。
- ``presence.recovered`` 只是 RECOVERING -> NONE，不代表人回來了，因此**不會**自動恢復任何 Task；
  恢復走 ``resume_run`` 與 T-7 的重新驗證。
- Core 重啟時 ``presence.start()`` 會在 Bus 訂閱之前就發出 ``presence.recovering``，所以啟動後必須呼叫
  ``reconcile()`` 補一次（Bus 事件可能遺失，reconcile 也可定期呼叫）。
- 暫停本身是 Task Manager 的寫入（單一寫入規則）；橋接只決定「何時」。
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from uep.contracts.presence import PresenceStatus
from uep.events.bus import EventBus
from uep.events.envelope import UEPEvent
from uep.tasks.manager import TaskManager
from uep.tasks.states import PauseReason

logger = logging.getLogger("uep.tasks.presence_bridge")

PRESENCE_LEFT_EVENTS = ("presence.revoked", "presence.recovering", "presence.recovered")


class PresenceTaskBridge:
    def __init__(
        self,
        bus: EventBus,
        tasks: TaskManager,
        presence_status: Callable[[], PresenceStatus],
        *,
        persona_id: str = "uep",
    ) -> None:
        self._bus = bus
        self._tasks = tasks
        self._status = presence_status
        self._persona = persona_id
        self._started = False
        self.failures = 0  # 處理失敗次數（供監控；失敗可由 reconcile 補）

    def start(self) -> None:
        if self._started:
            return
        for event_type in PRESENCE_LEFT_EVENTS:
            self._bus.subscribe(event_type, self._on_event)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        for event_type in PRESENCE_LEFT_EVENTS:
            self._bus.unsubscribe(event_type, self._on_event)
        self._started = False

    def _on_event(self, event: UEPEvent) -> None:
        if event.persona_id != self._persona:
            return
        try:
            self.reconcile()
        except Exception:
            self.failures += 1
            logger.exception("presence 離場暫停失敗：%s", event.event_type)

    def reconcile(self) -> list[str]:
        """Presence 非 ACTIVE 時暫停所有 presence_bound 的未終態 Task；回傳本次轉入 PAUSED 的 task_id。"""
        if self._status() is PresenceStatus.ACTIVE:
            return []
        paused = self._tasks.pause_presence_bound(self._persona, PauseReason.PRESENCE_LEFT)
        if paused:
            logger.info("presence 離場，已暫停 %d 個 presence_bound Task", len(paused))
        return paused
