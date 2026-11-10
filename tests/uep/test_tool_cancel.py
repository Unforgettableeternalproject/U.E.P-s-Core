"""D69：``ToolExecutor.cancel`` 可選協定、組裝層 fail-closed 檢查、Supervisor force 前的在途 action 取消。

離線部分用記憶體替身；打真實 Manager 的案例需 UEP_TEST_DATABASE_URL（未設時 skip）。
"""
from __future__ import annotations

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


def test_supervisor_cancels_in_flight_actions_before_force():
    order: list[str] = []
    manager, sup = forced_supervisor(lambda run_id: order.append(f"cancel:{run_id}") or {"a1": "stopped", "a2": "skipped"})
    stats = sup.tick(at(manager, 31))
    assert (stats.forced, stats.actions_cancelled) == (1, 1)  # skipped 不計
    assert order == ["cancel:run_1"]
    assert ("force", "run_1", "FAILED", "run_deadline") in manager.calls


def test_supervisor_still_forces_when_cancel_hook_raises():
    def boom(_run_id):
        raise RuntimeError("cancel 通道失敗")

    manager, sup = forced_supervisor(boom)
    stats = sup.tick(at(manager, 31))
    assert (stats.forced, stats.actions_cancelled, stats.failures) == (1, 0, 0)  # 取消是盡力而為，不算 tick 失敗
    assert manager.tasks["task_run_1"].status == "FAILED"


def test_supervisor_does_not_cancel_while_grace_has_not_expired():
    calls: list[str] = []
    manager, sup = forced_supervisor(lambda run_id: calls.append(run_id) or {})
    assert sup.tick(at(manager, 10)).forced == 0
    assert calls == []


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
    sup = Supervisor(env.mgr, deadline_grace=timedelta(seconds=30), clock=env.clock, cancel_actions=canceller.cancel_run_actions)
    sup.tick(env.clock.now)
    env.mgr.heartbeat(env.run.run_id, worker_epoch=env.epoch)
    stats = sup.tick(env.clock.advance(31))
    assert (stats.forced, stats.actions_cancelled) == (1, 1)
    assert executor.cancelled == [act.action_id]
    # cancel 不改 action 狀態：force 照舊把 EXECUTING 轉 UNKNOWN，交 resolve_unknown 查核（T-3）
    assert env.mgr.get_action(act.action_id).status == "UNKNOWN"
    assert env.mgr.list_executing_actions(env.run.run_id) == []
