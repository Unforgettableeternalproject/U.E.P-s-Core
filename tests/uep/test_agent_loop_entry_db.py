"""route=agent_loop 的 production 入口：DB 端到端（DL-4 PR4；需 PostgreSQL，沒有 UEP_TEST_DATABASE_URL 時自動 skip）。

鎖定：同一輸入只有一次對外輸出（文字經 Output Gate 後由端點渲染）、失敗時使用者只收到一則固定系統訊息、
Run／Task 收尾、並發上限、關機、Console 層級可見性。
"""
from __future__ import annotations

import dataclasses
import threading
import time
import types

import pytest
from sqlalchemy import select

from tests.uep.echo_support import Rig, fast_speak, make_identity, make_session
from tests.uep.test_agent_tool_path import FakePort, _Port, fin, text
from uep.agent.runtime import ModelPortBackend
from uep.assembly import assemble_core
from uep.control.channel import InProcessControlChannel
from uep.echo import agent_wiring
from uep.echo.agent_wiring import LOOP_FAILURE_TEXT
from uep.echo.host import EchoPipelineHost
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.presence.epoch import HostEpochStore
from uep.stream.adapters.think_agent import AgentThinkStage
from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.tracer import LatencyTracer
from uep.tasks.models import RunRow, TaskRow
from uep.tools.catalog.memory import memory_registry
from uep.turn.models import Proposal, ReplyMode

pytestmark = pytest.mark.usefixtures("session_factory")


class _LoopProposer:
    proposer_id = "test.agent_loop"
    version = "t"

    def __init__(self, route: str) -> None:
        self.route = route

    def propose(self, candidate):
        return Proposal(
            reply_mode=ReplyMode.RESPOND, route=self.route, reason_code="default_respond",
            proposer_id=self.proposer_id, proposer_version=self.version,
        )


class _Memory(_Port):
    def retrieve(self, *a, **k):
        return []

    def store(self, **kw):
        pass

    def profile(self, **kw):
        return ""

    def set_session(self, session_id):
        pass

    def tool_definitions(self):
        return []


def _agent_pieces(route):
    """agent 工具路徑的 Host 零件（Think 為 AgentThinkStage；ES 端的模型埠 es_port 不該被呼叫）。"""
    es_port = FakePort([])
    think = AgentThinkStage(LLMThinkStage(ModelPortBackend(es_port)))
    return think, es_port, {"tool_path": "agent", "memory": _Memory(), "proposer": _LoopProposer(route)}


def _agent_rig(session_factory, tmp_path, *, route, flag, loop_port):
    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    think, es_port, host_kw = _agent_pieces(route)
    host = EchoPipelineHost(
        publisher=publisher, think=think, speak=fast_speak(), sink_factory=lambda v, t: "x", session=make_session(),
        identity=make_identity(), tracer=LatencyTracer(), endpoint_kwargs={"tick_interval": 0.05}, **host_kw,
    )
    core = assemble_core(
        session_factory, bus=bus, publisher=publisher, control=InProcessControlChannel(), adapter=host.adapter,
        executors={}, tool_resolver=memory_registry(), turn_generation_source=host.generation_source,
        presence_audience=lambda: "scope_private_owner", epoch_store=HostEpochStore(tmp_path / "host_epoch.json"),
        maintenance_interval=None, echo=host, tool_path="agent", model_port=loop_port, agent_loop_entry=flag,
        decision_log=False, supervisor=False,
    )
    rig = Rig(core, host, session_factory, think, host.speak, LatencyTracer(), [])
    core.start()
    return rig, es_port


def _submits(rig):
    calls: list = []
    original = rig.host.coordinator.submit

    def spy(request):
        calls.append(request)
        return original(request)

    rig.host.coordinator.submit = spy
    return calls


def _rows(session_factory):
    with session_factory() as s:
        return list(s.scalars(select(TaskRow))), list(s.scalars(select(RunRow)))


