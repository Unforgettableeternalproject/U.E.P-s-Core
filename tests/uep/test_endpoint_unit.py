"""CLI 端點行為（不需 DB；時鐘皆為注入的假單調時鐘，不等真時間）。"""
from __future__ import annotations

import threading

import pytest

from tests.uep.endpoint_support import (
    TTL,
    FakeCore,
    Mono,
    intent_for_auth,
    signal,
    wall_now,
)
from uep.adapters.output import SendFailed
from uep.contracts.presence import (
    AcquireResult,
    ConfirmResult,
    GateDecision,
    RenewResult,
)
from uep.endpoint.endpoint import (
    CliEndpoint,
    EndpointRejected,
    PlaybackCancelled,
    ReconnectOutcome,
)
from uep.output.delivery import DeliveryEvidence
from uep.output.intent import ControlKind, ControlScope


def make(**kw):
    core, clock = FakeCore(), Mono()
    log: list[str] = []
    ep = CliEndpoint("ep_cli", core, clock=clock, wall_clock=wall_now, sink=log.append, **kw)
    assert ep.start_lease() is AcquireResult.GRANTED
    return ep, core, clock, log


def intent(core: FakeCore, **kw):
    return intent_for_auth(core.auth(), **kw)


# ---- (1) 控制 handler：只設旗標、冪等、執行緒安全、處理時判斷 generation ----

def test_handler_only_records_and_does_not_stop():
    ep, core, _clock, _log = make()
    ep.on_control(signal(ControlKind.REVOKE))
    assert ep.lease is not None and core.quiesced == []  # handler 本身不停止、不碰網路
    ep.process_controls()
    assert ep.lease is None and len(core.quiesced) == 1


def test_handler_is_idempotent_by_control_id():
    ep, core, _clock, _log = make()
    sig = signal(ControlKind.REVOKE)
    for _ in range(3):
        ep.on_control(sig)
    ep.process_controls()
    ep.on_control(sig)  # 處理後再收到同一個 control_id 也不再生效
    ep.process_controls()
    assert len(core.quiesced) == 1 and ep.ignored_controls == 0


def test_handlers_for_different_kinds_are_thread_safe():
    ep, core, _clock, _log = make()
    barrier = threading.Barrier(3)

    def fire(kind: ControlKind) -> None:
        barrier.wait()
        for _ in range(50):
            ep.on_control(signal(kind))

    threads = [threading.Thread(target=fire, args=(k,)) for k in ControlKind]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ep.process_controls()
    assert ep.lease is None and len(core.quiesced) == 1  # 150 個訊號只造成一次停止與一份證據


def test_stale_generation_signal_ignored_at_processing_time():
    ep, core, _clock, _log = make()
    ep.on_control(signal(ControlKind.REVOKE, generation=core.generation - 1))  # 舊 Lease 的 revoke
    ep.on_control(signal(ControlKind.REVOKE, core_epoch=core.core_epoch - 1))  # 舊 Core
    ep.process_controls()
    assert ep.lease is not None and ep.ignored_controls == 2
    ep.on_control(signal(ControlKind.REVOKE, generation=core.generation))
    ep.process_controls()
    assert ep.lease is None


def test_newer_or_missing_generation_is_applied_fail_safe():
    ep, core, _clock, _log = make()
    ep.on_control(signal(ControlKind.REVOKE, generation=core.generation + 5))
    ep.process_controls()
    assert ep.lease is None
    ep2, _core2, _c, _l = make()
    ep2.on_control(signal(ControlKind.REVOKE, generation=None))
    ep2.process_controls()
    assert ep2.lease is None


def test_older_generation_revoke_before_lease_is_ignored():
    core, clock = FakeCore(), Mono()
    ep = CliEndpoint("ep_cli", core, clock=clock, wall_clock=wall_now)
    ep.on_control(signal(ControlKind.REVOKE, generation=core.generation - 1))  # 前一個 Lease 的 revoke
    ep.process_controls()
    assert ep.start_lease() is AcquireResult.GRANTED and ep.lease is not None


