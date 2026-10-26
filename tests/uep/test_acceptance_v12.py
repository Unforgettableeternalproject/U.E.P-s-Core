"""V12 驗收：舊 generation／舊 core_epoch 的輸出與 ACK 被拒（VC-4-2；需 PostgreSQL）。

Desktop 持有 generation=G 的 Lease 並有 3 段待播輸出；Presence 被撤銷後 Discord 取得 G+1。
舊 Desktop 的輸出在三層（Output Gate、DeliveryManager、端點二次接納）與 renew／release／confirm 都被拒；
重連不能恢復已撤銷的權限。Core 重啟（core_epoch+1）後同理。

順序說明：Discord 尚未 ``confirm_activated`` 前，Gate 對舊輸出回 REJECT_REVOKED（Lease 已撤銷）；
Discord 進場後才回 REJECT_STALE（generation 已前進）。兩者皆為拒絕，本檔分開驗證。
"""
from __future__ import annotations

import pytest

from tests.uep.acceptance_p2_support import (
    SpyApi,
    journal_payloads,
    make_world,
    restart_core,
    settle,
)
from tests.uep.endpoint_support import intent_for_auth, signal
from tests.uep.presence_support import intent_for
from uep.contracts.presence import (
    AcquireResult,
    ConfirmResult,
    GateDecision,
    PresenceStatus,
    ReleaseResult,
    RenewResult,
)
from uep.endpoint.endpoint import EndpointRejected, ReconnectOutcome
from uep.output.delivery import DeliveryStatus as S
from uep.output.intent import ControlKind
from uep.output.manager import DeliveryManager

pytestmark = pytest.mark.usefixtures("session_factory")

STALE_OR_REVOKED = {GateDecision.REJECT_STALE, GateDecision.REJECT_REVOKED}


@pytest.fixture(params=["inproc", "socket"])
def world(request, session_factory, tmp_path):
    w = make_world(session_factory, tmp_path, endpoint_ids=("ep_desktop", "ep_discord"), socket=request.param == "socket")
    yield w
    w.close()


def _handover(world):
    """Desktop 取得 G 並留 3 段待播輸出 -> 撤銷 -> Discord 取得 G+1（尚未確認到場）。"""
    desktop, discord = world.endpoint("ep_desktop"), world.endpoint("ep_discord")
    assert desktop.start_lease() is AcquireResult.GRANTED
    old_auth = desktop.lease.auth  # type: ignore[union-attr]
    old_intents = [intent_for(old_auth, sequence=i) for i in range(3)]
    return desktop, discord, old_auth, old_intents


def _discord_takes_over(world, discord):
    """撤銷後 Discord acquire（直接走 RPC 以便控制是否 confirm）。"""
    reply = world.api("ep_discord").acquire("discord")
    assert reply.result is AcquireResult.GRANTED and reply.auth is not None
    return reply.auth


def test_v12_old_generation_outputs_rejected_at_gate_delivery_and_endpoint(world):
    """V12／VC-4-2：舊輸出在 Gate、DeliveryManager、端點二次接納三層都被拒，且 journal 有拒絕紀錄。"""
    sf = world.rig.session_factory
    desktop, discord, old_auth, old_intents = _handover(world)
    world.rig.manager.revoke("takeover")
    new_auth = _discord_takes_over(world, discord)
    assert new_auth.presence_generation == old_auth.presence_generation + 1
    # Discord 尚未到場：舊輸出被撤銷拒絕
    assert world.rig.gate.admit_intent(old_intents[0]) is GateDecision.REJECT_REVOKED
    assert world.api("ep_discord").confirm_activated(new_auth.lease_id, new_auth.presence_generation).result is ConfirmResult.ACCEPTED
    # Discord 進場：generation 已前進 -> STALE
    for intent in old_intents:
        assert world.rig.gate.admit_intent(intent) is GateDecision.REJECT_STALE

    sent: list = []
    original = desktop.send
    desktop.send = lambda i, d: sent.append(d) or original(i, d)  # type: ignore[method-assign]
    manager = DeliveryManager(sf, desktop, gate=world.rig.gate)
    for intent in old_intents:
        record = manager.dispatch(intent)
        assert record.status is S.REJECTED and record.failure["code"] == GateDecision.REJECT_STALE.value
    assert sent == []  # 三段全部被 Gate 擋下，沒有任何一段到端點

    # 端點二次接納：即使繞過 Gate 直接送，Desktop 也不得復活播放
    assert settle(desktop)
    for index, intent in enumerate(old_intents):
        with pytest.raises(EndpointRejected) as caught:
            original(intent, f"dsp_direct_{index}")
        assert caught.value.decision in STALE_OR_REVOKED
    assert desktop.playing is False

    rejected = journal_payloads(sf, "output.rejected")
    stale = [p for p in rejected if p.get("reason_code") == GateDecision.REJECT_STALE.value]
    assert len(stale) >= 6  # 3 次 admit_intent ＋ 3 次 dispatch 的 Gate 檢查
    assert {p["ref_id"] for p in stale} == {i.output_id for i in old_intents}


