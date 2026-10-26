"""V15 驗收：網路隔離下舊端 watchdog 停止並留證據；嚴格模式下證據未到新端不可啟用（VC-4-4；需 PostgreSQL）。

調校起點值：Lease TTL=30 秒、心跳=5 秒。所有時間都是可注入時鐘：端點用 ``Mono``（單調），Core 用 ``FakeClock``（牆鐘）；
兩者同步推進以模擬「時間經過」，但隔離期間端點的 RPC 一律失敗，只能靠本地單調時鐘自行停止。
"""
from __future__ import annotations

import pytest

from tests.uep.acceptance_p2_support import (
    HEARTBEAT,
    TTL,
    journal_payloads,
    make_world,
    wait_until,
)
from tests.uep.endpoint_support import intent_for_auth
from uep.contracts.presence import (
    AcquireResult,
    ConfirmResult,
    GateDecision,
    PresenceStatus,
)
from uep.endpoint.endpoint import ReconnectOutcome
from uep.output.intent import OutputKind

pytestmark = pytest.mark.usefixtures("session_factory")


def _world(session_factory, tmp_path, **kw):
    return make_world(session_factory, tmp_path, endpoint_ids=("ep_desktop", "ep_discord"), **kw)


def _elapse(world, seconds: float) -> None:
    """時間經過（端點單調時鐘與 Core 牆鐘同步推進）。"""
    world.mono.advance(seconds)
    world.rig.clock.advance(seconds)


def _isolate_and_run(world, ep, *, total: float = 35.0, step: float = 1.0) -> float | None:
    """隔離 ``ep`` 並逐步推進時間、每步 tick；回傳端點停止時相對隔離起點的秒數（沒停回 None）。"""
    world.down[ep.endpoint_id] = True
    elapsed = 0.0
    while elapsed < total:
        _elapse(world, step)
        elapsed += step
        ep.tick()
        if ep.lease is None:
            return elapsed
    return None


def test_v15_watchdog_stops_within_ttl_while_isolated_and_heartbeats_fail(session_factory, tmp_path):
    """V15／VC-4-4：隔離 35 秒，舊端本地 watchdog 在失聯後 TTL（30 秒）內停止；心跳有嘗試但全數失敗。"""
    world = _world(session_factory, tmp_path)
    try:
        ep = world.endpoint("ep_desktop")
        assert ep.start_lease() is AcquireResult.GRANTED
        assert ep.remaining_ttl() == pytest.approx(TTL)
        stopped_at = _isolate_and_run(world, ep)
        assert stopped_at is not None, "隔離 35 秒仍未停止（缺陷）"
        assert stopped_at <= TTL, f"watchdog 於 {stopped_at}s 才停止，超過 TTL={TTL}s（缺陷）"
        assert stopped_at >= TTL - 1  # 沒有過早停止（心跳失敗前都應持有）
        renew_attempts = world.api("ep_desktop").calls.count("renew")
        assert renew_attempts >= HEARTBEAT  # 隔離期間心跳持續嘗試（每秒退避重試），全部失敗
        assert ep.remaining_ttl() is None and ep.playing is False
        assert ep.pending_report_count == 1  # 停止證據保留待補送
        assert world.api("ep_desktop").quiesced == []  # 隔離中一則也沒送到 Core
    finally:
        world.close()


def test_v15_lease_survives_just_before_deadline_and_stops_just_after(session_factory, tmp_path):
    """邊界：期限前 0.01 秒仍持有、期限當下停止（>= 判定）。"""
    world = _world(session_factory, tmp_path)
    try:
        ep = world.endpoint("ep_desktop")
        ep.start_lease()
        world.down["ep_desktop"] = True
        _elapse(world, TTL - 0.01)
        ep.tick()
        assert ep.lease is not None
        _elapse(world, 0.01)
        ep.tick()
        assert ep.lease is None
    finally:
        world.close()