def test_same_generation_revoke_arriving_before_install_must_take_effect():
    """confirm_activated 與安裝之間到達（已處理成無 Lease）的 revoke，安裝後必須生效。"""
    core, clock = FakeCore(), Mono()
    holder = {}
    original = core.confirm_activated

    def confirm(lease_id, generation):
        reply = original(lease_id, generation)
        holder["ep"].on_control(signal(ControlKind.REVOKE, generation=generation))
        holder["ep"].process_controls()  # 此刻端點仍無 Lease：訊號被判為不適用
        return reply

    core.confirm_activated = confirm  # type: ignore[method-assign]
    ep = CliEndpoint("ep_cli", core, clock=clock, wall_clock=wall_now)
    holder["ep"] = ep
    assert ep.start_lease() is AcquireResult.DENIED
    assert ep.lease is None and len(core.quiesced) == 1


def test_newer_core_epoch_revoke_is_applied_unconditionally():
    ep, core, _c, _l = make()
    ep.on_control(signal(ControlKind.REVOKE, core_epoch=core.core_epoch + 1, generation=1))  # generation 較小也照停
    ep.process_controls()
    assert ep.lease is None


def test_stop_cancels_playback_midway_but_keeps_lease():
    holder = {}

    def on_chunk(_intent, chunk):
        if chunk == 1:
            holder["ep"].on_control(signal(ControlKind.STOP, scope=ControlScope.SEGMENT))

    ep, core, _clock, _log = make(on_chunk=on_chunk, chunk_count=lambda _i: 4)
    holder["ep"] = ep
    result = ep.send(intent(core, kind="audio_stream", content_format={"codec": "pcm"}), "dsp_1")
    assert result.evidence is DeliveryEvidence.LOCAL_PLAYBACK
    assert result.played_range == {"from_chunk": 0, "to_chunk": 2, "complete": False}
    assert ep.lease is not None and not ep.playing and core.quiesced == []


def test_turn_stop_for_older_turn_does_not_cancel_newer_turn():
    holder = {}

    def on_chunk(_i, chunk):
        if chunk == 0:  # 舊回合（generation 4）的 stop，不該影響 generation 5 的輸出
            holder["ep"].on_control(signal(ControlKind.STOP, scope=ControlScope.TURN, turn_generation=4))

    ep, core, _c, _l = make(on_chunk=on_chunk, chunk_count=lambda _i: 3)
    holder["ep"] = ep
    result = ep.send(intent(core), "dsp_1")
    assert result.played_range == {"from_chunk": 0, "to_chunk": 3, "complete": True}


# ---- (2) 本地單調時鐘 watchdog（VC-4-4）----

def test_watchdog_stops_without_any_revoke_and_reports_evidence():
    ep, core, clock, log = make()
    assert ep.remaining_ttl() == pytest.approx(TTL)
    clock.advance(TTL + 0.1)  # 沒有 revoke、沒有續約回應：純本地期限
    ep.tick()
    assert ep.lease is None
    old_generation, evidence = core.quiesced[0]
    assert old_generation == core.generation
    assert evidence["kind"] == "adapter_stop_report" and evidence["reason"] == "lease_expired_local"
    assert evidence["buffer_flushed"] is True and any("已停止" in line for line in log)


def test_watchdog_stops_local_buffer_under_network_isolation_and_reports_after_recovery():
    holder = {}

    def on_chunk(_i, chunk):
        if chunk == 1:
            holder["clock"].advance(TTL + 1)  # 播放途中時間走完，且 Core 完全不可達

    ep, core, clock, _log = make(on_chunk=on_chunk, chunk_count=lambda _i: 5)
    holder["clock"] = clock
    core.down = True
    result = ep.send(intent(core, kind="audio_stream", content_format={"codec": "pcm"}), "dsp_1")
    assert result.played_range["complete"] is False and result.played_range["to_chunk"] == 2
    assert ep.lease is None and not ep.playing  # 本地 buffer 已停止
    assert ep.pending_report_count == 1 and core.quiesced == []
    ep.tick()  # 仍斷線：不拋例外、證據保留
    assert ep.pending_report_count == 1
    core.down = False
    ep.tick()
    assert ep.pending_report_count == 1  # 退避中：還沒到重試時間
    clock.advance(2)
    ep.tick()
    assert ep.pending_report_count == 0 and len(core.quiesced) == 1


