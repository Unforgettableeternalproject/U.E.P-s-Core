"""T-19：T-17／T-18 審查轉入的 8 項，逐項以測試實證現況（需 PostgreSQL，除非另述）。

每個測試的 docstring 開頭標明判定：
- 【缺陷（已修）】確認為缺陷且修法小，已修並由此測試防回歸。
- 【缺陷（回報）】確認為缺陷但修法大或需設計決定，只回報；測試釘住現況（標明為已知缺口）。
- 【符合設計】行為符合現行設計，測試防止無意改動。
- 【設計取捨】行為如設計文件所述，但值得登記風險。
"""
from __future__ import annotations

import dataclasses
import json
import socket
import threading
from datetime import timedelta

import pytest

import uep.endpoint.endpoint as endpoint_module
from tests.uep.acceptance_p2_support import SECRETS, TTL, WAIT, make_world, wait_until
from tests.uep.endpoint_support import FakeCore, Mono, intent_for_auth, signal, wall_now
from tests.uep.presence_support import activate, intent_for, make_rig
from tests.uep.state_support import activate_endpoint, make_stack
from uep.contracts.presence import (
    AcquireDenyReason,
    AcquireResult,
    GateDecision,
    PresenceStatus,
)
from uep.control.socket_transport import SocketControlClient
from uep.endpoint.auth import EndpointAuthError, RequestVerifier, SignedRequest
from uep.endpoint.endpoint import CliEndpoint, EndpointRejected, ReconnectOutcome
from uep.endpoint.protocol import RpcCoreApi
from uep.endpoint.service import EndpointService
from uep.fakes.tool import FakeToolExecutor
from uep.output.intent import ControlKind, ControlScope
from uep.presence.manager import AcquireOutcome
from uep.stop.service import make_flag_blocker
from uep.tasks.manager import TaskManager, TaskPolicy
from uep.tasks.presence_bridge import PresenceTaskBridge
from uep.tasks.states import InterruptPolicy, RunStatus

pytestmark = pytest.mark.usefixtures("session_factory")

DESKTOP = "ep_desktop"


def _world(session_factory, tmp_path, **kw):
    return make_world(session_factory, tmp_path, endpoint_ids=(DESKTOP, "ep_discord"), **kw)


def _elapse(world, seconds: float) -> None:
    world.mono.advance(seconds)
    world.rig.clock.advance(seconds)


# ---- (1) 停止證據只在播放真的停止後才送 confirm_quiesced ----

def test_item1_stop_evidence_is_held_while_sink_still_playing_then_sent_with_playing_false(
    session_factory, tmp_path, monkeypatch
):
    """【缺陷（已修）】watchdog 到期時播放執行緒仍卡在 chunk 內（playing=True）：不得送 confirm_quiesced，
    否則 Core 會放行新端、舊端 sink 卻還在出聲。修法：證據保留重試，待 chunk 結束後以 playing=False 送出。"""
    monkeypatch.setattr(endpoint_module, "_IDLE_WAIT", 0.05)  # 縮短「等播放執行緒離開 chunk」的真時間
    entered, release = threading.Event(), threading.Event()

    def on_chunk(_intent, chunk):
        if chunk == 0:
            entered.set()
            assert release.wait(WAIT)  # sink 卡住（模擬 writer 阻塞）

    world = _world(session_factory, tmp_path, on_chunk=on_chunk, chunk_count=lambda _i: 3)
    try:
        desktop = world.endpoint(DESKTOP)
        spy = world.api(DESKTOP)
        desktop.start_lease()
        intent = intent_for_auth(desktop.lease.auth)  # type: ignore[union-attr]
        outcome: list = []

        def play():
            try:
                outcome.append(desktop.send(intent, "dsp_blocked"))
            except Exception as exc:  # noqa: BLE001
                outcome.append(exc)

        worker = threading.Thread(target=play, daemon=True)
        worker.start()
        assert entered.wait(WAIT)
        _elapse(world, TTL + 1)
        desktop.local_tick()  # watchdog 到期：本地丟棄 Lease，但 sink 仍卡在 chunk 0
        assert desktop.lease is None and desktop.playing is True
        # 還在播放：證據不可送出，Core 不得放行新端
        assert spy.quiesced == [], "playing=True 的停止證據不應送出（缺陷）"
        assert desktop.pending_report_count == 1
        assert world.api("ep_discord").acquire("discord").result is AcquireResult.NOT_QUIESCED
        # sink 放開、播放執行緒離開 chunk：證據以 playing=False 補送，新端才可啟用
        release.set()
        worker.join(WAIT)
        assert not worker.is_alive()
        _elapse(world, 2)  # 越過退避
        desktop.tick()
        assert desktop.pending_report_count == 0
        (_generation, evidence), = spy.quiesced
        assert evidence["playing"] is False and evidence["buffer_flushed"] is True
        assert world.api("ep_discord").acquire("discord").result is AcquireResult.GRANTED
    finally:
        release.set()
        world.close()


