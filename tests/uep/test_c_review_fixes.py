"""切片 C 審查修正：回合收尾（turn_not_running／abandon）、孤兒掃描防呆、turn_id 單一來源、取消有上限、回合清理。

離線測試不需 DB；標示「DB」者需要 PostgreSQL（未設 UEP_TEST_DATABASE_URL 時 skip）。
"""
from __future__ import annotations

import asyncio
import threading
import time
import types
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.uep.test_agent_tool_path import (
    FakeTurnPort,
    _agent_stage,
    _ctx,
    _drain,
    _events,
)
from tests.uep.test_memory_tool_executor import CTX, RecordingPort, _params, make_roster
from tests.uep.test_task_model import add_action, executor, mgr  # noqa: F401  mgr 依賴 executor，兩個 fixture 一併匯入
from tests.uep.test_turn_task import events_of, force_status, turn_task
from uep.adapters.tool import ToolCall
from uep.stream.adapters.think_agent import AgentThinkStage
from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.types import Utterance
from uep.tools.catalog.memory import memory_registry
from uep.tools.executors.memory import MemoryToolExecutor
from uep.tools.outcomes import TurnOutcomeCache

from .stream.test_think_llm import ToolBackend, _Call

# ---- 孤兒掃描防呆（離線）----

def test_sweep_requires_older_than_above_turn_max_duration():
    from uep.fakes.tool_spec import FakeToolSpecResolver
    from uep.tasks.manager import TURN_MAX_DURATION, TaskManager

    offline = TaskManager(None, None, {}, tool_resolver=FakeToolSpecResolver())  # 防呆在碰 DB 之前
    for too_short in (timedelta(0), timedelta(minutes=1), TURN_MAX_DURATION):
        with pytest.raises(ValueError, match="TURN_MAX_DURATION"):
            offline.sweep_orphan_turn_tasks(too_short)
    assert TURN_MAX_DURATION == timedelta(minutes=10)  # 模型 attempt deadline（5 分鐘）的兩倍


# ---- turn_id 單一來源（離線）----

async def test_agent_stage_rejects_when_turn_id_sources_disagree(caplog):
    port = FakeTurnPort()
    stage, _ = _agent_stage(port)
    ctx = {"ticket_ref": "turn_OTHER", "audience": "scope_private_owner"}  # 與 Utterance.turn_id 不同
    with caplog.at_level("ERROR"):
        await _drain(stage, Utterance(text="hi", speaker_id="u", turn_id="turn_a", metadata={"tool_context": ctx}))
    assert port.calls == []  # 該次工具呼叫被拒絕，沒有進到 Port
    assert any(r.levelname == "ERROR" and "turn_id" in r.getMessage() for r in caplog.records)


# ---- 取消有上限（離線）----

class BlockingTurnPort(FakeTurnPort):
    def __init__(self, release_after=3.0):
        super().__init__()
        self.release = threading.Event()
        self.release_after = release_after
        self.entered = threading.Event()

    def run_tool(self, name, args, context):
        self.entered.set()
        self.release.wait(self.release_after)
        return super().run_tool(name, args, context)


async def test_agent_stage_cancel_with_stuck_tool_completes_within_budget():
    port = BlockingTurnPort(release_after=3.0)
    backend = ToolBackend(call=_Call("c1", "deep_recall", '{"query": "貓"}'))
    stage = AgentThinkStage(LLMThinkStage(backend, tools=[{"x": 1}]), port, tool_timeout_s=0.3)
    ctx = {"ticket_ref": "turn_s", "audience": "scope_private_owner"}
    utterance = Utterance(text="hi", speaker_id="u", turn_id="turn_s", metadata={"tool_context": ctx})
    task = asyncio.ensure_future(_drain(stage, utterance))
    await asyncio.get_running_loop().run_in_executor(None, port.entered.wait, 2.0)
    started = time.perf_counter()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.perf_counter() - started < 2.0  # 工具卡住 3 秒，取消仍在預算內完成
    assert port.finished == ["turn_s"]  # 回合收尾照常通知
    port.release.set()


# ---- 回合清理：cache 以 turn 為索引，receipt 一起清（離線）----