def test_renew_extends_deadline_from_request_start_time():
    ep, core, clock, _log = make()
    clock.advance(TTL / 3 + 0.1)
    ep.tick()  # 到達續約時間點
    assert "renew" in core.calls
    assert ep.remaining_ttl() == pytest.approx(TTL)
    clock.advance(TTL - 1)
    ep.tick()
    assert ep.lease is not None


def test_renew_failure_while_down_does_not_extend_and_watchdog_still_fires():
    ep, core, clock, _log = make()
    core.down = True
    clock.advance(TTL / 3 + 0.1)
    ep.tick()
    assert ep.lease is not None and ep.remaining_ttl() < TTL
    clock.advance(TTL)
    ep.tick()
    assert ep.lease is None


@pytest.mark.parametrize("result", [RenewResult.STALE, RenewResult.EXPIRED])
def test_renew_rejected_by_core_stops_endpoint(result):
    ep, core, clock, _log = make()
    core.renew_result = result
    clock.advance(TTL / 3 + 0.1)
    ep.tick()
    assert ep.lease is None and len(core.quiesced) == 1


# ---- (3) 二次接納 ----

def test_send_plays_and_reports_complete_local_playback():
    ep, core, _c, _l = make()
    result = ep.send(intent(core), "dsp_1")
    assert result.evidence is DeliveryEvidence.LOCAL_PLAYBACK and result.played_range["complete"] is True
    assert ep.send(intent(core), "dsp_1") is result  # 同 dispatch_id 不重播
    assert ep.lookup("dsp_1").outcome == "found" and ep.lookup("dsp_x").outcome == "undetermined"
    assert ep.lookup_authoritative is False


@pytest.mark.parametrize(("override", "expected"), [
    ({"presence_generation": 2}, GateDecision.REJECT_STALE),
    ({"fencing_epoch": 3}, GateDecision.REJECT_STALE),
    ({"core_epoch": 1}, GateDecision.REJECT_STALE),
    ({"lease_id": "lease_other"}, GateDecision.REJECT_STALE),
    ({"endpoint_id": "ep_other"}, GateDecision.REJECT_STALE),
    ({"persona_id": "other"}, GateDecision.REJECT_STALE),
])
def test_send_rejects_any_stale_tuple_element(override, expected):
    ep, core, _c, _l = make()
    with pytest.raises(EndpointRejected) as err:
        ep.send(intent(core, **override), "dsp_1")
    assert err.value.decision is expected and not ep.playing


def test_send_rejects_when_local_ttl_exhausted_even_if_gate_admitted_earlier():
    ep, core, clock, _l = make()
    stale_ok_intent = intent(core)  # Core Gate 已核可
    clock.advance(TTL + 1)  # Gate -> send 之間本地期限走完，且沒有任何 tick／revoke
    with pytest.raises(EndpointRejected) as err:
        ep.send(stale_ok_intent, "dsp_1")
    assert err.value.decision is GateDecision.REJECT_EXPIRED


def test_send_rejects_after_revoke_arrived_between_gate_and_send():
    ep, core, _c, _l = make()
    admitted = intent(core)
    ep.on_control(signal(ControlKind.REVOKE))  # Gate 放行之後 revoke 才到
    with pytest.raises(EndpointRejected) as err:
        ep.send(admitted, "dsp_1")
    assert err.value.decision is GateDecision.REJECT_REVOKED


def test_send_rejects_expired_valid_until():
    from datetime import timedelta
    ep, core, _c, _l = make()
    with pytest.raises(EndpointRejected) as err:
        ep.send(intent(core, valid_until=wall_now() - timedelta(seconds=1)), "dsp_1")
    assert err.value.decision is GateDecision.REJECT_EXPIRED


def test_each_chunk_is_readmitted_during_playback():
    holder = {}

    def on_chunk(_i, chunk):
        if chunk == 0:
            holder["clock"].advance(TTL + 1)  # 播第一塊時期限到，沒有任何控制訊號

    ep, core, clock, _l = make(on_chunk=on_chunk, chunk_count=lambda _i: 4)
    holder["clock"] = clock
    result = ep.send(intent(core), "dsp_1")
    assert result.played_range == {"from_chunk": 0, "to_chunk": 1, "complete": False}