# ---- (2) 無 generation 的 revoke 在 start_lease 空窗內 ----

class _RevokeDuringActivation(FakeCore):
    """confirm_activated 回覆前，端點恰好收到並處理了一則 revoke。"""

    def __init__(self, revoke_signal) -> None:
        super().__init__()
        self.endpoint: CliEndpoint | None = None
        self.revoke_signal = revoke_signal

    def confirm_activated(self, lease_id, generation):
        reply = super().confirm_activated(lease_id, generation)
        assert self.endpoint is not None
        self.endpoint.on_control(self.revoke_signal)
        self.endpoint.process_controls()  # 此刻端點尚無 Lease：訊號在這裡被處理
        return reply


def test_item2_revoke_without_generation_is_lost_in_start_lease_window():
    """【缺陷（回報）】無 generation 的 revoke 若在 start_lease 的「Core 已啟用、端點尚未 install」空窗被處理，
    會被丟掉（沒有 Lease 可停、也不記高水位），之後 Lease 照常安裝。有帶 generation 者則被高水位攔下（對照）。
    未就地修：把「無 generation」當無限大高水位會誤殺同 epoch 之後所有合法新 Lease（過度封鎖）；
    in-tree 送端（Manager._queue_revoke、GlobalStop._fence）都帶 generation，實際風險低，且 Core 端
    renew 回 STALE 會在一個心跳週期內收斂。"""
    for gen, expect in ((None, AcquireResult.GRANTED), (3, AcquireResult.DENIED)):
        core = _RevokeDuringActivation(signal(ControlKind.REVOKE, generation=gen))
        ep = CliEndpoint("ep_cli", core, clock=Mono(), wall_clock=wall_now)
        core.endpoint = ep
        assert ep.start_lease() is expect, f"generation={gen}"
        assert (ep.lease is not None) is (expect is AcquireResult.GRANTED)


def test_item2_revoke_without_generation_pending_in_queue_is_honoured():
    """對照：無 generation 的 revoke 只要在 Lease 安裝後才被處理，就會照停（fail-safe）。"""
    core = FakeCore()
    ep = CliEndpoint("ep_cli", core, clock=Mono(), wall_clock=wall_now)
    assert ep.start_lease() is AcquireResult.GRANTED
    ep.on_control(signal(ControlKind.REVOKE, generation=None))
    ep.process_controls()
    assert ep.lease is None


# ---- (3) STOP 先到、輸出後到 ----

def test_item3_turn_stop_before_send_blocks_that_turn_but_not_newer_turn(session_factory, tmp_path):
    """【符合設計】Lease 內先收到 turn=5 的 STOP，之後才 send 的 turn<=5 輸出被擋（STALE）、turn=6 放行。"""
    world = _world(session_factory, tmp_path)
    try:
        ep = world.endpoint(DESKTOP)
        ep.start_lease()
        auth = ep.lease.auth  # type: ignore[union-attr]
        ep.on_control(signal(
            ControlKind.STOP, scope=ControlScope.TURN, generation=auth.presence_generation,
            core_epoch=auth.core_epoch, turn_generation=5,
        ))
        ep.process_controls()
        with pytest.raises(EndpointRejected) as caught:
            ep.send(intent_for_auth(auth, turn_generation=5), "dsp_t5")
        assert caught.value.decision is GateDecision.REJECT_STALE
        with pytest.raises(EndpointRejected):
            ep.send(intent_for_auth(auth, turn_generation=4), "dsp_t4")
        assert ep.send(intent_for_auth(auth, turn_generation=6), "dsp_t6").dispatch_id == "dsp_t6"
    finally:
        world.close()