def test_agent_loop_route_delivers_once_and_skips_direct_chat(session_factory, tmp_path):
    loop_port = FakePort([[text("查完了，答案是四十二。"), fin()]])
    rig, es_port = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=loop_port)
    try:
        submits = _submits(rig)
        result = rig.say("幫我想一下答案")
        assert result.ticket is not None and result.proposal.route == "agent_loop"
        turn_id = result.ticket.turn_id
        kinds = rig.wait_terminal(turn_id)
        assert kinds.count("turn.completed") == 1 and "turn.cancelled" not in kinds
        assert submits == [] and es_port.requests == []  # direct_chat（ES）一次都沒被啟動
        # 使用者端實際看到的文字：端點在 Gate 放行後渲染，只有一則，且是角色回覆（非系統訊息）
        shown = rig.host.endpoint.rendered_texts(turn_id)
        assert [(t.text, t.system) for t in shown] == [("查完了，答案是四十二。", False)]
        intents = rig.intents(turn_id)
        assert len(intents) == 1 and intents[0].kind == "text" and len(rig.deliveries()) == 1
        assert rig.deliveries()[0].status == "sent"
        assert rig.core.echo.orchestrator.metrics["turn.loop_claimed"] == 1
        assert rig.core.echo.orchestrator._live == {}  # turn 已終結，沒有殘留
        assert len(rig.host.loop_text) == 0  # 文字暫存隨 turn 清除
        tasks, runs = _rows(session_factory)
        assert [t.status for t in tasks] == ["SUCCEEDED"] and [r.status for r in runs] == ["SUCCEEDED"]
        store = rig.core.loop_launcher._store
        assert not store._store.list_sessions()  # Run 終態：持久 store 的 child／parent 條目已清
        assert rig.core.loop_launcher.active == 0
    finally:
        rig.core.shutdown()


def test_flag_off_agent_loop_route_behaves_as_direct_chat(session_factory, tmp_path):
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=False, loop_port=FakePort([]))
    try:
        assert rig.core.echo.orchestrator._loop_entry is None and rig.core.loop_launcher is None
        submits = _submits(rig)
        assert rig.say("你好").ticket is not None
        rig.wait_for(lambda: len(submits) == 1, what="direct_chat 啟動")  # 旗標關閉：行為不變，仍走 ES
        assert rig.core.echo.orchestrator.metrics["turn.loop_claimed"] == 0
    finally:
        rig.core.shutdown()


def test_direct_chat_route_is_untouched_even_with_flag_on(session_factory, tmp_path):
    loop_port = FakePort([])
    rig, _ = _agent_rig(session_factory, tmp_path, route="direct_chat", flag=True, loop_port=loop_port)
    try:
        submits = _submits(rig)
        assert rig.say("你好").ticket is not None
        rig.wait_for(lambda: len(submits) == 1, what="direct_chat 啟動")
        assert loop_port.requests == [] and rig.core.echo.orchestrator.metrics["turn.loop_claimed"] == 0
    finally:
        rig.core.shutdown()


def test_input_without_active_lease_opens_no_turn_and_no_run(session_factory, tmp_path):
    """§10 查核：沒有生效 Lease 時 intake 在簽 ticket 之前就被否決，LoopEntry 不會被呼叫（既有 turn 層行為，PR4 沒有新增判定）。"""
    loop_port = FakePort([])
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=loop_port)
    try:
        rig.core.presence.revoke("presence_lost_test")
        result = rig.say("在嗎")
        assert result.ticket is None and result.reason_code == "presence_inactive"
        assert loop_port.requests == [] and rig.core.echo.orchestrator.metrics["turn.loop_claimed"] == 0
        assert _rows(session_factory) == ([], [])
    finally:
        rig.core.shutdown()


