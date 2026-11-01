"""T-24 第二輪修補（聚焦複審發現）的回歸測試；需 PostgreSQL 者經 session_factory（未設時 skip）。

項目：1 fresh_process 自證須等舊 Lease 到期、2 停止證據欄位必填、3 payload hash 金鑰輪替（kid）、
4 execute_action 階段 1.5 重檢、5 階段 3 失敗 log、6 RequestVerifier 啟動下限、7 revoke 後併發 acquire。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from tests.uep.endpoint_support import SECRET, FakeCore, Mono, wall_now
from tests.uep.presence_support import activate, make_rig
from tests.uep.test_cycle2_audit_b import GOOD_STOP, _in_process_api
from tests.uep.test_task_model import StubExecutor, declare, make_manager, new_task
from uep.assembly import build_request_verifier
from uep.contracts.presence import AcquireResult, ConfirmResult, PresenceStatus
from uep.endpoint.auth import EndpointAuthError, sign_request
from uep.endpoint.endpoint import CliEndpoint
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, IdempotencyConflict, TrustedContext
from uep.events.payload_hash import (
    HMAC_PREFIX,
    PayloadHasher,
    VerifyOutcome,
    sha256_hex,
)
from uep.events.publisher import EventPublisher
from uep.tasks.manager import ActionBlocked, PresenceRequired, TaskPolicy
from uep.tasks.models import ReceiptRow
from uep.tasks.states import InterruptPolicy

FRESH = {"kind": "fresh_process", "playing": False, "buffer_flushed": True}


# ---- 1. fresh_process 不得繞過 RECOVERING（舊行程仍持有 Lease 時） ----

def _old_process_holding_lease(rig):
    old = CliEndpoint("ep_cli", _in_process_api(rig, "ep_cli"), clock=Mono(), wall_clock=rig.clock)
    assert old.start_lease() is AcquireResult.GRANTED
    rig.manager.start()  # Core 重啟 -> RECOVERING(core_restart)；revoke 未送達舊行程
    assert rig.manager.current().status is PresenceStatus.RECOVERING
    return old


def test_manager_rejects_fresh_process_until_last_lease_expiry_plus_skew(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    a = activate(rig, "ep_cli")  # Lease 於 t0 + 30 到期
    rig.manager.start()
    gen = rig.manager.current().generation
    assert gen == a.presence_generation
    assert rig.manager.confirm_quiesced("ep_cli", gen, FRESH) is ConfirmResult.EVIDENCE_REJECTED
    rig.clock.advance(59.9)  # 30 秒 TTL + 30 秒偏差容忍尚未滿
    assert rig.manager.confirm_quiesced("ep_cli", gen, FRESH) is ConfirmResult.EVIDENCE_REJECTED
    assert rig.manager.current().status is PresenceStatus.RECOVERING
    rig.clock.advance(0.2)
    assert rig.manager.confirm_quiesced("ep_cli", gen, FRESH) is ConfirmResult.ACCEPTED
    assert rig.manager.current().status is PresenceStatus.NONE


def test_adapter_stop_report_is_not_delayed_by_lease_expiry(session_factory, tmp_path):
    """等待只針對 fresh_process 自證；舊端自己送的停止回報照舊立即生效。"""
    rig = make_rig(session_factory, tmp_path)
    activate(rig, "ep_cli")
    rig.manager.start()
    gen = rig.manager.current().generation
    assert rig.manager.confirm_quiesced("ep_cli", gen, GOOD_STOP) is ConfirmResult.ACCEPTED


def test_new_process_cannot_get_lease_while_old_process_still_holds_one(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    old = _old_process_holding_lease(rig)
    new = CliEndpoint(
        "ep_cli", _in_process_api(rig, "ep_cli"), clock=Mono(), wall_clock=rig.clock, sleep=lambda _s: None
    )
    assert new.start_lease() is AcquireResult.NOT_QUIESCED  # 時間沒前進：不得雙重在場
    assert rig.manager.current().status is PresenceStatus.RECOVERING
    assert old.lease is not None
    new.close()
    old.close()


def test_new_process_gets_lease_after_old_lease_expired(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    old = _old_process_holding_lease(rig)
    rig.clock.advance(61)  # 舊 Lease 到期 + 偏差容忍
    new = CliEndpoint(
        "ep_cli", _in_process_api(rig, "ep_cli"), clock=Mono(), wall_clock=rig.clock, sleep=lambda _s: None
    )
    assert new.start_lease() is AcquireResult.GRANTED
    new.close()
    old.close()


def test_new_process_waits_boundedly_then_gets_lease(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    old = _old_process_holding_lease(rig)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        rig.clock.advance(seconds)  # 等待期間 Core 時間照走

    new = CliEndpoint("ep_cli", _in_process_api(rig, "ep_cli"), clock=Mono(), wall_clock=rig.clock, sleep=sleep)
    assert new.start_lease() is AcquireResult.GRANTED
    assert 0 < len(sleeps) <= 40 and sum(sleeps) <= 65
    new.close()
    old.close()


class _RejectingCore(FakeCore):
    def __init__(self) -> None:
        super().__init__()
        self.unquiesced = True
        self.confirm_result = ConfirmResult.EVIDENCE_REJECTED

    def acquire(self, location):
        from uep.endpoint.protocol import AcquireReply

        self.calls.append("acquire")
        return AcquireReply(AcquireResult.NOT_QUIESCED)


def test_endpoint_wait_is_bounded_not_a_busy_loop():
    core, sleeps = _RejectingCore(), []
    ep = CliEndpoint("ep_cli", core, clock=Mono(), wall_clock=wall_now, sleep=sleeps.append)
    assert ep.start_lease() is AcquireResult.NOT_QUIESCED
    assert 1 <= len(sleeps) <= 40 and all(s > 0 for s in sleeps)
    assert core.calls.count("acquire") == 1  # 被拒期間不重複 acquire
    assert len(core.quiesced) == len(sleeps) + 1  # 每次等待後各重試自證一次，最後一次被拒後放棄


class _SlowExpiryCore(FakeCore):
    """舊 Lease 還要 ``expiry_after`` 秒（以 sleep 累計）才到期：到期前自證被拒、到期後 acquire 才 GRANTED。"""

    def __init__(self, expiry_after: float) -> None:
        super().__init__()
        self.unquiesced = True
        self.expiry_after = expiry_after
        self.elapsed = 0.0

    def acquire(self, location):
        from uep.endpoint.protocol import AcquireReply

        self.calls.append("acquire")
        if self.unquiesced:
            return AcquireReply(AcquireResult.NOT_QUIESCED)
        return super().acquire(location)

    def confirm_quiesced(self, old_generation, evidence):
        self.quiesced.append((old_generation, dict(evidence)))
        if self.elapsed < self.expiry_after:
            return ConfirmResult.EVIDENCE_REJECTED
        self.unquiesced = False
        return ConfirmResult.ACCEPTED


def test_endpoint_wait_cap_follows_configured_lease_ttl():
    """Lease TTL 設長（120 秒）時，等待上限隨之放大，仍能等到舊 Lease 到期（舊固定 65 秒會放棄）。"""
    core = _SlowExpiryCore(expiry_after=150.0)

    def sleep(seconds: float) -> None:
        core.elapsed += seconds

    ep = CliEndpoint(
        "ep_cli", core, clock=Mono(), wall_clock=wall_now, sleep=sleep,
        lease_ttl_seconds=120.0, fresh_process_skew_seconds=30.0,
    )
    assert ep.start_lease() is AcquireResult.GRANTED
    assert 150.0 <= core.elapsed <= 120.0 + 30.0 + 5.0 + 2.0  # 有上限（TTL + 偏差 + 餘裕），沒有忙迴圈


def test_endpoint_wait_cap_default_still_gives_up_after_ttl_skew_margin():
    core = _SlowExpiryCore(expiry_after=10_000.0)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        core.elapsed += seconds

    ep = CliEndpoint("ep_cli", core, clock=Mono(), wall_clock=wall_now, sleep=sleep)
    assert ep.start_lease() is AcquireResult.NOT_QUIESCED
    assert 65.0 <= sum(sleeps) <= 67.0


# ---- 2. 停止證據欄位必填 ----

@pytest.mark.parametrize("evidence", [
    {"kind": "adapter_stop_report"},
    {"kind": "adapter_stop_report", "playing": False},
    {"kind": "adapter_stop_report", "buffer_flushed": True},
    {"kind": "adapter_stop_report", "playing": None, "buffer_flushed": True},
    {"kind": "fresh_process"},
    {"kind": "fresh_process", "playing": False},
])
def test_evidence_missing_fields_is_rejected(session_factory, tmp_path, evidence):
    rig = make_rig(session_factory, tmp_path)
    a = activate(rig, "ep_a")
    rig.clock.advance(100)  # 過期且超過偏差容忍：fresh_process 的時間條件已滿足，單看欄位
    rig.manager.expire_due()
    result = rig.manager.confirm_quiesced("ep_a", a.presence_generation, evidence)
    assert result is ConfirmResult.EVIDENCE_REJECTED
    assert rig.manager.current().unquiesced_endpoint_id == "ep_a"
    assert rig.manager.confirm_quiesced("ep_a", a.presence_generation, GOOD_STOP) is ConfirmResult.ACCEPTED


# ---- 3. payload hash：金鑰輪替 ----

KEY_A = SecretStr("key-aaaaaaaaaaaaaaaaaaaa")
KEY_B = SecretStr("key-bbbbbbbbbbbbbbbbbbbb")
EXTERNAL = TrustedContext(source="adapter.discord", actor_id="guest_1", scope_ref="scope_public", external=True)


class _NoKidHasher(PayloadHasher):
    """T-24 之前的格式：``hmac-sha256:<hex>``（無 kid）。"""

    def hash(self, data: bytes) -> str:
        return HMAC_PREFIX + self._digest(data)


def test_new_hash_format_carries_non_secret_kid():
    h = PayloadHasher(KEY_A)
    stored = h.hash(b"x")
    prefix, kid, digest = stored.split(":")
    assert prefix + ":" == HMAC_PREFIX and len(kid) == 8 and len(digest) == 64
    assert kid == h.key_id() and kid != PayloadHasher(KEY_B).key_id()
    assert KEY_A.get_secret_value() not in stored


def test_verify_recognises_three_formats():
    a = PayloadHasher(KEY_A)
    assert a.check(b"v", sha256_hex(b"v")) is VerifyOutcome.MATCH
    assert a.check(b"w", sha256_hex(b"v")) is VerifyOutcome.MISMATCH
    legacy = _NoKidHasher(KEY_A).hash(b"v")
    assert a.check(b"v", legacy) is VerifyOutcome.MATCH
    assert a.check(b"w", legacy) is VerifyOutcome.UNVERIFIABLE  # 無 kid：分不出內容不同或金鑰已換
    assert PayloadHasher(KEY_B).check(b"v", legacy) is VerifyOutcome.UNVERIFIABLE
    new = a.hash(b"v")
    assert a.check(b"v", new) is VerifyOutcome.MATCH and a.verify(b"v", new)
    assert a.check(b"w", new) is VerifyOutcome.MISMATCH and not a.verify(b"w", new)
    assert PayloadHasher(KEY_B).check(b"v", new) is VerifyOutcome.UNVERIFIABLE


def _publisher(session_factory, key, cls=PayloadHasher):
    return EventPublisher(session_factory, EventBus(), hasher=cls(key))


def _draft(blob=b"same", event_type="input.text_final"):
    return EventDraft(event_type=event_type, idempotency_key="k", payload_blob=blob)


def test_resend_after_key_rotation_is_duplicate_not_conflict(session_factory, caplog):
    _publisher(session_factory, KEY_A).publish(_draft(), EXTERNAL)
    with caplog.at_level(logging.WARNING, logger="uep.events.journal"):
        again = _publisher(session_factory, KEY_B).publish(_draft(), EXTERNAL)
    assert again.duplicate
    assert any("無法驗證重送內容" in r.getMessage() for r in caplog.records)


def test_resend_after_rotation_from_legacy_no_kid_hash_is_duplicate(session_factory):
    _publisher(session_factory, KEY_A, _NoKidHasher).publish(_draft(), EXTERNAL)
    assert _publisher(session_factory, KEY_B).publish(_draft(), EXTERNAL).duplicate
    assert _publisher(session_factory, KEY_A).publish(_draft(), EXTERNAL).duplicate  # 同金鑰也能驗


def test_resend_with_same_key_but_different_content_still_conflicts(session_factory):
    pub = _publisher(session_factory, KEY_A)
    pub.publish(_draft(b"original"), EXTERNAL)
    with pytest.raises(IdempotencyConflict):
        pub.publish(_draft(b"tampered"), EXTERNAL)


def test_resend_with_other_mismatching_fields_conflicts_even_after_rotation(session_factory):
    _publisher(session_factory, KEY_A).publish(_draft(), EXTERNAL)
    with pytest.raises(IdempotencyConflict):
        _publisher(session_factory, KEY_B).publish(_draft(event_type="input.utterance_final"), EXTERNAL)


# ---- 4. execute_action 階段 1 提交後、executor 呼叫前的重檢 ----

BOUND = TaskPolicy(presence_bound=True, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_b")


def _setup_action(mgr, *, policy=None):
    task = new_task(mgr, policy=policy) if policy is not None else new_task(mgr)
    run = mgr.start_run(task.task_id, persona_version=1, policy_version=1)
    step = mgr.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    params = {"a": 1}
    declare(mgr, side_effect="internal_read")
    action = mgr.create_action(
        run_id=run.run_id, step_id=step.step_id, tool="files.write", params=params,
        worker_epoch=run.worker_epoch,
    )
    return run, action, params


def _receipt_of(session_factory, action_id):
    with session_factory() as s:
        return s.scalar(select(ReceiptRow).where(ReceiptRow.action_id == action_id))


def test_stop_engaged_after_stage1_commit_never_calls_executor_and_fails_action(session_factory):
    state = {"blocked": None}
    executor = StubExecutor()

    def hook(point: str) -> None:
        if point == "action.after_executing_commit":
            state["blocked"] = "global_stop_engaged"  # 階段 1 已提交後才 engage 全域停止

    mgr = make_manager(session_factory, executor, action_guard=lambda _s: state["blocked"], crash_hook=hook)
    run, action, params = _setup_action(mgr)
    with pytest.raises(ActionBlocked, match="global_stop_engaged"):
        mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert executor.calls == []
    assert mgr.get_action(action.action_id).status == "FAILED"  # 確定未呼叫：不是 UNKNOWN
    receipt = _receipt_of(session_factory, action.action_id)
    assert receipt is not None and receipt.error_code == "stopped_before_call"
    assert receipt.succeeded is False and receipt.source_verified is True


def test_presence_lost_after_stage1_commit_never_calls_executor(session_factory):
    state = {"ok": True}
    executor = StubExecutor()

    def hook(point: str) -> None:
        if point == "action.after_executing_commit":
            state["ok"] = False

    mgr = make_manager(session_factory, executor, presence_check=lambda _p: state["ok"], crash_hook=hook)
    run, action, params = _setup_action(mgr, policy=BOUND)
    with pytest.raises(PresenceRequired):
        mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert executor.calls == []
    assert mgr.get_action(action.action_id).status == "FAILED"
    assert _receipt_of(session_factory, action.action_id).error_code == "presence_lost_before_call"


def test_recheck_passes_when_nothing_changed(session_factory):
    executor = StubExecutor()
    mgr = make_manager(session_factory, executor, action_guard=lambda _s: None)
    run, action, params = _setup_action(mgr)
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert outcome.executed and executor.calls == [action.action_id]


# ---- 第三輪：階段 1.5 的殭屍 worker 與 PresenceRequired 可重試性 ----

@pytest.mark.parametrize("with_guards", [False, True], ids=["no_guards", "with_guards"])
def test_zombie_worker_after_takeover_never_calls_executor(session_factory, with_guards):
    """階段 1 提交後 Run 被 resume_run 接手（epoch 變、action 轉 UNKNOWN）：舊 worker 不得呼叫 executor。"""
    from datetime import timedelta

    from uep.tasks.manager import StaleWorkerEpoch

    executor = StubExecutor()
    holder: dict = {}

    def hook(point: str) -> None:
        if point == "action.after_executing_commit" and "plan" not in holder:
            holder["plan"] = holder["mgr"].resume_run(holder["run_id"], stale_after=timedelta(0))

    kwargs = {"action_guard": lambda _s: None} if with_guards else {}
    mgr = make_manager(session_factory, executor, crash_hook=hook, **kwargs)
    holder["mgr"] = mgr
    run, action, params = _setup_action(mgr)
    holder["run_id"] = run.run_id
    with pytest.raises(StaleWorkerEpoch):
        mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert executor.calls == []
    assert mgr.get_action(action.action_id).status == "UNKNOWN"  # 維持接手後的值，未被殭屍覆寫
    assert _receipt_of(session_factory, action.action_id) is None


def test_presence_required_in_stage1_is_retryable_and_keeps_approval(session_factory):
    from tests.uep.test_task_model import approve
    from uep.tasks.models import ApprovalRow

    state = {"ok": False}
    executor = StubExecutor()
    mgr = make_manager(session_factory, executor, presence_check=lambda _p: state["ok"])
    task = new_task(mgr, policy=BOUND)
    run = mgr.start_run(task.task_id, persona_version=1, policy_version=1)
    step = mgr.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    params = {"a": 1}
    declare(mgr, side_effect="external_effect")
    action = mgr.create_action(
        run_id=run.run_id, step_id=step.step_id, tool="files.write", params=params,
        worker_epoch=run.worker_epoch,
    )
    approval = approve(mgr, params)
    with pytest.raises(PresenceRequired) as err:
        mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch, approval_id=approval.approval_id)
    assert err.value.retryable is True
    assert mgr.get_action(action.action_id).status == "INTENDED"
    with session_factory() as s:
        assert s.get(ApprovalRow, approval.approval_id).consumed_at is None
    state["ok"] = True  # Presence 恢復後以同一份核准重試
    outcome = mgr.execute_action(
        action.action_id, params, worker_epoch=run.worker_epoch, approval_id=approval.approval_id
    )
    assert outcome.executed and executor.calls == [action.action_id]


def test_presence_required_in_stage15_is_terminal_not_retryable(session_factory):
    state = {"ok": True}
    executor = StubExecutor()

    def hook(point: str) -> None:
        if point == "action.after_executing_commit":
            state["ok"] = False

    mgr = make_manager(session_factory, executor, presence_check=lambda _p: state["ok"], crash_hook=hook)
    run, action, params = _setup_action(mgr, policy=BOUND)
    with pytest.raises(PresenceRequired) as err:
        mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert err.value.retryable is False and "新 action" in str(err.value)
    assert mgr.get_action(action.action_id).status == "FAILED"
    state["ok"] = True
    outcome = mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    assert outcome.executed is False and outcome.status.value == "FAILED" and executor.calls == []


# ---- 5. 階段 3 失敗路徑 log 原 executor 結果／例外型別 ----

class _Exploding(StubExecutor):
    def execute(self, call):
        raise KeyError("pii-secret-text")


def test_stage3_failure_logs_original_exception_types_without_message(session_factory, caplog):
    mgr = make_manager(session_factory, _Exploding())
    run, action, params = _setup_action(mgr)

    def broken(*_a, **_k):
        raise RuntimeError("leak-text-xyz")

    mgr._finish_execution = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="uep.tasks.manager"), pytest.raises(RuntimeError):
        mgr.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "KeyError" in text and "RuntimeError" in text
    assert "pii-secret-text" not in text and "leak-text-xyz" not in text
    assert mgr.get_action(action.action_id).status == "UNKNOWN"


# ---- 6. RequestVerifier 啟動下限 ----

def test_verifier_accepts_slightly_lagging_endpoint_right_after_core_restart():
    start = 1000.0
    verifier = build_request_verifier({"ep": SECRET}, clock=lambda: start)  # Core 在 start 時啟動
    lagging = sign_request(SECRET, "ep", "renew", {"lease_id": "x"}, clock=lambda: start - 10.0)  # 端點時鐘落後 10 秒
    assert verifier.verify(lagging) == "ep"
    too_old = sign_request(SECRET, "ep", "renew", {"lease_id": "x"}, clock=lambda: start - 31.0)
    with pytest.raises(EndpointAuthError):
        verifier.verify(too_old)


def test_build_request_verifier_uses_skew_as_startup_grace(monkeypatch):
    from uep import assembly

    seen: dict = {}
    real = assembly.RequestVerifier

    def spy(secrets, **kw):
        seen.update(kw)
        return real(secrets, **kw)

    monkeypatch.setattr(assembly, "RequestVerifier", spy)
    build_request_verifier({"ep": SECRET})
    assert seen["startup_grace"] == seen["skew_seconds"] == 30.0


# ---- 7. revoke 後三端併發 acquire ----

def _concurrent_acquire(rig, endpoints):
    barrier = threading.Barrier(len(endpoints))

    def run(endpoint_id):
        barrier.wait(5)
        return endpoint_id, rig.manager.acquire(endpoint_id, "desktop").result

    with ThreadPoolExecutor(len(endpoints)) as pool:
        return dict(pool.map(run, endpoints))


def test_after_revoke_only_old_endpoint_can_acquire_concurrently(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    activate(rig, "ep_a")
    rig.manager.revoke("round_end")  # 舊端 ep_a 未證明停止，其舊 Lease 尚未過期
    results = _concurrent_acquire(rig, ["ep_a", "ep_b", "ep_c"])
    assert results["ep_a"] is AcquireResult.GRANTED
    # ep_a 先提交時，他端可能看到「已核發待確認」；無論哪種都不得核發
    assert all(
        results[e] in (AcquireResult.NOT_QUIESCED, AcquireResult.TRANSFER_PENDING) for e in ("ep_b", "ep_c")
    ), results
    assert list(results.values()).count(AcquireResult.GRANTED) == 1


def test_after_evidence_exactly_one_of_three_concurrent_acquires_is_granted(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    activate(rig, "ep_a")
    out = rig.manager.revoke("round_end")
    assert rig.manager.confirm_quiesced(out.endpoint_id, out.generation, GOOD_STOP) is ConfirmResult.ACCEPTED
    results = _concurrent_acquire(rig, ["ep_a", "ep_b", "ep_c"])
    assert list(results.values()).count(AcquireResult.GRANTED) == 1, results
    assert all(r in (AcquireResult.GRANTED, AcquireResult.TRANSFER_PENDING) for r in results.values()), results


# ---- 第三輪追加：6 RECOVERING 期間 revoke 不得覆寫未證明停止的端點 ----

def test_revoke_during_recovering_is_noop_and_keeps_unproven_endpoint(session_factory, tmp_path):
    """不變式：RECOVERING 期間不存在有效 Lease（進入時清空、acquire 回 NOT_QUIESCED），revoke 必為 no-op。

    因此 revoke 不可能把「尚未證明停止的端點 X」覆寫成其他端點 Y；X 的停止證據要求保留。
    """
    rig = make_rig(session_factory, tmp_path)
    activate(rig, "ep_cli")
    rig.manager.start()  # Core 重啟：ep_cli 為未證明停止端點
    snap = rig.manager.current()
    assert snap.status is PresenceStatus.RECOVERING and snap.unquiesced_endpoint_id == "ep_cli"
    # 他端在 RECOVERING 期間拿不到 Lease，因此沒有 Lease 可被 revoke
    assert rig.manager.acquire("ep_other", "desktop").result is AcquireResult.NOT_QUIESCED
    outcome = rig.manager.revoke("global_stop")
    assert outcome.lease_id is None
    after = rig.manager.current()
    assert after.status is PresenceStatus.RECOVERING and after.unquiesced_endpoint_id == "ep_cli"
    assert after.generation == snap.generation


# ---- 7 strict 金鑰探測：全空白視同缺失 ----

def test_assembly_strict_refuses_whitespace_only_payload_hash_key(session_factory, tmp_path, monkeypatch):
    from tests.uep.test_cycle2_audit_c_context import kwargs
    from uep.assembly import AssemblyError, assemble_core
    from uep.config import get_settings

    monkeypatch.setattr(get_settings(), "payload_hash_key", SecretStr(" " * 20))
    with pytest.raises(AssemblyError, match="UEP_PAYLOAD_HASH_KEY"):
        assemble_core(session_factory, strict=True, **kwargs(session_factory, tmp_path))


# ---- 8 EventDraft 的 activity_id／run_id 與其他 ID 欄位同等驗證 ----

@pytest.mark.parametrize("field", ["activity_id", "run_id", "task_id", "conversation_id", "segment_id", "turn_id"])
def test_event_draft_id_fields_reject_malformed_values(field):
    from pydantic import ValidationError

    for bad in ("actv_1", "not-an-id", "", "run_a"):
        with pytest.raises(ValidationError):
            EventDraft(event_type="input.text_final", **{field: bad})


def test_event_draft_accepts_well_formed_activity_and_run_ids():
    from uep.ids import new_id

    draft = EventDraft(event_type="input.text_final", activity_id=new_id("actv"), run_id=new_id("run"))
    assert draft.activity_id and draft.run_id
