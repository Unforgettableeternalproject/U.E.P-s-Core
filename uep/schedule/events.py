"""行程事件發布（PP-4a；契約 §10）：``schedule.*``，durable，source ``core.schedule``。

- 這一版只發布：plan_changed、slot_changed、slot_entered／slot_left、directive_issued／directive_dropped、
  divergence_observed。override_*、slot_marked／unmarked、abandon_work 已在 registry 登記，等仲裁器接上才發布。
- payload 只放 ID／列舉／數字（不含自由文字、備註、請求者）；發布失敗只記 log，不影響呼叫端（事件是稽核／通知，
  不是行程狀態的權威）。
- 行程模組不得 import Presence 寫入方法：這裡只用 ``uep.events`` 的發布介面。
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from uep.contracts.schedule import PlanDirective, PlanSlot
from uep.events.envelope import EventDraft, TrustedContext

logger = logging.getLogger(__name__)

SOURCE = "core.schedule"
ACTOR = "uep:schedule"
SCOPE = "scope_private_owner"  # location 等屬 owner 面（平台端點契約 V-3）

# store 的變更通知 kind → plan_changed.change_kind。override／mark 屬各自的事件（等仲裁器），不在此發布
_PLAN_CHANGE_KINDS = {
    "item_created": "item_created", "item_updated": "item_updated", "item_deleted": "item_deleted",
    "rest_window": "rest_window", "tz_changed": "tz_changed", "reconcile": "reconcile",
}


def plan_change_kind(store_kind: str) -> str | None:
    """None＝這種變更不發 plan_changed（override／mark）。"""
    return _PLAN_CHANGE_KINDS.get(store_kind)


def slot_ref(slot: PlanSlot) -> str:
    """格的穩定參考：行程項／override 的 id 加上藍圖起點（沒有時用格起點）；預設格為 ``default``。"""
    anchor = slot.blueprint_start_at or slot.starts_at
    return f"{slot.item_ref or 'default'}@{anchor.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"


class ScheduleEvents:
    """把行程的變化轉成 ``schedule.*`` 事件。``publisher`` 是 ``EventPublisher``（或同介面的替身）。"""

    def __init__(self, publisher: Any, *, persona_id: str = "uep",
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._publisher = publisher
        self._persona = persona_id
        self._clock = clock

    def _publish(self, event_type: str, payload: dict[str, Any], *, aggregate_version: int | None = None,
                 idempotency_key: str | None = None) -> None:
        try:
            draft = EventDraft(
                event_type=event_type, occurred_at=self._clock(), aggregate_type="schedule",
                aggregate_id=self._persona, aggregate_version=aggregate_version, idempotency_key=idempotency_key,
                payload={k: v for k, v in payload.items() if v is not None})
            trusted = TrustedContext(source=SOURCE, actor_id=ACTOR, scope_ref=SCOPE, persona_id=self._persona)
            self._publisher.publish(draft, trusted)
        except Exception:
            logger.exception("發布 %s 失敗", event_type)

    # ---- store ----

    def plan_changed(self, revision: int, change_kind: str, ref_id: str | None = None) -> None:
        self._publish("schedule.plan_changed", {"revision": revision, "change_kind": change_kind, "ref_id": ref_id},
                      aggregate_version=revision)

    def on_store_change(self, revision: int, store_kind: str, ref_id: str | None) -> None:
        """接到 ``PlanStore`` 的 ``on_change``（提交後）：只有行程本體的變更才發 plan_changed。"""
        kind = plan_change_kind(store_kind)
        if kind is not None:
            self.plan_changed(revision, kind, ref_id)

    # ---- planner ----

    @staticmethod
    def _slot_payload(slot: PlanSlot) -> dict[str, Any]:
        return {"slot_ref": slot_ref(slot), "activity_kind": slot.activity_kind.value, "source": slot.source.value,
                "location": slot.location, "plan_revision": slot.plan_revision}

    def slot_changed(self, slot: PlanSlot) -> None:
        self._publish("schedule.slot_changed", self._slot_payload(slot), aggregate_version=slot.plan_revision)

    def slot_entered(self, slot: PlanSlot, reason: str) -> None:
        self._publish("schedule.slot_entered",
                      {**self._slot_payload(slot), "slot_state": slot.slot_state, "reason": reason})

    def slot_left(self, slot: PlanSlot, reason: str) -> None:
        self._publish("schedule.slot_left",
                      {**self._slot_payload(slot), "slot_state": slot.slot_state, "reason": reason})

    def directive_issued(self, directive: PlanDirective) -> None:
        self._publish("schedule.directive_issued",
                      {"directive_id": directive.directive_id, "reason": directive.reason},
                      idempotency_key="directive:" + hashlib.sha1(directive.directive_id.encode()).hexdigest()[:32])  # id 含 +／@，雜湊後才合法

    def directive_dropped(self, directive_id: str, drop_reason: str, next_eval_at: datetime | None = None) -> None:
        self._publish("schedule.directive_dropped", {
            "directive_id": directive_id, "drop_reason": drop_reason,
            "next_eval_at": None if next_eval_at is None else next_eval_at.astimezone(UTC).isoformat()})

    def divergence_observed(self, plan_location: str, actual_location: str, override_state: str | None,
                            override_id: str | None) -> None:
        self._publish("schedule.divergence_observed", {
            "plan_location": plan_location, "actual_location": actual_location, "override_state": override_state,
            "override_id": override_id})