def test_loop_failure_marks_run_task_failed_and_user_gets_one_system_notice(session_factory, tmp_path):
    loop_port = FakePort([RuntimeError("provider down")])
    rig, es_port = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=loop_port)
    try:
        submits = _submits(rig)
        turn_id = rig.say("幫我查").ticket.turn_id
        kinds = rig.wait_terminal(turn_id)
        assert kinds.count("turn.cancelled") == 1 and "turn.completed" not in kinds
        shown = rig.host.endpoint.rendered_texts(turn_id)
        assert [(t.text, t.system) for t in shown] == [(LOOP_FAILURE_TEXT, True)]  # 有且只有一則，且是固定的系統訊息
        assert submits == [] and es_port.requests == []  # 沒有回退成角色回答
        intents = rig.intents(turn_id)
        assert len(intents) == 1 and intents[0].kind == "system_notice" and intents[0].origin == "system"
        tasks, runs = _rows(session_factory)
        assert [t.status for t in tasks] == ["FAILED"] and [r.status for r in runs] == ["FAILED"]
        assert rig.core.echo.orchestrator._live == {} and rig.core.loop_launcher.active == 0
    finally:
        rig.core.shutdown()


def test_worker_exception_finalizes_run_and_task_and_notifies_once(session_factory, tmp_path, monkeypatch):
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=FakePort([]))
    try:
        def boom(self, ctx):
            raise RuntimeError("loop 建構失敗")

        monkeypatch.setattr(agent_wiring.LoopLauncher, "_build_loop", boom)
        turn_id = rig.say("幫我查").ticket.turn_id
        rig.wait_terminal(turn_id)
        assert [t.system for t in rig.host.endpoint.rendered_texts(turn_id)] == [True]
        tasks, runs = _rows(session_factory)
        assert [t.status for t in tasks] == ["FAILED"] and [r.status for r in runs] == ["FAILED"]
    finally:
        rig.core.shutdown()


def test_start_run_failure_rolls_back_task_and_turn_falls_back_to_direct_chat(session_factory, tmp_path, monkeypatch):
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=FakePort([]))
    try:
        def boom(*a, **k):
            raise RuntimeError("start_run 失敗")

        monkeypatch.setattr(rig.core.tasks, "start_run", boom)
        submits = _submits(rig)
        assert rig.say("你好").ticket is not None
        rig.wait_for(lambda: len(submits) == 1, what="回退 direct_chat")
        tasks, runs = _rows(session_factory)
        assert [t.status for t in tasks] == ["FAILED"] and runs == []  # 剛建的 Task 已回收，不留孤兒
        assert rig.core.echo.orchestrator.metrics["turn.loop_entry_failed"] == 1
        assert rig.core.loop_launcher.active == 0
    finally:
        rig.core.shutdown()


def test_thread_start_failure_rolls_back_run_and_task(session_factory, tmp_path, monkeypatch):
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=FakePort([]))
    try:
        class NoStart(threading.Thread):
            def start(self):
                raise RuntimeError("can't start new thread")

        shim = types.SimpleNamespace(Thread=NoStart, current_thread=threading.current_thread)
        monkeypatch.setattr(agent_wiring, "threading", shim)  # 只換掉 agent_wiring 看到的 threading，不影響其他模組
        submits = _submits(rig)
        assert rig.say("你好").ticket is not None
        monkeypatch.undo()
        rig.wait_for(lambda: len(submits) == 1, what="回退 direct_chat")
        tasks, runs = _rows(session_factory)
        assert [t.status for t in tasks] == ["FAILED"] and [r.status for r in runs] == ["FAILED"]
        assert rig.core.loop_launcher.active == 0
    finally:
        rig.core.shutdown()


