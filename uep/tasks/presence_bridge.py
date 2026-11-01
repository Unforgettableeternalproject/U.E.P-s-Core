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
import threading
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


DEFAULT_MAINTENANCE_INTERVAL = 5.0  # 秒


class PresenceMaintenance:
    """Presence 維護迴圈（T-23）：定期 ``expire_due()`` 與 ``bridge.reconcile()``。

    - 到期的 Lease 若沒人呼叫 Presence 就不會被發現；Bus 事件也可能遺失（E-13）。這個迴圈讓到期掃描與
      Task 暫停補償不依賴任何外部觸發。
    - 兩個步驟各自隔離例外：``expire_due`` 失敗仍會嘗試 ``reconcile``，失敗只計數並記 log，下個 tick 重試；
      橋接處理失敗（``failures``）因此最晚在下一個 tick 被補上。
    - 第三步 ``project``（可選）：把 Presence 權威狀態重新投影到 State 的 Presence 軸。State 的 Presence 投影
      原本只走 Bus，Bus 佇列滿而丟棄 presence 事件時 State 會停在舊值；投影以 source_version 去重、冪等，
      所以每個 tick 呼叫是安全的。
    - ``tick()`` 可手動呼叫（測試不需要真時間）；背景線程的等待函式可注入（預設 ``Event.wait``）。
    """

    def __init__(
        self,
        expire_due: Callable[[], bool],
        reconcile: Callable[[], list[str]],
        *,
        project: Callable[[], object] | None = None,
        interval: float = DEFAULT_MAINTENANCE_INTERVAL,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval 必須 > 0")
        self._expire_due = expire_due
        self._reconcile = reconcile
        self._project = project
        self._interval = interval
        self._stop = threading.Event()
        self._wait = wait if wait is not None else self._stop.wait  # 回 True 表示被要求停止
        self._thread: threading.Thread | None = None
        self.ticks = 0
        self.expire_failures = 0
        self.reconcile_failures = 0
        self.project_failures = 0

    def tick(self) -> None:
        """執行一輪：先掃到期，再 reconcile，最後重新投影 Presence 軸；任一步驟例外都不外洩。"""
        try:
            self._expire_due()
        except Exception:
            self.expire_failures += 1
            logger.exception("presence 到期掃描失敗，下個 tick 重試")
        try:
            self._reconcile()
        except Exception:
            self.reconcile_failures += 1
            logger.exception("presence reconcile 失敗，下個 tick 重試")
        if self._project is not None:
            try:
                self._project()
            except Exception:
                self.project_failures += 1
                logger.exception("Presence 軸重新投影失敗，下個 tick 重試")
        self.ticks += 1

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="UepPresenceMaintenance")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.error("presence 維護線程未能正常結束")
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._wait(self._interval) or self._stop.is_set():
                return
            self.tick()
