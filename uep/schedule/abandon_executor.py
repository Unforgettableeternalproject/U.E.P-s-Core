"""放棄訊號的消費端執行器（AR-3a；契約 §9.5）。

入口介面就是 ``AbandonWorkSignal`` 物件本身（決策層日後直接呼叫 ``handle(signal)``；產出端不在這裡）。
流程：先沿用 ``consume_abandon`` 的判斷（not_owner → tz → stale_round → not_in_work → stale）：
- ACCEPTED：``SlotMarkWriter.begin_mark(kind=abandon, cause_request_id=signal_id)`` 寫入（冪等），發
  ``schedule.slot_marked`` 與 ``schedule.abandon_work(result=accepted)``，並 poke Planner 讓它在下一個 tick
  發 slot_left／slot_entered。
- IGNORED：只發 ``schedule.abandon_work(result=ignored, reason_code)``。
- 同一個 ``signal_id`` 重送：已有該 cause 的未結束 mark 就直接回 ACCEPTED（``duplicate``），不重複寫、不重複發事件。
事件不放原文與顯示名稱；``is_owner`` 由 Core 計算（``is_owner_fn``，預設 fail-closed：全部視為非 owner）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from uep.contracts.schedule import AbandonResult, AbandonWorkSignal, SlotMark
from uep.schedule.abandon import consume_abandon

logger = logging.getLogger(__name__)


class AbandonExecutor:
    def __init__(self, reader: Any, mark_writer: Any, events: Any, *,
                 is_owner_fn: Callable[[AbandonWorkSignal], bool] | None = None,
                 tz_fn: Callable[[], str] | None = None, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 poke: Callable[[], None] | None = None, metrics_inc: Callable[[str], None] | None = None) -> None:
        self._reader = reader
        self._marks = mark_writer
        self._events = events
        self._is_owner = is_owner_fn or (lambda signal: False)  # fail-closed：沒有 Core 的 owner 判定就全部拒絕
        self._tz_fn = tz_fn or getattr(reader, "tz", None) or (lambda: "Asia/Taipei")
        self._clock = clock
        self._poke = poke
        self._metrics_inc = metrics_inc

    def _count(self, name: str) -> None:
        if self._metrics_inc is not None:
            try:
                self._metrics_inc(name)
            except Exception:
                logger.debug("指標寫入失敗：%s", name, exc_info=True)

    def handle(self, signal: AbandonWorkSignal) -> AbandonResult:
        existing = next((m for m in self._marks.active_marks() if m.cause_request_id == signal.signal_id), None)
        if existing is not None:  # 重送：冪等
            self._count("schedule.abandon.duplicate")
            return AbandonResult("ACCEPTED", "duplicate", existing)
        now = self._clock()
        try:
            is_owner = bool(self._is_owner(signal))
        except Exception:
            logger.exception("owner 判定失敗，視為非 owner")
            is_owner = False
        slot = self._reader.resolve(now)
        result = consume_abandon(signal, is_owner=is_owner, now=now, slot=slot, tz=self._tz_fn())
        if result.status != "ACCEPTED" or result.mark is None:
            self._events.abandon_work(signal, "ignored", result.reason_code, None)
            self._count(f"schedule.abandon.ignored.{result.reason_code}")
            return result
        mark = result.mark
        try:
            mark_id = self._marks.begin_mark(kind="abandon", item_ref=mark.item_ref,
                                             planned_start_at=mark.planned_start_at, cause_request_id=signal.signal_id)
        except ValueError:  # 同一個 signal_id 先前以不同參數建立過 mark：不可信，忽略
            self._events.abandon_work(signal, "ignored", "signal_id_conflict", mark.item_ref)
            self._count("schedule.abandon.ignored.signal_id_conflict")
            return AbandonResult("IGNORED", "signal_id_conflict")
        written = SlotMark(kind="abandon", item_ref=mark.item_ref, planned_start_at=mark.planned_start_at,
                           started_at=now, mark_id=mark_id, cause_request_id=signal.signal_id)
        self._events.slot_marked(mark_id, "abandon", mark.item_ref, signal.signal_id)
        self._events.abandon_work(signal, "accepted", "accepted", mark.item_ref)
        self._count("schedule.abandon.accepted")
        if self._poke is not None:
            try:
                self._poke()  # 讓 Planner 在下一個 tick 發 slot_left／slot_entered
            except Exception:
                logger.debug("poke Planner 失敗", exc_info=True)
        return AbandonResult("ACCEPTED", "accepted", written)