def test_concurrency_limit_falls_back_to_direct_chat_without_creating_runs(session_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_wiring, "DEFAULT_MAX_CONCURRENT_LOOPS", 2)
    loop_port = FakePort([])
    loop_port.hang = True  # 模型呼叫卡住：前兩個 Run 一直占著名額
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=loop_port)
    try:
        submits = _submits(rig)
        for n in range(4):
            assert rig.say(f"問題{n}").ticket is not None
        metrics = rig.core.echo.orchestrator.metrics
        assert metrics["turn.loop_claimed"] == 2 and metrics["turn.loop_entry_refused"] == 2
        rig.wait_for(lambda: len(submits) == 2, what="被拒的兩個回合退回 direct_chat")
        tasks, runs = _rows(session_factory)
        assert len(tasks) == 2 and len(runs) == 2  # 超過上限的回合沒有建 Task／Run，也沒有排隊
    finally:
        rig.core.loop_launcher.close(timeout=0.2)
        rig.core.shutdown()


def test_shutdown_while_worker_is_running_does_not_hang(session_factory, tmp_path):
    loop_port = FakePort([])
    loop_port.hang = True
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=loop_port)
    turn_id = rig.say("幫我查").ticket.turn_id
    launcher = rig.core.loop_launcher
    rig.wait_for(lambda: launcher.active == 1 and bool(loop_port.requests), what="Run 進入模型呼叫")
    (thread,) = list(launcher._threads)
    assert thread.daemon  # daemon：卡住的 worker 不阻擋行程結束
    launcher._close_timeout_s = 0.3
    started = time.monotonic()
    rig.core.shutdown()
    assert time.monotonic() - started < 15
    assert "loop_launcher.close" in rig.core.start_log
    assert "turn.cancelled" in rig.kinds(turn_id) and rig.core.echo.orchestrator._live == {}
    assert rig.host.endpoint.rendered_texts(turn_id) == []  # 關機取消：不補發失敗訊息


# ---- Console 層級：開啟 UEP_AGENT_LOOP_ENTRY 的端到端 ----

class _Capture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)


def _console(session_factory, tmp_path, loop_port):
    from uep.console.commands import Console
    from uep.console.rig import build_fake_runtime

    think, es_port, host_kw = _agent_pieces("agent_loop")
    rt = build_fake_runtime(
        session_factory, tmp_path / "console", think_factory=lambda tracer: think, host_kwargs=host_kw,
        assemble_kwargs={
            "tool_path": "agent", "model_port": loop_port, "agent_loop_entry": True, "executors": {},
            "tool_resolver": memory_registry(), "decision_log": False, "supervisor": False,
        },
    )
    out = _Capture()
    return Console(rt, out=out, wait_s=10.0), rt, out, es_port


def test_console_agent_loop_reply_is_visible_exactly_once(session_factory, tmp_path):
    loop_port = FakePort([[text("Console 端到端的答案。"), fin()]])
    console, rt, out, es_port = _console(session_factory, tmp_path, loop_port)
    try:
        assert console.execute("/say 你好") is True
        replies = [line for line in out.lines if line.startswith("回應：")]
        assert replies == ["回應：Console 端到端的答案。"]  # 可見且只出現一次
        assert not [line for line in out.lines if line.startswith("系統訊息：")]
        assert any("turn.completed" in line for line in out.lines)
        assert es_port.requests == [] and len(loop_port.requests) == 1
        assert rt.wavs == {}  # 沒有走 Think／Speak，不會有音訊
    finally:
        rt.core.shutdown()


def test_console_agent_loop_failure_shows_one_system_notice(session_factory, tmp_path):
    loop_port = FakePort([RuntimeError("provider down")])
    console, rt, out, es_port = _console(session_factory, tmp_path, loop_port)
    try:
        assert console.execute("/say 你好") is True
        assert [line for line in out.lines if line.startswith("系統訊息：")] == [f"系統訊息：{LOOP_FAILURE_TEXT}"]
        assert not [line for line in out.lines if line.startswith("回應：")]
        assert es_port.requests == []
    finally:
        rt.core.shutdown()


# ---- Supervisor：loop Run 帶 deadline、背景心跳、worker 消失後被收尾 ----

