"""D69：``ToolExecutor.cancel`` 可選協定、組裝層 fail-closed 檢查、Supervisor force 前的在途 action 取消。

離線部分用記憶體替身；打真實 Manager 的案例需 UEP_TEST_DATABASE_URL（未設時 skip）。
"""
from __future__ import annotations

import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from tests.uep.test_supervisor import T0, FakeManager, at
from tests.uep.test_supervisor_db import Env, past
from tests.uep.test_task_model import StubExecutor, add_action
from uep.adapters.tool import CancellableToolExecutor, ToolExecutor, supports_cancel
from uep.assembly import AssemblyError, _check_registry_executors
from uep.fakes.tool import FakeToolExecutor
from uep.fakes.tool_spec import FakeToolSpecResolver
from uep.tasks.models import ActionIntentRow
from uep.tasks.supervisor import Supervisor
from uep.tools.cancel import ActionCanceller


class NoCancelExecutor:
    """只實作原協定的 executor（沒有 cancel）。"""

    lookup_authoritative = True

    def execute(self, call):  # pragma: no cover - 不會被呼叫
        raise NotImplementedError

    def lookup_receipt(self, action_id):  # pragma: no cover
        raise NotImplementedError


class FakeTasks:
    def __init__(self, *actions: SimpleNamespace) -> None:
        self.actions = list(actions)

    def list_executing_actions(self, run_id: str):
        return [a for a in self.actions if a.run_id == run_id]


def action(action_id: str, tool: str = "t.run", executor: str = "fake", run_id: str = "run_1") -> SimpleNamespace:
    return SimpleNamespace(action_id=action_id, tool=tool, executor=executor, run_id=run_id)


# ---- 協定與 cancellable 判定 ----

def test_supports_cancel_distinguishes_executors():
    assert supports_cancel(FakeToolExecutor()) and isinstance(FakeToolExecutor(), CancellableToolExecutor)
    assert not supports_cancel(NoCancelExecutor()) and not isinstance(NoCancelExecutor(), CancellableToolExecutor)
    assert isinstance(NoCancelExecutor(), ToolExecutor)  # 沒有 cancel 仍是合法 ToolExecutor（可選能力）


# ---- 組裝層 fail-closed（RA-5、D69） ----

@pytest.mark.parametrize("kind", ["interruptible", "checkpoint_only"])
def test_interruptible_tool_without_cancel_is_rejected(kind):
    resolver = FakeToolSpecResolver(**{"t.run": {"executor": "x", "interruptible": kind}})
    with pytest.raises(AssemblyError, match="t.run"):
        _check_registry_executors(resolver, {"x": NoCancelExecutor()}, ())


def test_interruptible_tool_with_cancel_and_non_interruptible_without_cancel_pass():
    resolver = FakeToolSpecResolver(**{
        "t.run": {"executor": "x", "interruptible": "interruptible"},
        "t.read": {"executor": "y"},  # 預設 non_interruptible，executor 沒有 cancel 也合法
    })
    _check_registry_executors(resolver, {"x": FakeToolExecutor(), "y": NoCancelExecutor()}, ())


def test_cancellable_entry_without_cancel_is_rejected():
    spec = SimpleNamespace(executor="x", interruptible="non_interruptible")
    resolver = SimpleNamespace(
        specs=lambda: {"t.run": spec}, entries=lambda: {"t.run": SimpleNamespace(cancellable=True)},
    )
    # 直接測規則函式：宣告 cancellable 但 executor 無 cancel（RA-11 的 one_shot 資格依賴這個判定）
    from uep.assembly import _check_cancel_support

    with pytest.raises(AssemblyError, match="t.run"):
        _check_cancel_support(resolver, resolver.specs(), {"x": NoCancelExecutor()})
    _check_cancel_support(resolver, resolver.specs(), {"x": FakeToolExecutor()})


def test_real_tool_registry_cancellable_entry_without_cancel_is_rejected():
    from tests.uep.test_tool_registry import make_entry
    from uep.tools.registry import ToolRegistry

    registry = ToolRegistry.build([make_entry(spec={"executor": "x", "interruptible": "non_interruptible"}, cancellable=True)])
    with pytest.raises(AssemblyError, match="demo.tool"):
        _check_registry_executors(registry, {"x": NoCancelExecutor()}, ())
    _check_registry_executors(registry, {"x": FakeToolExecutor()}, ())  # 有 cancel 就通過
    plain = ToolRegistry.build([make_entry(spec={"executor": "x"})])  # 未宣告 cancellable／interruptible：不要求 cancel
    _check_registry_executors(plain, {"x": NoCancelExecutor()}, ())


# ---- ActionCanceller ----

def make_canceller(*actions, **tools):
    fake = FakeToolExecutor()
    resolver = FakeToolSpecResolver(**{
        "t.run": {"executor": "fake", "interruptible": "interruptible"},
        "t.check": {"executor": "fake", "interruptible": "checkpoint_only"},
        "t.hold": {"executor": "fake", "interruptible": "non_interruptible"},
        **tools,
    })
    return fake, ActionCanceller(FakeTasks(*actions), {"fake": fake}, resolver)


