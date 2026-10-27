"""PresenceManager（CC-4）：原子 acquire、generation／epoch、過期、恢復（需 PostgreSQL）。

對應 VC-4-1（併發 acquire）、VC-4-2（舊 generation 被拒）、VC-4-4（過期後他端不得直接啟用）、
P-1／P-2／P-3／P-4／P-5／P-6。位置移轉 [B] 與崩潰注入（VC-4-3）屬多平台階段，不在 P2 單平台範圍。
"""
from __future__ import annotations

import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.uep.presence_support import (
    FakeClock,
    activate,
    count_events,
    count_outbox,
    make_rig,
)
from uep.contracts.presence import (
    AcquireResult,
    ConfirmResult,
    GateDecision,
    PresenceStatus,
    ReleaseResult,
    RenewResult,
)
from uep.control.channel import InProcessControlChannel
from uep.events.models import EventRow
from uep.presence.epoch import HostEpochError, HostEpochStore
from uep.presence.gate import ALLOW_ALL, OutputGate
from uep.presence.manager import PresenceManager, PresenceNotStarted
from uep.presence.models import (
    PresenceCurrentRow,
    PresenceLeaseRow,
    PresenceTransferRow,
)

pytestmark = pytest.mark.usefixtures("session_factory")

ENDPOINTS = ("ep_desktop", "ep_discord", "ep_website")


def _current(rig):
    return rig.manager.current()


def _transfer_phases(session_factory):
    with session_factory() as s:
        return [t.phase for t in s.scalars(select(PresenceTransferRow).order_by(PresenceTransferRow.created_at))]


# ---- VC-4-1：併發 acquire ----

def test_concurrent_acquire_exactly_one_granted_per_round(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    rounds = 100
    barrier = Barrier(len(ENDPOINTS))

    def worker(endpoint_id: str, expected: int | None):
        barrier.wait()
        return rig.manager.acquire(endpoint_id, "desktop", expected_version=expected).result

    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
        for n in range(1, rounds + 1):
            version = _current(rig).row_version
            futures = [
                pool.submit(worker, ep, version if i == 0 else None) for i, ep in enumerate(ENDPOINTS)
            ]
            results = [f.result(timeout=30) for f in futures]
            assert results.count(AcquireResult.GRANTED) == 1, results
            assert all(
                r in (AcquireResult.TRANSFER_PENDING, AcquireResult.VERSION_CONFLICT, AcquireResult.BUSY)
                for r in results if r is not AcquireResult.GRANTED
            ), results
            view = _current(rig)
            assert view.generation == n and view.status is PresenceStatus.NONE and view.transfer_id
            revoked = rig.manager.revoke()
            assert revoked.lease_id is not None  # 回到 NONE
            # revoke 後舊端被記為未證實停止（A20）：先送停止證據，下一回合的併發 acquire 才不會被擋
            assert rig.manager.confirm_quiesced(
                revoked.endpoint_id, revoked.generation, {"kind": "adapter_stop_report", "playing": False, "buffer_flushed": True}
            ) is ConfirmResult.ACCEPTED
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(PresenceCurrentRow)) == 1
    assert count_events(session_factory, "presence.granted") == rounds


