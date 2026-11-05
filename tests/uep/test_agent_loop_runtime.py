"""AgentLoop 搭配真實的 ``ModelPortRuntime``＋``PydanticAIModelPort``（FunctionModel 驅動）：D61 A 路徑續跑、
迴圈 history 與 Runtime 內部 history 逐則相等、未知工具不多打一次模型。不需 DB。"""
from __future__ import annotations

from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from tests.uep.loop_support import FakeTasks, make_registry, projector_for
from tests.uep.test_agent_loop import PARENT, RUN, trigger
from uep.agent.budget import LoopBudget
from uep.agent.loop import AgentLoop, LoopSnapshots
from uep.agent.runtime import ModelPortRuntime
from uep.fakes.loop_context import InMemoryLoopContextStore
from uep.models.pydantic_ai_port import PydanticAIModelPort
from uep.tools.gateway import ToolGateway


def _build(fn):
    registry = make_registry()
    tasks = FakeTasks(registry)
    snapshots = LoopSnapshots()
    port = PydanticAIModelPort("openai_chat", model=FunctionModel(stream_function=fn))
    runtime = ModelPortRuntime(port, snapshots.take, continue_after_tools=True, max_rounds=50)
    store = InMemoryLoopContextStore()
    loop = AgentLoop(
        runtime=runtime, tasks=tasks, gateway=ToolGateway(registry), registry=registry, store=store,
        projector=projector_for(registry), budget=LoopBudget(max_parallel=1), worker_epoch=1, snapshots=snapshots,
        content_lookup=tasks.contents.get,
    )
    return loop, runtime, tasks, store


def _has_return(messages) -> int:
    return sum(1 for m in messages for p in m.parts if type(p).__name__ == "ToolReturnPart")


def test_loop_history_equals_runtime_history_over_a_path():
    seen: list[set[str]] = []

    async def fn(messages, info: AgentInfo):
        seen.append({t.name for t in info.function_tools})
        returned = _has_return(messages)
        if returned == 0:
            yield "我先查"
            yield {1: DeltaToolCall(name="read.a", json_args='{"path": "1"}', tool_call_id="c0")}
            yield {2: DeltaToolCall(name="read.b", json_args='{"path": "2"}', tool_call_id="c1")}
        elif returned == 2:
            yield {1: DeltaToolCall(name="read.lone", json_args='{"path": "3"}', tool_call_id="c2")}
        else:
            yield "完成了"

    loop, runtime, _tasks, store = _build(fn)
    out = loop.run(trigger(), RUN, parent=PARENT, system_text="你是助理", trigger_text="幫我查")
    assert (out.state, out.text) == ("completed", "完成了") and out.steps == 3 and out.tool_calls == 3
    assert len(runtime._attempts) == 1  # 全程同一個 attempt（A 路徑）
    attempt = next(iter(runtime._attempts.values()))
    child, _ = store.load_child("loop:run_loop1")
    assert child[:-1] == attempt.messages  # 迴圈 child 與 Runtime 內部 history 逐則相等
    assert child[-1].role == "assistant" and child[-1].content == "完成了"
    assert [m.role for m in child[-6:-1]] == ["assistant", "tool", "tool", "assistant", "tool"]
    assert seen[0] == seen[-1] == {"read.a", "read.b", "read.lone", "write.s", "post.ask"}


def test_unknown_tool_is_rejected_without_extra_model_call():
    calls = {"n": 0}

    async def fn(messages, info: AgentInfo):
        calls["n"] += 1
        if _has_return(messages) == 0:
            yield {1: DeltaToolCall(name="ghost_tool", json_args='{"path": "x"}', tool_call_id="c0")}
        else:
            yield "好吧"

    loop, _runtime, tasks, store = _build(fn)
    out = loop.run(trigger(), RUN, parent=PARENT, system_text="你是助理")
    assert out.state == "completed" and tasks.actions == {}  # 沒有建 action
    assert calls["n"] == 2  # 第一輪（未知工具）＋回餵拒絕後的第二輪；修正前會是 3
    tool_msg = next(m for m in store.load_child("loop:run_loop1")[0] if m.role == "tool")
    assert tool_msg.content == "tool rejected: capability_missing"


def test_runtime_status_reports_usage_for_token_budget():
    async def fn(messages, info: AgentInfo):
        yield "只有文字"

    loop, runtime, _tasks, _store = _build(fn)
    out = loop.run(trigger(), RUN, parent=PARENT, system_text="s")
    assert out.state == "completed"
    attempt_id = next(iter(runtime._attempts))
    assert "usage" in runtime.status(attempt_id).meta


def test_missing_snapshot_fails_without_calling_the_model():
    """起始 history 遺失 ⇒ LoopSnapshots.take 拋 KeyError ⇒ 迴圈以 snapshot_missing 失敗，模型 0 次呼叫（不以空 history 呼叫）。"""
    calls = {"n": 0}

    async def fn(messages, info: AgentInfo):
        calls["n"] += 1
        yield "不該被呼叫"

    class Lossy(LoopSnapshots):
        def put(self, ref, messages):
            pass

    registry = make_registry()
    tasks = FakeTasks(registry)
    snapshots = Lossy()
    port = PydanticAIModelPort("openai_chat", model=FunctionModel(stream_function=fn))
    runtime = ModelPortRuntime(port, snapshots.take, continue_after_tools=True, max_rounds=5)
    loop = AgentLoop(
        runtime=runtime, tasks=tasks, gateway=ToolGateway(registry), registry=registry, store=InMemoryLoopContextStore(),
        projector=projector_for(registry), budget=LoopBudget(), worker_epoch=1, snapshots=snapshots,
    )
    out = loop.run(trigger(), RUN, parent=PARENT, system_text="s")
    assert (out.state, out.reason_code) == ("failed", "snapshot_missing") and calls["n"] == 0
