"""放棄訊號的消費端執行器（AR-3a；契約 §9.5）。

入口介面就是 ``AbandonWorkSignal`` 物件本身（決策層直接呼叫 ``handle(signal, is_owner=...)``）。
流程：先沿用 ``consume_abandon`` 的判斷（not_owner → tz → stale_round → not_in_work → stale）：
- ACCEPTED：``SlotMarkWriter.begin_mark(kind=abandon, cause_request_id=signal_id)`` 寫入，發
  ``schedule.slot_marked`` 與 ``schedule.abandon_work(result=accepted)``，並 poke Planner 讓它在下一個 tick
  發 slot_left／slot_entered。
- IGNORED：只發 ``schedule.abandon_work(result=ignored, reason_code)``。
- 寫入失敗（非 ValueError 的例外）：發 ``abandon_work(result=error, reason_code=mark_write_failed)``，不拋出。

owner 判定：``is_owner`` 由呼叫端（決策層，已知 Core 的 platform_links）傳入時直接採用；沒傳（None）時改用
``is_owner_fn``；兩者都沒有就回 ``IGNORED(owner_unverifiable)``——這是「無法判定」，不能和「確定不是 owner」
（``not_owner``）混用。

冪等不靠預查（check-then-act 有競態）：重送時 ``consume_abandon`` 會因為該格已是 abandoned 而判 not_in_work，這時改把
同樣的參數交給 ``begin_mark``（store 在鎖內冪等）：回傳的 mark 若屬於同一個 ``signal_id`` 就是重送（``duplicate``，
不重複發事件）；屬於別的請求則維持 ``not_in_work``。參數衝突（同 signal_id、不同出現）由 ``begin_mark`` 的 ValueError
擋下，回 ``signal_id_conflict``。事件不放原文與顯示名稱。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from uep.contracts.schedule import AbandonResult, AbandonWorkSignal, SlotMark
from uep.schedule.abandon import consume_abandon

logger = logging.getLogger(__name__)

R_OWNER_UNVERIFIABLE = "owner_unverifiable"
R_MARK_WRITE_FAILED = "mark_write_failed"
R_SIGNAL_ID_CONFLICT = "signal_id_conflict"


class AbandonExecutor:
    def __init__(self, reader: Any, mark_writer: Any, events: Any, *,
                 is_owner_fn: Callable[[AbandonWorkSignal], bool] | None = None,
                 tz_fn: Callable[[], str] | None = None, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 poke: Callable[[], None] | None = None, metrics_inc: Callable[[str], None] | None = None) -> None:
        self._reader = reader
        self._marks = mark_writer
        self._events = events
        self._is_owner_fn = is_owner_fn
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

    def _ignored(self, signal: AbandonWorkSignal, reason: str, item_ref: str | None = None) -> AbandonResult:
        self._events.abandon_work(signal, "ignored", reason, item_ref)
        self._count(f"schedule.abandon.ignored.{reason}")
        return AbandonResult("IGNORED", reason)

    def _resolve_owner(self, signal: AbandonWorkSignal, is_owner: bool | None) -> bool | None:
        """True／False＝已判定；None＝無法判定（沒有任何 owner 來源）。"""
        if is_owner is not None:
            return bool(is_owner)
        if self._is_owner_fn is None:
            return None
        try:
            return bool(self._is_owner_fn(signal))
        except Exception:
            logger.exception("owner 判定失敗，視為非 owner")
            return False

    def handle(self, signal: AbandonWorkSignal, *, is_owner: bool | None = None) -> AbandonResult:
        owner = self._resolve_owner(signal, is_owner)
        if owner is None:
            return self._ignored(signal, R_OWNER_UNVERIFIABLE)
        now = self._clock()
        slot = self._reader.resolve(now)
        result = consume_abandon(signal, is_owner=owner, now=now, slot=slot, tz=self._tz_fn())
        if result.status != "ACCEPTED" or result.mark is None:
            if (result.reason_code == "not_in_work" and slot.slot_state == "abandoned" and slot.item_ref is not None
                    and slot.blueprint_start_at is not None):
                duplicate = self._detect_duplicate(signal, slot.item_ref, slot.blueprint_start_at)
                if duplicate is not None:
                    return duplicate
            return self._ignored(signal, result.reason_code)
        mark = result.mark
        try:
            mark_id = self._marks.begin_mark(kind="abandon", item_ref=mark.item_ref,
                                             planned_start_at=mark.planned_start_at, cause_request_id=signal.signal_id)
        except ValueError:  # 同一個 signal_id 先前以不同參數建立過 mark：不可信，忽略
            return self._ignored(signal, R_SIGNAL_ID_CONFLICT, mark.item_ref)
        except Exception:
            logger.exception("放棄 mark 寫入失敗：%s", signal.signal_id)
            self._events.abandon_work(signal, "error", R_MARK_WRITE_FAILED, mark.item_ref)
            self._count("schedule.abandon.error")
            return AbandonResult("IGNORED", R_MARK_WRITE_FAILED)
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

    def _detect_duplicate(self, signal: AbandonWorkSignal, item_ref: str, planned_start_at: datetime
                          ) -> AbandonResult | None:
        """該格已是 abandoned：把同樣的參數交給 begin_mark（鎖內冪等，不會新增），看回傳的 mark 是不是這個 signal 建的。"""
        try:
            mark_id = self._marks.begin_mark(kind="abandon", item_ref=item_ref, planned_start_at=planned_start_at,
                                             cause_request_id=signal.signal_id)
            existing = next((m for m in self._marks.active_marks() if m.mark_id == mark_id), None)
        except Exception:
            logger.debug("重送偵測失敗，維持 not_in_work", exc_info=True)
            return None  # 查不到就維持原判斷（not_in_work）
        if existing is None or existing.cause_request_id != signal.signal_id:
            return None  # 這格是別的請求放棄的
        self._count("schedule.abandon.duplicate")
        return AbandonResult("ACCEPTED", "duplicate", existing)