def test_concurrent_acquire_and_activate_leaves_single_active(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    barrier = Barrier(len(ENDPOINTS))

    def worker(endpoint_id: str):
        barrier.wait()
        outcome = rig.manager.acquire(endpoint_id, "desktop")
        if outcome.grant is not None:
            rig.manager.confirm_activated(outcome.grant.lease_id)
        return outcome.result

    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
        results = [f.result(timeout=30) for f in [pool.submit(worker, ep) for ep in ENDPOINTS]]
    assert results.count(AcquireResult.GRANTED) == 1
    with session_factory() as s:
        rows = s.scalars(select(PresenceCurrentRow)).all()
        assert len(rows) == 1 and rows[0].status == "ACTIVE"
        assert s.scalar(select(func.count()).select_from(PresenceLeaseRow).where(PresenceLeaseRow.state == "ACTIVE")) == 1


def test_only_one_open_transfer_per_persona(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    assert rig.manager.acquire("ep_desktop").result is AcquireResult.GRANTED
    with pytest.raises(IntegrityError), session_factory.begin() as s:
        s.add(PresenceTransferRow(
            transfer_id="xfer_dup", persona_id=rig.manager.persona_id, to_endpoint_id="ep_x", reason={},
            phase="PREPARING", deadline=rig.clock.now, row_version=1,
        ))


# ---- [A] 首次 acquire 與 ACTIVATING ----

def test_activating_period_gate_rejects_then_confirm_activates(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    outcome = rig.manager.acquire("ep_desktop", "desktop")
    assert outcome.result is AcquireResult.GRANTED and outcome.grant is not None
    auth = outcome.grant.authorization
    view = _current(rig)
    assert view.status is PresenceStatus.NONE and view.lease_id == outcome.grant.lease_id
    assert _transfer_phases(session_factory) == ["ACTIVATING"]
    # ACTIVATING 期間 Output Gate 一律拒絕，且他端 acquire 回 TRANSFER_PENDING
    assert rig.gate.admit(auth) is GateDecision.REJECT_NO_PRESENCE
    assert rig.manager.acquire("ep_discord").result is AcquireResult.TRANSFER_PENDING
    assert rig.manager.authorization() is None
    assert rig.manager.confirm_activated(outcome.grant.lease_id) is ConfirmResult.ACCEPTED
    assert rig.gate.admit(auth) is GateDecision.ADMIT
    assert _current(rig).status is PresenceStatus.ACTIVE and _transfer_phases(session_factory) == ["DONE"]
    assert rig.manager.acquire("ep_discord").result is AcquireResult.BUSY  # 不自動 failover


def test_activation_timeout_goes_recovering_then_recovery_returns_none(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    outcome = rig.manager.acquire("ep_desktop")
    assert outcome.grant is not None
    rig.clock.advance(11)  # 超過 activation_timeout（10 秒）
    assert rig.manager.confirm_activated(outcome.grant.lease_id) is ConfirmResult.STALE
    view = _current(rig)
    assert view.status is PresenceStatus.RECOVERING and view.recovery_reason == "activation_timeout"
    assert _transfer_phases(session_factory) == ["ACTIVATING"]  # phase 停在失敗階段
    assert rig.gate.admit(outcome.grant.authorization) is GateDecision.REJECT_NO_PRESENCE
    assert rig.manager.acquire("ep_discord").result is AcquireResult.NOT_QUIESCED
    assert rig.manager.resolve_recovery("op_admin") is True
    assert _current(rig).status is PresenceStatus.NONE and _transfer_phases(session_factory) == ["ABORTED"]
    again = rig.manager.acquire("ep_discord")
    assert again.result is AcquireResult.GRANTED and again.grant is not None
    assert again.grant.authorization.presence_generation == 2  # 不回退、不重核發舊 generation


def test_acquire_version_conflict(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    version = _current(rig).row_version
    assert rig.manager.acquire("ep_desktop", expected_version=version + 5).result is AcquireResult.VERSION_CONFLICT
    assert rig.manager.acquire("ep_desktop", expected_version=version).result is AcquireResult.GRANTED


def test_events_and_outbox_written_in_same_transaction(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    activate(rig)
    assert count_events(session_factory, "presence.granted") == 1
    assert count_events(session_factory, "presence.activated") == 1
    assert count_outbox(session_factory) == 2


# ---- VC-4-2、P-3：舊 generation／epoch／lease 被拒 ----

def test_stale_lease_rejected_for_output_renew_and_ack(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    old = activate(rig, "ep_desktop")
    rig.manager.revoke()
    assert rig.manager.acquire("ep_discord", "discord").result is AcquireResult.NOT_QUIESCED  # 舊端未證實停止（A20）
    rig.manager.confirm_quiesced("ep_desktop", old.presence_generation, {"kind": "adapter_stop_report", "playing": False, "buffer_flushed": True})
    new_grant = rig.manager.acquire("ep_discord", "discord").grant
    assert new_grant is not None
    rig.manager.confirm_activated(new_grant.lease_id)
    assert new_grant.authorization.presence_generation == old.presence_generation + 1

    assert rig.gate.admit(old) is GateDecision.REJECT_STALE
    assert rig.manager.renew(old.lease_id, old.core_epoch, old.presence_generation, old.fencing_epoch) is RenewResult.STALE
    assert rig.manager.confirm_activated(old.lease_id) is ConfirmResult.STALE
    assert rig.manager.release(old.lease_id, old.core_epoch, old.presence_generation, old.fencing_epoch) is ReleaseResult.STALE
    assert rig.gate.admit(new_grant.authorization) is GateDecision.ADMIT
    assert count_events(session_factory, "output.rejected") == 1  # 被拒紀錄進 journal


@pytest.mark.parametrize("field", ["lease_id", "core_epoch", "generation", "fencing_epoch"])
def test_renew_requires_exact_match(session_factory, tmp_path, field):
    rig = make_rig(session_factory, tmp_path)
    auth = activate(rig)
    args = {
        "lease_id": auth.lease_id, "core_epoch": auth.core_epoch, "generation": auth.presence_generation,
        "fencing_epoch": auth.fencing_epoch,
    }
    args[field] = "lease_other" if field == "lease_id" else args[field] - 1
    assert rig.manager.renew(**args) is RenewResult.STALE
    assert rig.manager.renew(
        auth.lease_id, auth.core_epoch, auth.presence_generation, auth.fencing_epoch
    ) is RenewResult.RENEWED


def test_revoke_then_reconnect_cannot_resume_old_lease(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    old = activate(rig)
    outcome = rig.manager.revoke("global_stop")
    assert outcome.lease_id == old.lease_id and outcome.generation == old.presence_generation
    view = _current(rig)
    assert view.status is PresenceStatus.NONE and view.lease_id is None
    # 重連後以舊 Lease 續約／輸出／確認都不能恢復
    assert rig.manager.renew(old.lease_id, old.core_epoch, old.presence_generation, old.fencing_epoch) is RenewResult.STALE
    assert rig.gate.admit(old) is GateDecision.REJECT_REVOKED
    assert rig.manager.confirm_activated(old.lease_id) is ConfirmResult.STALE
    assert _current(rig).status is PresenceStatus.NONE
    # 必須重新 acquire，且得到新的 Lease 與較大的 generation
    again = rig.manager.acquire("ep_desktop").grant
    assert again is not None and again.lease_id != old.lease_id
    assert again.authorization.presence_generation == old.presence_generation + 1
    assert rig.manager.revoke().lease_id == again.lease_id
    assert rig.manager.revoke().lease_id is None  # 冪等


def test_release_by_owner_allows_other_endpoint_immediately(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    auth = activate(rig, "ep_desktop")
    assert rig.manager.release(auth.lease_id, auth.core_epoch, auth.presence_generation, auth.fencing_epoch) is ReleaseResult.RELEASED
    assert _current(rig).status is PresenceStatus.NONE
    assert rig.manager.acquire("ep_discord", "discord").result is AcquireResult.GRANTED


# ---- Lease 過期 ----

def test_renew_extends_and_expiry_returns_to_none(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    auth = activate(rig)
    rig.clock.advance(20)
    assert rig.manager.renew(auth.lease_id, auth.core_epoch, auth.presence_generation, auth.fencing_epoch) is RenewResult.RENEWED
    rig.clock.advance(20)  # 距上次續約 20 秒，仍有效（若沒續約則已過 40 秒）
    assert rig.gate.admit(auth) is GateDecision.ADMIT
    rig.clock.advance(11)  # 距續約 31 秒 > TTL 30 秒
    assert rig.gate.admit(auth) is GateDecision.REJECT_EXPIRED  # 掃描前 Gate 也以時間直接判定
    assert rig.manager.renew(auth.lease_id, auth.core_epoch, auth.presence_generation, auth.fencing_epoch) is RenewResult.EXPIRED
    view = _current(rig)
    assert view.status is PresenceStatus.NONE and view.lease_id is None
    assert view.unquiesced_endpoint_id == "ep_desktop"
    assert rig.gate.admit(auth) is GateDecision.REJECT_NO_PRESENCE
    # 過期不可續活
    assert rig.manager.renew(auth.lease_id, auth.core_epoch, auth.presence_generation, auth.fencing_epoch) is RenewResult.STALE


def test_expire_due_sweeps_without_other_calls(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    activate(rig)
    assert rig.manager.expire_due() is False
    rig.clock.advance(31)
    assert rig.manager.expire_due() is True and rig.manager.expire_due() is False
    assert _current(rig).status is PresenceStatus.NONE


def test_expired_lease_blocks_other_endpoint_until_quiesced(session_factory, tmp_path):
    """VC-4-4：Lease 過期但舊端未證明已停止，他端 acquire 回 NOT_QUIESCED 並進 RECOVERING。"""
    rig = make_rig(session_factory, tmp_path)
    activate(rig, "ep_desktop")
    rig.clock.advance(35)
    assert rig.manager.acquire("ep_discord", "discord").result is AcquireResult.NOT_QUIESCED
    view = _current(rig)
    assert view.status is PresenceStatus.RECOVERING and view.recovery_reason == "unquiesced_endpoint"
    assert rig.manager.acquire("ep_discord", "discord").result is AcquireResult.NOT_QUIESCED
    assert rig.manager.resolve_recovery("op_admin") is True
    assert rig.manager.acquire("ep_discord", "discord").result is AcquireResult.GRANTED


def test_expired_owner_may_reacquire_itself(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    activate(rig, "ep_desktop")
    rig.clock.advance(35)
    outcome = rig.manager.acquire("ep_desktop")
    assert outcome.result is AcquireResult.GRANTED and outcome.grant is not None
    assert outcome.grant.authorization.presence_generation == 2


def test_resolve_recovery_clears_unquiesced_marker(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    activate(rig, "ep_desktop")
    rig.clock.advance(35)
    assert rig.manager.expire_due() is True
    assert rig.manager.resolve_recovery("op_admin", "adapter_stop_report") is True  # NONE 但標記未證實停止
    assert rig.manager.resolve_recovery("op_admin") is False
    assert rig.manager.acquire("ep_discord", "discord").result is AcquireResult.GRANTED


# ---- core_epoch／fencing_epoch（D26、P-4、P-5）----

def test_core_restart_bumps_epochs_and_closes_old_output(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    old = activate(rig)
    assert (old.core_epoch, old.fencing_epoch) == (1, 1)
    assert json.loads((tmp_path / "host_epoch.json").read_text(encoding="utf-8")) == {
        "fencing_epoch": 1, "core_epoch": 1,
    }

    restarted = PresenceManager(
        session_factory, epoch_store=rig.store, publisher=rig.publisher, clock=rig.clock
    )
    view = restarted.start()
    assert (view.core_epoch, view.fencing_epoch) == (2, 2)
    # 不得直接恢復快照中的 ACTIVE Lease：進 RECOVERING，輸出全關
    assert view.status is PresenceStatus.RECOVERING and view.recovery_reason == "core_restart"
    assert view.lease_id is None
    assert rig.gate.admit(old) is GateDecision.REJECT_STALE
    # 舊 Manager（被取代的 Core）的所有操作都被拒
    assert rig.manager.acquire("ep_desktop").result is AcquireResult.DENIED
    assert rig.manager.renew(old.lease_id, old.core_epoch, old.presence_generation, old.fencing_epoch) is RenewResult.STALE
    assert rig.manager.revoke().lease_id is None
    # 新 Core 查核後恢復，並重新 acquire；舊 epoch 的輸出仍被拒
    assert restarted.acquire("ep_desktop").result is AcquireResult.NOT_QUIESCED
    assert restarted.resolve_recovery("op_admin") is True
    grant = restarted.acquire("ep_desktop").grant
    assert grant is not None
    restarted.confirm_activated(grant.lease_id)
    assert grant.authorization.core_epoch == 2 and grant.authorization.fencing_epoch == 2
    new_gate = OutputGate(
        session_factory, restarted, clock=rig.clock, turn_generation_source=ALLOW_ALL,
        presence_audience=ALLOW_ALL,
    )
    assert new_gate.admit(old) is GateDecision.REJECT_STALE
    assert rig.gate.admit(old) is GateDecision.REJECT_STALE
    assert new_gate.admit(grant.authorization) is GateDecision.ADMIT
    with session_factory() as s:  # 舊 Lease 已被撤銷並記原因
        assert s.get(PresenceLeaseRow, old.lease_id).end_reason == "core_restart"


def test_core_restart_without_active_lease_stays_none(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    restarted = PresenceManager(session_factory, epoch_store=rig.store, clock=rig.clock)
    assert restarted.start().status is PresenceStatus.NONE
    assert restarted.acquire("ep_desktop").result is AcquireResult.GRANTED


def test_epoch_file_bumped_externally_rejects_old_epoch_output(session_factory, tmp_path):
    """DB 還原後由操作員／腳本 +1 epoch 檔，再啟動 Core：舊 epoch 的輸出一律被拒（P-5）。"""
    rig = make_rig(session_factory, tmp_path)
    old = activate(rig)
    rig.store.bump(core=False)  # 模擬 restore 腳本只動 fencing_epoch
    restarted = PresenceManager(session_factory, epoch_store=rig.store, clock=rig.clock)
    view = restarted.start()
    assert view.fencing_epoch == 3 and view.core_epoch == 2
    assert rig.gate.admit(old) is GateDecision.REJECT_STALE


def test_epoch_file_behind_db_refuses_to_start(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    activate(rig)
    lost = HostEpochStore(tmp_path / "lost" / "host_epoch.json")  # 檔案遺失：重新從 0 開始
    with pytest.raises(HostEpochError):
        PresenceManager(session_factory, epoch_store=lost, clock=rig.clock).start()


def test_operations_before_start_are_refused(session_factory, tmp_path):
    manager = PresenceManager(session_factory, epoch_store=HostEpochStore(tmp_path / "e.json"), clock=FakeClock())
    with pytest.raises(PresenceNotStarted):
        manager.core_epoch  # noqa: B018


def test_corrupt_epoch_file_refuses_to_start(session_factory, tmp_path):
    path = tmp_path / "host_epoch.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(HostEpochError):
        PresenceManager(session_factory, epoch_store=HostEpochStore(path), clock=FakeClock()).start()


# ---- DB 約束 ----

def test_active_row_requires_complete_authorization(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    with pytest.raises(IntegrityError), session_factory.begin() as s:
        s.execute(text("UPDATE presence_current SET status='ACTIVE' WHERE persona_id=:p"),
                  {"p": rig.manager.persona_id})


# ---- 審查補強：冪等 acquire、confirm_quiesced、自動撤銷通知、唯讀路徑、DB 守門 ----

def test_acquire_is_idempotent_for_same_endpoint_while_activating(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    first = rig.manager.acquire("ep_desktop").grant
    assert first is not None
    again = rig.manager.acquire("ep_desktop", expected_version=0)  # 重送即使帶舊 version 也回同一份 grant
    assert again.result is AcquireResult.GRANTED and again.grant == first
    assert _current(rig).generation == 1 and count_events(session_factory, "presence.granted") == 1
    assert rig.manager.acquire("ep_discord").result is AcquireResult.TRANSFER_PENDING
    rig.clock.advance(11)  # 逾時後不再回舊 grant
    assert rig.manager.acquire("ep_desktop").result is AcquireResult.NOT_QUIESCED


def test_confirm_quiesced_requires_matching_endpoint_and_generation(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    old = activate(rig, "ep_desktop")
    rig.clock.advance(35)
    assert rig.manager.expire_due() is True  # NONE，標記 ep_desktop 未證實停止
    assert rig.manager.confirm_quiesced("ep_discord", old.presence_generation) is ConfirmResult.STALE
    assert rig.manager.confirm_quiesced("ep_desktop", old.presence_generation - 1) is ConfirmResult.STALE
    assert _current(rig).unquiesced_endpoint_id == "ep_desktop"
    assert rig.manager.confirm_quiesced(
        "ep_desktop", old.presence_generation, {"kind": "adapter_stop_report", "playing": False, "buffer_flushed": True}  # 證據 kind 須在白名單（B2）
    ) is ConfirmResult.ACCEPTED
    assert _current(rig).unquiesced_endpoint_id is None
    assert rig.manager.confirm_quiesced("ep_desktop", old.presence_generation) is ConfirmResult.STALE
    assert rig.manager.acquire("ep_discord", "discord").result is AcquireResult.GRANTED
    assert count_events(session_factory, "presence.recovered") == 1


def test_confirm_quiesced_resolves_core_restart_recovering(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    old = activate(rig, "ep_desktop")
    restarted = PresenceManager(session_factory, epoch_store=rig.store, publisher=rig.publisher, clock=rig.clock)
    assert restarted.start().unquiesced_endpoint_id == "ep_desktop"
    assert restarted.confirm_quiesced("ep_other", old.presence_generation) is ConfirmResult.STALE
    assert restarted.confirm_quiesced(
        "ep_desktop", old.presence_generation, {"kind": "adapter_stop_report", "playing": False, "buffer_flushed": True}
    ) is ConfirmResult.ACCEPTED
    assert restarted.current().status is PresenceStatus.NONE
    assert restarted.acquire("ep_discord", "discord").result is AcquireResult.GRANTED


def test_resolve_recovery_needs_operator_and_emits_recovered_event(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    assert rig.manager.acquire("ep_desktop").grant is not None
    rig.clock.advance(11)
    rig.manager.expire_due()
    assert _current(rig).status is PresenceStatus.RECOVERING
    with pytest.raises(ValueError):
        rig.manager.resolve_recovery("")
    assert rig.manager.resolve_recovery("op_admin", "operator_confirmed") is True
    with session_factory() as s:
        event = s.scalars(select(EventRow).where(EventRow.event_type == "presence.recovered")).one()
    assert event.actor_id == "op_admin" and event.payload["kind"] == "operator_confirmed"
    assert set(event.payload) <= {"lease_id", "status", "reason_code", "kind"}


def _control_rig(session_factory, tmp_path, endpoint="ep_desktop"):
    got: queue.Queue = queue.Queue()
    channel = InProcessControlChannel()
    channel.subscribe(endpoint, got.put)
    return make_rig(session_factory, tmp_path, control=channel), got, channel


def test_expiry_notifies_old_endpoint(session_factory, tmp_path):
    rig, got, _ = _control_rig(session_factory, tmp_path)
    activate(rig)
    rig.clock.advance(35)
    assert rig.manager.expire_due() is True
    assert got.get(timeout=2).kind.value == "revoke"


def test_activation_failure_notifies_old_endpoint(session_factory, tmp_path):
    rig, got, _ = _control_rig(session_factory, tmp_path)
    assert rig.manager.acquire("ep_desktop").grant is not None
    rig.clock.advance(11)
    rig.manager.expire_due()
    assert got.get(timeout=2).kind.value == "revoke"


def test_core_restart_notifies_old_endpoint(session_factory, tmp_path):
    rig, got, channel = _control_rig(session_factory, tmp_path)
    activate(rig)
    restarted = PresenceManager(session_factory, epoch_store=rig.store, clock=rig.clock, control=channel)
    restarted.start()
    assert got.get(timeout=2).kind.value == "revoke"


def test_control_failure_does_not_break_state_transition(session_factory, tmp_path):
    class Boom:
        def subscribe(self, *_a):
            raise AssertionError

        def send(self, *_a, **_k):
            raise RuntimeError("channel down")

    rig = make_rig(session_factory, tmp_path, control=Boom())
    activate(rig)
    rig.clock.advance(35)
    assert rig.manager.expire_due() is True
    assert _current(rig).status is PresenceStatus.NONE


def test_current_read_path_takes_no_row_lock(session_factory, tmp_path):
    """M6：無轉換需求的 current()／authorization() 不等待另一個持有 row lock 的交易。"""
    rig = make_rig(session_factory, tmp_path)
    auth = activate(rig)
    locked, release = threading.Event(), threading.Event()

    def holder():
        with session_factory() as s, s.begin():
            s.execute(text("SELECT 1 FROM presence_current WHERE persona_id=:p FOR UPDATE"),
                      {"p": rig.manager.persona_id})
            locked.set()
            release.wait(10)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert locked.wait(5)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(rig.manager.authorization).result(timeout=3) == auth
    finally:
        release.set()
        t.join(5)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE presence_current SET generation = generation - 1",
        "UPDATE presence_current SET persona_id = 'other'",
        "UPDATE presence_current SET status='RECOVERING', recovery_reason=NULL",
        "UPDATE presence_leases SET generation = 99",
        "UPDATE presence_leases SET endpoint_id = 'ep_x'",
        "DELETE FROM presence_leases",
    ],
)
def test_db_guards_generation_persona_and_lease_identity(session_factory, tmp_path, sql):
    rig = make_rig(session_factory, tmp_path)
    activate(rig)
    with pytest.raises(DBAPIError), session_factory.begin() as s:
        s.execute(text(sql))
    with session_factory.begin() as s:  # 允許的更新不受影響
        s.execute(text("UPDATE presence_leases SET end_reason='x'"))