def test_v15_heartbeat_recovery_before_ttl_keeps_lease(session_factory, tmp_path):
    """對照組：隔離 20 秒（< TTL）後恢復，心跳續約成功，端點不停止、Core 仍是 owner。"""
    world = _world(session_factory, tmp_path)
    try:
        ep = world.endpoint("ep_desktop")
        ep.start_lease()
        assert _isolate_and_run(world, ep, total=20.0) is None
        world.down["ep_desktop"] = False
        _elapse(world, 1)
        ep.tick()
        assert ep.lease is not None and ep.remaining_ttl() == pytest.approx(TTL, abs=0.01)
        assert world.rig.manager.current().status is PresenceStatus.ACTIVE
        assert world.api("ep_desktop").quiesced == []
    finally:
        world.close()


def test_v15_buffered_playback_is_cut_and_buffer_flushed_when_watchdog_fires(session_factory, tmp_path):
    """播放中隔離到期：剩餘 buffer 清空、不再播下一塊，證據如實記錄 buffer_flushed／playing=False。"""
    played: list[int] = []
    holder: dict = {}

    def on_chunk(_intent, chunk):
        played.append(chunk)
        if chunk == 0:  # 第一塊播放期間網路隔離，且隔離時間一口氣跨過 TTL
            holder["world"].down["ep_desktop"] = True
            holder["world"].mono.advance(TTL + 1)

    world = _world(session_factory, tmp_path, on_chunk=on_chunk, chunk_count=lambda _i: 3)
    holder["world"] = world
    try:
        ep = world.endpoint("ep_desktop")
        ep.start_lease()
        intent = intent_for_auth(
            ep.lease.auth, kind=OutputKind.AUDIO_STREAM,  # type: ignore[union-attr]
            content_format={"sample_rate": 24000, "channels": 1, "codec": "pcm"},
        )
        result = ep.send(intent, "dsp_buffered")
        assert played == [0], "watchdog 到期後不得再播剩餘 buffer"
        assert result.played_range == {"from_chunk": 0, "to_chunk": 1, "complete": False}
        assert ep.lease is None and ep.playing is False and ep.pending_report_count == 1
        # 隔離結束：證據補送，內容如實
        world.down["ep_desktop"] = False
        _elapse(world, 2)
        ep.tick()
        (generation, evidence), = world.api("ep_desktop").quiesced
        assert generation == 1
        assert evidence["kind"] == "adapter_stop_report" and evidence["reason"] == "lease_expired_local"
        assert evidence["buffer_flushed"] is True and evidence["playing"] is False
        assert evidence["endpoint_id"] == "ep_desktop"
    finally:
        world.close()


def test_v15_strict_mode_new_endpoint_blocked_until_stop_evidence_then_enabled(session_factory, tmp_path):
    """嚴格模式：Lease 已到期但證據未到 -> Discord NOT_QUIESCED（進 RECOVERING、不可啟用）；證據到後才可啟用。"""
    world = _world(session_factory, tmp_path)
    mgr = world.rig.manager
    try:
        desktop, discord = world.endpoint("ep_desktop"), world.endpoint("ep_discord")
        desktop.start_lease()
        old_generation = desktop.lease.auth.presence_generation  # type: ignore[union-attr]
        old_auth = desktop.lease.auth  # type: ignore[union-attr]
        assert _isolate_and_run(world, desktop) is not None
        # Core 側 Lease 也已過期（35 秒 > 30 秒）
        assert world.api("ep_discord").acquire("discord").result is AcquireResult.NOT_QUIESCED
        view = mgr.current()
        assert view.status is PresenceStatus.RECOVERING and view.unquiesced_endpoint_id == "ep_desktop"
        assert discord.start_lease() is AcquireResult.NOT_QUIESCED and discord.lease is None
        assert mgr.authorization() is None
        assert world.rig.gate.admit_intent(intent_for_auth(old_auth)) is not GateDecision.ADMIT
        # 再多等也不會自動放行：沒有證據就是沒有證據
        _elapse(world, 3600)
        assert discord.start_lease() is AcquireResult.NOT_QUIESCED
        assert mgr.current().generation == old_generation  # 沒有核發第二張 Lease

        # Core 恢復（隔離結束）：舊端補送停止證據
        world.down["ep_desktop"] = False
        desktop.tick()
        assert desktop.pending_report_count == 0
        after = mgr.current()
        assert after.status is PresenceStatus.NONE and after.unquiesced_endpoint_id is None
        recovered = journal_payloads(session_factory, "presence.recovered")
        assert recovered and recovered[-1]["kind"] == "adapter_stop_report"
        assert discord.start_lease() is AcquireResult.GRANTED
        assert mgr.current().generation == old_generation + 1
        assert mgr.authorization() == discord.lease.auth  # type: ignore[union-attr]
    finally:
        world.close()