def test_v12_old_lease_renew_release_confirm_are_all_stale(world):
    """舊 Lease 的 renew／release／confirm_activated／confirm_quiesced 被拒，且不影響新 owner。"""
    _desktop, discord, old_auth, _ = _handover(world)
    world.rig.manager.revoke("takeover")
    new_auth = _discord_takes_over(world, discord)
    api = world.api("ep_desktop")
    world.api("ep_discord").confirm_activated(new_auth.lease_id, new_auth.presence_generation)

    a = old_auth
    assert api.renew(a.lease_id, a.core_epoch, a.presence_generation, a.fencing_epoch).result is RenewResult.STALE
    assert api.release(a.lease_id, a.core_epoch, a.presence_generation, a.fencing_epoch) is ReleaseResult.STALE
    assert api.confirm_activated(a.lease_id, a.presence_generation).result is ConfirmResult.STALE
    assert api.confirm_quiesced(a.presence_generation, {"kind": "adapter_stop_report"}) is ConfirmResult.STALE
    # 用「Discord 的」lease_id 冒領：Service 只認核發對象，Desktop 無法續約別人的 Lease
    n = new_auth
    assert api.renew(n.lease_id, n.core_epoch, n.presence_generation, n.fencing_epoch).result is RenewResult.STALE
    assert api.release(n.lease_id, n.core_epoch, n.presence_generation, n.fencing_epoch) is ReleaseResult.STALE
    view = world.rig.manager.current()
    assert view.status is PresenceStatus.ACTIVE and view.endpoint_id == "ep_discord" and view.lease_id == n.lease_id
    assert world.rig.gate.admit_intent(intent_for(new_auth)) is GateDecision.ADMIT  # 新 owner 完全不受影響


def test_v12_reconnect_cannot_restore_revoked_authority(world):
    """Desktop 漏收 revoke 後重連：快照顯示非 owner -> LOST；之後舊輸出被拒，只有新 acquire 才能拿回權限。"""
    desktop, discord, old_auth, old_intents = _handover(world)
    world.rig.manager.revoke("takeover")
    new_auth = _discord_takes_over(world, discord)
    world.api("ep_discord").confirm_activated(new_auth.lease_id, new_auth.presence_generation)
    # Desktop 沒處理到 revoke（本地 Lease 仍在）-> 重連同步快照
    outcome = desktop.reconnect()
    assert outcome in (ReconnectOutcome.LOST, ReconnectOutcome.IDLE)
    assert desktop.lease is None
    assert desktop.reconnect() is ReconnectOutcome.IDLE  # 再重連不會憑空復活
    with pytest.raises(EndpointRejected):
        desktop.send(old_intents[0], "dsp_after_reconnect")
    assert world.rig.gate.admit_intent(old_intents[0]) is GateDecision.REJECT_STALE
    # Discord 仍持有時 Desktop 重新 acquire：BUSY
    assert desktop.start_lease() is AcquireResult.BUSY and desktop.lease is None
    # Discord 釋放後 Desktop 以新 acquire 取得新 generation；舊輸出仍然是舊的
    assert world.api("ep_discord").release(
        new_auth.lease_id, new_auth.core_epoch, new_auth.presence_generation, new_auth.fencing_epoch
    ) is ReleaseResult.RELEASED
    assert desktop.start_lease() is AcquireResult.GRANTED
    fresh = desktop.lease.auth  # type: ignore[union-attr]
    assert fresh.presence_generation == old_auth.presence_generation + 2 and fresh.lease_id != old_auth.lease_id
    assert world.rig.gate.admit_intent(old_intents[1]) is GateDecision.REJECT_STALE
    assert world.rig.gate.admit_intent(intent_for(fresh)) is GateDecision.ADMIT