def test_loop_run_carries_a_deadline_and_turn_tasks_do_not(session_factory, tmp_path):
    from datetime import UTC, datetime, timedelta

    from uep.tasks.supervisor import DEFAULT_DEADLINE_GRACE

    loop_port = FakePort([])
    loop_port.hang = True
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=loop_port)
    try:
        before = datetime.now(UTC)
        rig.say("幫我查")
        rig.wait_for(lambda: bool(loop_port.requests), what="Run 進入模型呼叫")
        _tasks, runs = _rows(session_factory)
        (run,) = runs
        expected = timedelta(seconds=rig.core.loop_launcher._budget.run_deadline_s) + DEFAULT_DEADLINE_GRACE
        assert run.run_deadline is not None
        assert before + expected <= run.run_deadline <= datetime.now(UTC) + expected
    finally:
        rig.core.loop_launcher.close(timeout=0.2)
        rig.core.shutdown()


def _tight_launcher(rig, *, stale_after_s=0.6, deadline_s=0.5, grace_s=0.1):
    """把心跳門檻與期限縮到亞秒級，讓測試不必真的等一分鐘。"""
    from datetime import timedelta

    from uep.agent.budget import LoopBudget

    launcher = rig.core.loop_launcher
    rig.core.tasks._stale_after = timedelta(seconds=stale_after_s)
    launcher._budget = LoopBudget(run_deadline_s=deadline_s)
    launcher._deadline_grace = timedelta(seconds=grace_s)
    return launcher


def test_beating_loop_run_is_not_forced_but_silent_one_is_after_deadline(session_factory, tmp_path):
    from datetime import UTC, datetime, timedelta

    from uep.tasks.supervisor import Supervisor

    loop_port = FakePort([])
    loop_port.hang = True  # 模型呼叫卡住：迴圈自己不會送心跳，只剩背景心跳
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=loop_port)
    try:
        launcher = _tight_launcher(rig)
        supervisor = Supervisor(rig.core.tasks, deadline_grace=timedelta(seconds=30))
        rig.say("幫我查")
        rig.wait_for(lambda: bool(loop_port.requests), what="Run 進入模型呼叫")
        time.sleep(1.2)  # 超過 stale_after（0.6s）也超過 run_deadline（0.5s+0.1s）：只有背景心跳在維持心跳
        stats = supervisor.tick(datetime.now(UTC))
        _tasks, (run,) = _rows(session_factory)
        assert stats.cancel_requested == 1 and stats.forced == 0  # 心跳新鮮：旗標已設，但不 force（T-12）
        assert run.status == "RUNNING" and run.worker_epoch == 1
        # 心跳停止（worker 消失的等價做法）：stale 之後，Supervisor 在 deadline 已到期的前提下 force 收尾
        launcher._closing.set()
        time.sleep(1.0)
        stats = supervisor.tick(datetime.now(UTC))
        assert stats.forced == 1
        tasks, (run,) = _rows(session_factory)
        assert run.status == "FAILED" and [t.status for t in tasks] == ["FAILED"]
    finally:
        rig.core.loop_launcher.close(timeout=0.2)
        rig.core.shutdown()


def test_supervisor_does_not_touch_a_loop_run_before_its_deadline_even_if_idle(session_factory, tmp_path):
    from datetime import UTC, datetime, timedelta

    from uep.tasks.supervisor import Supervisor

    loop_port = FakePort([])
    loop_port.hang = True
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=loop_port)
    try:
        rig.say("幫我查")
        rig.wait_for(lambda: bool(loop_port.requests), what="Run 進入模型呼叫")
        # 未到 deadline：Supervisor 只在 deadline 到期後才看 stale，即使心跳在未來的時間點上看起來已過期也不動
        stats = Supervisor(rig.core.tasks).tick(datetime.now(UTC) + timedelta(seconds=5))
        assert stats.forced == 0 and stats.cancel_requested == 0
        assert _rows(session_factory)[1][0].status == "RUNNING"
    finally:
        rig.core.loop_launcher.close(timeout=0.2)
        rig.core.shutdown()