def test_cache_is_indexed_by_turn_and_cleared_as_a_batch_with_receipts():
    cache = TurnOutcomeCache()
    ex = MemoryToolExecutor(RecordingPort(), make_roster(), cache=cache)
    for turn, action in (("turn_1", "act_1"), ("turn_1", "act_2"), ("turn_2", "act_3")):
        with cache.turn_scope(turn):
            ex.execute(ToolCall(action, "deep_recall", _params({"query": "q"}, CTX), "h", "internal_read", "native"))
    assert len(cache) == 6  # 3 筆結果 + 3 筆 receipt
    cache.discard_turn("turn_1")
    assert cache.text("act_1") is None and ex.lookup_receipt("act_2").outcome == "undetermined"  # 結果與 receipt 一起清
    assert cache.text("act_3") == "ok" and ex.lookup_receipt("act_3").outcome == "found"  # 其他回合不受影響
    cache.discard_turn("turn_2")
    assert len(cache) == 0


# ---- bridge 在途計數與收尾上限（離線，以 TaskManager 替身）----

class _FakeTasks:
    def __init__(self):
        self.finish_calls, self.steps = [], []

    def get_or_create_turn_task(self, turn_id, **kw):
        return types.SimpleNamespace(task_id="task_x"), types.SimpleNamespace(run_id="run_x", worker_epoch=1)

    def begin_step(self, run_id, label, *, worker_epoch):
        return types.SimpleNamespace(step_id="step_x")

    def complete_step(self, step_id, *, worker_epoch, ok=True):
        self.steps.append(ok)

    def finish_turn_task(self, task_id, *, abandon_executing=False):
        self.finish_calls.append(abandon_executing)


def _bridge(tasks, finish_timeout_s):
    from uep.agent.bridge import AgentToolTurns

    bridge = AgentToolTurns(tasks, memory_registry(), TurnOutcomeCache(), finish_timeout_s=finish_timeout_s)
    gate, started = threading.Event(), threading.Event()

    def slow(turn, name, args, context):
        bridge._db(turn, context)  # 讓回合有 DB 狀態
        started.set()
        gate.wait(5.0)
        return {"success": True, "result": "x"}

    bridge._run_tool = slow
    return bridge, gate, started


def test_bridge_finish_waits_for_inflight_then_finishes_normally():
    tasks = _FakeTasks()
    bridge, gate, started = _bridge(tasks, finish_timeout_s=3.0)
    ctx = {"ticket_ref": "turn_w", "audience": "scope_private_owner"}
    worker = threading.Thread(target=bridge.run_tool, args=("deep_recall", {}, ctx))
    worker.start()
    assert started.wait(2.0)
    threading.Timer(0.2, gate.set).start()  # 在途工具 0.2 秒後完成
    bridge.finish_turn("turn_w")
    worker.join()
    assert tasks.finish_calls == [False] and tasks.steps == [True]  # 等到它結束，正常收尾，不標 UNKNOWN


def test_bridge_finish_is_bounded_and_abandons_stuck_tool_then_rejects_late_calls():
    tasks = _FakeTasks()
    bridge, gate, started = _bridge(tasks, finish_timeout_s=0.3)
    ctx = {"ticket_ref": "turn_b", "audience": "scope_private_owner"}
    worker = threading.Thread(target=bridge.run_tool, args=("deep_recall", {}, ctx))
    worker.start()
    assert started.wait(2.0)
    begun = time.perf_counter()
    bridge.finish_turn("turn_b")
    assert time.perf_counter() - begun < 2.0  # 有上限
    assert tasks.finish_calls == [True] and tasks.steps == [False]  # 逾時：標 UNKNOWN 後收尾
    gate.set()
    worker.join()
    assert len(tasks.finish_calls) == 1  # 遲到的工具回來不會再觸發第二次收尾


# ---- DB ----

def test_finish_turn_task_fails_with_turn_not_running_when_paused(mgr, session_factory):  # noqa: F811
    from uep.tasks.states import PauseReason, RunStatus

    task, _run = turn_task(mgr)
    mgr.transition_task(task.task_id, RunStatus.PAUSED, pause_reason=PauseReason.USER)
    done = mgr.finish_turn_task(task.task_id)  # 不拋 IllegalTransition
    assert done.status == "FAILED"
    assert events_of(session_factory, "task.failed")[-1].payload["reason_code"] == "turn_not_running"


def test_finish_turn_task_abandon_executing_marks_unknown_then_fails(mgr, session_factory):  # noqa: F811
    task, run = turn_task(mgr)
    step = mgr.begin_step(run.run_id, "work", worker_epoch=run.worker_epoch)
    action = add_action(mgr, run, step, {"a": 1})
    force_status(session_factory, action.action_id, "EXECUTING")
    done = mgr.finish_turn_task(task.task_id, abandon_executing=True)
    assert done.status == "FAILED" and mgr.get_action(action.action_id).status == "UNKNOWN"  # 走正規轉移
    assert events_of(session_factory, "action.unknown")[-1].payload["status"] == "unknown"
    assert events_of(session_factory, "task.failed")[-1].payload["reason_code"] == "unresolved_actions"


