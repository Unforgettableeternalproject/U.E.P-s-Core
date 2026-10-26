"""presence 維護迴圈（T-23）：定期 expire_due 與 bridge.reconcile，例外隔離，失敗下次補上。

單元測試以手動 ``tick()`` 與注入的等待函式驅動，不 sleep 真時間；組裝層測試需 PostgreSQL。
"""
import threading

import pytest

from tests.uep.state_support import activate_endpoint, make_stack
from tests.uep.test_assembly import kwargs
from uep.assembly import CoreAssembly, assemble_core
from uep.contracts.presence import PresenceStatus
from uep.fakes.tool import FakeToolExecutor
from uep.state.coordinator import Axis
from uep.tasks.manager import TaskManager, TaskPolicy
from uep.tasks.presence_bridge import PresenceMaintenance, PresenceTaskBridge
from uep.tasks.states import InterruptPolicy, RunStatus

BOUND = TaskPolicy(presence_bound=True, interrupt_policy=InterruptPolicy.INTERRUPTIBLE, policy_ref="pol_b")


def test_tick_runs_expire_then_reconcile_in_order():
    calls = []
    m = PresenceMaintenance(lambda: calls.append("expire") or False, lambda: calls.append("reconcile") or [])
    m.tick()
    m.tick()
    assert calls == ["expire", "reconcile", "expire", "reconcile"] and m.ticks == 2


def test_expire_failure_is_isolated_and_reconcile_still_runs(caplog):
    calls = []

    def expire():
        calls.append("expire")
        raise RuntimeError("db 暫時不可用")

    m = PresenceMaintenance(expire, lambda: calls.append("reconcile") or [])
    m.tick()
    assert calls == ["expire", "reconcile"] and m.expire_failures == 1 and m.reconcile_failures == 0
    assert "到期掃描失敗" in caplog.text


def test_reconcile_failure_is_isolated_and_retried_next_tick():
    attempts = []

    def reconcile():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return ["tsk_1"]

    m = PresenceMaintenance(lambda: False, reconcile)
    m.tick()  # 第一次失敗不外洩
    m.tick()  # 下一輪補上
    assert len(attempts) == 2 and m.reconcile_failures == 1 and m.ticks == 2


def test_background_loop_ticks_on_injected_wait_and_stops():
    waits, ticked = [], threading.Semaphore(0)
    release = threading.Semaphore(0)

    def wait(timeout):
        waits.append(timeout)
        return not release.acquire(timeout=5)  # 放行一次才進下一輪；逾時視為停止

    m = PresenceMaintenance(lambda: ticked.release() or False, list, interval=7.5, wait=wait)
    m.start()
    m.start()  # 冪等
    release.release()
    assert ticked.acquire(timeout=5)
    release.release()
    assert ticked.acquire(timeout=5)
    m.stop()
    assert m.ticks == 2 and waits[0] == 7.5 and m._thread is None


def test_stop_interrupts_default_wait_without_real_interval():
    m = PresenceMaintenance(lambda: False, list, interval=3600.0)  # 預設 Event.wait：stop 立即喚醒
    m.start()
    m.stop(timeout=5)
    assert m._thread is None and m.ticks == 0


def test_interval_must_be_positive():
    with pytest.raises(ValueError):
        PresenceMaintenance(lambda: False, list, interval=0)


# ---- 需要 PostgreSQL ----

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


def test_assembly_wires_maintenance_and_stops_it(build):
    core = build(maintenance_interval=3600.0)
    assert core.maintenance is not None
    core.start()
    assert core.start_log.index("bridge.reconcile") < core.start_log.index("maintenance.start")
    assert core.maintenance._thread is not None and core.maintenance._thread.is_alive()
    core.shutdown()
    assert core.maintenance._thread is None


def test_assembly_can_disable_maintenance(build):
    core = build(maintenance_interval=None)
    assert core.maintenance is None
    core.start()
    assert "maintenance.start" not in core.start_log


def test_assembly_default_interval_enabled(session_factory, tmp_path):
    base = kwargs(session_factory, tmp_path)
    base.pop("maintenance_interval")
    core = assemble_core(session_factory, **base)
    assert core.maintenance is not None  # 預設啟用（尚未 start，不會起線程）


def test_assembly_failed_start_closes_maintenance(build):
    core = build(maintenance_interval=3600.0)
    core.maintenance.start()
    core._close_inputs()
    assert core.maintenance._thread is None