def test_production_wiring_hands_assembly_executors_to_the_loop_launcher(session_factory, tmp_path):
    from uep.tools.catalog.memory import MEMORY_EXECUTOR

    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=FakePort([]))
    try:
        launcher = rig.core.loop_launcher
        assert MEMORY_EXECUTOR in launcher._executors  # assembly 合併後的映射（含 memory executor）
        assert launcher._executors == dict(rig.core.tasks._executors)
    finally:
        rig.core.shutdown()


# ---- 續跑結果回 Host 的入口（D107 R1）：真實 Gate／Delivery／端點 ----

RESUMED_TEXT = "核准後完成了。"


def _resume_rig(session_factory, tmp_path):
    """跑完一個正常的 agent_loop turn，拿到原 ticket 與 Task 的 activity，並攔截記憶 store。"""
    loop_port = FakePort([[text("原本的回覆。"), fin()]])
    rig, _ = _agent_rig(session_factory, tmp_path, route="agent_loop", flag=True, loop_port=loop_port)
    result = rig.say("幫我處理")
    rig.wait_terminal(result.ticket.turn_id)
    tasks, _ = _rows(session_factory)
    stores: list[dict] = []
    rig.host._memory.store = lambda **kw: stores.append(kw) if kw.get("spoken_text") == RESUMED_TEXT else None
    return rig, result.ticket, tasks[0].activity_id, stores


def _resumed(ticket, activity_id, **over):
    from uep.echo.resume_return import ResumeCause, ResumedRunResult, ResumeOutcome

    base = {
        "run_id": "run_x", "worker_epoch": 2, "task_id": "task_x", "activity_id": activity_id, "ticket": ticket,
        "outcome": ResumeOutcome.COMPLETED, "cause": ResumeCause.APPROVED, "text": "核准後完成了。",
    }
    base.update(over)
    return ResumedRunResult(**base)


def _resume_texts(rig):
    return [t.text for t in rig.host.endpoint.rendered_texts() if t.text == "核准後完成了。"]


def test_resumed_result_goes_through_gate_and_writes_memory_exactly_once(session_factory, tmp_path):
    from uep.echo.resume_return import ResumeDisposition

    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        before = len(rig.deliveries())
        assert rig.host.accept_resumed_result(_resumed(ticket, activity)) is ResumeDisposition.DELIVERED
        assert _resume_texts(rig) == ["核准後完成了。"] and len(rig.deliveries()) == before + 1
        assert len(stores) == 1 and stores[0]["idempotency_key"] == f"turn:{ticket.turn_id}"
        assert stores[0]["spoken_text"] == "核准後完成了。" and stores[0]["session_id"] == ticket.segment_id
        # 同一個 (run_id, epoch) 再來一次：不重送、不重寫
        assert rig.host.accept_resumed_result(_resumed(ticket, activity)) is ResumeDisposition.DELIVERED
        assert len(rig.deliveries()) == before + 1 and len(stores) == 1 and _resume_texts(rig) == ["核准後完成了。"]
    finally:
        rig.core.shutdown()


def test_resumed_result_with_rejected_cause_is_sent_but_not_remembered(session_factory, tmp_path):
    from uep.echo.resume_return import ResumeCause, ResumeDisposition

    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        out = rig.host.accept_resumed_result(_resumed(ticket, activity, cause=ResumeCause.REJECTED))
        assert out is ResumeDisposition.DELIVERED and _resume_texts(rig) == ["核准後完成了。"] and stores == []
    finally:
        rig.core.shutdown()


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_failed_or_cancelled_resumed_result_sends_nothing_and_writes_nothing(session_factory, tmp_path, outcome):
    from uep.echo.resume_return import ResumeDisposition, ResumeOutcome

    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        before = len(rig.deliveries())
        out = rig.host.accept_resumed_result(_resumed(ticket, activity, outcome=ResumeOutcome(outcome), text=""))
        assert out is ResumeDisposition.NOT_APPLICABLE and len(rig.deliveries()) == before and stores == []
    finally:
        rig.core.shutdown()