def test_released_endpoint_rejects_send():
    ep, core, _c, _l = make()
    ep.release()
    with pytest.raises(EndpointRejected) as err:
        ep.send(intent(core), "dsp_1")
    assert err.value.decision is GateDecision.REJECT_NO_PRESENCE


def test_stop_before_first_chunk_raises_cancelled_not_partial():
    holder = {}

    def chunks(_intent):
        holder["ep"].on_control(signal(ControlKind.STOP))  # 播放開始前一刻到達
        return 3

    ep, core, _c, _l = make(chunk_count=chunks)
    holder["ep"] = ep
    with pytest.raises(PlaybackCancelled):
        ep.send(intent(core), "dsp_1")
    assert ep.lease is not None and not ep.playing


# ---- (4) 重連：先取權威快照 ----

def test_reconnect_resumes_when_snapshot_confirms_ownership():
    ep, core, clock, _l = make()
    clock.advance(10)
    assert ep.reconnect() is ReconnectOutcome.RESUMED
    assert core.calls.index("snapshot") < len(core.calls) - 1 and core.calls[-1] == "renew"
    assert ep.remaining_ttl() == pytest.approx(TTL)


def test_reconnect_stops_when_authority_moved_on():
    ep, core, _c, _l = make()
    core.owner = False  # Core 已不認為此端持有
    assert ep.reconnect() is ReconnectOutcome.LOST
    assert ep.lease is None and "renew" not in core.calls[core.calls.index("snapshot"):]


def test_reconnect_when_generation_differs_does_not_renew():
    ep, core, _c, _l = make()
    core.generation += 1  # Core 端已換世代
    assert ep.reconnect() is ReconnectOutcome.LOST and ep.lease is None


def test_reconnect_idle_delivers_pending_report():
    ep, core, _c, _l = make()
    core.down = True
    ep.on_control(signal(ControlKind.REVOKE))
    ep.process_controls()
    assert ep.pending_report_count == 1
    core.down = False
    assert ep.reconnect() is ReconnectOutcome.IDLE and ep.pending_report_count == 0


# ---- (6) 停止證據與 release 補救 ----

def test_stale_confirm_while_core_still_thinks_we_own_falls_back_to_release():
    ep, core, clock, _l = make()
    core.confirm_result = ConfirmResult.STALE  # 本地期限早於 Core 期限：Core 仍是 ACTIVE
    clock.advance(TTL + 0.1)
    ep.tick()
    assert core.released == [core.lease_id] and ep.pending_report_count == 0


def test_stale_confirm_with_unquiesced_marker_keeps_report_for_retry():
    ep, core, clock, _l = make()
    core.confirm_result = ConfirmResult.STALE
    core.owner = False
    core.unquiesced = True
    ep.release()
    assert ep.pending_report_count == 1
    core.confirm_result = ConfirmResult.ACCEPTED
    clock.advance(2)  # 越過退避
    ep.tick()
    assert ep.pending_report_count == 0


def test_release_is_idempotent():
    ep, core, _c, _l = make()
    ep.release()
    ep.release()
    assert len(core.quiesced) == 1


# ---- 背景執行緒（真實 Event 喚醒，不 sleep）----

def test_background_thread_processes_revoke_promptly():
    stopped = threading.Event()
    core, clock = FakeCore(), Mono()
    ep = CliEndpoint(
        "ep_cli", core, clock=clock, wall_clock=wall_now,
        sink=lambda text: stopped.set() if "已停止" in text else None,
    )
    ep.start_lease()
    ep.start()
    try:
        ep.on_control(signal(ControlKind.REVOKE))
        assert stopped.wait(2.0) and ep.lease is None
    finally:
        ep.close()


# ---- 審查修正：M1／M2／J2／J3／N5 ----

def _wait_until(predicate, timeout: float = 3.0) -> bool:
    ticker = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        ticker.wait(0.02)
    return predicate()


