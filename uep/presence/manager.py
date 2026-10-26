"""Presence Manager（契約 CC-4）：Lease 核發／續約／釋放／撤銷，單一權威 row 的原子 CAS。

- 每個操作在一個 DB 交易內以 ``SELECT ... FOR UPDATE`` 鎖定 ``presence_current``，
  檢查 expected version、owner、status、到期與移轉狀態後才寫入（P-2）。外部 I/O 不在鎖內：
  事件於提交後才投遞 Bus，控制訊號於提交後才送出（P-7）。
- P2 單平台範圍實作 [A] 首次 acquire、[C] 恢復（``confirm_quiesced``／``resolve_recovery``），以及
  Lease 過期與 Core 重啟的 P-4／P-6 處理；[B] 位置移轉（begin_transfer／CHECKPOINTED）留待多平台階段，
  目前 ACTIVE 時其他端 acquire 一律 ``BUSY``，不自動 failover。
- ``generation`` 只在核發時 +1，撤銷／過期／釋放不回退也不再 +1（VC-4-1）。
- ``core_epoch`` 與 ``fencing_epoch`` 來自 Host 本機檔（D26），``start`` 時各 +1；
  Manager 發現 DB 內的 epoch 與自己持有的不同（被較新的 Core 啟動取代）時一律拒絕。
- 時間以可注入 clock 判定（P2 為單 Host，不依賴 DB 時鐘）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from uep.contracts.presence import (
    AcquireDenyReason,
    AcquireResult,
    AuthorizationTuple,
    ConfirmResult,
    PresenceLocation,
    PresenceStatus,
    ReleaseResult,
    RenewResult,
    TransferPhase,
)
from uep.control.channel import ControlChannel
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.publisher import EventPublisher, RecordedEvent
from uep.ids import new_id
from uep.output.intent import ControlKind, ControlScope, ControlSignal
from uep.presence.epoch import HostEpochError, HostEpochStore
from uep.presence.models import (
    PresenceCurrentRow,
    PresenceLeaseRow,
    PresenceTransferRow,
)

logger = logging.getLogger("uep.presence.manager")

SOURCE = "core.presence"
ACTOR = "uep:presence_manager"
DEFAULT_LEASE_TTL = timedelta(seconds=30)  # 調校起點，非契約常數（4.4）
DEFAULT_ACTIVATION_TIMEOUT = timedelta(seconds=10)
_OPEN_PHASES = (
    TransferPhase.PREPARING.value, TransferPhase.QUIESCING.value,
    TransferPhase.CHECKPOINTED.value, TransferPhase.ACTIVATING.value,
)


class PresenceNotStarted(RuntimeError):
    """尚未呼叫 ``start``（Core 啟動程序）；此時沒有 epoch，不得核發任何授權。"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PresenceView:
    """presence_current 的唯讀快照。"""

    persona_id: str
    status: PresenceStatus
    location: str | None
    endpoint_id: str | None
    lease_id: str | None
    core_epoch: int
    fencing_epoch: int
    generation: int
    expires_at: datetime | None
    row_version: int
    transfer_id: str | None
    unquiesced_endpoint_id: str | None
    recovery_reason: str | None

    def authorization(self) -> AuthorizationTuple | None:
        """ACTIVE 時的授權六元組；其餘狀態沒有可核准輸出的授權。"""
        if self.status is not PresenceStatus.ACTIVE or self.lease_id is None or self.endpoint_id is None:
            return None
        return AuthorizationTuple(
            self.persona_id, self.endpoint_id, self.lease_id,
            self.fencing_epoch, self.core_epoch, self.generation,
        )


@dataclass(frozen=True)
class LeaseGrant:
    """acquire 成功後給端點的授權；``confirm_activated`` 之前 Output Gate 一律拒絕。"""

    lease_id: str
    transfer_id: str
    expires_at: datetime
    authorization: AuthorizationTuple


@dataclass(frozen=True)
class AcquireOutcome:
    result: AcquireResult
    grant: LeaseGrant | None
    row_version: int
    reason: AcquireDenyReason | None = None  # 只有 DENIED 才有


@dataclass(frozen=True)
class RevokeOutcome:
    """revoke 永遠回 REVOKED（冪等）；``lease_id`` 為本次實際撤銷的 Lease（沒有則為 None）。"""

    lease_id: str | None
    endpoint_id: str | None
    generation: int


