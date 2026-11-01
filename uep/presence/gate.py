"""Output Gate（契約 CC-4 4.3、P-3、P-8）。

可見輸出（文字、TTS、typing、進場、動畫、前景工具入口）使用同一規則；admission 發生兩次：
Core 接納時（本類別）與平台發送／播放器接納時（端點以同一份結果碼表自行檢查）。

判定順序（先中者先回）：
0. 綁定的 PresenceManager 尚未 ``start``（epoch 未就緒）-> 一律 ``REJECT_NO_PRESENCE``。
1. 授權的 persona、core_epoch、fencing_epoch 與 Manager 持有值或 DB 不同 -> ``REJECT_STALE``
   （舊 Core／舊還原的輸出不可復活）。
2. status 不是 ACTIVE（含 ACTIVATING 期間的 NONE、RECOVERING）-> 該 Lease 已被撤銷則
   ``REJECT_REVOKED``，否則 ``REJECT_NO_PRESENCE``。
3. endpoint／lease_id／generation 與目前 row 不符 -> ``REJECT_STALE``。
4. Lease 已到期，或 ``valid_until`` 已過 -> ``REJECT_EXPIRED``。
5. 帶 turn_generation 且與目前回合 generation 不同 -> ``REJECT_STALE``（插話後舊回合輸出）。
6. 輸出 audience 比 Presence 可接受的 audience 寬 -> ``REJECT_AUDIENCE``。

fail-closed：``turn_generation_source`` 與 ``presence_audience`` 為必填 callable。來源回 None（回合未知、
目前 audience 未知）一律拒絕。要關閉某項檢查須顯式傳 ``ALLOW_ALL``，建構時會記 warning。

Gate 只讀不寫 presence 表；到期的轉換由 PresenceManager 處理，Gate 以時間直接判定 EXPIRED。
拒絕時若有 publisher，於獨立交易記一筆 ``output.rejected``（只含 ID 與結果碼）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from uep.contracts.presence import AuthorizationTuple, GateDecision
from uep.events.envelope import EventDraft, TrustedContext, scope_rank
from uep.events.publisher import EventPublisher
from uep.output.intent import OutputIntent
from uep.presence.manager import PresenceManager
from uep.presence.models import PresenceCurrentRow, PresenceLeaseRow

logger = logging.getLogger("uep.presence.gate")

SOURCE = "core.output_gate"
ACTOR = "uep:output_gate"


class _AllowAll:
    """顯式關閉某項檢查的 sentinel（僅供測試或尚未接線的階段）。"""

    def __repr__(self) -> str:
        return "ALLOW_ALL"


ALLOW_ALL = _AllowAll()
TurnGenerationSource = Callable[[str], int | None] | _AllowAll
AudienceProvider = Callable[[], str | None] | _AllowAll


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OutputGate:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        manager: PresenceManager,
        *,
        turn_generation_source: TurnGenerationSource,
        presence_audience: AudienceProvider,
        clock: Callable[[], datetime] = _utcnow,
        publisher: EventPublisher | None = None,
    ) -> None:
        self._sf = session_factory
        self._manager = manager
        self._persona = manager.persona_id
        self._clock = clock
        self._publisher = publisher
        self._turn_generation = turn_generation_source
        self._audience = presence_audience
        for name, value in (("turn_generation_source", turn_generation_source),
                            ("presence_audience", presence_audience)):
            if value is ALLOW_ALL:
                logger.warning("OutputGate 的 %s 為 ALLOW_ALL：此項檢查已關閉", name)
            elif not callable(value):
                raise TypeError(f"{name} 必須是 callable 或 ALLOW_ALL")

    def admit(
        self,
        auth: AuthorizationTuple,
        *,
        turn_id: str | None = None,
        turn_generation: int | None = None,
        audience_scope: str | None = None,
        valid_until: datetime | None = None,
        ref_id: str | None = None,
        kind: str | None = None,
    ) -> GateDecision:
        decision = self._decide(auth, turn_id, turn_generation, audience_scope, valid_until)
        if not decision.admitted:
            self._record_rejection(auth, decision, turn_generation, ref_id, kind)
        return decision

    def admit_intent(self, intent: OutputIntent) -> GateDecision:
        auth = AuthorizationTuple(
            intent.persona_id, intent.endpoint_id, intent.lease_id,
            intent.fencing_epoch, intent.core_epoch, intent.presence_generation,
        )
        return self.admit(
            auth, turn_id=intent.turn_id, turn_generation=intent.turn_generation,
            audience_scope=intent.audience_scope, valid_until=intent.valid_until,
            ref_id=intent.output_id, kind=intent.kind.value,
        )

    def _decide(
        self, auth: AuthorizationTuple, turn_id: str | None, turn_generation: int | None,
        audience_scope: str | None, valid_until: datetime | None,
    ) -> GateDecision:
        if not self._manager.started:
            return GateDecision.REJECT_NO_PRESENCE  # epoch 未就緒：輸出全關
        now = self._clock()
        with self._sf() as s:
            row = s.get(PresenceCurrentRow, self._persona)
            if row is None:
                return GateDecision.REJECT_NO_PRESENCE
            if (
                auth.persona_id != self._persona
                or auth.core_epoch != self._manager.core_epoch or auth.fencing_epoch != self._manager.fencing_epoch
                or auth.core_epoch != row.core_epoch or auth.fencing_epoch != row.fencing_epoch
            ):
                return GateDecision.REJECT_STALE
            if row.status != "ACTIVE":
                lease = s.get(PresenceLeaseRow, auth.lease_id)
                if lease is not None and lease.state == "REVOKED":
                    return GateDecision.REJECT_REVOKED
                return GateDecision.REJECT_NO_PRESENCE
            if (
                auth.endpoint_id != row.endpoint_id or auth.lease_id != row.lease_id
                or auth.presence_generation != row.generation
            ):
                return GateDecision.REJECT_STALE
            if row.expires_at is not None and row.expires_at <= now:
                return GateDecision.REJECT_EXPIRED
        if valid_until is not None and valid_until <= now:
            return GateDecision.REJECT_EXPIRED
        if self._turn_generation is not ALLOW_ALL and turn_id is not None:
            current = self._turn_generation(turn_id)  # type: ignore[operator]
            if current is None or turn_generation is None or current != turn_generation:
                return GateDecision.REJECT_STALE  # 回合未知或 generation 已前進（插話後舊回合）
        if self._audience is not ALLOW_ALL:
            allowed = self._audience()  # type: ignore[operator]
            if allowed is None or audience_scope is None or scope_rank(audience_scope) > scope_rank(allowed):
                return GateDecision.REJECT_AUDIENCE
        return GateDecision.ADMIT

    def _record_rejection(
        self, auth: AuthorizationTuple, decision: GateDecision, turn_generation: int | None,
        ref_id: str | None, kind: str | None,
    ) -> None:
        if self._publisher is None:
            return
        try:
            fence: dict[str, int | str | None] = {
                "persona_id": auth.persona_id, "endpoint_id": auth.endpoint_id, "lease_id": auth.lease_id,
                "fencing_epoch": auth.fencing_epoch, "core_epoch": auth.core_epoch,
                "presence_generation": auth.presence_generation, "turn_generation": turn_generation,
            }
            payload: dict[str, str] = {"reason_code": decision.value}
            if ref_id:
                payload["ref_id"] = ref_id
            if kind:
                payload["kind"] = kind
            draft = EventDraft(event_type="output.rejected", fence=fence, payload=payload)
            trusted = TrustedContext(
                source=SOURCE, actor_id=ACTOR, scope_ref="scope_private_owner", persona_id=self._persona
            )
            self._publisher.publish(draft, trusted)
        except Exception:
            logger.exception("Output Gate 拒絕紀錄寫入 journal 失敗")