def test_resumed_result_without_lease_is_dropped_with_metric(session_factory, tmp_path):
    from uep.echo.resume_return import ResumeDisposition

    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        rig.host.endpoint.lease_auth = lambda: None
        assert rig.host.accept_resumed_result(_resumed(ticket, activity)) is ResumeDisposition.DROPPED
        assert stores == [] and rig.host.metrics["echo.resume_dropped.no_lease"] == 1
    finally:
        rig.core.shutdown()


def test_stale_lease_is_retried_once_with_fresh_authorization(session_factory, tmp_path):
    from uep.echo.resume_return import ResumeDisposition

    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        real = rig.host.endpoint.lease_auth
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            auth = real()
            return dataclasses.replace(auth, fencing_epoch=auth.fencing_epoch + 99) if calls["n"] == 1 else auth

        rig.host.endpoint.lease_auth = flaky
        assert rig.host.accept_resumed_result(_resumed(ticket, activity)) is ResumeDisposition.DELIVERED
        assert calls["n"] == 2 and len(stores) == 1
    finally:
        rig.core.shutdown()


def test_always_stale_lease_retries_only_once_then_drops(session_factory, tmp_path):
    from uep.echo.resume_return import ResumeDisposition

    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        real = rig.host.endpoint.lease_auth
        calls = {"n": 0}

        def stale():
            calls["n"] += 1
            auth = real()
            return dataclasses.replace(auth, fencing_epoch=auth.fencing_epoch + 99)

        rig.host.endpoint.lease_auth = stale
        assert rig.host.accept_resumed_result(_resumed(ticket, activity)) is ResumeDisposition.DROPPED
        assert calls["n"] == 2 and stores == [] and _resume_texts(rig) == []
    finally:
        rig.core.shutdown()


def test_unknown_delivery_is_uncertain_and_not_remembered(session_factory, tmp_path):
    from types import SimpleNamespace

    from uep.echo.resume_return import ResumeDisposition
    from uep.output.delivery import DeliveryStatus

    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        services = rig.host._services
        original = services.delivery.dispatch
        services.delivery.dispatch = lambda intent: SimpleNamespace(status=DeliveryStatus.UNKNOWN, failure=None)
        try:
            assert rig.host.accept_resumed_result(_resumed(ticket, activity)) is ResumeDisposition.UNCERTAIN
        finally:
            services.delivery.dispatch = original
        assert stores == []
        # UNCERTAIN 也被冪等鍵記住：之後同鍵再呼叫不重送
        before = len(rig.deliveries())
        assert rig.host.accept_resumed_result(_resumed(ticket, activity)) is ResumeDisposition.UNCERTAIN
        assert len(rig.deliveries()) == before
    finally:
        rig.core.shutdown()


# 註：「park -> 核准 -> resume -> Gate -> 記憶」沒有串成單一測試：核准工具（testkit）加完整 Host rig 的成本高，
# 改成兩段互鎖——resume 端（核准後 Run 終態 -> on_resumed 的映射）見 test_resume_return.py，送出與記憶端（真實 Gate／Delivery／端點）
# 見上面各測試；兩段以 ResumedRunResult 為介面，各自鎖住。

def _patch_dispatch(rig, status, code=None):
    """攔截 delivery.dispatch，回指定狀態（不經 Gate）；回傳呼叫計數與還原函式。"""
    from types import SimpleNamespace

    calls = {"n": 0}
    services = rig.host._services
    original = services.delivery.dispatch

    def fake(intent):
        calls["n"] += 1
        return SimpleNamespace(status=status, failure={"code": code} if code else None)

    services.delivery.dispatch = fake
    return calls, lambda: setattr(services.delivery, "dispatch", original)


