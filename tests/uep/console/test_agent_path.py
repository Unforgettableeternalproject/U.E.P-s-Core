"""Console 的 agent 工具路徑組裝（tool_path=agent）：以 FakePort 驅動，不打真實 API；需要 PostgreSQL。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.uep.test_agent_tool_path import FakePort, fin, intent, text
from uep.agent.runtime import ModelPortBackend
from uep.console.agent_rig import ConsoleFakeMemory, build_model_port
from uep.console.commands import Console
from uep.console.rig import ConsoleError, build_runtime
from uep.stream.adapters.think_agent import AgentThinkStage
from uep.stream.adapters.think_config import ThinkConfig, ThinkConfigError
from uep.tasks.models import ActionIntentRow, RunRow, TaskRow


class Capture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text_: str) -> None:
        self.lines.append(text_)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def make_agent_console(session_factory, tmp_path):
    made = []

    def _make(port, **over):
        rt = build_runtime("fake", session_factory, tmp_path / "e2e", think="real", tool_path="agent", model_port=port, **over)
        out = Capture()
        made.append(rt)
        return Console(rt, out=out, wait_s=15.0), rt, out

    yield _make
    for rt in made:
        rt.core.shutdown()


def test_agent_rig_builds_agent_think_with_model_port(make_agent_console):
    port = FakePort([[text("你好，我在。"), fin()]])
    console, rt, out = make_agent_console(port)
    assert rt.tool_path == "agent" and rt.model_port is port and rt.loop_switch is not None
    assert isinstance(rt.think, AgentThinkStage) and isinstance(rt.think.inner.backend, ModelPortBackend)
    assert rt.host._tool_path == "agent" and isinstance(rt.host._memory, ConsoleFakeMemory)
    console.execute("你好")
    assert "turn.completed" in out.text and "你好，我在。" in out.text
    assert len(port.requests) == 1  # 一般對話只呼叫模型一次（direct_chat，沒有 loop）


def test_loop_command_runs_agent_loop_once_with_tool_call(make_agent_console, session_factory):
    port = FakePort([
        [intent("deep_recall", {"query": "貓"}), fin("tool_calls")],
        [text("查完了，沒有關於貓的資料。"), fin()],
    ])
    console, rt, out = make_agent_console(port, agent_loop_entry=True)
    assert rt.core.loop_launcher is not None
    console.execute("/loop 幫我查一下貓的事")
    assert out.text.count("回應：查完了，沒有關於貓的資料。") == 1  # 只回覆一次
    assert "SUCCEEDED" in out.text and "deep_recall[" in out.text
    with session_factory() as s:
        assert [r.status for r in s.scalars(select(RunRow))] == ["SUCCEEDED"]
        assert [t.status for t in s.scalars(select(TaskRow))] == ["SUCCEEDED"]
        assert [a.tool for a in s.scalars(select(ActionIntentRow))] == ["deep_recall"]
    # 開關是一次性的：下一句回到一般對話，不再開 loop
    assert not rt.loop_switch.take()


def test_loop_command_without_entry_flag_warns_and_falls_back(make_agent_console):
    port = FakePort([[text("一般回覆"), fin()]])
    console, _rt, out = make_agent_console(port, agent_loop_entry=False)
    console.execute("/loop 隨便說點什麼")
    assert "未啟用 UEP_AGENT_LOOP_ENTRY" in out.text and "一般回覆" in out.text


def test_loop_command_in_fake_runtime_is_refused(session_factory, tmp_path):
    rt = build_runtime("fake", session_factory, tmp_path / "e2e")
    try:
        out = Capture()
        Console(rt, out=out).execute("/loop 你好")
        assert "只在 UEP_TOOL_PATH=agent" in out.text and rt.loop_switch is None and rt.tool_path == "legacy"
    finally:
        rt.core.shutdown()


def test_agent_path_requires_real_think(session_factory, tmp_path):
    with pytest.raises(ConsoleError, match="--think real"):
        build_runtime("fake", session_factory, tmp_path / "e2e", tool_path="agent")


def test_build_model_port_requires_key_and_maps_dialect():
    with pytest.raises(ThinkConfigError):
        build_model_port(ThinkConfig(api_key=None))
    chat = build_model_port(ThinkConfig(api_key="sk-test"))
    assert chat.dialect == "openai_chat" and chat.model_name == "gpt-6-luna"
    assert build_model_port(ThinkConfig(api_key="sk-test", api="responses")).dialect == "openai_responses"


def test_fake_memory_round_trip():
    memory = ConsoleFakeMemory()
    args = {"content": "使用者養了一隻貓"}
    assert memory.execute_tool("force_remember", args, actor_id="a", audience="x")["success"]
    assert "貓" in memory.execute_tool("deep_recall", {"query": "貓"}, actor_id="a", audience="x")["result"]
    assert memory.execute_tool("unknown", {}, actor_id="a", audience="x")["success"] is False
