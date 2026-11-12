"""在場請求仲裁的 shadow 觀察（AR-2）：只記錄判決（log＋指標），**不呼叫任何 Presence 寫入、不寫 override／mark**。

兩個觀察點（都不改決策層的檔）：
- ``schedule.directive_issued``（bus 事件）：事件只帶 directive_id，所以收到後以「當下」的行程格與 Presence 重算同一個
  directive（``reconcile_directive``）；id 對得上才仲裁，對不上記 ``stale``（行程在事件投遞期間已變）。
- ``turn.skipped``（缺席端輸入的 shadow 紀錄）：決策層目前只把 ``presence_request`` 當布林寫進去，**沒有 target**，
  所以只能計數與記 log（``no_target``）。payload 日後若帶 ``target_location``，且 actor 已認證、是 owner，就會建出
  ``ArbiterRequest`` 做仲裁判決（前向相容）。
"""
from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from uep.contracts.schedule import (
    PLAN_LOCATIONS,
    ArbiterRequest,
    ArbitrationDecision,
    InterruptFacts,
)
from uep.schedule import arbiter
from uep.schedule.planner import PresenceReading
from uep.schedule.resolver import reconcile_directive

logger = logging.getLogger(__name__)

_METRIC = "schedule.arbiter.shadow"


@dataclass(frozen=True)
class ShadowJudgment:
    source: str  # directive｜request
    ref: str
    decision: ArbitrationDecision


class PresenceArbiterShadow:
    def __init__(self, reader: Any, presence_fn: Callable[[], PresenceReading], override_fn: Callable[[], Any] | None,
                 probe_fn: Callable[[], InterruptFacts], *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 metrics_inc: Callable[[str], None] | None = None, max_records: int = 100) -> None:
        self._reader = reader
        self._presence_fn = presence_fn
        self._override_fn = override_fn
        self._probe_fn = probe_fn
        self._clock = clock
        self._metrics_inc = metrics_inc
        self._seq = 0
        self.judgments: deque[ShadowJudgment] = deque(maxlen=max_records)
        self._cleanups: list[Callable[[], None]] = []

    def _count(self, name: str) -> None:
        if self._metrics_inc is not None:
            try:
                self._metrics_inc(name)
            except Exception:
                logger.debug("指標寫入失敗：%s", name, exc_info=True)

    def _record(self, source: str, ref: str, decision: ArbitrationDecision) -> None:
        self.judgments.append(ShadowJudgment(source, ref, decision))
        self._count(f"{_METRIC}.{source}.{decision.outcome.lower()}")
        self._count(f"{_METRIC}.{source}.reason.{decision.reason_code}")
        logger.info("仲裁 shadow 判決：%s %s → %s/%s（override=%s target=%s）", source, ref, decision.outcome,
                    decision.reason_code, decision.override_action, decision.transfer_target)

    # ---- 觀察點 1：schedule.directive_issued ----

    def on_directive_event(self, event: Any) -> None:
        directive_id = (getattr(event, "payload", None) or {}).get("directive_id")
        try:
            now = self._clock()
            slot = self._reader.resolve(now)
            reading = self._presence_fn()
            actual = arbiter.actual_location(reading)
            if actual is None:  # Presence 不明：無法重算 directive
                self._count(f"{_METRIC}.directive.stale")
                return
            directive = reconcile_directive(slot, actual, now)
            if directive is None or directive.directive_id != directive_id:
                self._count(f"{_METRIC}.directive.stale")
                return
            override = self._override_fn() if self._override_fn is not None else None
            decision = arbiter.arbitrate_directive(
                directive, slot=slot, presence=reading, override=override, probe=self._probe_fn(), now=now)
            self._record("directive", directive.directive_id, decision)
        except Exception:
            logger.exception("仲裁 shadow 處理 directive 失敗")
            self._count(f"{_METRIC}.error")

    # ---- 觀察點 2：turn.skipped（缺席端輸入的 presence_request 判斷）----

    def on_turn_skipped(self, event: Any) -> None:
        payload = getattr(event, "payload", None) or {}
        if payload.get("presence_request") is not True:
            return
        self._count(f"{_METRIC}.presence_request_observed")
        target = payload.get("target_location")
        authenticated = payload.get("actor_authenticated") is True
        is_owner = payload.get("is_owner") == "true"
        ref = getattr(event, "event_id", "unknown")
        if target not in PLAN_LOCATIONS or not authenticated:
            self._count(f"{_METRIC}.request.no_target" if target not in PLAN_LOCATIONS
                        else f"{_METRIC}.request.unauthenticated")
            logger.info("仲裁 shadow：觀察到在場請求但無法判決（target=%s authenticated=%s）", target, authenticated)
            return
        if not is_owner:
            # member 需要 platform_links 的「已登記」事實，目前拿不到：非 owner 一律不建請求，只計數
            self._count(f"{_METRIC}.request.member_unverified")
            return
        try:
            # owner 只在 payload 明確 owner 且已認證時才建。AR-3 前要改成由 Core 依 platform_links 計算，不信 payload 字串
            self._seq += 1
            request = ArbiterRequest(
                request_id=f"observed:{ref}", authority="owner",
                requester_ref=str(payload.get("actor_id") or "unknown"), actor_authenticated=True, target=target,
                received_seq=self._seq)
            now = self._clock()
            slot = self._reader.resolve(now)
            override = self._override_fn() if self._override_fn is not None else None
            decision = arbiter.arbitrate_request(
                request, slot=slot, presence=self._presence_fn(), override=override, probe=self._probe_fn(), now=now)
            self._record("request", request.request_id, decision)
        except Exception:
            logger.exception("仲裁 shadow 處理在場請求失敗")
            self._count(f"{_METRIC}.error")

    # ---- 生命週期 ----

    def add_cleanup(self, fn: Callable[[], None]) -> None:
        self._cleanups.append(fn)

    def stop(self) -> None:
        cleanups, self._cleanups = self._cleanups, []
        for fn in cleanups:
            try:
                fn()
            except Exception:
                logger.warning("仲裁 shadow 清理失敗", exc_info=True)