def test_v12_missed_revoke_stale_renew_stops_endpoint(session_factory, tmp_path):
    """端點完全沒收到 revoke（控制通道漏送）：下一次續約被 Core 回 STALE，端點自行停止，不能繼續持有。"""
    world = make_world(session_factory, tmp_path, endpoint_ids=("ep_desktop", "ep_discord"), attach_control=False)
    try:
        desktop, discord, _old_auth, _ = _handover(world)
        world.rig.manager.revoke("takeover")
        new_auth = _discord_takes_over(world, discord)
        world.api("ep_discord").confirm_activated(new_auth.lease_id, new_auth.presence_generation)
        assert desktop.lease is not None  # 漏收：本地仍以為持有
        assert desktop.renew_once() is RenewResult.STALE
        assert desktop.lease is None and desktop.playing is False
        assert world.rig.manager.current().endpoint_id == "ep_discord"
    finally:
        world.close()


def test_v12_stale_generation_signals_cannot_stop_newer_lease_but_newer_epoch_can(session_factory, tmp_path):
    """控制訊號也有 fence：舊 generation 的 revoke 不傷新 Lease；較新 core_epoch 的 revoke 無條件生效。"""
    world = make_world(session_factory, tmp_path, endpoint_ids=("ep_desktop",))
    try:
        ep = world.endpoint("ep_desktop")
        assert ep.start_lease() is AcquireResult.GRANTED
        auth = ep.lease.auth  # type: ignore[union-attr]
        ep.on_control(signal(ControlKind.REVOKE, generation=auth.presence_generation - 1, core_epoch=auth.core_epoch))
        ep.process_controls()
        assert ep.lease is not None and ep.ignored_controls == 1  # 舊 generation：忽略
        ep.on_control(signal(ControlKind.REVOKE, generation=0, core_epoch=auth.core_epoch + 1))
        ep.process_controls()
        assert ep.lease is None  # 較新 epoch：無條件停
    finally:
        world.close()


# ---- Core 重啟：舊 core_epoch ----

@pytest.fixture
def inproc_world(session_factory, tmp_path):
    w = make_world(session_factory, tmp_path, endpoint_ids=("ep_desktop", "ep_discord"))
    yield w
    w.close()


def test_v12_old_core_epoch_outputs_and_acks_rejected_after_restart(inproc_world, session_factory):
    """Core 重啟（core_epoch+1）：舊 epoch 的輸出被新舊兩個 Gate 拒、舊 Manager 全面失效、舊授權不可 ACK。"""
    world = inproc_world
    old_rig = world.rig
    desktop = world.endpoint("ep_desktop")  # 不啟動背景執行緒：由測試手動處理控制訊號，避免與斷言競態
    assert desktop.start_lease() is AcquireResult.GRANTED
    old_auth = desktop.lease.auth  # type: ignore[union-attr]
    old_intent = intent_for(old_auth)
    assert old_rig.gate.admit_intent(old_intent) is GateDecision.ADMIT

    new_manager, new_gate = restart_core(world)
    assert new_manager.core_epoch == old_auth.core_epoch + 1
    view = new_manager.current()
    assert view.status is PresenceStatus.RECOVERING and view.recovery_reason == "core_restart"
    assert view.unquiesced_endpoint_id == "ep_desktop"
    assert view.generation == old_auth.presence_generation  # 不回退，也不被新核發重用

    # 輸出：新 Gate（epoch 不符）與舊 Gate（綁舊 Manager，已被取代）都拒
    assert new_gate.admit_intent(old_intent) is GateDecision.REJECT_STALE
    assert old_rig.gate.admit_intent(old_intent) is GateDecision.REJECT_STALE
    # 同 generation、新 epoch 但仍 RECOVERING：同樣不放行
    assert new_gate.admit_intent(intent_for(old_auth, core_epoch=new_manager.core_epoch,
                                            fencing_epoch=new_manager.fencing_epoch)) is not GateDecision.ADMIT
    # ACK：新 Manager 對舊 epoch 的 renew／release／confirm_activated 一律 STALE
    a = old_auth
    assert new_manager.renew(a.lease_id, a.core_epoch, a.presence_generation, a.fencing_epoch) is RenewResult.STALE
    assert new_manager.release(a.lease_id, a.core_epoch, a.presence_generation, a.fencing_epoch) is ReleaseResult.STALE
    assert new_manager.confirm_activated(a.lease_id, generation=a.presence_generation) is ConfirmResult.STALE
    # 舊 Manager（殘存行程）所有寫入都被拒，不得動 DB
    old = old_rig.manager
    assert old.acquire("ep_discord", "discord").result is AcquireResult.DENIED
    assert old.renew(a.lease_id, a.core_epoch, a.presence_generation, a.fencing_epoch) is RenewResult.STALE
    assert old.release(a.lease_id, a.core_epoch, a.presence_generation, a.fencing_epoch) is ReleaseResult.STALE
    assert old.confirm_quiesced("ep_desktop", a.presence_generation, {"kind": "x"}) is ConfirmResult.STALE
    assert old.revoke("zombie").lease_id is None
    assert new_manager.current().status is PresenceStatus.RECOVERING  # 舊行程什麼都沒改到

    # Desktop 端點收到重啟時的 revoke（舊 epoch 的 fence）-> 停止；晚到的舊輸出被端點拒
    assert settle(desktop)
    with pytest.raises(EndpointRejected):
        desktop.send(old_intent, "dsp_after_restart")
    # 重連不能恢復：快照已不是 owner
    assert desktop.reconnect() is ReconnectOutcome.IDLE and desktop.lease is None


