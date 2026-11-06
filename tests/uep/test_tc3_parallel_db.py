"""切片 D1b：TC-3 的 Run 內並行放寬，對真實 TaskManager／PostgreSQL 的測試（需 UEP_TEST_DATABASE_URL；未設時 skip）。

涵蓋：兩個 parallel_safe 同時 EXECUTING、serial 與其他互斥、parallel_safe 不與 serial 並存、非唯讀 UNKNOWN 阻擋、
**條件 1**（兩個 serial 從兩個執行緒同時搶，恰好一個成功）、max_parallel=2 的 AgentLoop 並行批次、concurrency 建立後不可改、
0012 升降級往返。離線對照在 ``test_tc3_parallel.py``。
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from tests.uep.acceptance_support import POLICY, SCOPE, make_tm
from tests.uep.loop_support import Round, ScriptedRuntime, make_registry, projector_for
from tests.uep.test_agent_loop import PARENT, call, trigger
from tests.uep.test_task_model import StubExecutor, make_manager, running
from uep.agent.budget import LoopBudget
from uep.agent.core import stable_action_id
from uep.agent.loop import AgentLoop
from uep.fakes.loop_context import InMemoryLoopContextStore
from uep.fakes.tool import FakeToolExecutor
from uep.tasks.manager import ActionBlocked
from uep.tasks.models import ActionIntentRow
from uep.tasks.states import ActionStatus, RunStatus
from uep.tools.gateway import ToolGateway

pytestmark = pytest.mark.usefixtures("session_factory")
WAIT_S = 10


class GatedExecutor(StubExecutor):
    """execute 時卡在 gate 上，讓 action 停在 EXECUTING；started 記錄已進入 executor 的 action。"""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.entered = threading.Semaphore(0)

    def execute(self, call):
        self.entered.release()
        assert self.gate.wait(WAIT_S), "gate 逾時"
        return super().execute(call)


def declare_tool(mgr, tool, *, concurrency, side_effect="internal_read"):
    mgr.tool_resolver.register(
        tool, executor="stub", side_effect_class=side_effect, idempotency="via_action_id", tool_version=1,
        concurrency=concurrency,
    )


def spawn(mgr, action, run, results):
    def attempt():
        try:
            results.append(mgr.execute_action(action.action_id, {"n": _tag(action)}, worker_epoch=run.worker_epoch))
        except Exception as exc:  # noqa: BLE001  由呼叫端逐一斷言型別
            results.append(exc)

    thread = threading.Thread(target=attempt)
    thread.start()
    return thread


_TAGS: dict[str, str] = {}


def _tag(action) -> str:
    return _TAGS[action.action_id]


def make_action(mgr, run, step, tool, tag):
    action = mgr.create_action(
        run_id=run.run_id, step_id=step.step_id, tool=tool, params={"n": tag}, worker_epoch=run.worker_epoch,
    )
    _TAGS[action.action_id] = tag
    return action


def status_of(mgr, action):
    return mgr.get_action(action.action_id).status


def _setup(session_factory):
    executor = GatedExecutor()
    mgr = make_manager(session_factory, executor)
    for name, conc in (("t.safe1", "parallel_safe"), ("t.safe2", "parallel_safe"), ("t.ser1", "serial"), ("t.ser2", "serial")):
        declare_tool(mgr, name, concurrency=conc)
    declare_tool(mgr, "t.ser_write", concurrency="serial", side_effect="reversible_local")
    _task, run, step = running(mgr)
    return executor, mgr, run, step


def _wait_entered(executor, n):
    for _ in range(n):
        assert executor.entered.acquire(timeout=WAIT_S), "executor 沒有在時限內被進入"


def test_concurrency_column_is_snapshotted_at_create(session_factory):
    _executor, mgr, run, step = _setup(session_factory)
    safe = make_action(mgr, run, step, "t.safe1", "a")
    ser = make_action(mgr, run, step, "t.ser1", "b")
    assert (safe.concurrency, ser.concurrency) == ("parallel_safe", "serial")
    declare_tool(mgr, "t.safe1", concurrency="serial")  # 註冊表事後改宣告：既有列不變
    assert mgr.get_action(safe.action_id).concurrency == "parallel_safe"
    mgr.tool_resolver.register("t.undeclared", executor="stub", side_effect_class="internal_read", tool_version=1)
    assert make_action(mgr, run, step, "t.undeclared", "c").concurrency == "serial"  # 沒宣告 ⇒ serial


def test_two_parallel_safe_execute_simultaneously_in_two_threads(session_factory):
    executor, mgr, run, step = _setup(session_factory)
    a = make_action(mgr, run, step, "t.safe1", "a")
    b = make_action(mgr, run, step, "t.safe2", "b")
    results: list[object] = []
    threads = [spawn(mgr, a, run, results), spawn(mgr, b, run, results)]
    _wait_entered(executor, 2)  # 兩個都進了 executor ⇒ 兩個同時 EXECUTING
    assert status_of(mgr, a) == "EXECUTING" and status_of(mgr, b) == "EXECUTING"
    executor.gate.set()
    for t in threads:
        t.join(WAIT_S)
    assert [r.status for r in results] == [ActionStatus.SUCCEEDED] * 2


def test_serial_excludes_other_actions(session_factory):
    executor, mgr, run, step = _setup(session_factory)
    holder = make_action(mgr, run, step, "t.ser1", "a")
    other_serial = make_action(mgr, run, step, "t.ser2", "b")
    other_safe = make_action(mgr, run, step, "t.safe1", "c")
    results: list[object] = []
    thread = spawn(mgr, holder, run, results)
    _wait_entered(executor, 1)
    for blocked, tag in ((other_serial, "b"), (other_safe, "c")):
        with pytest.raises(ActionBlocked, match="TC-3"):
            mgr.execute_action(blocked.action_id, {"n": tag}, worker_epoch=run.worker_epoch)
        assert status_of(mgr, blocked) == "INTENDED"
    executor.gate.set()
    thread.join(WAIT_S)
    assert results[0].status is ActionStatus.SUCCEEDED


def test_parallel_safe_cannot_coexist_with_serial_in_either_order(session_factory):
    executor, mgr, run, step = _setup(session_factory)
    safe = make_action(mgr, run, step, "t.safe1", "a")
    ser = make_action(mgr, run, step, "t.ser1", "b")
    results: list[object] = []
    thread = spawn(mgr, safe, run, results)
    _wait_entered(executor, 1)
    with pytest.raises(ActionBlocked, match="TC-3"):  # parallel_safe 進行中，serial 不能進
        mgr.execute_action(ser.action_id, {"n": "b"}, worker_epoch=run.worker_epoch)
    executor.gate.set()
    thread.join(WAIT_S)
    assert mgr.execute_action(ser.action_id, {"n": "b"}, worker_epoch=run.worker_epoch).status is ActionStatus.SUCCEEDED


def test_non_read_unknown_blocks_even_parallel_safe(session_factory):
    _executor, mgr, run, step = _setup(session_factory)
    unknown = make_action(mgr, run, step, "t.ser_write", "a")
    safe = make_action(mgr, run, step, "t.safe1", "b")
    with session_factory.begin() as s:
        s.get(ActionIntentRow, unknown.action_id).status = "UNKNOWN"
    with pytest.raises(ActionBlocked, match="TC-3"):
        mgr.execute_action(safe.action_id, {"n": "b"}, worker_epoch=run.worker_epoch)


def test_two_serial_racing_from_two_threads_exactly_one_wins(session_factory):
    """條件 1：移除部分唯一索引後，檢查與轉 EXECUTING 在同一交易、同一把 Run 鎖內；兩個 serial 同時進入時
    恰好一個成功、另一個 ActionBlocked。贏家卡在 executor（維持 EXECUTING）直到輸家回報，結果與時序無關。"""
    executor, mgr, run, step = _setup(session_factory)
    a = make_action(mgr, run, step, "t.ser1", "a")
    b = make_action(mgr, run, step, "t.ser2", "b")
    results: list[object] = []
    barrier = threading.Barrier(2, timeout=WAIT_S)

    def attempt(action, tag):
        barrier.wait()
        try:
            results.append(mgr.execute_action(action.action_id, {"n": tag}, worker_epoch=run.worker_epoch))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=attempt, args=(a, "a")), threading.Thread(target=attempt, args=(b, "b"))]
    for t in threads:
        t.start()
    deadline = threading.Event()
    for _ in range(WAIT_S * 20):  # 等輸家被擋（結果裡出現 ActionBlocked）
        if any(isinstance(r, ActionBlocked) for r in results):
            break
        deadline.wait(0.05)
    executor.gate.set()
    for t in threads:
        t.join(WAIT_S)
    losers = [r for r in results if isinstance(r, Exception)]
    winners = [r for r in results if not isinstance(r, Exception)]
    assert len(winners) == 1 and len(losers) == 1 and isinstance(losers[0], ActionBlocked)
    assert len(executor.calls) == 1
    with session_factory() as s:
        statuses = sorted(s.scalars(select(ActionIntentRow.status).where(ActionIntentRow.run_id == run.run_id)))
    assert statuses == ["INTENDED", "SUCCEEDED"]


def test_concurrency_cannot_be_changed_after_create(session_factory):
    """Manager 沒有任何更新 concurrency 的路徑：走完整個生命週期（執行、終態）後欄位不變。
    （程式結構面的證明在離線測試 ``test_no_code_path_in_tasks_package_writes_action_concurrency``。）"""
    executor, mgr, run, step = _setup(session_factory)
    executor.gate.set()
    safe = make_action(mgr, run, step, "t.safe1", "a")
    assert mgr.execute_action(safe.action_id, {"n": "a"}, worker_epoch=run.worker_epoch).status is ActionStatus.SUCCEEDED
    declare_tool(mgr, "t.safe1", concurrency="serial")
    again = mgr.create_action(  # 同 action_id 重呼叫回既有列，不改 concurrency
        run_id=run.run_id, step_id=step.step_id, tool="t.safe1", params={"n": "a"}, worker_epoch=run.worker_epoch,
        action_id=safe.action_id,
    )
    assert again.concurrency == "parallel_safe"
    assert mgr.get_action(safe.action_id).concurrency == "parallel_safe"


def test_check_constraint_rejects_invalid_concurrency(session_factory):
    _executor, mgr, run, step = _setup(session_factory)
    action = make_action(mgr, run, step, "t.ser1", "a")
    with pytest.raises(IntegrityError), session_factory.begin() as s:
        s.get(ActionIntentRow, action.action_id).concurrency = "parallel_everything"


class BarrierExecutor(FakeToolExecutor):
    """兩個 parallel_safe 必須同時在 executor 裡，否則 barrier 逾時失敗。"""

    def __init__(self) -> None:
        super().__init__()
        self.barrier = threading.Barrier(2, timeout=WAIT_S)

    def execute(self, call):
        self.barrier.wait()
        return super().execute(call)


def test_agent_loop_max_parallel_2_runs_parallel_batch_against_real_manager(session_factory):
    registry = make_registry()
    executor = BarrierExecutor()
    tm = make_tm(session_factory, executor, tool_resolver=registry)
    task = tm.create_task(persona_id="uep", goal="查資料", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY)
    run = tm.start_run(task.task_id, persona_version=1, policy_version=1)
    runtime = ScriptedRuntime([Round("", (call("read.a", "1"), call("read.b", "2"))), Round("好")])
    loop = AgentLoop(
        runtime=runtime, tasks=tm, gateway=ToolGateway(registry), registry=registry, store=InMemoryLoopContextStore(),
        projector=projector_for(registry), budget=LoopBudget(max_parallel=2), worker_epoch=run.worker_epoch,
    )
    out = loop.run(trigger(), run.run_id, parent=PARENT, system_text="你是助理", trigger_text="幫我查")
    assert out.state == "completed" and tm.get_run(run.run_id).status == RunStatus.SUCCEEDED.value
    with tm._sf() as s:
        rows = list(s.scalars(select(ActionIntentRow).where(ActionIntentRow.run_id == run.run_id)))
    assert len(rows) == 2 and {r.concurrency for r in rows} == {"parallel_safe"}
    assert all(r.status == ActionStatus.SUCCEEDED.value for r in rows)
    fed = next(iter(runtime.submitted.values()))
    assert [aid for aid, _ in fed] == [stable_action_id(run.run_id, "step_1", 0), stable_action_id(run.run_id, "step_1", 1)]


def test_agent_loop_default_max_parallel_1_still_serial_against_real_manager(session_factory):
    registry = make_registry()
    executor = FakeToolExecutor()
    tm = make_tm(session_factory, executor, tool_resolver=registry)
    task = tm.create_task(persona_id="uep", goal="查資料", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY)
    run = tm.start_run(task.task_id, persona_version=1, policy_version=1)
    runtime = ScriptedRuntime([Round("", (call("read.a", "1"), call("read.b", "2"))), Round("好")])
    loop = AgentLoop(
        runtime=runtime, tasks=tm, gateway=ToolGateway(registry), registry=registry, store=InMemoryLoopContextStore(),
        projector=projector_for(registry), budget=LoopBudget(), worker_epoch=run.worker_epoch,
    )
    out = loop.run(trigger(), run.run_id, parent=PARENT, system_text="你是助理", trigger_text="幫我查")
    assert out.state == "completed"


# ---- migration 0012 往返（沿用 test_decision_log 的 alembic_cfg／engine 模式）----

def test_db_migration_0012_upgrade_downgrade_roundtrip(alembic_cfg, engine):
    from alembic import command

    def has_index():
        return any(i["name"] == "uq_action_intents_one_executing_per_run" for i in inspect(engine).get_indexes("action_intents"))

    def has_column():
        return "concurrency" in {c["name"] for c in inspect(engine).get_columns("action_intents")}

    assert has_column() and not has_index()
    with engine.begin() as conn:  # downgrade 重建唯一索引：先確保沒有同 Run 多個 EXECUTING 的殘列
        left = conn.execute(text("SELECT count(*) FROM action_intents WHERE status = 'EXECUTING'")).scalar_one()
    if left:
        pytest.skip("測試庫仍有 EXECUTING 的列，downgrade 重建索引可能失敗")
    try:
        command.downgrade(alembic_cfg, "0011_one_executing_per_run")
        assert has_index() and not has_column()
    finally:
        command.upgrade(alembic_cfg, "head")
    assert has_column() and not has_index()
    column = next(c for c in inspect(engine).get_columns("action_intents") if c["name"] == "concurrency")
    assert column["nullable"] is False and "serial" in str(column["default"])
    checks = {c["name"] for c in inspect(engine).get_check_constraints("action_intents")}
    assert "ck_action_intents_concurrency" in checks
