"""Core 組裝層（R1 必填前提、啟動順序、CC-7 guard 接線；需 PostgreSQL）。"""
from __future__ import annotations

import json
import urllib.request

import pytest
from pydantic import SecretStr

from tests.uep.presence_support import FakeClock
from tests.uep.state_support import TOKEN, activate_endpoint
from uep.assembly import AssemblyError, CoreAssembly, assemble_core
from uep.contracts.presence import AcquireResult, GateDecision
from uep.control.channel import InProcessControlChannel
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.fakes.output import FakeOutputAdapter
from uep.fakes.tool import FakeToolExecutor
from uep.ids import new_id
from uep.presence.epoch import HostEpochStore
from uep.presence.gate import ALLOW_ALL
from uep.presence.models import PresenceCurrentRow
from uep.state.coordinator import Axis
from uep.stop.api import PATH_STOP
from uep.tasks.manager import IllegalTransition, TaskPolicy
from uep.tasks.states import InterruptPolicy, RunStatus

pytestmark = pytest.mark.usefixtures("session_factory")

BOUND = TaskPolicy(presence_bound=True, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_b")
FREE = TaskPolicy(presence_bound=False, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_f")


def kwargs(session_factory, tmp_path, **over):
    base = {
        "bus": EventBus(), "control": InProcessControlChannel(), "adapter": FakeOutputAdapter(),
        "executors": {"fake": FakeToolExecutor()},
        "turn_generation_source": ALLOW_ALL, "presence_audience": ALLOW_ALL, "allow_gate_bypass": True,
        "epoch_store": HostEpochStore(tmp_path / "host_epoch.json"), "clock": FakeClock(),
        "stop_token": SecretStr(TOKEN), "maintenance_interval": None,  # 測試以手動 tick，不起背景線程
    }
    base.update(over)
    if base.get("publisher") is None and "publisher" not in over:
        base["publisher"] = EventPublisher(session_factory, base["bus"])
    return base


@pytest.fixture
def build(session_factory, tmp_path):
    made: list[CoreAssembly] = []

    def _build(**over) -> CoreAssembly:
        core = assemble_core(session_factory, **kwargs(session_factory, tmp_path, **over))
        made.append(core)
        return core

    yield _build
    for core in made:
        core.shutdown()


# ---- R1：缺 Gate／presence_check 就拒絕組裝 ----

@pytest.mark.parametrize("missing", ["turn_generation_source", "presence_audience"])
def test_missing_gate_providers_refuse_assembly(session_factory, tmp_path, missing):
    with pytest.raises(AssemblyError, match="Gate"):
        assemble_core(session_factory, **kwargs(session_factory, tmp_path, **{missing: None}))


def test_allow_all_requires_explicit_bypass(session_factory, tmp_path):
    with pytest.raises(AssemblyError, match="allow_gate_bypass"):
        assemble_core(session_factory, **kwargs(session_factory, tmp_path, allow_gate_bypass=False))


@pytest.mark.parametrize("missing", ["bus", "publisher", "control", "adapter", "executors"])
def test_missing_core_components_refuse_assembly(session_factory, tmp_path, missing):
    with pytest.raises(AssemblyError, match=missing):
        assemble_core(session_factory, **kwargs(session_factory, tmp_path, **{missing: None}))


def test_assembled_core_has_gate_and_presence_check_wired(build):
    core = build()
    assert core.tasks.presence_check_configured
    assert core.gate is not None and core.delivery is not None


def test_real_gate_providers_do_not_need_bypass(session_factory, tmp_path):
    core = assemble_core(
        session_factory,
        **kwargs(session_factory, tmp_path, turn_generation_source=lambda turn_id: 1,
                 presence_audience=lambda: "scope_private_owner", allow_gate_bypass=False),
    )
    assert core.gate is not None


# ---- 啟動順序 ----

def test_presence_start_precedes_relay_and_gate_rejects_before_start(build, session_factory):
    seen: list[tuple[bool, str]] = []
    holder: list[CoreAssembly] = []

    def relay() -> None:
        core = holder[0]
        seen.append((core.presence.started, core.state.current(Axis.RUNTIME).state))

    core = build(relay_starters=[relay])
    holder.append(core)
    ghost = core.presence  # 尚未 start：Gate 全拒
    assert not ghost.started
    from uep.contracts.presence import AuthorizationTuple
    assert core.gate.admit(AuthorizationTuple("uep", "ep", new_id("lease"), 1, 1, 1)) is GateDecision.REJECT_NO_PRESENCE
    core.start()
    assert seen == [(True, "STARTING")]  # relay 啟動時 presence 已就緒、Runtime 尚未 READY
    assert core.start_log[0] == "presence.start"
    assert core.start_log.index("presence.start") < core.start_log.index("relay.start")
    assert core.start_log[-1] == "state.runtime.ready"
    assert core.state.current(Axis.RUNTIME).state == "READY"
    core.start()  # 冪等
    assert core.start_log.count("presence.start") == 1


def test_presence_start_failure_starts_nothing_else(build, monkeypatch):
    called: list[str] = []
    core = build(relay_starters=[lambda: called.append("relay")])

    def fail() -> None:
        raise RuntimeError("epoch 檔落後 DB")

    monkeypatch.setattr(core.presence, "start", fail)
    with pytest.raises(RuntimeError):
        core.start()
    assert called == [] and core.start_log == [] and not core.started
    assert core.state.current(Axis.RUNTIME).version == 0


def test_start_syncs_presence_projection_and_pauses_bound_tasks_after_restart(build, session_factory, tmp_path):
    first = build()
    first.start()
    activate_endpoint(first.presence)
    bound = first.tasks.create_task(
        persona_id="uep", goal="g", owner_actor_id="o", scope_ref="scope_private_owner", policy=BOUND
    )
    first.tasks.start_run(bound.task_id, persona_version=1, policy_version=1)
    first.shutdown()
    # 同 DB、同 Host epoch 檔重啟：舊 Lease -> RECOVERING，bound Task 由 reconcile 暫停
    second = build()
    second.start()
    assert second.state.current(Axis.PRESENCE).state == "RECOVERING"
    assert second.tasks.get_task(bound.task_id).status == RunStatus.PAUSED.value


# ---- CC-7 guard 與全域停止接線 ----

def test_global_stop_blocks_task_running_and_reacquire_until_release(build, session_factory):
    core = build()
    core.start()
    free = core.tasks.create_task(
        persona_id="uep", goal="g", owner_actor_id="o", scope_ref="scope_private_owner", policy=FREE
    )
    run = core.tasks.start_run(free.task_id, persona_version=1, policy_version=1)
    core.tasks.pause_task(free.task_id, __import__("uep.tasks.states", fromlist=["PauseReason"]).PauseReason.USER)
    core.stop.trigger(TOKEN)
    with pytest.raises(IllegalTransition, match="global_stop_engaged"):
        core.tasks.transition_run(run.run_id, RunStatus.RUNNING)
    other = core.tasks.create_task(
        persona_id="uep", goal="g2", owner_actor_id="o", scope_ref="scope_private_owner", policy=FREE
    )
    with pytest.raises(IllegalTransition):
        core.tasks.start_run(other.task_id, persona_version=1, policy_version=1)
    assert core.presence.acquire("ep_desktop", "desktop").result is AcquireResult.DENIED
    core.stop.release(TOKEN)
    assert core.tasks.transition_run(run.run_id, RunStatus.RUNNING).status == RunStatus.RUNNING.value
    assert core.presence.acquire("ep_desktop", "desktop").result is AcquireResult.GRANTED


def test_stop_api_end_to_end_through_assembly(build, session_factory):
    core = build(stop_api_port=0)
    core.start()
    assert core.stop_api is not None
    request = urllib.request.Request(core.stop_api.url + PATH_STOP, data=b"{}", method="POST")
    request.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(request, timeout=5) as resp:
        assert json.loads(resp.read())["flag_engaged"] is True
    assert core.presence.acquire("ep_desktop", "desktop").result is AcquireResult.DENIED
    with session_factory() as s:
        assert s.get(PresenceCurrentRow, "uep") is not None


def test_endpoint_socket_is_optional_and_needs_secrets(build):
    assert build(endpoint_socket_port=0, endpoint_secrets={}).endpoint_server is None
    core = build(endpoint_socket_port=0, endpoint_secrets={"ep_cli": b"s" * 32})
    assert core.endpoint_server is not None
    core.start()
    assert core.start_log.index("presence.start") < core.start_log.index("endpoint_server.start")


# ---- 審查修正 ----

def _bg_action(core, executor, *, side_effect: str):
    task = core.tasks.create_task(
        persona_id="uep", goal="g", owner_actor_id="owner", scope_ref="scope_private_owner", policy=FREE
    )
    run = core.tasks.start_run(task.task_id, persona_version=1, policy_version=1)  # stop 之前已 RUNNING
    step = core.tasks.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    params = {"path": "a"}
    action = core.tasks.create_action(
        run_id=run.run_id, step_id=step.step_id, tool="files.write", executor="fake", params=params,
        side_effect_class=side_effect, idempotency="via_action_id", worker_epoch=run.worker_epoch,
    )
    return run, action, params


def test_stop_blocks_new_tool_execution_of_already_running_background_task(build, session_factory):
    """T-10：start_run guard 之後旗標才提交；已 RUNNING 的背景 run 仍不得執行新工具，且核准不被燒掉。"""
    from datetime import timedelta

    from uep.tasks.manager import ActionBlocked, AuthenticatedPrincipal, params_hash
    from uep.tasks.models import ApprovalRow

    executor = FakeToolExecutor()
    core = build(executors={"fake": executor})
    core.start()
    run, action, params = _bg_action(core, executor, side_effect="external_effect")
    approval = core.tasks.record_approval(
        principal=AuthenticatedPrincipal(actor_id="owner", kind="human", verified=True),
        action_hash=params_hash(params), target="files.write", scope="scope_private_owner",
        expiry=FakeClock().now + timedelta(hours=1),
    )
    core.stop.trigger(TOKEN)
    with pytest.raises(ActionBlocked, match="global_stop_engaged"):
        core.tasks.execute_action(
            action.action_id, params, worker_epoch=run.worker_epoch, approval_id=approval.approval_id
        )
    assert executor.calls == []  # executor 未被呼叫
    assert core.tasks.get_action(action.action_id).status == "INTENDED"  # 沒轉 EXECUTING
    with session_factory() as s:
        row = s.get(ApprovalRow, approval.approval_id)
        assert row.consumed_at is None and row.consumed_by_action_id is None  # 核准未被消耗
    core.stop.release(TOKEN)
    outcome = core.tasks.execute_action(
        action.action_id, params, worker_epoch=run.worker_epoch, approval_id=approval.approval_id
    )
    assert outcome.executed and executor.calls  # 解除後可正常執行，核准仍有效


def test_stop_does_not_block_resolve_unknown_or_receipt_recording(build):
    executor = FakeToolExecutor()
    core = build(executors={"fake": executor})
    core.start()
    run, action, params = _bg_action(core, executor, side_effect="internal_read")
    executor.execute_behavior(action.action_id, "lose_after_exec")
    assert core.tasks.execute_action(action.action_id, params, worker_epoch=run.worker_epoch).status.value == "UNKNOWN"
    core.stop.trigger(TOKEN)
    resolved = core.tasks.resolve_unknown(action.action_id)  # 查核已發生副作用的結果不擋
    assert resolved.status.value == "SUCCEEDED"


def test_stop_api_starts_right_after_presence_and_bus_before_relay_and_endpoint(build):
    core = build(stop_api_port=0, endpoint_socket_port=0, endpoint_secrets={"ep_cli": b"s" * 32},
                 relay_starters=[lambda: None])
    core.start()
    log = core.start_log
    assert log[:3] == ["presence.start", "bus.start", "stop_api.start"]
    assert log.index("stop_api.start") < log.index("relay.start") < log.index("endpoint_server.start")


def test_start_failure_closes_bound_stop_api(build, monkeypatch):
    import socket

    core = build(stop_api_port=0, relay_starters=[lambda: (_ for _ in ()).throw(RuntimeError("relay 壞了"))])
    port = core.stop_api.port
    with pytest.raises(RuntimeError, match="relay"):
        core.start()
    assert not core.started
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", port)) != 0  # 入口已關閉，不留半啟動的停止 API


def test_assembly_refuses_when_taskmanager_loses_guards(session_factory, tmp_path, monkeypatch):
    import uep.assembly as assembly_module

    real = assembly_module.TaskManager
    for dropped in ("presence_check", "transition_guard", "action_guard"):
        def factory(*args, _dropped=dropped, **kw):
            kw[_dropped] = None
            return real(*args, **kw)

        monkeypatch.setattr(assembly_module, "TaskManager", factory)
        with pytest.raises(AssemblyError, match=dropped):
            assemble_core(session_factory, **kwargs(session_factory, tmp_path))


def test_assembly_refuses_when_delivery_manager_has_no_gate(session_factory, tmp_path, monkeypatch):
    import uep.assembly as assembly_module

    real = assembly_module.DeliveryManager
    monkeypatch.setattr(assembly_module, "DeliveryManager", lambda sf, adapter, **kw: real(sf, adapter))
    with pytest.raises(AssemblyError, match="Output Gate"):
        assemble_core(session_factory, **kwargs(session_factory, tmp_path))


def test_fanout_isolates_failures_on_each_side(build):
    from uep.assembly import _FanoutControl
    from uep.control.channel import ControlSendResult
    from uep.output.intent import ControlKind, ControlScope, ControlSignal

    signal = ControlSignal(
        control_id=new_id("ctl"), kind=ControlKind.REVOKE, scope=ControlScope.PROCESS,
        target_fence={"core_epoch": 1}, issued_at=FakeClock().now,
    )

    class Boom:
        def send(self, signal, *, endpoint_id=None):
            raise RuntimeError("down")

    class Ok:
        def __init__(self, name):
            self.name = name

        def send(self, signal, *, endpoint_id=None):
            return ControlSendResult(signal.control_id, (self.name,), False)

    local_fails = _FanoutControl(Boom())
    local_fails.remote = Ok("remote_ep")
    assert local_fails.send(signal).delivered_to == ("remote_ep",)  # 本地壞了，遠端仍送
    remote_fails = _FanoutControl(Ok("local_ep"))
    remote_fails.remote = Boom()
    assert remote_fails.send(signal).delivered_to == ("local_ep",)
    both = _FanoutControl(Boom())
    both.remote = Boom()
    with pytest.raises(RuntimeError):
        both.send(signal)


# ---- action_guard 殘留窗口：旗標共享鎖持有到階段 1 提交 ----

@pytest.mark.parametrize("flag_row_exists", [False, True], ids=["flag_row_absent", "flag_row_present"])
def test_engage_between_guard_read_and_stage1_commit_waits_and_no_new_tool_starts_after(
    build, session_factory, flag_row_exists
):
    import threading

    executor = FakeToolExecutor()
    core = build(executors={"fake": executor})
    core.start()
    if flag_row_exists:
        core.stop.trigger(TOKEN)
        core.stop.release(TOKEN)  # 旗標列存在且 engaged=false
    run, action, params = _bg_action(core, executor, side_effect="internal_read")
    second_run, second_action, second_params = _bg_action(core, executor, side_effect="internal_read")

    read_done, let_commit = threading.Event(), threading.Event()
    original = core.tasks._action_guard

    def paused_guard(session):
        result = original(session)  # 讀旗標（取共享鎖）
        read_done.set()
        assert let_commit.wait(5), "測試卡住"
        return result

    core.tasks._action_guard = paused_guard
    outcome: dict = {}
    engage_done = threading.Event()

    def worker() -> None:
        outcome["a"] = core.tasks.execute_action(action.action_id, params, worker_epoch=run.worker_epoch)

    def stopper() -> None:
        assert read_done.wait(5)
        outcome["stop"] = core.stop.trigger(TOKEN)
        engage_done.set()

    t_a, t_b = threading.Thread(target=worker), threading.Thread(target=stopper)
    t_a.start()
    t_b.start()
    assert read_done.wait(5)
    # guard 已讀旗標、階段 1 尚未提交：engage 必須被擋住，不能插進來
    assert not engage_done.wait(0.7)
    let_commit.set()
    t_a.join(10)
    t_b.join(10)
    assert engage_done.is_set() and outcome["stop"].flag_engaged
    assert outcome["a"].executed and executor.calls  # 階段 1 已通過者可完成（它在 engage 之前提交）
    started = len(executor.calls)
    # engage 回傳之後：任何新工具都不得開始
    core.tasks._action_guard = original
    from uep.tasks.manager import ActionBlocked

    with pytest.raises(ActionBlocked, match="global_stop_engaged"):
        core.tasks.execute_action(second_action.action_id, second_params, worker_epoch=second_run.worker_epoch)
    assert len(executor.calls) == started
