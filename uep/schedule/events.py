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
ABANDON_RESULTS = frozenset({"accepted", "ignored", "error"})
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

    def _publish(self, event_type: str, payload: dict[str, Any], *, aggregate_id: str | None = None,
                 aggregate_version: int | None = None, idempotency_key: str | None = None) -> None:
        """events 的 ``(aggregate_id, aggregate_version)`` 唯一鍵是共用命名空間（Presence、State 都用 persona_id）：
        除了 plan_changed 以外的事件一律不設 aggregate，避免佔用別人的版本序號。"""
        try:
            draft = EventDraft(
                event_type=event_type, occurred_at=self._clock(),
                aggregate_type="schedule" if aggregate_id else None, aggregate_id=aggregate_id,
                aggregate_version=aggregate_version if aggregate_id else None, idempotency_key=idempotency_key,
                payload={k: v for k, v in payload.items() if v is not None})
            trusted = TrustedContext(source=SOURCE, actor_id=ACTOR, scope_ref=SCOPE, persona_id=self._persona)
            self._publisher.publish(draft, trusted)
        except Exception:
            logger.exception("發布 %s 失敗", event_type)

    # ---- store ----

    def plan_changed(self, revision: int, change_kind: str, ref_id: str | None = None) -> None:
        # 獨立的 aggregate 命名空間 ``<persona>:schedule``；同一 revision 只會有一筆（idempotency key 去重，
        # Planner 觀察到 revision 變動時會補發同一個 key 的事件）
        self._publish("schedule.plan_changed", {"revision": revision, "change_kind": change_kind, "ref_id": ref_id},
                      aggregate_id=f"{self._persona}:schedule", aggregate_version=revision,
                      idempotency_key=f"plan_changed:{revision}")

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
        self._publish("schedule.slot_changed", self._slot_payload(slot))

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

    # ---- 放棄與仲裁（AR-3a）----

    def abandon_work(self, signal: Any, result: str, reason_code: str, item_ref: str | None) -> None:
        """放棄訊號的受理／忽略／錯誤紀錄（result 閉集：accepted｜ignored｜error）；不含原文與 actor 顯示名稱。"""
        if result not in ABANDON_RESULTS:
            raise ValueError(f"未知的 abandon_work result：{result!r}")
        self._publish("schedule.abandon_work", {
            "signal_id": signal.signal_id, "item_ref": item_ref, "result": result, "reason_code": reason_code,
            "path": signal.path, "decider_id": signal.decider_id, "decider_version": signal.decider_version},
            idempotency_key=f"abandon_work:{signal.signal_id}:{result}")

    def slot_marked(self, mark_id: str, kind: str, item_ref: str, cause_request_id: str) -> None:
        self._publish("schedule.slot_marked", {
            "mark_id": mark_id, "kind": kind, "item_ref": item_ref, "cause_request_id": cause_request_id},
            idempotency_key=f"slot_marked:{mark_id}")

    def arbitration_decided(self, *, source: str, ref: str, decision: Any, executed: bool, exec_reason: str,
                            mark_written: bool = False) -> None:
        """仲裁判決落地。``executed=True`` 只用在整個判決都完成時（mark 已寫、且不需要移轉）；只寫了 mark、移轉沒做的
        部分完成是 ``executed=False`` 加 ``mark_written=True``（exec_reason 說明原因）。"""
        payload = {
            "source": source, "outcome": decision.outcome, "reason_code": decision.reason_code,
            "band": decision.priority, "override_action": decision.override_action,
            "transfer_target": decision.transfer_target, "executed": executed, "exec_reason": exec_reason,
            "mark_written": mark_written}
        payload["directive_id" if source == "directive" else "request_id"] = ref
        # 同一個 ref 的不同判決（outcome／reason_code 不同）是不同的事實，不能被 idempotency 吃掉
        digest = hashlib.sha1(f"{source}:{ref}:{decision.outcome}:{decision.reason_code}".encode()).hexdigest()[:32]
        self._publish("schedule.arbitration_decided", payload, idempotency_key=f"arbitration:{digest}")

    def divergence_observed(self, plan_location: str, actual_location: str, override_state: str | None,
                            override_id: str | None) -> None:
        """``plan_location`` 是投影後的行程位置：override 生效時就是 override 的目標位置（不是藍圖位置）。

        ``actual_location`` 的寫法：Presence ACTIVE 為其平台位置；Presence NONE（她在主機）寫 ``"none"``（契約 §10
        的「含 none」）。注意 directive 的 target 用 ``"host"`` 表示主機，兩者是不同的詞彙，不要互相比對。
        """
        self._publish("schedule.divergence_observed", {
            "plan_location": plan_location, "actual_location": actual_location, "override_state": override_state,
            "override_id": override_id})
