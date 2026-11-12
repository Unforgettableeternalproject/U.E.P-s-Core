"""PresencePlanner（PP-4b；契約 §6.1／§6.2）：在行程邊界喚醒，發出 ``schedule.*`` 事件。

- 每次喚醒：``resolve(now)`` → 讀 Presence（只讀，經注入的 ``presence_fn``；PP-3：不得 import 或呼叫 Presence 寫入方法）。
- 格切換 → ``slot_changed`` 加 ``slot_left``／``slot_entered``（沿用 ``slot_transition``，delay 延長同一項不算切換）。
- 位置不一致、且沒有 PENDING／ACTIVE override、不在移轉中 → 只發 ``directive_issued``（同一 directive_id 只發一次）。
  目前沒有仲裁器：不消費 directive、不做延後狀態機（``defer_decision``／``make_delay`` 留給仲裁器階段），
  只在 directive 過期、已達成、被取代或被 override 蓋過時發 ``directive_dropped``（留好接口）。
- override 存在或移轉中且行程目標與實際位置不同 → ``divergence_observed``（只記錄，狀態改變才發）。
- ``startup_reconcile`` 只在第一次評估做一次（以 baseline 建立，不補發過去的邊界、不對啟動前的格發 entered／left）。
- 行程不可用（unavailable）時不發任何事件與 directive（PP-1／§6.1），保留原 baseline，恢復後再比較。
- 喚醒時間：``min(next_boundary_at - now, poll_s)``（預設 30 秒安全輪詢，避免系統睡眠漏邊界）；clock 可注入；
  ``poke()`` 可由 ``schedule.plan_changed`` 觸發立即重新評估。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from uep.contracts.schedule import (
    PRESENCE_TARGET_HOST,
    PlanDirective,
    PlanReader,
    PlanSlot,
    PlanSource,
)
from uep.schedule.resolver import DirectivePolicy, reconcile_directive, slot_transition

logger = logging.getLogger(__name__)

DEFAULT_POLL_S = 30.0
MIN_WAIT_S = 0.2
_MAX_OPEN_DIRECTIVES = 16
_ERROR_LOG_INTERVAL_S = 60.0

# resolver 的 slot_transition 理由 → 事件 reason（契約 §10：boundary｜override｜override_end｜abandon｜delay_end；
# pause／resume 為本版補充）
_NOTICE_REASONS = {
    "boundary": "boundary", "override_started": "override", "override_ended": "override_end",
    "abandoned": "abandon", "paused": "pause", "resumed": "resume",
}


@dataclass(frozen=True)
class PresenceReading:
    """Planner 讀到的 Presence（唯讀快照）。NONE＝在主機；RECOVERING 視為不明（不發 directive）。"""

    status: str  # NONE｜ACTIVE｜RECOVERING
    location: str | None
    transfer_active: bool = False


class PresencePlanner:
    def __init__(self, reader: PlanReader, presence_fn: Callable[[], PresenceReading], events: Any, *,
                 override_fn: Callable[[], Any] | None = None, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 poll_s: float = DEFAULT_POLL_S, policy: DirectivePolicy | None = None,
                 metrics_inc: Callable[[str], None] | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._reader = reader
        self._presence_fn = presence_fn
        self._events = events
        self._override_fn = override_fn
        self._clock = clock
        self._poll_s = poll_s
        self._policy = policy or DirectivePolicy()
        self._metrics_inc = metrics_inc
        self._monotonic = monotonic
        self._prev: PlanSlot | None = None
        self._open: dict[str, PlanDirective] = {}
        self._last_divergence: tuple | None = None
        self._unavailable = False
        self._error_log: dict[str, float] = {}
        self.next_wake_s: float = poll_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._cleanups: list[Callable[[], None]] = []

    # ---- 評估 ----

    def _count(self, name: str) -> None:
        if self._metrics_inc is not None:
            try:
                self._metrics_inc(name)
            except Exception:
                logger.debug("指標寫入失敗：%s", name, exc_info=True)

    @staticmethod
    def _actual_location(reading: PresenceReading) -> str | None:
        if reading.status == "ACTIVE":
            return reading.location
        if reading.status == "NONE":
            return PRESENCE_TARGET_HOST
        return None  # RECOVERING：不明

    def _wait_for(self, slot: PlanSlot, now: datetime) -> float:
        wait = self._poll_s
        if slot.next_boundary_at is not None:
            wait = min(wait, (slot.next_boundary_at - now).total_seconds())
        return max(MIN_WAIT_S, wait)

    def _drop(self, directive_id: str, drop_reason: str) -> None:
        self._open.pop(directive_id, None)
        self._events.directive_dropped(directive_id, drop_reason)
        self._count("schedule.planner.directive_dropped")

    def _reap(self, now: datetime, actual: str | None, override_active: bool) -> None:
        """directive 過期、已達成、被 override 蓋過時發 directive_dropped（留好接口；不做延後狀態機）。"""
        for directive_id, directive in list(self._open.items()):
            if now >= directive.expires_at:
                self._drop(directive_id, "slot_ended")
            elif override_active:
                self._drop(directive_id, "overridden")
            elif actual is not None and actual == directive.target:
                self._drop(directive_id, "satisfied")

    def tick(self, now: datetime | None = None) -> PlanSlot:
        now = now or self._clock()
        slot = self._reader.resolve(now)
        if slot.availability != "available":
            if not self._unavailable:
                self._unavailable = True
                logger.warning("行程不可用（%s），Planner 暫停發事件與 directive", slot.reason_code)
            self.next_wake_s = self._poll_s
            return slot
        if self._unavailable:
            self._unavailable = False
            logger.info("行程恢復可用，Planner 繼續評估")
        first = self._prev is None
        reason = "startup_reconcile" if first else "plan_boundary"
        if not first:
            notices = slot_transition(self._prev, slot)
            if notices:
                self._events.slot_changed(slot)
                self._count("schedule.planner.slot_changed")
                for notice in notices:
                    emit = self._events.slot_left if notice.kind == "left" else self._events.slot_entered
                    emit(notice.slot, _NOTICE_REASONS.get(notice.reason, "boundary"))
            elif slot.plan_revision != self._prev.plan_revision:
                reason = "plan_changed"
        self._prev = slot
        self._reconcile_presence(slot, now, reason)
        self.next_wake_s = self._wait_for(slot, now)
        return slot

    def _reconcile_presence(self, slot: PlanSlot, now: datetime, reason: str) -> None:
        reading = self._presence_fn()
        override = self._override_fn() if self._override_fn is not None else None
        override_active = override is not None or slot.source == PlanSource.OVERRIDE
        actual = self._actual_location(reading)
        self._reap(now, actual, override_active)
        if actual is None:  # RECOVERING：不明，不發 directive 也不記 divergence
            return
        if override_active or reading.transfer_active:
            # 移轉中（或 override 生效中）行程目標與實際位置的差異：只記錄，狀態改變才發
            if slot.location != actual:
                key = (slot.location, reading.status, actual, getattr(override, "status", None),
                       getattr(override, "override_id", None))
                if key != self._last_divergence:
                    self._last_divergence = key
                    self._events.divergence_observed(
                        slot.location, "none" if reading.status == "NONE" else actual,
                        getattr(override, "status", None), getattr(override, "override_id", None))
                    self._count("schedule.planner.divergence_observed")
            else:
                self._last_divergence = None
            return
        self._last_divergence = None
        directive = reconcile_directive(slot, actual, now, reason=reason, policy=self._policy)
        if directive is None or directive.directive_id in self._open:
            return
        for stale_id in list(self._open):  # 新 directive 取代舊的
            self._drop(stale_id, "superseded")
        if len(self._open) >= _MAX_OPEN_DIRECTIVES:
            self._open.clear()
        self._open[directive.directive_id] = directive
        self._events.directive_issued(directive)
        self._count("schedule.planner.directive_issued")

    def tick_safe(self) -> PlanSlot | None:
        """失敗時不發任何事件；同類錯誤 60 秒才記一次（首次帶 traceback）。"""
        try:
            return self.tick()
        except Exception as exc:
            self._count("schedule.planner.tick_error")
            name = type(exc).__name__
            now = self._monotonic()
            last = self._error_log.get(name)
            if last is None:
                logger.exception("PresencePlanner tick 失敗")
                self._error_log[name] = now
            elif now - last >= _ERROR_LOG_INTERVAL_S:
                logger.warning("PresencePlanner tick 持續失敗（%s：%s）", name, exc)
                self._error_log[name] = now
            self.next_wake_s = self._poll_s
            return None

    # ---- 背景迴圈 ----

    def add_cleanup(self, fn: Callable[[], None]) -> None:
        self._cleanups.append(fn)

    def poke(self) -> None:
        """schedule.plan_changed 等事件觸發：立即重新評估。"""
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="presence-planner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        cleanups, self._cleanups = self._cleanups, []
        for fn in cleanups:
            try:
                fn()
            except Exception:
                logger.warning("PresencePlanner 清理失敗", exc_info=True)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick_safe()
            self._wake.wait(self.next_wake_s)
            self._wake.clear()