def test_item3_stop_boundaries_no_lease_and_process_scope(session_factory, tmp_path):
    """【設計取捨】兩個邊界：(a) 沒有 Lease 時收到的 TURN STOP 被忽略，且 start_lease 會重置回合停止紀錄，
    不跨 Lease 保留——舊回合輸出由 Core 的 Output Gate（turn_generation_source）擋，不靠端點記憶；
    (b) scope=process 的 STOP 只取消「正在播」的輸出，不預先封鎖之後才到的輸出。"""
    world = _world(session_factory, tmp_path)
    try:
        ep = world.endpoint(DESKTOP)
        ep.on_control(signal(ControlKind.STOP, scope=ControlScope.TURN, generation=0, core_epoch=1, turn_generation=5))
        ep.process_controls()
        assert ep.ignored_controls == 1  # (a) 無 Lease：忽略
        ep.start_lease()
        auth = ep.lease.auth  # type: ignore[union-attr]
        assert ep.send(intent_for_auth(auth, turn_generation=5), "dsp_after_stop").dispatch_id  # 端點不擋（Core Gate 負責）
        # (b) process 範圍 STOP 沒有正在播的輸出可取消，之後的輸出照常播放
        ep.on_control(signal(
            ControlKind.STOP, scope=ControlScope.PROCESS, generation=auth.presence_generation, core_epoch=auth.core_epoch
        ))
        ep.process_controls()
        assert ep.send(intent_for_auth(auth, turn_generation=7), "dsp_after_proc_stop").dispatch_id
    finally:
        world.close()


def test_item3_core_gate_blocks_old_turn_generation(session_factory, tmp_path):
    """對照 (3)(a)：回合 generation 已前進時，Core Output Gate 擋下舊回合輸出（STALE）——端點不重複承擔。"""
    rig = make_rig(session_factory, tmp_path, turn_generation_source=lambda _turn: 6)
    auth = activate(rig)
    assert rig.gate.admit_intent(intent_for(auth, turn_generation=5)) is GateDecision.REJECT_STALE
    assert rig.gate.admit_intent(intent_for(auth, turn_generation=6)) is GateDecision.ADMIT


# ---- (4) 端點被停止後不自動重 acquire ----

def test_item4_endpoint_never_reacquires_by_itself_after_being_stopped(session_factory, tmp_path):
    """【符合設計（取捨）】revoke／watchdog 停止後，端點的 tick、reconnect 都不會自行 acquire；
    要不要重取是上層（使用者／Presence Planner）的決定，避免與全域停止或新 owner 對打。"""
    world = _world(session_factory, tmp_path)
    try:
        ep = world.endpoint(DESKTOP)
        spy = world.api(DESKTOP)
        ep.start_lease()
        world.rig.manager.revoke("takeover")
        for _ in range(20):
            _elapse(world, 5)
            ep.tick()
        assert ep.lease is None
        assert ep.reconnect() is ReconnectOutcome.IDLE
        assert spy.calls.count("acquire") == 1  # 只有最初那一次
        assert world.rig.manager.current().status is PresenceStatus.NONE
    finally:
        world.close()


# ---- (5) supervisor 與手動 reconnect 並行 connect ----

def test_item5_concurrent_connect_calls_are_serialized_and_leave_one_working_connection(session_factory, tmp_path):
    """【缺陷（已修）】同一個 SocketControlClient 被多執行緒同時 connect（背景 supervisor 與手動 reconnect 的情境）：
    修前各執行緒互相關閉對方的 wire，加上伺服器 re-hello 會關掉較早的連線，偶發 ConnectionError('hello 未獲回應')
    （實測 60 輪 × 8 執行緒約 46 次）。修法：connect 以鎖序列化。最終須只剩一條可用連線、無洩漏的讀取執行緒。"""
    world = _world(session_factory, tmp_path, socket=True)
    try:
        client = world.clients[DESKTOP]

        def readers() -> int:
            return sum(1 for t in threading.enumerate() if t.name == f"UepControlClient[{DESKTOP}]" and t.is_alive())

        for _round in range(5):
            barrier = threading.Barrier(8)
            errors: list[BaseException] = []

            def hammer(barrier=barrier, errors=errors):
                try:
                    barrier.wait(5)
                    client.connect()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=hammer, daemon=True) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(15)
            assert errors == [], errors
            assert wait_until(lambda: readers() == 1, 5), f"洩漏的讀取執行緒：{readers()}"
            assert client.connected and world.server.connected(DESKTOP)  # type: ignore[union-attr]
            assert client.api.snapshot().owns is False  # RPC 可用
        got: list = []
        client._on_control = got.append  # type: ignore[attr-defined]
        world.server.send(signal(ControlKind.REVOKE), endpoint_id=DESKTOP)  # type: ignore[union-attr]
        assert wait_until(lambda: len(got) == 1)  # 最後一條連線收得到 Core 推送
    finally:
        world.close()