def test_tick_expires_lease_and_failed_bridge_is_compensated_next_tick(session_factory, tmp_path):
    """Lease 到期由 tick 掃出（沒有任何 acquire／renew 觸發）；橋接 pause 失敗一次，下個 tick 補上。"""
    stack = make_stack(session_factory, tmp_path)
    tasks = TaskManager(
        session_factory, stack.publisher, {"fake": FakeToolExecutor()},
        presence_check=lambda persona: stack.manager.current().status is PresenceStatus.ACTIVE,
    )
    bridge = PresenceTaskBridge(stack.bus, tasks, lambda: stack.manager.current().status)  # 不 start Bus：只靠 reconcile
    activate_endpoint(stack.manager)
    task = tasks.create_task(
        persona_id="uep", goal="g", owner_actor_id="owner", scope_ref="scope_private_owner", policy=BOUND
    )
    tasks.start_run(task.task_id, persona_version=1, policy_version=1)

    real, calls = tasks.pause_presence_bound, {"n": 0}

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("暫時失敗")
        return real(*args, **kw)

    tasks.pause_presence_bound = flaky  # type: ignore[method-assign]
    maintenance = PresenceMaintenance(stack.manager.expire_due, bridge.reconcile)

    maintenance.tick()  # Lease 尚未到期：Presence 仍 ACTIVE，reconcile 不暫停
    assert stack.manager.current().status is PresenceStatus.ACTIVE and calls["n"] == 0

    stack.clock.advance(100_000)  # 推過 Lease TTL
    maintenance.tick()  # expire_due 撤銷；reconcile 的 pause 失敗（被隔離）
    assert stack.manager.current().status is PresenceStatus.NONE
    assert maintenance.reconcile_failures == 1
    assert tasks.get_task(task.task_id).status == RunStatus.RUNNING.value

    maintenance.tick()  # 下一輪補上
    assert tasks.get_task(task.task_id).status == RunStatus.PAUSED.value
    assert maintenance.reconcile_failures == 1


def test_project_step_runs_last_and_is_isolated():
    calls = []
    boom = {"on": True}

    def project():
        calls.append("project")
        if boom["on"]:
            raise RuntimeError("x")

    m = PresenceMaintenance(
        lambda: calls.append("expire") or False, lambda: calls.append("reconcile") or [], project=project
    )
    m.tick()
    assert calls == ["expire", "reconcile", "project"] and m.project_failures == 1
    boom["on"] = False
    m.tick()
    assert m.project_failures == 1 and m.ticks == 2


def test_tick_repairs_state_presence_axis_after_bus_dropped_presence_events(session_factory, tmp_path):
    """Bus 沒投遞 presence 事件（丟棄）時，State 的 Presence 軸停在舊值；下一個 tick 以權威狀態修正，且冪等。"""
    stack = make_stack(session_factory, tmp_path)  # Bus 未訂閱／未啟動：等同事件被丟棄
    stack.state.project(Axis.PRESENCE, "NONE", source_version=stack.manager.current().row_version)
    activate_endpoint(stack.manager)
    assert stack.state.current(Axis.PRESENCE).state == "NONE"  # 過期

    def reproject():
        view = stack.manager.current()
        return stack.state.project(Axis.PRESENCE, view.status.value, source_version=view.row_version)

    m = PresenceMaintenance(stack.manager.expire_due, list, project=reproject)
    m.tick()
    assert stack.state.current(Axis.PRESENCE).state == "ACTIVE"
    version = stack.state.current(Axis.PRESENCE).version
    m.tick()  # 冪等：沒有變化不再寫入
    assert stack.state.current(Axis.PRESENCE).version == version and m.project_failures == 0


def test_assembly_maintenance_includes_presence_reprojection(build):
    core = build(maintenance_interval=3600.0)
    core.start()
    activate_endpoint(core.presence)
    core.bus.stop()  # 模擬事件被丟棄：之後 publish 不會到達 State
    core.maintenance.tick()
    assert core.state.current(Axis.PRESENCE).state == "ACTIVE"


def test_assembly_warns_when_payload_hash_key_missing_or_short(session_factory, tmp_path, monkeypatch, caplog):
    from pydantic import SecretStr

    from uep.config import get_settings

    for value, expected in ((None, "未設定 UEP_PAYLOAD_HASH_KEY"), (SecretStr("short"), "少於")):
        monkeypatch.setattr(get_settings(), "payload_hash_key", value)
        caplog.clear()
        with caplog.at_level("WARNING", logger="uep.assembly"):
            assemble_core(session_factory, **kwargs(session_factory, tmp_path))  # 不阻擋組裝
        assert expected in caplog.text


def test_assembly_no_warning_with_valid_key(session_factory, tmp_path, caplog):
    with caplog.at_level("WARNING", logger="uep.assembly"):
        assemble_core(session_factory, **kwargs(session_factory, tmp_path))
    assert "UEP_PAYLOAD_HASH_KEY" not in caplog.text
