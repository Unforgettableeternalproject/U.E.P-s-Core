"""Output Gate、DeliveryManager 接 Gate、TaskManager Presence 檢查點（需 PostgreSQL）。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.uep.acceptance_support import SCOPE, make_tm
from tests.uep.factories import NOW
from tests.uep.presence_support import activate, count_events, intent_for, make_rig
from uep.contracts.presence import AuthorizationTuple, GateDecision
from uep.events.models import EventRow
from uep.fakes.output import FakeOutputAdapter
from uep.fakes.tool import FakeToolExecutor
from uep.ids import new_id
from uep.output.delivery import DeliveryStatus as S
from uep.output.manager import DeliveryManager
from uep.presence.gate import ALLOW_ALL, OutputGate
from uep.presence.manager import PresenceManager
from uep.tasks.manager import PresenceRequired, TaskPolicy
from uep.tasks.states import ActionStatus, InterruptPolicy

pytestmark = pytest.mark.usefixtures("session_factory")


# ---- Gate 判定 ----

def test_admit_when_all_six_fields_match(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    auth = activate(rig)
    assert rig.gate.admit_intent(intent_for(auth)) is GateDecision.ADMIT
    assert count_events(session_factory, "output.rejected") == 0


@pytest.mark.parametrize(
    "field",
    ["endpoint_id", "lease_id", "fencing_epoch", "core_epoch", "presence_generation", "persona_id"],
)
def test_any_stale_authorization_field_is_rejected(session_factory, tmp_path, field):
    rig = make_rig(session_factory, tmp_path)
    auth = activate(rig)
    value = getattr(auth, field)
    changed = f"{value}_x" if isinstance(value, str) else value - 1
    stale = AuthorizationTuple(**{**auth.__dict__, field: changed})
    assert rig.gate.admit(stale) is GateDecision.REJECT_STALE


def test_no_presence_rows_and_none_status(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    ghost = AuthorizationTuple(rig.manager.persona_id, "ep", new_id("lease"), 1, 1, 1)
    assert rig.gate.admit(ghost) is GateDecision.REJECT_NO_PRESENCE


def test_valid_until_audience_and_turn_generation(session_factory, tmp_path):
    generations = {"turn_a": 5}
    rig = make_rig(
        session_factory, tmp_path, turn_generation_source=generations.get,
        presence_audience=lambda: "scope_group_g1",
    )
    auth = activate(rig)
    assert rig.gate.admit_intent(intent_for(auth, turn_id="turn_a", turn_generation=5)) is GateDecision.ADMIT
    # 插話後 turn_generation 前進：舊回合輸出被拒
    generations["turn_a"] = 6
    assert rig.gate.admit_intent(intent_for(auth, turn_id="turn_a", turn_generation=5)) is GateDecision.REJECT_STALE
    # audience 比 Presence 可接受的範圍更寬
    cur = {"turn_id": "turn_a", "turn_generation": 6}
    wide = intent_for(auth, audience_scope="scope_public", **cur)
    assert rig.gate.admit_intent(wide) is GateDecision.REJECT_AUDIENCE
    assert rig.gate.admit_intent(intent_for(auth, audience_scope="scope_private_owner", **cur)) is GateDecision.ADMIT
    # 輸出自身的短期限
    expired = intent_for(auth, valid_until=NOW - timedelta(seconds=1), **cur)
    assert rig.gate.admit_intent(expired) is GateDecision.REJECT_EXPIRED


def test_gate_fails_closed_when_sources_unknown(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path, turn_generation_source=lambda _t: None, presence_audience=lambda: None)
    auth = activate(rig)
    # 回合未知 -> STALE；audience 未知 -> REJECT_AUDIENCE；沒有 turn 的主動輸出只看 audience
    assert rig.gate.admit_intent(intent_for(auth)) is GateDecision.REJECT_STALE
    proactive = intent_for(auth, turn_id=None, turn_generation=None, attempt_id=None, activity_id="actv_1")
    assert rig.gate.admit_intent(proactive) is GateDecision.REJECT_AUDIENCE


def test_gate_requires_explicit_sources_or_allow_all(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    with pytest.raises(TypeError):
        OutputGate(session_factory, rig.manager, turn_generation_source=ALLOW_ALL, presence_audience="scope_private_owner")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        OutputGate(session_factory, rig.manager)  # type: ignore[call-arg]


def test_gate_rejects_everything_until_manager_started(session_factory, tmp_path):
    """H1：重啟後 start() 完成前，舊 epoch 的 intent 一律被拒，不論 DB 內容。"""
    rig = make_rig(session_factory, tmp_path)
    old = activate(rig)
    assert rig.gate.admit(old) is GateDecision.ADMIT
    restarting = PresenceManager(session_factory, epoch_store=rig.store, clock=rig.clock)
    gate = OutputGate(
        session_factory, restarting, clock=rig.clock, turn_generation_source=ALLOW_ALL, presence_audience=ALLOW_ALL
    )
    assert gate.admit_intent(intent_for(old)) is GateDecision.REJECT_NO_PRESENCE  # start() 之前
    restarting.start()
    assert gate.admit_intent(intent_for(old)) is GateDecision.REJECT_STALE  # 之後舊 epoch 仍被拒


def test_rejection_is_journaled_with_code_only(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    auth = activate(rig)
    rig.manager.revoke()
    intent = intent_for(auth)
    assert rig.gate.admit_intent(intent) is GateDecision.REJECT_REVOKED
    with session_factory() as s:
        row = s.scalars(select(EventRow).where(EventRow.event_type == "output.rejected")).one()
    assert row.payload == {"reason_code": "REJECT_REVOKED", "ref_id": intent.output_id, "kind": "text"}
    assert row.fence["lease_id"] == auth.lease_id


# ---- DeliveryManager 接 Gate（O-5）----

def _delivery(session_factory, rig, **adapter_kw):
    adapter = FakeOutputAdapter(**adapter_kw)
    return DeliveryManager(session_factory, adapter, gate=rig.gate), adapter


def test_delivery_admitted_output_is_sent(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    auth = activate(rig)
    mgr, adapter = _delivery(session_factory, rig)
    intent = intent_for(auth)
    assert mgr.dispatch(intent).status is S.RENDERED_PLAYED
    assert adapter.send_count(intent.output_id, 0) == 1


def test_delivery_rejected_when_gate_refuses_and_never_resent(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    old = activate(rig)
    rig.manager.revoke()
    mgr, adapter = _delivery(session_factory, rig)
    intent = intent_for(old)
    record = mgr.dispatch(intent)
    assert record.status is S.REJECTED and record.failure == {"code": "REJECT_REVOKED", "retryable": False}
    # 授權之後恢復也不會重送、不遞補（O-5）
    grant = rig.manager.acquire("ep_desktop").grant
    assert grant is not None
    rig.manager.confirm_activated(grant.lease_id)
    again = mgr.dispatch(intent)
    assert again.delivery_id == record.delivery_id and again.status is S.REJECTED
    assert adapter.send_count(intent.output_id, 0) == 0


def test_delivery_rejected_during_activating(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    grant = rig.manager.acquire("ep_desktop").grant
    assert grant is not None
    mgr, adapter = _delivery(session_factory, rig)
    intent = intent_for(grant.authorization)
    record = mgr.dispatch(intent)
    assert record.status is S.REJECTED and record.failure["code"] == "REJECT_NO_PRESENCE"
    assert adapter.send_count(intent.output_id, 0) == 0


def test_synthesized_audio_rejected_becomes_cancelled(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    auth = activate(rig)
    mgr, adapter = _delivery(session_factory, rig)
    intent = intent_for(auth, kind="audio_stream", content_format={"sample_rate": 24000, "channels": 1, "codec": "pcm"})
    record = mgr.prepare(intent)
    mgr.mark_synthesized(record.delivery_id)
    rig.manager.revoke()
    done = mgr.dispatch(intent)
    assert done.status is S.CANCELLED and done.failure["code"] == "REJECT_REVOKED"  # 已越過 generated：cancelled
    assert adapter.send_count(intent.output_id, 0) == 0


class _FlipGate:
    """第一次放行、之後拒絕，模擬 claim 期間授權被撤銷。"""

    def __init__(self) -> None:
        self.calls = 0

    def admit_intent(self, intent):
        self.calls += 1
        return GateDecision.ADMIT if self.calls == 1 else GateDecision.REJECT_REVOKED


def test_gate_rechecked_just_before_send(session_factory, tmp_path):
    adapter = FakeOutputAdapter()
    gate = _FlipGate()
    mgr = DeliveryManager(session_factory, adapter, gate=gate)  # type: ignore[arg-type]
    rig = make_rig(session_factory, tmp_path)
    intent = intent_for(activate(rig))
    record = mgr.dispatch(intent)
    assert gate.calls == 2 and record.status is S.CANCELLED  # enqueued 之後被撤銷記為 cancelled
    assert adapter.send_count(intent.output_id, 0) == 0


# ---- TaskManager 的 Presence 檢查點（D12）----

def _foreground_task(tm, *, presence_bound: bool):
    policy = TaskPolicy(
        presence_bound=presence_bound, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_1"
    )
    task = tm.create_task(
        persona_id="uep", goal="桌面操作", owner_actor_id="owner", scope_ref=SCOPE, policy=policy,
    )
    run = tm.start_run(task.task_id, persona_version=1, policy_version=1)
    step = tm.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    params = {"path": "f0"}
    act = tm.create_action(
        run_id=run.run_id, step_id=step.step_id, tool="files.write", executor="fake", params=params,
        side_effect_class="reversible_local", idempotency="via_action_id", worker_epoch=run.worker_epoch,
    )
    return run, act, params


def test_foreground_action_requires_valid_presence(session_factory):
    present = {"ok": False}
    seen: list[str] = []

    def check(persona_id: str) -> bool:
        seen.append(persona_id)
        return present["ok"]

    executor = FakeToolExecutor()
    tm = make_tm(session_factory, executor, presence_check=check)
    run, act, params = _foreground_task(tm, presence_bound=True)
    with pytest.raises(PresenceRequired):
        tm.execute_action(act.action_id, params, worker_epoch=run.worker_epoch)
    assert seen == ["uep"] and executor.executions[act.action_id] == 0
    assert tm.get_action(act.action_id).status == ActionStatus.INTENDED.value  # 狀態不變
    present["ok"] = True
    tm.execute_action(act.action_id, params, worker_epoch=run.worker_epoch)
    assert executor.executions[act.action_id] == 1


def test_background_action_is_exempt_from_presence_check(session_factory):
    executor = FakeToolExecutor()
    tm = make_tm(session_factory, executor, presence_check=lambda _persona: False)
    run, act, params = _foreground_task(tm, presence_bound=False)
    tm.execute_action(act.action_id, params, worker_epoch=run.worker_epoch)
    assert executor.executions[act.action_id] == 1


def test_presence_check_is_off_by_default(session_factory):
    executor = FakeToolExecutor()
    tm = make_tm(session_factory, executor)
    run, act, params = _foreground_task(tm, presence_bound=True)
    tm.execute_action(act.action_id, params, worker_epoch=run.worker_epoch)
    assert executor.executions[act.action_id] == 1


def test_presence_manager_authorization_works_as_check(session_factory, tmp_path):
    rig = make_rig(session_factory, tmp_path)
    tm = make_tm(
        session_factory, FakeToolExecutor(),
        presence_check=lambda _persona: rig.manager.authorization() is not None,
    )
    run, act, params = _foreground_task(tm, presence_bound=True)
    with pytest.raises(PresenceRequired):
        tm.execute_action(act.action_id, params, worker_epoch=run.worker_epoch)
    activate(rig)
    tm.execute_action(act.action_id, params, worker_epoch=run.worker_epoch)