# ---- (6) bridge／DENIED／pause 與 interrupt_policy ----

BOUND = TaskPolicy(presence_bound=True, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_b")
NON_INTERRUPTIBLE = TaskPolicy(presence_bound=True, interrupt_policy=InterruptPolicy.NON_INTERRUPTIBLE, policy_ref="pol_n")
SCOPE = "scope_private_owner"


def _bridge_stack(session_factory, tmp_path):
    stack = make_stack(session_factory, tmp_path)
    tasks = TaskManager(
        session_factory, stack.publisher, {"fake": FakeToolExecutor()},
        presence_check=lambda persona: stack.manager.current().status is PresenceStatus.ACTIVE,
    )
    bridge = PresenceTaskBridge(stack.bus, tasks, lambda: stack.manager.current().status)
    return stack, tasks, bridge


def _running(tasks, policy):
    task = tasks.create_task(persona_id="uep", goal="g", owner_actor_id="owner", scope_ref=SCOPE, policy=policy)
    tasks.start_run(task.task_id, persona_version=1, policy_version=1)
    return task


def test_item6a_bridge_alone_does_not_retry_until_next_event_or_reconcile(session_factory, tmp_path):
    """橋接本身不會自行重試：處理失敗只計 failures，presence_bound Task 保持 RUNNING，直到下一個 presence 事件
    或有人呼叫 reconcile()。T-23 起由組裝層的 PresenceMaintenance 定期呼叫 reconcile 補上
    （見 test_presence_maintenance.py）；這裡釘住「橋接單獨使用時」的行為。"""
    stack, tasks, bridge = _bridge_stack(session_factory, tmp_path)
    bridge.start()
    stack.bus.start()
    try:
        activate_endpoint(stack.manager)
        task = _running(tasks, BOUND)
        real = tasks.pause_presence_bound
        calls = {"n": 0}

        def flaky(persona_id, reason=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("暫時性失敗")
            return real(persona_id, reason) if reason is not None else real(persona_id)

        tasks.pause_presence_bound = flaky  # type: ignore[method-assign]
        stack.manager.revoke("leave")
        assert wait_until(lambda: bridge.failures == 1)
        # 沒有任何機制再試：Task 仍在跑，而 Presence 早已離場
        assert wait_until(lambda: False, 0.5) is False
        assert tasks.get_task(task.task_id).status == RunStatus.RUNNING.value
        assert stack.manager.current().status is PresenceStatus.NONE
        assert bridge.reconcile() == [task.task_id]  # 手動 reconcile 才補上
    finally:
        stack.bus.stop()


def test_item6b_denied_carries_reason_for_global_stop_and_superseded_core(session_factory, tmp_path):
    """【T-23 已修】DENIED 帶原因碼：旗標成立為 global_stop_engaged（端點不重試），
    被較新 Core 取代為 superseded_core；其餘結果碼沒有原因。"""
    stack = make_stack(session_factory, tmp_path)
    assert "reason" in {f.name for f in dataclasses.fields(AcquireOutcome)}
    # 情況 A：全域停止旗標
    stack.stop.engage()
    flagged = stack.manager.acquire("ep_desktop", "desktop")
    assert flagged.result is AcquireResult.DENIED and flagged.reason is AcquireDenyReason.GLOBAL_STOP_ENGAGED
    stack.stop.disengage()
    # 情況 B：被取代的舊 Manager
    stack.new_manager()
    superseded = stack.manager.acquire("ep_desktop", "desktop")
    assert superseded.result is AcquireResult.DENIED and superseded.reason is AcquireDenyReason.SUPERSEDED_CORE
    assert flagged.grant is None and superseded.grant is None
    assert make_flag_blocker("uep")  # 旗標檢查器本身存在（語意由 GlobalStop 負責）


def test_item6c_pause_presence_bound_ignores_interrupt_policy(session_factory, tmp_path):
    """【設計取捨】presence 離場時，連 interrupt_policy=NON_INTERRUPTIBLE 的 presence_bound Task 也被暫停——
    暫停只改 Task／Run 狀態，已提交的 EXECUTING action 仍由 worker 完成（docstring 與 P-10 所述）。
    此測試釘住現況；若日後要讓 NON_INTERRUPTIBLE 延後到 checkpoint 再暫停，這裡會先紅。"""
    stack, tasks, bridge = _bridge_stack(session_factory, tmp_path)
    activate_endpoint(stack.manager)
    interruptible = _running(tasks, BOUND)
    pinned = _running(tasks, NON_INTERRUPTIBLE)
    assert tasks.get_task(pinned.task_id).interrupt_policy == InterruptPolicy.NON_INTERRUPTIBLE.value
    stack.manager.revoke("leave")
    assert set(bridge.reconcile()) == {interruptible.task_id, pinned.task_id}
    assert tasks.get_task(pinned.task_id).status == RunStatus.PAUSED.value


# ---- (7) EndpointService 時鐘須與 PresenceManager 共用 ----

def test_item7_mismatched_service_clock_corrupts_ttl_and_endpoint_flaps(session_factory, tmp_path):
    """【設計取捨】EndpointService 的 clock 與 PresenceManager 不同時，回給端點的 ttl_seconds 會整個偏掉
    （此例服務時鐘快 1 小時 -> ttl=0 -> 端點一拿到 Lease 就自行停止，而 Core 仍認為它是 owner）。
    assembly 對兩者傳同一個 clock，故正式組裝不受影響；但獨立使用 EndpointService 時預設時鐘就是真時間，
    易誤用。建議改成預設取自 Manager（需 Manager 公開 now()）；本任務不改公開 API。"""
    rig = make_rig(session_factory, tmp_path)
    verifier = RequestVerifier({DESKTOP: SECRETS[DESKTOP]})
    results = {}
    for name, clock in (("shared", rig.clock), ("skewed", lambda: rig.clock.now + timedelta(hours=1))):
        service = EndpointService(rig.manager, verifier, clock=clock)
        api = RpcCoreApi(DESKTOP, SECRETS[DESKTOP], lambda wire, s=service: s.handle(SignedRequest.from_wire(wire)))
        mono = Mono()
        ep = CliEndpoint(DESKTOP, api, clock=mono, wall_clock=rig.clock)
        assert ep.start_lease() is AcquireResult.GRANTED
        results[name] = ep.remaining_ttl()
        ep.tick()
        results[name + "_alive"] = ep.lease is not None
        rig.manager.revoke("reset")
    assert results["shared"] == pytest.approx(TTL) and results["shared_alive"] is True
    assert results["skewed"] <= 0 and results["skewed_alive"] is False


# ---- (8) socket 回應無 MAC ----

def test_item8_rogue_loopback_server_cannot_make_endpoint_trust_forged_lease():
    """【已修（T-24 B7）】hello 改為 challenge-response：伺服器須以端點 secret 對 client nonce 簽章，
    搶綁該埠的本機行程沒有 secret，client 拒絕其 hello_ok，偽造的 GRANTED／ttl 不會被端點採信。"""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    forged_auth = {
        "persona_id": "uep", "endpoint_id": "ep_cli", "lease_id": "lease_forged",
        "fencing_epoch": 1, "core_epoch": 1, "presence_generation": 99,
    }

    def rogue():
        conn, _ = listener.accept()
        with conn, conn.makefile("rwb") as f:
            f.readline()  # hello（不驗證）
            f.write(b'{"type":"hello_ok"}\n')
            f.flush()
            while True:
                line = f.readline()
                if not line:
                    return
                message = json.loads(line)
                op = message["req"]["op"]
                result = (
                    {"result": "GRANTED", "auth": forged_auth, "ttl_seconds": 3600.0} if op == "acquire"
                    else {"result": "ACCEPTED", "ttl_seconds": 3600.0}
                )
                f.write(json.dumps({"type": "resp", "id": message["id"], "resp": {"ok": True, "result": result}}).encode() + b"\n")
                f.flush()

    threading.Thread(target=rogue, daemon=True).start()
    client = SocketControlClient("127.0.0.1", port, "ep_cli", b"cli-secret-0123456789abcdef", on_control=lambda s: None)
    ep = CliEndpoint("ep_cli", client.api, clock=Mono(), wall_clock=wall_now)
    try:
        with pytest.raises(EndpointAuthError):
            client.connect()  # 偽造的 hello_ok 沒有有效的伺服器證明
        assert not client.connected and ep.lease is None
    finally:
        ep.close()
        client.close()
        listener.close()