def test_cancels_interruptible_actions_only():
    fake, canceller = make_canceller(
        action("a1", "t.run"), action("a2", "t.check"), action("a3", "t.hold"),
    )
    assert canceller.cancel_run_actions("run_1") == {"a1": "stopped", "a2": "skipped", "a3": "skipped"}
    assert fake.cancels == ["a1"]  # checkpoint_only 與 non_interruptible 不呼叫 cancel


def test_isolated_outcome_is_reported_not_swallowed():
    fake, canceller = make_canceller(action("a1", "t.run"))
    fake.cancel_behavior("a1", "isolated")
    assert canceller.cancel_run_actions("run_1") == {"a1": "isolated"}


def test_cancel_exception_is_contained_and_other_actions_still_cancelled():
    fake, canceller = make_canceller(action("a1", "t.run"), action("a2", "t.run"))
    fake.cancel_behavior("a1", "raise")
    assert canceller.cancel_run_actions("run_1") == {"a1": "error", "a2": "stopped"}
    assert fake.cancels == ["a1", "a2"]


def test_executor_without_cancel_or_unresolvable_tool_is_skipped():
    resolver = FakeToolSpecResolver(**{"t.run": {"executor": "x", "interruptible": "interruptible"}})
    canceller = ActionCanceller(FakeTasks(action("a1", "t.run", "x"), action("a2", "t.gone", "x")), {"x": NoCancelExecutor()}, resolver)
    assert canceller.cancel_run_actions("run_1") == {"a1": "skipped", "a2": "skipped"}
    fake = FakeToolExecutor()
    canceller = ActionCanceller(FakeTasks(action("a3", "t.gone")), {"fake": fake}, resolver)
    assert canceller.cancel_run_actions("run_1") == {"a3": "skipped"}  # 解析不到宣告＝fail-closed
    assert fake.cancels == []


def test_invalid_cancel_return_is_treated_as_unknown_outcome():
    fake, canceller = make_canceller(action("a1", "t.run"))
    fake.cancel = lambda action_id: "maybe"  # type: ignore[method-assign]
    assert canceller.cancel_run_actions("run_1") == {"a1": "error"}


# ---- Supervisor 接線（離線） ----

def forced_supervisor(cancel_actions):
    manager = FakeManager()
    manager.add(run_deadline=T0 - timedelta(seconds=1), beat=T0)
    sup = Supervisor(manager, clock=lambda: manager.now, cancel_actions=cancel_actions)  # type: ignore[arg-type]
    sup.tick(T0)
    manager.runs["run_1"].worker_heartbeat_at = T0 + timedelta(seconds=30)
    return manager, sup


def test_supervisor_snapshots_before_force_and_cancels_after_force():
    manager, sup = None, None
    order: list[str] = []

    def prepare(run_id):
        order.append(f"snapshot:{run_id}:force_called={any(c[0] == 'force' for c in manager.calls)}")

        def run_cancel():
            order.append(f"cancel:force_called={any(c[0] == 'force' for c in manager.calls)}")
            return {"a1": "stopped", "a2": "skipped"}

        return run_cancel

    manager, sup = forced_supervisor(prepare)
    stats = sup.tick(at(manager, 31))
    assert (stats.forced, stats.actions_cancelled) == (1, 1)  # skipped 不計
    assert order == ["snapshot:run_1:force_called=False", "cancel:force_called=True"]  # cancel 不延後 force
    assert manager.tasks["task_run_1"].status == "FAILED"


def test_supervisor_still_forces_when_snapshot_raises():
    def boom(_run_id):
        raise RuntimeError("快照失敗")

    manager, sup = forced_supervisor(boom)
    stats = sup.tick(at(manager, 31))
    assert (stats.forced, stats.actions_cancelled, stats.failures) == (1, 0, 0)
    assert manager.tasks["task_run_1"].status == "FAILED"


def test_supervisor_survives_cancel_raising_or_non_mapping_result():
    for bad in (RuntimeError("cancel 通道失敗"), None, ["a1"], 42):
        def prepare(_run_id, bad=bad):
            def run_cancel():
                if isinstance(bad, Exception):
                    raise bad
                return bad
            return run_cancel

        manager, sup = forced_supervisor(prepare)
        stats = sup.tick(at(manager, 31))
        assert (stats.forced, stats.actions_cancelled, stats.failures) == (1, 0, 0), bad
        assert manager.tasks["task_run_1"].status == "FAILED"


def test_supervisor_does_not_cancel_while_grace_has_not_expired():
    calls: list[str] = []
    manager, sup = forced_supervisor(lambda run_id: calls.append(run_id) or (dict))
    assert sup.tick(at(manager, 10)).forced == 0
    assert calls == []