def test_v15_wrong_or_forged_evidence_does_not_unblock_new_endpoint(session_factory, tmp_path):
    """證據不等於授權：generation 不符、或由別的端點偽造，都不能解除 NOT_QUIESCED。"""
    world = _world(session_factory, tmp_path)
    mgr = world.rig.manager
    try:
        desktop = world.endpoint("ep_desktop")
        desktop.start_lease()
        gen = desktop.lease.auth.presence_generation  # type: ignore[union-attr]
        _isolate_and_run(world, desktop)
        assert world.api("ep_discord").acquire("discord").result is AcquireResult.NOT_QUIESCED
        discord_api = world.api("ep_discord")
        # Discord 自己宣稱 Desktop 已停止：Service 以已驗證的 endpoint_id=ep_discord 呼叫，與未證實端點不符
        assert discord_api.confirm_quiesced(gen, {"kind": "adapter_stop_report"}) is ConfirmResult.STALE
        assert mgr.current().status is PresenceStatus.RECOVERING
        # Desktop 回報錯誤的 generation（舊的／未來的）
        world.down["ep_desktop"] = False
        for wrong in (gen - 1, gen + 1):
            assert world.api("ep_desktop").confirm_quiesced(wrong, {"kind": "adapter_stop_report"}) is ConfirmResult.STALE
        assert mgr.current().status is PresenceStatus.RECOVERING
        assert discord_api.acquire("discord").result is AcquireResult.NOT_QUIESCED
        # 正確證據才放行
        assert world.api("ep_desktop").confirm_quiesced(gen, {"kind": "adapter_stop_report"}) is ConfirmResult.ACCEPTED
        assert discord_api.acquire("discord").result is AcquireResult.GRANTED
    finally:
        world.close()


def test_v15_operator_confirmation_path_when_old_endpoint_never_returns(session_factory, tmp_path):
    """人為確認：舊端永不回報時，操作員 resolve_recovery 才能解除；無操作者身分拒絕。"""
    world = _world(session_factory, tmp_path)
    mgr = world.rig.manager
    try:
        desktop = world.endpoint("ep_desktop")
        desktop.start_lease()
        _isolate_and_run(world, desktop)
        assert world.api("ep_discord").acquire("discord").result is AcquireResult.NOT_QUIESCED
        with pytest.raises(ValueError):
            mgr.resolve_recovery("")
        assert mgr.current().status is PresenceStatus.RECOVERING
        assert mgr.resolve_recovery("owner_console", "operator_confirmed") is True
        view = mgr.current()
        assert view.status is PresenceStatus.NONE and view.unquiesced_endpoint_id is None
        assert world.api("ep_discord").acquire("discord").result is AcquireResult.GRANTED
        assert journal_payloads(session_factory, "presence.recovered")[-1]["kind"] == "operator_confirmed"
        assert mgr.resolve_recovery("owner_console") is False  # 冪等
    finally:
        world.close()


def test_v15_local_stop_before_core_expiry_releases_so_no_unquiesced_mark(session_factory, tmp_path):
    """端點本地期限（偏保守）先到、Core 尚未到期，網路恢復後改以 release 結束：不留未證實標記，新端立即可啟用。"""
    world = _world(session_factory, tmp_path)
    try:
        desktop = world.endpoint("ep_desktop")
        desktop.start_lease()
        world.mono.advance(TTL + 1)  # 端點單調時鐘已過期
        world.rig.clock.advance(TTL - 5)  # Core 牆鐘尚未到期
        desktop.tick()
        assert desktop.lease is None and desktop.pending_report_count == 0
        view = world.rig.manager.current()
        assert view.status is PresenceStatus.NONE and view.unquiesced_endpoint_id is None
        assert world.api("ep_discord").acquire("discord").result is AcquireResult.GRANTED
    finally:
        world.close()