def _view(row: PresenceCurrentRow) -> PresenceView:
    return PresenceView(
        persona_id=row.persona_id, status=PresenceStatus(row.status), location=row.location,
        endpoint_id=row.endpoint_id, lease_id=row.lease_id, core_epoch=row.core_epoch,
        fencing_epoch=row.fencing_epoch, generation=row.generation, expires_at=row.expires_at,
        row_version=row.row_version, transfer_id=row.transfer_id,
        unquiesced_endpoint_id=row.unquiesced_endpoint_id, recovery_reason=row.recovery_reason,
    )


class PresenceManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        persona_id: str = "uep",
        epoch_store: HostEpochStore | None = None,
        publisher: EventPublisher | None = None,
        control: ControlChannel | None = None,
        clock: Callable[[], datetime] = _utcnow,
        lease_ttl: timedelta = DEFAULT_LEASE_TTL,
        activation_timeout: timedelta = DEFAULT_ACTIVATION_TIMEOUT,
        outbox_targets: tuple[str, ...] = ("presence",),
        acquire_guard: Callable[[Session], str | None] | None = None,
    ) -> None:
        self._sf = session_factory
        self._persona = persona_id
        self._store = epoch_store or HostEpochStore()
        self._publisher = publisher
        self._control = control
        self._clock = clock
        self._ttl = lease_ttl
        self._activation_timeout = activation_timeout
        self._outbox_targets = outbox_targets
        # 全域停止的暫停旗標（T-17）：在 acquire 的鎖定交易內呼叫，回非 None 即拒絕核發（DENIED）
        self._acquire_guard = acquire_guard
        self._core_epoch: int | None = None
        self._fencing_epoch: int | None = None

    # ---- 基礎 ----

    @property
    def persona_id(self) -> str:
        return self._persona

    @property
    def started(self) -> bool:
        """``start`` 已完成（持有本次啟動的 epoch）；Output Gate 以此判斷 epoch 是否就緒。"""
        return self._core_epoch is not None and self._fencing_epoch is not None

    @property
    def core_epoch(self) -> int:
        if self._core_epoch is None:
            raise PresenceNotStarted("PresenceManager 尚未 start")
        return self._core_epoch

    @property
    def fencing_epoch(self) -> int:
        if self._fencing_epoch is None:
            raise PresenceNotStarted("PresenceManager 尚未 start")
        return self._fencing_epoch

    def _now(self) -> datetime:
        return self._clock()

    @contextmanager
    def _tx(self) -> Iterator[tuple[Session, list[RecordedEvent], list[tuple[str, ControlSignal]]]]:
        """單一交易；提交成功後才投遞事件與控制訊號（失敗只記 log，不影響已提交的狀態轉換）。"""
        pending: list[RecordedEvent] = []
        controls: list[tuple[str, ControlSignal]] = []
        with self._sf() as session, session.begin():
            yield session, pending, controls
        for recorded in pending:
            try:
                if self._publisher is None:
                    raise RuntimeError("有待投遞事件但沒有 publisher")
                self._publisher.dispatch(recorded)
            except Exception:
                logger.exception("presence 事件投遞失敗：%s", recorded.event.event_type)
        for endpoint_id, signal in controls:
            try:
                if self._control is None:
                    raise RuntimeError("有待送控制訊號但沒有 control channel")
                self._control.send(signal, endpoint_id=endpoint_id)
            except Exception:
                logger.exception("presence 控制訊號送出失敗")

    def _lock_row(self, s: Session) -> PresenceCurrentRow:
        return s.execute(
            select(PresenceCurrentRow)
            .where(PresenceCurrentRow.persona_id == self._persona)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

    def _open_transfer(self, s: Session) -> PresenceTransferRow | None:
        return s.execute(
            select(PresenceTransferRow)
            .where(PresenceTransferRow.persona_id == self._persona, PresenceTransferRow.phase.in_(_OPEN_PHASES))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def _superseded(self, row: PresenceCurrentRow) -> bool:
        """DB 內的 epoch 不是本 Manager 持有的：已有較新的 Core 啟動或還原（P-4、P-5）。"""
        return row.core_epoch != self.core_epoch or row.fencing_epoch != self.fencing_epoch

    def _bump(self, row: PresenceCurrentRow) -> None:
        row.row_version += 1
        row.updated_at = self._now()

    def _emit(
        self, s: Session, pending: list[RecordedEvent], row: PresenceCurrentRow, event_type: str,
        reason: str, *, lease_id: str | None, endpoint_id: str | None, actor: str = ACTOR,
        extra: dict[str, str] | None = None,
    ) -> None:
        if self._publisher is None:
            return
        draft = EventDraft(
            event_type=event_type,
            occurred_at=self._now(),
            aggregate_type="presence",
            aggregate_id=self._persona,
            aggregate_version=row.row_version,
            fence={
                "persona_id": self._persona, "endpoint_id": endpoint_id, "lease_id": lease_id,
                "fencing_epoch": row.fencing_epoch, "core_epoch": row.core_epoch,
                "presence_generation": row.generation,
            },
            payload={"lease_id": lease_id, "status": row.status, "reason_code": reason, **(extra or {})},
        )
        trusted = TrustedContext(
            source=SOURCE, actor_id=actor, scope_ref="scope_private_owner", persona_id=self._persona
        )
        pending.append(
            self._publisher.record(s, draft, trusted, outbox_targets=self._outbox_targets)
        )

    def _queue_revoke(
        self, controls: list[tuple[str, ControlSignal]], endpoint_id: str | None, core_epoch: int, generation: int
    ) -> None:
        """盡力通知舊端停止（提交後才送；失敗不影響狀態轉換）。"""
        if self._control is None or endpoint_id is None:
            return
        controls.append((endpoint_id, ControlSignal(
            control_id=new_id("ctl"), kind=ControlKind.REVOKE, scope=ControlScope.PROCESS,
            target_fence={"core_epoch": core_epoch, "presence_generation": generation},
            issued_at=self._now(),
        )))

    @staticmethod
    def _end_lease(s: Session, lease_id: str | None, state: str, reason: str, now: datetime) -> PresenceLeaseRow | None:
        if lease_id is None:
            return None
        lease = s.get(PresenceLeaseRow, lease_id, with_for_update=True, populate_existing=True)
        if lease is not None and lease.state in ("ISSUED", "ACTIVE"):
            lease.state, lease.ended_at, lease.end_reason = state, now, reason
        return lease

    @staticmethod
    def _clear_lease_fields(row: PresenceCurrentRow) -> None:
        row.location = row.endpoint_id = row.lease_id = row.expires_at = None
        row.owner_activity_id = None

    # ---- Core 啟動 ----

    def start(self) -> PresenceView:
        """Core 啟動程序：Host 檔 epoch +1 並寫入 DB（P-4、P-5）。

        舊 epoch 的 Lease 一律撤銷；原本 ACTIVE（或啟用中）者進 RECOVERING 待查核，輸出維持全關，
        並盡力對舊端送 ``control.revoke``。Host 檔落後 DB（檔案遺失或被還原）時拒絕啟動。
        """
        epochs = self._store.bump()
        with self._tx() as (s, pending, controls):
            s.execute(
                pg_insert(PresenceCurrentRow)
                .values(
                    persona_id=self._persona, status="NONE", core_epoch=0, fencing_epoch=0,
                    generation=0, row_version=1,
                )
                .on_conflict_do_nothing(index_elements=["persona_id"])
            )
            row = self._lock_row(s)
            if epochs.fencing_epoch <= row.fencing_epoch:
                raise HostEpochError("Host epoch 檔的 fencing_epoch 不大於 DB 已核發值，拒絕啟動")
            core = max(epochs.core_epoch, row.core_epoch + 1)
            if core != epochs.core_epoch:
                self._store.raise_to(core_at_least=core)
            now = self._now()
            transfer = self._open_transfer(s)
            had_authority = row.status == "ACTIVE" or row.lease_id is not None
            lease_id, endpoint_id, old_core = row.lease_id, row.endpoint_id, row.core_epoch
            if had_authority:
                lease = self._end_lease(s, lease_id, "REVOKED", "core_restart", now)
                endpoint_id = endpoint_id or (lease.endpoint_id if lease else None)
                row.status = "RECOVERING"
                row.recovery_reason = "core_restart"
                row.unquiesced_endpoint_id = endpoint_id  # confirm_quiesced 以此比對舊端
                self._clear_lease_fields(row)
                if transfer is not None:
                    transfer.recovery_reason, transfer.row_version = "core_restart", transfer.row_version + 1
                self._queue_revoke(controls, endpoint_id, old_core, row.generation)
            row.core_epoch, row.fencing_epoch = core, epochs.fencing_epoch
            self._bump(row)
            if had_authority:
                self._emit(
                    s, pending, row, "presence.recovering", "core_restart",
                    lease_id=lease_id, endpoint_id=endpoint_id,
                )
            view = _view(row)
        self._core_epoch, self._fencing_epoch = core, epochs.fencing_epoch
        return view

    # ---- 查詢 ----

    def _needs_transition(self, row: PresenceCurrentRow) -> bool:
        if self._superseded(row):
            return False  # 過期處理屬於當前 epoch 的 Core
        now = self._now()
        if row.status == "ACTIVE":
            return row.expires_at is not None and row.expires_at <= now
        return row.status == "NONE" and row.lease_id is not None  # 啟用中：需查 transfer deadline

    def current(self) -> PresenceView:
        """目前權威狀態。唯讀路徑先無鎖 SELECT；只有需要轉換（過期等）時才進鎖定交易。"""
        with self._sf() as s:
            row = s.get(PresenceCurrentRow, self._persona)
            if row is None:
                raise PresenceNotStarted("presence_current 尚無資料；請先 start")
            if not self._needs_transition(row):
                return _view(row)
        with self._tx() as (s, pending, controls):
            locked = self._lock_row(s)
            self._apply_expiry(s, pending, controls, locked)
            return _view(locked)

    def authorization(self) -> AuthorizationTuple | None:
        """目前可用於輸出的授權六元組；沒有有效 Presence 時為 None。"""
        return self.current().authorization()

    def expire_due(self) -> bool:
        """主動掃描到期；回傳是否有狀態變更。"""
        with self._tx() as (s, pending, controls):
            return self._apply_expiry(s, pending, controls, self._lock_row(s))

    def _apply_expiry(
        self, s: Session, pending: list[RecordedEvent], controls: list[tuple[str, ControlSignal]],
        row: PresenceCurrentRow,
    ) -> bool:
        if self._superseded(row):
            return False
        now = self._now()
        if row.status == "ACTIVE" and row.expires_at is not None and row.expires_at <= now:
            lease_id, endpoint_id = row.lease_id, row.endpoint_id
            self._end_lease(s, lease_id, "EXPIRED", "lease_expired", now)
            row.status = "NONE"
            # 舊端尚未證明已停止：其他端不得直接啟用（P-6、VC-4-4）
            row.unquiesced_endpoint_id = endpoint_id
            self._clear_lease_fields(row)
            self._bump(row)
            self._emit(s, pending, row, "presence.revoked", "lease_expired", lease_id=lease_id, endpoint_id=endpoint_id)
            self._queue_revoke(controls, endpoint_id, row.core_epoch, row.generation)
            return True
        if row.status == "NONE" and row.lease_id is not None:
            transfer = self._open_transfer(s)
            if transfer is not None and transfer.phase == TransferPhase.ACTIVATING.value and transfer.deadline <= now:
                self._fail_activation(s, pending, controls, row, transfer, "activation_timeout")
                return True
        return False

    def _fail_activation(
        self, s: Session, pending: list[RecordedEvent], controls: list[tuple[str, ControlSignal]],
        row: PresenceCurrentRow, transfer: PresenceTransferRow, reason: str,
    ) -> None:
        """ACTIVATING 啟用失敗 -> RECOVERING，phase 停在 ACTIVATING（4.5 [A]）。"""
        lease_id = row.lease_id
        lease = self._end_lease(s, lease_id, "EXPIRED", reason, self._now())
        endpoint_id = lease.endpoint_id if lease else transfer.to_endpoint_id
        row.status = "RECOVERING"
        row.recovery_reason = reason
        row.unquiesced_endpoint_id = endpoint_id
        self._clear_lease_fields(row)
        transfer.recovery_reason, transfer.row_version = reason, transfer.row_version + 1
        self._bump(row)
        self._emit(s, pending, row, "presence.recovering", reason, lease_id=lease_id, endpoint_id=endpoint_id)
        self._queue_revoke(controls, endpoint_id, row.core_epoch, row.generation)

    # ---- acquire［A］----

    def acquire(
        self,
        endpoint_id: str,
        location: PresenceLocation | str = PresenceLocation.DESKTOP,
        *,
        expected_version: int | None = None,
        owner_activity_id: str | None = None,
        reason: dict | None = None,
    ) -> AcquireOutcome:
        """自 NONE 首次 acquire：同交易建立 transfer(ACTIVATING)、核發 Lease、generation+1。

        成功只代表授權已核發；``confirm_activated`` 之前 status 仍為 NONE，Output Gate 拒絕。
        冪等：同一 endpoint 在其 transfer 仍為 ACTIVATING 時重送，回同一份 grant（generation 不再 +1）。
        """
        loc = PresenceLocation(location).value
        with self._tx() as (s, pending, controls):
            row = self._lock_row(s)

            def out(
                result: AcquireResult, grant: LeaseGrant | None = None, reason: AcquireDenyReason | None = None
            ) -> AcquireOutcome:
                return AcquireOutcome(result, grant, row.row_version, reason)

            if self._superseded(row):
                return out(AcquireResult.DENIED, reason=AcquireDenyReason.SUPERSEDED_CORE)
            blocked = self._acquire_guard(s) if self._acquire_guard is not None else None
            if blocked is not None:
                # 全域停止後暫停：直到明確解除前端點不得重 acquire
                try:
                    reason = AcquireDenyReason(blocked)
                except ValueError:
                    reason = AcquireDenyReason.GUARD_REJECTED
                return out(AcquireResult.DENIED, reason=reason)
            if row.status == "NONE" and row.transfer_id is not None:
                transfer = self._open_transfer(s)
                lease = s.get(PresenceLeaseRow, row.lease_id) if row.lease_id else None
                if (
                    transfer is not None and lease is not None
                    and transfer.phase == TransferPhase.ACTIVATING.value
                    and transfer.to_endpoint_id == endpoint_id and transfer.deadline > self._now()
                ):
                    return out(AcquireResult.GRANTED, LeaseGrant(
                        lease.lease_id, transfer.transfer_id, lease.expires_at,
                        AuthorizationTuple(
                            self._persona, endpoint_id, lease.lease_id, row.fencing_epoch, row.core_epoch,
                            row.generation,
                        ),
                    ))
            if expected_version is not None and expected_version != row.row_version:
                return out(AcquireResult.VERSION_CONFLICT)
            self._apply_expiry(s, pending, controls, row)
            if row.status == "RECOVERING":
                return out(AcquireResult.NOT_QUIESCED)
            if row.status == "ACTIVE":
                return out(AcquireResult.BUSY)
            if row.transfer_id is not None:  # NONE 且已核發待確認的 Lease（他端）
                return out(AcquireResult.TRANSFER_PENDING)
            if row.unquiesced_endpoint_id is not None and row.unquiesced_endpoint_id != endpoint_id:
                # Lease 已過期但舊端未證明已停止：進恢復，不讓新端啟用（INV-08）
                row.status, row.recovery_reason = "RECOVERING", "unquiesced_endpoint"
                self._bump(row)
                self._emit(s, pending, row, "presence.recovering", "unquiesced_endpoint",
                           lease_id=None, endpoint_id=row.unquiesced_endpoint_id)
                return out(AcquireResult.NOT_QUIESCED)

            now = self._now()
            row.generation += 1
            lease_id, transfer_id = new_id("lease"), new_id("xfer")
            expires_at = now + self._ttl
            s.add(PresenceLeaseRow(
                lease_id=lease_id, persona_id=self._persona, endpoint_id=endpoint_id, location=loc,
                generation=row.generation, core_epoch=row.core_epoch, fencing_epoch=row.fencing_epoch,
                state="ISSUED", issued_at=now, expires_at=expires_at,
            ))
            s.add(PresenceTransferRow(
                transfer_id=transfer_id, persona_id=self._persona, from_endpoint_id=None,
                to_endpoint_id=endpoint_id, reason=reason or {"origin": "user"},
                phase=TransferPhase.ACTIVATING.value, lease_id=lease_id,
                deadline=now + self._activation_timeout, row_version=1,
            ))
            row.lease_id, row.transfer_id, row.owner_activity_id = lease_id, transfer_id, owner_activity_id
            row.unquiesced_endpoint_id = None
            row.recovery_reason = None
            self._bump(row)
            self._emit(s, pending, row, "presence.granted", "granted", lease_id=lease_id, endpoint_id=endpoint_id)
            grant = LeaseGrant(
                lease_id, transfer_id, expires_at,
                AuthorizationTuple(
                    self._persona, endpoint_id, lease_id, row.fencing_epoch, row.core_epoch, row.generation
                ),
            )
            return out(AcquireResult.GRANTED, grant)

    def confirm_activated(self, lease_id: str, *, generation: int | None = None) -> ConfirmResult:
        """新端到場確認：同交易 status=ACTIVE、phase=DONE，Lease 自此開始計時。"""
        with self._tx() as (s, pending, controls):
            row = self._lock_row(s)
            if self._superseded(row):
                return ConfirmResult.STALE
            self._apply_expiry(s, pending, controls, row)
            transfer = self._open_transfer(s)
            if (
                row.status != "NONE" or row.lease_id != lease_id or transfer is None
                or transfer.phase != TransferPhase.ACTIVATING.value or transfer.lease_id != lease_id
                or (generation is not None and generation != row.generation)
            ):
                return ConfirmResult.STALE
            now = self._now()
            lease = s.get(PresenceLeaseRow, lease_id, with_for_update=True, populate_existing=True)
            if lease is None or lease.state != "ISSUED":
                return ConfirmResult.STALE
            lease.state, lease.expires_at = "ACTIVE", now + self._ttl
            transfer.phase, transfer.row_version = TransferPhase.DONE.value, transfer.row_version + 1
            row.status, row.location, row.endpoint_id = "ACTIVE", lease.location, lease.endpoint_id
            row.expires_at, row.transfer_id = lease.expires_at, None
            self._bump(row)
            self._emit(s, pending, row, "presence.activated", "activated", lease_id=lease_id, endpoint_id=lease.endpoint_id)
            return ConfirmResult.ACCEPTED

    # ---- renew／release／revoke ----

    def _matches_active(
        self, row: PresenceCurrentRow, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int
    ) -> bool:
        return (
            row.status == "ACTIVE" and row.lease_id == lease_id and row.core_epoch == core_epoch
            and row.generation == generation and row.fencing_epoch == fencing_epoch
        )

    def renew(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> RenewResult:
        """須精確匹配 lease_id／core_epoch／generation／fencing_epoch（P-3）；過期不可續活，需重新 acquire。"""
        with self._tx() as (s, pending, controls):
            row = self._lock_row(s)
            if self._superseded(row) or not self._matches_active(row, lease_id, core_epoch, generation, fencing_epoch):
                self._apply_expiry(s, pending, controls, row)
                return RenewResult.STALE
            now = self._now()
            if row.expires_at is not None and row.expires_at <= now:
                self._apply_expiry(s, pending, controls, row)
                return RenewResult.EXPIRED
            row.expires_at = now + self._ttl
            lease = s.get(PresenceLeaseRow, lease_id, with_for_update=True, populate_existing=True)
            if lease is not None:
                lease.expires_at = row.expires_at
            return RenewResult.RENEWED  # 心跳不改 row_version，避免干擾樂觀併發

    def release(self, lease_id: str, core_epoch: int, generation: int, fencing_epoch: int) -> ReleaseResult:
        """目前 owner 主動釋放：owner 自己已停止，視為有停止證據，直接回 NONE。"""
        with self._tx() as (s, pending, _controls):
            row = self._lock_row(s)
            if self._superseded(row) or not self._matches_active(row, lease_id, core_epoch, generation, fencing_epoch):
                return ReleaseResult.STALE
            endpoint_id = row.endpoint_id
            self._end_lease(s, lease_id, "RELEASED", "released", self._now())
            row.status = "NONE"
            self._clear_lease_fields(row)
            self._bump(row)
            self._emit(s, pending, row, "presence.revoked", "released", lease_id=lease_id, endpoint_id=endpoint_id)
            return ReleaseResult.RELEASED

    def revoke(self, reason: str = "revoked") -> RevokeOutcome:
        """Core／全域停止入口（不依賴 LLM）：撤銷目前 Lease 並回 NONE；冪等。

        generation 不再 +1。撤銷後舊 Lease 不能恢復；若有控制通道，提交後對舊端送 ``control.revoke``。
        RECOVERING 維持 RECOVERING（仍需查核停止）。
        """
        with self._tx() as (s, pending, controls):
            row = self._lock_row(s)
            if self._superseded(row) or row.lease_id is None:
                return RevokeOutcome(None, None, row.generation)
            lease_id = row.lease_id
            lease = self._end_lease(s, lease_id, "REVOKED", reason, self._now())
            endpoint_id = lease.endpoint_id if lease else row.endpoint_id
            transfer = self._open_transfer(s)
            if transfer is not None:
                transfer.phase, transfer.row_version = TransferPhase.ABORTED.value, transfer.row_version + 1
                transfer.recovery_reason = reason
            if row.status != "RECOVERING":
                row.status = "NONE"
            row.transfer_id = None
            self._clear_lease_fields(row)
            self._bump(row)
            self._emit(s, pending, row, "presence.revoked", reason, lease_id=lease_id, endpoint_id=endpoint_id)
            self._queue_revoke(controls, endpoint_id, row.core_epoch, row.generation)
            return RevokeOutcome(lease_id, endpoint_id, row.generation)

    # ---- 恢復［C］----

    def _recovered(
        self, s: Session, pending: list[RecordedEvent], row: PresenceCurrentRow, *, actor: str,
        evidence: dict[str, object], kind: str,
    ) -> None:
        """RECOVERING／未證實停止 -> NONE，並發 ``presence.recovered``（只含 ID 與 evidence_kind）。"""
        if row.status == "RECOVERING":
            transfer = self._open_transfer(s)
            if transfer is not None:
                transfer.phase, transfer.row_version = TransferPhase.ABORTED.value, transfer.row_version + 1
                transfer.stop_evidence = evidence
            row.status, row.recovery_reason, row.transfer_id = "NONE", None, None
        row.unquiesced_endpoint_id = None
        self._bump(row)
        self._emit(
            s, pending, row, "presence.recovered", "recovered", lease_id=None, endpoint_id=None,
            actor=actor, extra={"kind": kind},
        )

    def confirm_quiesced(
        self, endpoint_id: str, old_generation: int, evidence: Mapping[str, Any] | None = None
    ) -> ConfirmResult:
        """舊端回報已停止：須與記錄的未證實端點與 generation 完全相符（含 core_restart 的 RECOVERING）。

        證據不等於授權：只有相符才由 Manager 轉 NONE；不相符回 STALE，狀態不變。
        """
        kind = str((evidence or {}).get("kind") or "adapter_stop_report")[:64]
        with self._tx() as (s, pending, controls):
            row = self._lock_row(s)
            if self._superseded(row):
                return ConfirmResult.STALE
            self._apply_expiry(s, pending, controls, row)
            if (
                row.status not in ("NONE", "RECOVERING") or row.unquiesced_endpoint_id is None
                or row.unquiesced_endpoint_id != endpoint_id or old_generation != row.generation
            ):
                return ConfirmResult.STALE
            record = {
                "endpoint_id": endpoint_id, "old_generation": old_generation, "kind": kind,
                "reported_at": self._now().isoformat(),
            }
            self._recovered(s, pending, row, actor=endpoint_id, evidence=record, kind=kind)
            return ConfirmResult.ACCEPTED

    def resolve_recovery(self, operator: str, evidence_kind: str = "operator_confirmed") -> bool:
        """操作員確認各端停止：RECOVERING -> NONE（transfer.phase=ABORTED）；也可清除未證實停止的標記。

        ``operator`` 為認證後的操作者 ID（記為事件 actor）。回傳是否有狀態變更。
        """
        if not operator:
            raise ValueError("resolve_recovery 需要 operator")
        with self._tx() as (s, pending, _controls):
            row = self._lock_row(s)
            if self._superseded(row):
                return False
            if row.status == "RECOVERING" or (row.status == "NONE" and row.unquiesced_endpoint_id is not None):
                record = {
                    "endpoint_id": row.unquiesced_endpoint_id, "old_generation": row.generation,
                    "kind": evidence_kind, "reported_at": self._now().isoformat(), "operator": operator,
                }
                self._recovered(s, pending, row, actor=operator, evidence=record, kind=evidence_kind)
                return True
            return False