def test_concurrent_send_with_same_dispatch_id_plays_once():
    played: list[int] = []
    gate, entered = threading.Event(), threading.Event()

    def on_chunk(_i, chunk):
        played.append(chunk)
        if chunk == 0:
            entered.set()
            assert gate.wait(3.0)

    ep, core, _c, _l = make(on_chunk=on_chunk, chunk_count=lambda _i: 2)
    item = intent(core)
    out: list = []
    first = threading.Thread(target=lambda: out.append(ep.send(item, "dsp_same")))
    first.start()
    assert entered.wait(3.0)
    second = threading.Thread(target=lambda: out.append(ep.send(item, "dsp_same")))
    second.start()
    threading.Event().wait(0.2)  # 讓第二個執行緒卡在播放鎖上
    gate.set()
    first.join(3.0)
    second.join(3.0)
    assert played == [0, 1] and len(out) == 2 and out[0] is out[1]


def test_revoke_drops_lease_atomically_and_evidence_is_honest():
    in_chunk, go = threading.Event(), threading.Event()
    played: list[int] = []

    def on_chunk(_i, chunk):
        played.append(chunk)
        in_chunk.set()
        assert go.wait(3.0)

    ep, core, _c, _l = make(on_chunk=on_chunk, chunk_count=lambda _i: 4)
    outcome: list = []

    def run():
        try:
            outcome.append(ep.send(intent(core), "dsp_1"))
        except SendFailed as exc:
            outcome.append(exc)

    sender = threading.Thread(target=run)
    sender.start()
    assert in_chunk.wait(3.0)
    ep.on_control(signal(ControlKind.REVOKE))
    stopper = threading.Thread(target=ep.process_controls)
    stopper.start()
    assert _wait_until(lambda: ep.lease is None)  # Lease 在播放執行緒仍在 chunk 內時就已原子丟棄
    assert core.quiesced == []  # 證據要等播放執行緒真正離開 chunk
    go.set()
    stopper.join(3.0)
    sender.join(3.0)
    assert played == [0]  # revoke 之後沒有再放行任何 chunk
    evidence = core.quiesced[0][1]
    assert evidence["playing"] is False and evidence["buffer_flushed"] is True


def test_watchdog_fires_while_renew_rpc_is_blocked():
    entered, release = threading.Event(), threading.Event()

    class SlowCore(FakeCore):
        def renew(self, *a):
            entered.set()
            release.wait(3.0)
            return super().renew(*a)

    core, clock = SlowCore(), Mono()
    ep = CliEndpoint("ep_cli", core, clock=clock, wall_clock=wall_now)
    ep.start_lease()
    ep.start()
    try:
        clock.advance(TTL / 3 + 0.1)
        ep._net_wake.set()
        assert entered.wait(3.0)  # 網路執行緒卡在 renew
        clock.advance(TTL)
        ep._wake.set()
        assert _wait_until(lambda: ep.lease is None)  # 本地 watchdog 不受 RPC 影響
    finally:
        release.set()
        ep.close()


def test_stop_arriving_before_send_blocks_that_turn_and_older():
    ep, core, _c, _l = make()
    ep.on_control(signal(ControlKind.STOP, scope=ControlScope.TURN, turn_generation=5))
    ep.process_controls()  # 沒有任何播放中，stop 先到
    with pytest.raises(EndpointRejected) as err:
        ep.send(intent(core, turn_generation=5), "dsp_1")
    assert err.value.decision is GateDecision.REJECT_STALE
    with pytest.raises(EndpointRejected):
        ep.send(intent(core, turn_generation=4), "dsp_2")
    assert ep.send(intent(core, turn_generation=6), "dsp_3").played_range["complete"] is True


def test_reconnect_retry_keeps_lease_when_core_unreachable_again():
    ep, core, _c, _l = make()
    original = core.snapshot

    def snapshot_then_drop():
        snap = original()
        core.down = True  # 快照成功後 Core 又斷線，renew 失敗
        return snap

    core.snapshot = snapshot_then_drop  # type: ignore[method-assign]
    assert ep.reconnect() is ReconnectOutcome.RETRY
    assert ep.lease is not None