def _expired_unquiesced(world):
    """Desktop 被隔離到期；Core 側 Lease 過期，標記 Desktop 未證實停止（NONE）。"""
    desktop = world.endpoint("ep_desktop")
    desktop.start_lease()
    _isolate_and_run(world, desktop)
    world.down["ep_desktop"] = False
    mgr = world.rig.manager
    assert mgr.expire_due() is True
    view = mgr.current()
    assert view.status is PresenceStatus.NONE and view.unquiesced_endpoint_id == "ep_desktop"
    return desktop


def test_v15_original_endpoint_may_reacquire_while_unquiesced_and_none(session_factory, tmp_path):
    """契約 §4.5 P2 補充：未證實停止且仍為 NONE 時，原端點可重新 acquire（不需先送證據）。"""
    world = _world(session_factory, tmp_path)
    try:
        _expired_unquiesced(world)
        outcome = world.rig.manager.acquire("ep_desktop", "desktop")
        assert outcome.result is AcquireResult.GRANTED
        assert world.rig.manager.current().unquiesced_endpoint_id is None  # 重新核發即清除標記
    finally:
        world.close()


def test_v15_observed_original_endpoint_blocked_after_other_endpoint_forced_recovering(session_factory, tmp_path):
    """觀察（待釐清，不改）：他端 acquire 先把狀態推進 RECOVERING 後，原端點在證據到達前也得 NOT_QUIESCED，
    與契約「原端點可重新 acquire」字面有出入；證據到達後即恢復正常。"""
    world = _world(session_factory, tmp_path)
    mgr = world.rig.manager
    try:
        desktop = _expired_unquiesced(world)
        assert world.api("ep_discord").acquire("discord").result is AcquireResult.NOT_QUIESCED
        assert mgr.current().status is PresenceStatus.RECOVERING
        assert mgr.acquire("ep_desktop", "desktop").result is AcquireResult.NOT_QUIESCED  # 觀察到的行為
        _elapse(world, 2)  # 越過證據回報的退避
        desktop.tick()  # 證據補送
        assert mgr.current().status is PresenceStatus.NONE
        assert mgr.acquire("ep_desktop", "desktop").result is AcquireResult.GRANTED
    finally:
        world.close()


def test_v15_real_socket_isolation_then_recovery_with_hmac(session_factory, tmp_path):
    """真實 socket＋HMAC：切斷連線＝隔離；到期後 Discord 被擋，重連補送證據後才可啟用。"""
    world = _world(session_factory, tmp_path, socket=True)
    mgr = world.rig.manager
    try:
        desktop, discord = world.endpoint("ep_desktop"), world.endpoint("ep_discord")
        world.clients["ep_discord"].connect()
        assert desktop.start_lease() is AcquireResult.GRANTED
        gen = desktop.lease.auth.presence_generation  # type: ignore[union-attr]
        world.clients["ep_desktop"].close()  # 網路隔離：之後 RPC 都是 ConnectionError
        elapsed = 0.0
        while elapsed < 35 and desktop.lease is not None:
            _elapse(world, 1)
            elapsed += 1
            desktop.tick()
        assert desktop.lease is None and elapsed <= TTL
        assert desktop.pending_report_count == 1
        assert mgr.expire_due() is True  # Core 側 Lease 到期
        assert discord.start_lease() is AcquireResult.NOT_QUIESCED and discord.lease is None
        assert mgr.current().status is PresenceStatus.RECOVERING

        world.clients["ep_desktop"].connect()  # 網路恢復
        assert desktop.reconnect() is ReconnectOutcome.IDLE  # 本來就沒 Lease；順便強制補送證據
        assert desktop.pending_report_count == 0
        (sent_gen, evidence), = world.api("ep_desktop").quiesced
        assert sent_gen == gen and evidence["reason"] == "lease_expired_local"
        assert mgr.current().status is PresenceStatus.NONE
        assert discord.start_lease() is AcquireResult.GRANTED
        assert wait_until(lambda: mgr.authorization() is not None)
        assert mgr.current().generation == gen + 1
    finally:
        world.close()