class _BlockingPort:
    """execute_tool 卡住直到釋放；用來模擬工具卡死時的回合收尾。"""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()

    def execute_tool(self, name, args, *, actor_id, audience, session_id=None, context=None):
        self.entered.set()
        self.release.wait(10.0)
        return {"success": True, "result": "遲到的結果"}


@pytest.mark.usefixtures("session_factory")
def test_db_stuck_tool_finish_is_bounded_action_unknown_and_task_failed(session_factory):
    from uep.agent.bridge import AgentToolTurns
    from uep.events.bus import EventBus
    from uep.events.publisher import EventPublisher
    from uep.ids import new_id
    from uep.tasks.manager import TaskManager
    from uep.tasks.models import ActionIntentRow, TaskRow

    cache = TurnOutcomeCache()
    port = _BlockingPort()
    memory_executor = MemoryToolExecutor(port, make_roster(), cache=cache)
    tm = TaskManager(session_factory, EventPublisher(session_factory, EventBus()),
                     {"core_memory": memory_executor}, tool_resolver=memory_registry())
    turns = AgentToolTurns(tm, memory_registry(), cache, finish_timeout_s=0.5)
    turn_id = new_id("turn")
    ctx = _ctx(turn_id)
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(turns.run_tool("deep_recall", {"query": "x"}, ctx)))
    worker.start()
    assert port.entered.wait(5.0)
    begun = time.perf_counter()
    turns.finish_turn(turn_id)  # 工具卡住：收尾仍在時限內完成
    assert time.perf_counter() - begun < 3.0
    with session_factory() as s:
        action = s.scalar(select(ActionIntentRow))
        task = s.scalar(select(TaskRow))
        assert action.status == "UNKNOWN" and task.status == "FAILED"
    assert events_of(session_factory, "task.failed")[-1].payload["reason_code"] == "unresolved_actions"
    assert len(_events(session_factory, turn_id, "action.unknown")) == 1
    port.release.set()  # 遲到的結果回來
    worker.join(5.0)
    assert len(cache) == 0  # cache 不殘留（遲到呼叫寫入的也被清掉）
    with session_factory() as s:
        assert s.scalar(select(ActionIntentRow)).status == "UNKNOWN"  # 遲到的 receipt 不會把已收尾的狀態改回去


@pytest.mark.usefixtures("session_factory")
def test_db_cancel_in_flight_leaves_correct_task_state_and_empty_cache(session_factory):
    """CancelledError 在途取消：stage 等工具回來（有上限）再往外拋，回合收尾後 Task 狀態正確、cache 無殘留。"""
    from uep.agent.bridge import AgentToolTurns
    from uep.events.bus import EventBus
    from uep.events.publisher import EventPublisher
    from uep.ids import new_id
    from uep.tasks.manager import TaskManager
    from uep.tasks.models import TaskRow

    class SlowPort:
        entered = threading.Event()

        def execute_tool(self, name, args, *, actor_id, audience, session_id=None, context=None):
            self.entered.set()
            time.sleep(0.4)
            return {"success": True, "result": "有結果"}

    cache = TurnOutcomeCache()
    slow = SlowPort()
    memory_executor = MemoryToolExecutor(slow, make_roster(), cache=cache)
    tm = TaskManager(session_factory, EventPublisher(session_factory, EventBus()),
                     {"core_memory": memory_executor}, tool_resolver=memory_registry())
    turns = AgentToolTurns(tm, memory_registry(), cache, finish_timeout_s=2.0)
    turn_id = new_id("turn")
    ctx = _ctx(turn_id)
    ctx["audience"] = "scope_private_owner"
    backend = ToolBackend(call=_Call("c1", "deep_recall", '{"query": "貓"}'))
    stage = AgentThinkStage(LLMThinkStage(backend, tools=[{"x": 1}]), turns, tool_timeout_s=2.0)

    async def scenario():
        utterance = Utterance(text="hi", speaker_id="u", turn_id=turn_id, metadata={"tool_context": ctx})
        task = asyncio.ensure_future(_drain(stage, utterance))
        await asyncio.get_running_loop().run_in_executor(None, slow.entered.wait, 5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    with session_factory() as s:
        task = s.scalar(select(TaskRow))
    assert task.status == "SUCCEEDED"  # 工具在取消後仍完成（有上限地等到），沒有未決 action
    assert len(cache) == 0