def test_blocking_cancel_times_out_as_isolated_and_does_not_stall_the_tick():
    release = threading.Event()
    fake = FakeToolExecutor()
    fake.cancel = lambda action_id: release.wait(30) and "stopped"  # type: ignore[method-assign]
    resolver = FakeToolSpecResolver(**{"t.run": {"executor": "fake", "interruptible": "interruptible"}})
    tasks = FakeTasks(*(action(f"a{i}", "t.run") for i in range(3)))
    canceller = ActionCanceller(tasks, {"fake": fake}, resolver, timeout_s=0.3)
    manager, sup = forced_supervisor(canceller.prepare)
    started = time.monotonic()
    try:
        stats = sup.tick(at(manager, 31))
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 2.0  # 3 個卡住的 cancel 平行等待、總逾時 0.3s，不累加
    assert (stats.forced, stats.actions_cancelled) == (1, 3)
    assert manager.tasks["task_run_1"].status == "FAILED"
    assert canceller.prepare("run_1")  # 仍可重複使用


def test_timeout_outcome_is_isolated():
    release = threading.Event()
    fake = FakeToolExecutor()
    fake.cancel = lambda action_id: release.wait(30) and "stopped"  # type: ignore[method-assign]
    resolver = FakeToolSpecResolver(**{"t.run": {"executor": "fake", "interruptible": "interruptible"}})
    canceller = ActionCanceller(FakeTasks(action("a1", "t.run")), {"fake": fake}, resolver, timeout_s=0.2)
    try:
        assert canceller.cancel_run_actions("run_1") == {"a1": "isolated"}
    finally:
        release.set()


def test_canceller_rejects_non_positive_timeout():
    with pytest.raises(ValueError):
        ActionCanceller(FakeTasks(), {}, FakeToolSpecResolver(), timeout_s=0)


# ---- 對 PostgreSQL（真實 Manager） ----

class CancellableStub(StubExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []

    def cancel(self, action_id: str) -> str:
        self.cancelled.append(action_id)
        return "stopped"


@pytest.mark.usefixtures("session_factory")
def test_supervisor_force_cancels_executing_interruptible_action_and_leaves_it_unknown(session_factory):
    executor = CancellableStub()
    env = Env(session_factory, run_deadline=past(), executor=executor)
    step = env.mgr.begin_step(env.run.run_id, "work", worker_epoch=env.epoch)
    act = add_action(env.mgr, env.run, step, {"n": 1})
    # add_action 會以預設分類覆寫登記，所以在建立 action 之後才改宣告可中斷
    env.mgr.tool_resolver.register("files.write", executor="stub", side_effect_class="internal_read",
                                   idempotency="via_action_id", tool_version=1, interruptible="interruptible")
    with session_factory.begin() as s:
        s.execute(update(ActionIntentRow).where(ActionIntentRow.action_id == act.action_id).values(status="EXECUTING"))
    assert [a.action_id for a in env.mgr.list_executing_actions(env.run.run_id)] == [act.action_id]
    canceller = ActionCanceller(env.mgr, {"stub": executor}, env.mgr.tool_resolver)
    sup = Supervisor(env.mgr, deadline_grace=timedelta(seconds=30), clock=env.clock, cancel_actions=canceller.prepare)
    sup.tick(env.clock.now)
    env.mgr.heartbeat(env.run.run_id, worker_epoch=env.epoch)
    stats = sup.tick(env.clock.advance(31))
    assert (stats.forced, stats.actions_cancelled) == (1, 1)
    assert executor.cancelled == [act.action_id]
    # cancel 不改 action 狀態：force 照舊把 EXECUTING 轉 UNKNOWN，交 resolve_unknown 查核（T-3）
    assert env.mgr.get_action(act.action_id).status == "UNKNOWN"
    assert env.mgr.list_executing_actions(env.run.run_id) == []


def test_stuck_cancel_does_not_keep_the_process_alive():
    """M2：卡住的 executor.cancel 跑在 daemon 執行緒；行程必須能在 cancel 還卡著時正常結束。"""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import threading
        from types import SimpleNamespace
        from uep.fakes.tool import FakeToolExecutor
        from uep.fakes.tool_spec import FakeToolSpecResolver
        from uep.tools.cancel import ActionCanceller

        fake = FakeToolExecutor()
        fake.cancel = lambda action_id: threading.Event().wait(600)  # 永遠卡住
        resolver = FakeToolSpecResolver(**{"t.run": {"executor": "fake", "interruptible": "interruptible"}})
        act = SimpleNamespace(action_id="a1", executor="fake", tool="t.run")
        tasks = SimpleNamespace(list_executing_actions=lambda run_id: [act])
        out = ActionCanceller(tasks, {"fake": fake}, resolver, timeout_s=0.3).cancel_run_actions("run_1")
        assert out == {"a1": "isolated"}, out
        print("done")
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0 and "done" in proc.stdout, proc.stderr
