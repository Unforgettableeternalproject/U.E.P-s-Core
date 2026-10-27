"""V11 驗收：多端同時 acquire，任一時刻最多一個有效 owner（VC-4-1；需 PostgreSQL）。

三個端點（Desktop／Discord／Website）以 Barrier 同時送出 acquire，多回合；斷言每回合恰一個 GRANTED、
presence_current 單筆、generation 每回合恰 +1、journal 每回合恰一筆 ``presence.granted``，
且並行取樣的 DB 快照任何時刻 ACTIVE／存活 Lease 都不超過 1。

P2 與契約文字的差異（非缺陷）：契約寫 loser 為 BUSY 或 VERSION_CONFLICT；P2 的 loser 在 winner 尚未
``confirm_activated`` 時會得到 TRANSFER_PENDING。帶 ``expected_version`` 的請求則一律 VERSION_CONFLICT。
"""
from __future__ import annotations

import threading

import pytest

from tests.uep.acceptance_p2_support import (
    LOCATIONS,
    SECRETS,
    invariant_snapshot,
    lease_total,
    live_lease_count,
    make_world,
    open_transfer_count,
    presence_row_count,
)
from tests.uep.presence_support import count_events, make_rig, revoke_and_quiesce
from uep.contracts.presence import AcquireResult as R
from uep.contracts.presence import PresenceStatus

pytestmark = pytest.mark.usefixtures("session_factory")

ENDPOINTS = ("ep_desktop", "ep_discord", "ep_website")
ROUNDS = 100


def _race(callers, barrier_timeout: float = 10.0):
    """所有 caller 於 Barrier 後同時執行；回傳各自結果，例外收集後統一拋出。"""
    barrier = threading.Barrier(len(callers))
    results: list = [None] * len(callers)
    errors: list[BaseException] = []

    def worker(index, fn):
        try:
            barrier.wait(barrier_timeout)
            results[index] = fn()
        except BaseException as exc:  # noqa: BLE001  執行緒內例外要帶回主執行緒
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i, fn), daemon=True) for i, fn in enumerate(callers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads), "執行緒卡住（疑似死鎖）"
    if errors:
        raise errors[0]
    return results


def test_v11_three_endpoints_race_100_rounds_exactly_one_grant_per_round(session_factory, tmp_path):
    """V11／VC-4-1：100 回合 × 3 端點同時 acquire（直接打 Manager）。"""
    rig = make_rig(session_factory, tmp_path)
    mgr = rig.manager
    saw_busy = False
    for round_no in range(1, ROUNDS + 1):
        before = mgr.current().generation
        results = _race([lambda e=e: mgr.acquire(e, LOCATIONS[e]) for e in ENDPOINTS])
        granted = [o for o in results if o.result is R.GRANTED]
        others = [o.result for o in results if o.result is not R.GRANTED]
        assert len(granted) == 1, [o.result for o in results]
        assert set(others) <= {R.BUSY, R.TRANSFER_PENDING, R.VERSION_CONFLICT}, others
        view = mgr.current()
        assert view.generation == before + 1, "generation 每回合恰 +1"
        assert presence_row_count(session_factory) == 1
        assert live_lease_count(session_factory) == 1 and open_transfer_count(session_factory) == 1
        assert count_events(session_factory, "presence.granted") == round_no
        winner = granted[0].grant
        assert winner.authorization.presence_generation == view.generation
        # 奇數回合讓 winner 確認到場（ACTIVE）；偶數回合維持 ACTIVATING，兩種狀態下的 loser 都要被擋
        if round_no % 2:
            assert mgr.confirm_activated(winner.lease_id).value == "ACCEPTED"
            assert mgr.current().status is PresenceStatus.ACTIVE
            again = _race([lambda e=e: mgr.acquire(e, LOCATIONS[e]) for e in ENDPOINTS])
            assert {o.result for o in again} == {R.BUSY}  # 已 ACTIVE：全部 BUSY（含 winner 自己）
            saw_busy = True
        revoke_and_quiesce(mgr)
        assert mgr.current().status is PresenceStatus.NONE and live_lease_count(session_factory) == 0
        assert open_transfer_count(session_factory) == 0
    assert saw_busy
    assert lease_total(session_factory) == ROUNDS  # 沒有多核發任何一張 Lease


def test_v11_expected_version_losers_get_version_conflict(session_factory, tmp_path):
    """帶相同 expected_version 同時送出：恰一個 GRANTED，其餘 VERSION_CONFLICT（契約字面）。"""
    rig = make_rig(session_factory, tmp_path)
    mgr = rig.manager
    for _ in range(30):
        version = mgr.current().row_version
        results = _race([lambda e=e, v=version: mgr.acquire(e, LOCATIONS[e], expected_version=v) for e in ENDPOINTS])
        assert sorted(o.result.value for o in results) == ["GRANTED", "VERSION_CONFLICT", "VERSION_CONFLICT"]
        revoke_and_quiesce(mgr)
    assert lease_total(session_factory) == 30


