"""DL-6b：``proactive.initiate`` 接上 assembly（shadow-only）。

本檔是 ``uep.decision.proactive`` 唯一的 production 呼叫端（AST 鎖白名單）。它只做三件事：

1. ``attach_proactive_shadow``：向決策中樞登記 ``proactive.initiate`` 並建立 ``ProactiveShadow``（須在
   ``DecisionLogWriter.attach`` 之前，日誌預設名單才會串到此 kind；決策日誌停用時不呼叫，零成本）。
2. ``ProactiveShadow.build_context``：把 ``PresenceView`` ／ State ／ Stop ／ 聽眾 provider 組成 ``ProactiveContext``。
3. ``ProactiveShadow.evaluate``：``build_request -> hub.submit -> loop_trigger_decision``，只把 ``would_start`` 與
   ``LT_*`` 原因寫進 logger（決策本身已由決策日誌觀察者落盤）。

shadow-only 保證：不建立 Run／Task、不產生 output intent、不投遞提醒、不呼叫 ``presence.join_or_move`` 與
``report.route``；``LoopTrigger`` 產生後即丟棄。Phase 1 沒有候選 producer、也不加計時器，``evaluate`` 只是可被呼叫的入口。

缺資料欄位一律取保守預設（寧可不提醒）：
- ``grant``（授權服務尚不存在）：None -> 非 assignment 候選得 ``no_authorization``。
- ``audience_scope``：provider 缺席、ALLOW_ALL 或回 None 時視為最寬的 ``scope_public``。
- ``private_locations``：空（沒有可移轉的私人位置）。
- ``muted``／``activity_interruptible``／``in_committed_interaction``（State 軸欄位尚待確認）：
  Behavior=SLEEP 視為 muted；Interaction 非 IDLE 視為不可中斷；SPEAKING／WAITING_USER 視為承諾中的互動；
  ``activity_cost`` 固定 0.0。讀取失敗時 muted=True、不可中斷。
- ``ledger``／``budget_used``：由呼叫端傳入，預設空與 0。
- Presence／Stop 讀取失敗：Presence 視為 RECOVERING、global_stop=True（fail-closed）。
- ``capability_lookup``：工具層 S-H 標籤表接上前回 None（-> ``capability_empty``）；``snapshot_ref`` 為 placeholder，
  待 context 側 ``SnapshotPurpose.PROACTIVE_EVAL`` 快照接上後替換。
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from uep.contracts.presence import PresenceLocation, PresenceStatus
from uep.decision.contracts import DecisionResult
from uep.decision.hub import DecisionHub
from uep.decision.proactive import (
    KIND_PROACTIVE_INITIATE,
    LT_WOULD_START,
    LedgerEntry,
    ProactiveCandidate,
    ProactiveContext,
    ProactiveEngine,
    ProactiveInjections,
    ProactivePolicy,
    build_request,
    loop_trigger_decision,
    register_proactive,
)
from uep.state.coordinator import Axis, BehaviorState, InteractionState

logger = logging.getLogger("uep.echo.proactive_wiring")

PLACEHOLDER_SNAPSHOT_REF = "snapshot_placeholder_dl6b"  # 暫定：PROACTIVE_EVAL 快照尚未接上
WIDEST_AUDIENCE_SCOPE = "scope_public"
SHADOW_NAMESPACE = "proactive_shadow"
_SEQ_CAP = 1024


def no_capability_lookup(goal_code: str) -> Collection[str] | None:
    """工具層 S-H 標籤表接上前的預設：一律查不到（-> ``capability_empty``）。"""
    return None


@dataclass(frozen=True)
class ProactiveEvaluation:
    """一次 shadow 評估的記錄；``would_start`` 只是「若開放會啟動」，沒有任何 Run 被建立。"""

    result: DecisionResult
    lt_reason: str  # ``LT_*``
    would_start: bool


class ProactiveShadow:
    """shadow-only 評估入口（無計時器、無 producer）。"""

    def __init__(
        self, hub: DecisionHub, engine: ProactiveEngine, *, presence: Any, state: Any, stop: Any,
        audience: Callable[[], str | None] | None = None, injections: ProactiveInjections | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._hub = hub
        self.engine = engine
        self._presence = presence
        self._state = state
        self._stop = stop
        self._audience = audience
        self._inj = injections or ProactiveInjections(
            capability_lookup=no_capability_lookup, snapshot_ref=PLACEHOLDER_SNAPSHOT_REF,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._seq: OrderedDict[str, int] = OrderedDict()

    def build_context(
        self, candidate: ProactiveCandidate, *, ledger: tuple[LedgerEntry, ...] = (), budget_used: int = 0,
        policy: ProactivePolicy | None = None,
    ) -> ProactiveContext:
        status, location, generation, transfer_pending = self._presence_facts()
        muted, interruptible, committed = self._activity_facts()
        return ProactiveContext(
            now=self._clock(), owner_actor_id=candidate.owner_actor_id, audience_scope=self._audience_scope(),
            presence_status=status, current_platform=location, presence_generation=generation,
            transfer_pending=transfer_pending, private_locations=(), grant=None, global_stop=self._global_stop(),
            muted=muted, in_committed_interaction=committed, activity_interruptible=interruptible, activity_cost=0.0,
            ledger=ledger, budget_used=budget_used, policy=policy or ProactivePolicy(),
        )

    def evaluate(
        self, candidate: ProactiveCandidate, *, ledger: tuple[LedgerEntry, ...] = (), budget_used: int = 0,
        policy: ProactivePolicy | None = None,
    ) -> ProactiveEvaluation:
        ctx = self.build_context(candidate, ledger=ledger, budget_used=budget_used, policy=policy)
        request = build_request(candidate, ctx, namespace=SHADOW_NAMESPACE, seq=self._next_seq(candidate.candidate_id))
        result = self._hub.submit(KIND_PROACTIVE_INITIATE, request)
        _trigger, reason = loop_trigger_decision(candidate, result, self._inj)  # 觸發物件即丟，絕不啟動
        would_start = reason == LT_WOULD_START
        logger.info(
            "proactive shadow candidate=%s outcome=%s reason=%s lt=%s would_start=%s",
            candidate.candidate_id, result.outcome, result.reason_code, reason, would_start,
        )
        return ProactiveEvaluation(result=result, lt_reason=reason, would_start=would_start)

    # ---- 事實蒐集（失敗一律保守）----

    def _next_seq(self, candidate_id: str) -> int:
        with self._lock:
            seq = self._seq.pop(candidate_id, 0) + 1
            self._seq[candidate_id] = seq
            while len(self._seq) > _SEQ_CAP:
                self._seq.popitem(last=False)
            return seq

    def _presence_facts(self) -> tuple[PresenceStatus, PresenceLocation | None, int, bool]:
        try:
            # 唯讀快照，不觸發過期轉換；只供 shadow 評估，Phase 2 實際投遞前須改走 Lease／current()
            view = self._presence.peek()
            # peek 不處理過期：ACTIVE 但 Lease 已過期，比照 platform_context 視同 NONE（不在任何平台）
            expires_at = getattr(view, "expires_at", None)
            if (
                PresenceStatus(view.status) is PresenceStatus.ACTIVE
                and expires_at is not None
                and expires_at <= self._clock()
            ):
                return PresenceStatus.NONE, None, int(view.generation), view.transfer_id is not None
            location = None
            if view.location:
                try:
                    location = PresenceLocation(view.location)
                except ValueError:
                    location = None
            return PresenceStatus(view.status), location, int(view.generation), view.transfer_id is not None
        except Exception as exc:  # noqa: BLE001 - 讀不到 Presence 視為恢復中，不提醒
            logger.warning("proactive shadow 讀取 Presence 失敗：%s", type(exc).__name__)
            return PresenceStatus.RECOVERING, None, 0, False

    def _global_stop(self) -> bool:
        try:
            return bool(self._stop.is_engaged())
        except Exception as exc:  # noqa: BLE001 - 讀不到停止旗標視為已停止（fail-closed）
            logger.warning("proactive shadow 讀取全域停止旗標失敗：%s", type(exc).__name__)
            return True

    def _activity_facts(self) -> tuple[bool, bool, bool]:
        """回傳 ``(muted, activity_interruptible, in_committed_interaction)``；欄位對應為暫定，見模組 docstring。"""
        try:
            interaction = self._state.current(Axis.INTERACTION).state
            behavior = self._state.current(Axis.BEHAVIOR).state
        except Exception as exc:  # noqa: BLE001
            logger.warning("proactive shadow 讀取 State 失敗：%s", type(exc).__name__)
            return True, False, False
        committed = interaction in (InteractionState.SPEAKING.value, InteractionState.WAITING_USER.value)
        return behavior == BehaviorState.SLEEP.value, interaction == InteractionState.IDLE.value, committed

    def _audience_scope(self) -> str:
        provider = self._audience
        if provider is None:
            return WIDEST_AUDIENCE_SCOPE
        try:
            return provider() or WIDEST_AUDIENCE_SCOPE
        except Exception as exc:  # noqa: BLE001
            logger.warning("proactive shadow 讀取聽眾 scope 失敗：%s", type(exc).__name__)
            return WIDEST_AUDIENCE_SCOPE


def attach_proactive_shadow(
    hub: DecisionHub, *, presence: Any, state: Any, stop: Any, audience: Any = None,
    injections: ProactiveInjections | None = None, clock: Callable[[], datetime] | None = None,
) -> ProactiveShadow:
    """登記 ``proactive.initiate`` 並回傳 shadow 入口。``audience`` 非 callable（None／ALLOW_ALL）視為最寬 scope。"""
    engine = register_proactive(hub)
    return ProactiveShadow(
        hub, engine, presence=presence, state=state, stop=stop,
        audience=audience if callable(audience) else None, injections=injections, clock=clock,
    )
