"""schedule.abandon_work 訊號的消費（純函式，PP-0b）：不接 DB、不發事件、不判斷語意。

訊號只帶話語層事實；消費端依收到當下的投影綁定當前工作項並判斷是否過期（契約 §9.5）。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfoNotFoundError

from uep.contracts.schedule import (
    AbandonResult,
    AbandonWorkSignal,
    ActivityKind,
    PlanSlot,
    PlanSource,
    SlotMark,
)
from uep.schedule.resolver import round_bounds


def consume_abandon(signal: AbandonWorkSignal, *, is_owner: bool, now: datetime, slot: PlanSlot,
                    tz: str) -> AbandonResult:
    """檢查順序：not_owner → stale_round → not_in_work；通過則回要交給 SlotMarkWriter 的 abandon mark。

    - 另有 ``tz_unavailable``（行程時區無法載入，fail-closed）與 ``stale``（decided_at 早於本格投影起點）。
    - ``is_owner`` 由 Core 依 platform_links 計算（訊號不自報）。
    - ``slot`` 是收到當下的投影（``resolve(now)``）；只有「進行中的工作行程項」可被放棄。
      WORK 中的系統緊急只形成 override，不是放棄（override 格 → not_in_work）。
    - 模型輸出只影響是否產出訊號，不影響授權（M-10）。
    """
    if not (signal.actor_authenticated and is_owner):
        return AbandonResult("IGNORED", "not_owner")
    try:
        same_round = round_bounds(signal.decided_at, tz) == round_bounds(now, tz)
    except (ZoneInfoNotFoundError, ValueError):  # 行程時區不可用：fail-closed，不拋例外
        return AbandonResult("IGNORED", "tz_unavailable")
    if not same_round:
        return AbandonResult("IGNORED", "stale_round")
    if (slot.availability != "available" or slot.source != PlanSource.ITEM
            or slot.activity_kind != ActivityKind.WORK or slot.slot_state == "abandoned"
            or slot.item_ref is None or slot.blueprint_start_at is None):
        return AbandonResult("IGNORED", "not_in_work")
    if signal.decided_at < slot.starts_at:  # 訊號早於本工作格的投影起點：舊訊號不得套到之後才開始的工作項
        return AbandonResult("IGNORED", "stale")
    mark = SlotMark(kind="abandon", item_ref=slot.item_ref, planned_start_at=slot.blueprint_start_at,
                    started_at=now, cause_request_id=signal.signal_id)
    return AbandonResult("ACCEPTED", "accepted", mark)