def test_resumed_result_is_dropped_when_muted_or_busy_before_lease_is_even_read(session_factory, tmp_path):
    from types import SimpleNamespace

    from uep.echo.resume_return import ResumeDisposition
    from uep.state.coordinator import Axis, BehaviorState

    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        before = len(rig.deliveries())
        state = rig.host._services.state
        real_current = state.current
        state.current = lambda axis: SimpleNamespace(state=BehaviorState.SLEEP.value) if axis is Axis.BEHAVIOR else real_current(axis)
        try:
            assert rig.host.accept_resumed_result(_resumed(ticket, activity, run_id="run_muted")) is ResumeDisposition.DROPPED
        finally:
            state.current = real_current
        assert rig.host.metrics["echo.resume_dropped.muted"] == 1
        rig.host.orchestrator.live_tickets = lambda: [object()]
        assert rig.host.accept_resumed_result(_resumed(ticket, activity, run_id="run_busy")) is ResumeDisposition.DROPPED
        assert rig.host.metrics["echo.resume_dropped.busy"] == 1
        assert len(rig.deliveries()) == before and stores == []
    finally:
        rig.core.shutdown()


def test_two_threads_with_the_same_key_send_exactly_once(session_factory, tmp_path):
    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        before = len(rig.deliveries())
        results: list = []
        barrier = threading.Barrier(2)

        def call():
            barrier.wait()
            results.append(rig.host.accept_resumed_result(_resumed(ticket, activity)))

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert len(results) == 2 and results[0] is results[1]
        assert len(rig.deliveries()) == before + 1 and len(stores) == 1 and _resume_texts(rig) == [RESUMED_TEXT]
    finally:
        rig.core.shutdown()


def test_enqueued_delivery_is_uncertain_and_never_resent(session_factory, tmp_path):
    from uep.echo.resume_return import ResumeDisposition
    from uep.output.delivery import DeliveryStatus

    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        calls, restore = _patch_dispatch(rig, DeliveryStatus.ENQUEUED)
        try:
            assert rig.host.accept_resumed_result(_resumed(ticket, activity)) is ResumeDisposition.UNCERTAIN
            assert rig.host.accept_resumed_result(_resumed(ticket, activity)) is ResumeDisposition.UNCERTAIN
        finally:
            restore()
        assert calls["n"] == 1 and stores == []
    finally:
        rig.core.shutdown()


@pytest.mark.parametrize("code", ["REJECT_AUDIENCE", "REJECT_REVOKED", "REJECT_EXPIRED", "REJECT_NO_PRESENCE"])
def test_gate_rejections_drop_without_memory_and_without_retry(session_factory, tmp_path, code):
    from uep.echo.resume_return import ResumeDisposition
    from uep.output.delivery import DeliveryStatus

    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        calls, restore = _patch_dispatch(rig, DeliveryStatus.REJECTED, code)
        try:
            assert rig.host.accept_resumed_result(_resumed(ticket, activity)) is ResumeDisposition.DROPPED
        finally:
            restore()
        assert calls["n"] == 1 and stores == [] and rig.host.metrics[f"echo.resume_dropped.{code.lower()}"] == 1
    finally:
        rig.core.shutdown()


def test_send_failed_is_not_retried(session_factory, tmp_path):
    from uep.echo.resume_return import ResumeDisposition
    from uep.output.delivery import DeliveryStatus

    rig, ticket, activity, stores = _resume_rig(session_factory, tmp_path)
    try:
        calls, restore = _patch_dispatch(rig, DeliveryStatus.FAILED, "SEND_FAILED")
        try:
            assert rig.host.accept_resumed_result(_resumed(ticket, activity)) is ResumeDisposition.DROPPED
        finally:
            restore()
        assert calls["n"] == 1 and stores == []
    finally:
        rig.core.shutdown()


def test_resume_text_is_cleared_from_loop_text_after_sending(session_factory, tmp_path):
    rig, ticket, activity, _ = _resume_rig(session_factory, tmp_path)
    try:
        rig.host.accept_resumed_result(_resumed(ticket, activity))
        assert len(rig.host.loop_text) == 0
    finally:
        rig.core.shutdown()