def test_v12_after_restart_recovery_then_new_epoch_grant_keeps_old_generation_dead(inproc_world):
    """重啟後：舊端證明停止 -> 新端取得新 epoch、generation 只 +1；舊 epoch 的輸出與 ACK 永遠過不了。"""
    world = inproc_world
    desktop, discord = world.endpoint("ep_desktop"), world.endpoint("ep_discord")
    desktop.start_lease()
    old_auth = desktop.lease.auth  # type: ignore[union-attr]
    old_intent = intent_for(old_auth)
    new_manager, new_gate = restart_core(world)

    assert world.api("ep_discord").acquire("discord").result is AcquireResult.NOT_QUIESCED  # 舊端未證實停止
    assert settle(desktop) and desktop.pending_report_count == 0  # 證據回到新 Core
    spy: SpyApi = world.api("ep_desktop")
    assert spy.quiesced and spy.quiesced[0][0] == old_auth.presence_generation
    assert new_manager.current().status is PresenceStatus.NONE

    assert discord.start_lease() is AcquireResult.GRANTED
    fresh = discord.lease.auth  # type: ignore[union-attr]
    assert fresh.core_epoch == old_auth.core_epoch + 1 and fresh.presence_generation == old_auth.presence_generation + 1
    assert new_gate.admit_intent(old_intent) is GateDecision.REJECT_STALE
    assert new_gate.admit_intent(intent_for(fresh)) is GateDecision.ADMIT
    # 舊 epoch 的 confirm_quiesced 證據重送：generation 已前進，不得再清任何標記
    assert new_manager.confirm_quiesced("ep_desktop", old_auth.presence_generation, {"kind": "replay"}) is ConfirmResult.STALE
    assert new_manager.current().status is PresenceStatus.ACTIVE


def test_v12_stale_gate_input_each_field_alone_is_rejected(session_factory, tmp_path):
    """授權六元組任一欄位落後即為舊授權：generation／core_epoch／fencing_epoch／lease_id／endpoint 各改一欄。"""
    world = make_world(session_factory, tmp_path, endpoint_ids=("ep_desktop",))
    try:
        ep = world.endpoint("ep_desktop")
        ep.start_lease()
        auth = ep.lease.auth  # type: ignore[union-attr]
        gate = world.rig.gate
        assert gate.admit_intent(intent_for_auth(auth)) is GateDecision.ADMIT
        for field, value in (
            ("presence_generation", auth.presence_generation - 1),
            ("presence_generation", auth.presence_generation + 1),
            ("core_epoch", auth.core_epoch - 1),
            ("fencing_epoch", auth.fencing_epoch - 1),
            ("lease_id", "lease_not_mine"),
            ("endpoint_id", "ep_discord"),
        ):
            decision = gate.admit_intent(intent_for_auth(auth, **{field: value}))
            assert decision is GateDecision.REJECT_STALE, (field, value, decision)
    finally:
        world.close()