def test_v11_invariant_sampled_concurrently_while_racing_and_revoking(session_factory, tmp_path):
    """並行取樣：acquire 競爭＋confirm＋revoke 同時進行，任一時刻 ACTIVE／存活 Lease／開啟 transfer ≤ 1。"""
    rig = make_rig(session_factory, tmp_path)
    mgr = rig.manager
    stop = threading.Event()
    violations: list[tuple[int, int, int]] = []
    samples = [0]

    def sampler():
        while not stop.is_set():
            snap = invariant_snapshot(session_factory)
            samples[0] += 1
            if max(snap) > 1:
                violations.append(snap)

    watcher = threading.Thread(target=sampler, daemon=True)
    watcher.start()
    grants = [0]
    glock = threading.Lock()

    def endpoint_loop(endpoint_id: str):
        for _ in range(25):
            out = mgr.acquire(endpoint_id, LOCATIONS[endpoint_id])
            if out.result is R.GRANTED and out.grant is not None:
                with glock:
                    grants[0] += 1
                mgr.confirm_activated(out.grant.lease_id)

    def revoker():
        for _ in range(40):
            mgr.revoke("chaos")

    try:
        _race([lambda e=e: endpoint_loop(e) for e in ENDPOINTS] + [revoker])
    finally:
        stop.set()
        watcher.join(5)
    assert samples[0] > 0
    assert violations == []
    mgr.revoke("end")
    # generation 只在核發時 +1：總核發數 == 最終 generation == lease 總筆數 == presence.granted 事件數
    assert mgr.current().generation == grants[0] == lease_total(session_factory)
    assert count_events(session_factory, "presence.granted") == grants[0]
    assert live_lease_count(session_factory) == 0 and open_transfer_count(session_factory) == 0


def test_v11_race_through_hmac_service_in_process(session_factory, tmp_path):
    """經 CliEndpoint.start_lease（HMAC 簽章 RPC → EndpointService）同時啟動：恰一端真的持有 Lease。"""
    world = make_world(session_factory, tmp_path)
    try:
        for round_no in range(1, 21):
            results = _race([lambda e=e: world.endpoint(e).start_lease() for e in ENDPOINTS])
            granted = [e for e, r in zip(ENDPOINTS, results, strict=True) if r is R.GRANTED]
            assert len(granted) == 1, results
            owners = [e for e in ENDPOINTS if world.endpoint(e).lease is not None]
            assert owners == granted  # 只有 winner 本地持有 Lease
            auth = world.rig.manager.authorization()
            assert auth is not None and auth.endpoint_id == granted[0]
            assert world.endpoint(granted[0]).lease.auth == auth  # type: ignore[union-attr]
            assert world.rig.manager.current().generation == round_no
            assert count_events(session_factory, "presence.granted") == round_no
            world.endpoint(granted[0]).release()  # owner 主動釋放，回 NONE 進下一回合
            assert world.rig.manager.current().status is PresenceStatus.NONE
    finally:
        world.close()


def test_v11_race_over_real_socket_with_hmac(session_factory, tmp_path):
    """真實 loopback socket＋HMAC：三個 SocketControlClient 同時 acquire，恰一個 GRANTED。"""
    world = make_world(session_factory, tmp_path, socket=True)
    try:
        assert set(SECRETS) == set(ENDPOINTS)
        for round_no in range(1, 16):
            results = _race([lambda e=e: world.clients[e].api.acquire(LOCATIONS[e]) for e in ENDPOINTS])
            granted = [r for r in results if r.result is R.GRANTED]
            assert len(granted) == 1, [r.result for r in results]
            assert {r.result for r in results if r.result is not R.GRANTED} <= {R.BUSY, R.TRANSFER_PENDING}
            assert granted[0].auth is not None and granted[0].auth.presence_generation == round_no
            assert live_lease_count(session_factory) == 1 and presence_row_count(session_factory) == 1
            assert count_events(session_factory, "presence.granted") == round_no
            revoke_and_quiesce(world.rig.manager)
        assert lease_total(session_factory) == 15
    finally:
        world.close()


def test_v11_same_endpoint_concurrent_acquire_is_idempotent(session_factory, tmp_path):
    """同一端點多執行緒同時 acquire（重送）：只核發一張 Lease、generation 只 +1（VC-4-3(b) 冪等）。"""
    rig = make_rig(session_factory, tmp_path)
    mgr = rig.manager
    results = _race([lambda: mgr.acquire("ep_desktop", "desktop") for _ in range(6)])
    assert all(o.result is R.GRANTED for o in results)
    assert len({o.grant.lease_id for o in results}) == 1  # type: ignore[union-attr]
    assert mgr.current().generation == 1 and lease_total(session_factory) == 1
    assert count_events(session_factory, "presence.granted") == 1
