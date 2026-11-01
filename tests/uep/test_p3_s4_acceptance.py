"""P3 S4 驗收：播放證據與插話讓位（P3接入方案 §5 S4 列、§5.3）。

V06（插話停播、Sink 不 drain、歷史只含已交付）、V08（生成完成不等於交付完成）、VC-4-5（控制訊號不被工作緒
積壓擋住）、VC-5-2（Discord 只到 transport_sent）、斷線與撤銷後舊輸出不復活；另含 D5（cancel_turn）、D79（工具白名單與
action 事件）、D82（spoken-only 開關）、D73（dispatch 預算）。以 ES 的 fakes 驅動，需 PostgreSQL 與已安裝的 echo_stream。
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime

import pytest

pytest.importorskip("echo_stream")

from echo_stream.contracts.output import PlayedEvidence
from echo_stream.contracts.types import AudioChunk
from echo_stream.fakes.audio import FakeAudioSink
from echo_stream.fakes.stages import FakeThinkStage

from tests.uep.echo_support import (
    SPEAKER,
    WAIT,
    GatedSpeak,
    build_rig,
    make_session,
)
from uep.echo.config import EchoOutputConfig
from uep.echo.memory import GuildRoster
from uep.echo.metrics import Metrics
from uep.echo.tools import CoreToolExecutor
from uep.fakes.memory import FakeMemoryEngine, FakeSharedItem
from uep.ids import new_id
from uep.memory.access import MemoryAccessLayer
from uep.memory.identity import IdentityDirectory
from uep.output.delivery import DeliveryStatus
from uep.output.intent import ControlKind, ControlScope, ControlSignal

pytestmark = pytest.mark.usefixtures("session_factory")

LONG3 = "第一句話很長很長很長很長很長很長。第二句話也很長很長很長很長很長。第三句話同樣很長很長很長很長。"


class CountingSink(FakeAudioSink):
    """記錄 drain 與 stop 呼叫（V06：插話不得 drain）。"""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.drain_calls = 0
        self.stop_calls = 0
        self.stopped_at: float | None = None

    async def drain(self) -> None:
        self.drain_calls += 1
        await super().drain()

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stopped_at is None:
            self.stopped_at = time.perf_counter()
        super().stop()


class SentOnlySink(CountingSink):
    """Discord 型：只有 transport_sent 證據，沒有播放確認（VC-5-2）。"""

    played_evidence = PlayedEvidence.TRANSPORT_SENT


@pytest.fixture
def rig(session_factory, tmp_path):
    made = []

    def _build(**over):
        r = build_rig(session_factory, tmp_path, **over)
        made.append(r)
        return r

    yield _build
    for r in made:
        r.core.shutdown()


def counting_factory(cls=CountingSink, realtime=True):
    sinks: list[CountingSink] = []

    def factory(view, ticket):
        sink = cls(realtime=realtime)
        sinks.append(sink)
        return sink

    factory.sinks = sinks  # type: ignore[attr-defined]
    return factory


def history_assistant(think) -> list[str]:
    return [h["content"] for h in think.history if h["role"] == "assistant"]


def control(r, kind: ControlKind, scope: ControlScope, **fence: int) -> None:
    auth = r.host.endpoint.lease_auth()
    target = {"core_epoch": auth.core_epoch, "presence_generation": auth.presence_generation, **fence}
    r.core.control.send(
        ControlSignal(
            control_id=new_id("ctl"), kind=kind, scope=scope, target_fence=target, issued_at=datetime.now(UTC)
        ),
        endpoint_id=r.host.endpoint_id,
    )


# ---- V06：插話停止播放，Sink 不 drain，歷史只含已交付 ----

def test_v06_barge_in_stops_sink_without_drain_and_history_keeps_only_delivered(rig):
    speak = GatedSpeak()
    factory = counting_factory()
    r = rig(realtime=True, speak=speak, sink_factory=factory, responses=(LONG3,))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: factory.sinks and factory.sinks[0].chunks, what="首個 chunk")
    sink = factory.sinks[0]
    r.wait_for(lambda: sink.played_duration_s > 0.05, what="已有實播量")
    r.voice(seconds=3.0)  # 插話
    speak.release.set()
    kinds = r.wait_terminal(turn_id)
    assert kinds[-1] == "turn.cancelled"
    assert r.events(turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "barge_in"
    assert sink.drain_calls == 0 and sink.stop_calls >= 1  # 插話停播：不 drain
    (committed,) = [c for c in r.think.committed if c[0] == turn_id]
    _tid, spoken, generated = committed
    assert generated == LONG3 and len(spoken) < len(generated)  # 只 commit 已交付的部分
    assert history_assistant(r.think) == ([spoken] if spoken else [])
    assert LONG3 not in "".join(history_assistant(r.think))


# ---- V08：生成完成不等於交付完成 ----

def test_v08_generation_completed_is_not_delivery_completed(rig):
    r = rig(realtime=True, responses=(LONG3,))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: bool(r.events(turn_id, ("turn.generation_completed",))), what="generation_completed")
    # 生成早就完成，但音訊還在播：沒有任何 delivery 到 rendered_played
    assert all(d.status != DeliveryStatus.RENDERED_PLAYED.value for d in r.deliveries())
    assert "turn.completed" not in r.kinds(turn_id)
    r.wait_terminal(turn_id, timeout=30.0)
    deliveries = r.deliveries()
    assert deliveries and all(d.status == DeliveryStatus.RENDERED_PLAYED.value for d in deliveries)
    order = {e.event_type: e.journal_seq for e in r.events(turn_id)}
    assert order["turn.generation_completed"] < order["turn.completed"]


def test_playback_progress_is_reported_while_still_playing_even_when_tts_is_faster(rig):
    """TTS 一次把所有 chunk 寫完、之後沒有 write 可觸發：靠輪詢讓已播畢的句子即時升證據，最後一句仍在播。"""
    factory = counting_factory()
    r = rig(
        realtime=True, sink_factory=factory, responses=(LONG3,),
        host_kwargs={"output": EchoOutputConfig(progress_poll_interval_s=0.05)},
    )
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: factory.sinks and len(factory.sinks[0].chunks) >= 2, what="chunk 全部寫入")
    sink = factory.sinks[0]
    r.wait_for(
        lambda: any(d.sequence == 0 and d.status == DeliveryStatus.RENDERED_PLAYED.value for d in r.deliveries()),
        timeout=30.0, what="首句在整輪結束前即升到 rendered_played",
    )
    deliveries = r.deliveries()
    assert sink.is_active  # sink 還在播
    assert deliveries[-1].status != DeliveryStatus.RENDERED_PLAYED.value  # 最後一句尚未播畢
    assert "turn.completed" not in r.kinds(turn_id)
    r.wait_terminal(turn_id, timeout=30.0)
    assert [(d.sequence, d.status) for d in r.deliveries()] == [(d.sequence, "rendered_played") for d in r.deliveries()]


# ---- VC-5-2：Discord 只有 transport_sent ----

def test_vc_5_2_transport_sent_sink_stays_sent_and_spoken_falls_back_to_whole_sentence(rig):
    factory = counting_factory(SentOnlySink, realtime=False)
    r = rig(sink_factory=factory, responses=(LONG3,))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_terminal(turn_id)
    deliveries = r.deliveries()
    assert len(deliveries) >= 2
    assert {d.status for d in deliveries} == {DeliveryStatus.SENT.value}  # 沒有播放確認：停在 sent
    assert {d.delivery_evidence for d in deliveries} == {"transport_sent"}
    assert r.think.committed[0][1] == LONG3  # auto 規則：整句制，整句都送出才算


def test_vc_5_2_realtime_transport_sent_sink_never_spoken_mid_sentence(rig):
    """部分送出時走整句制：被打斷時 spoken 只會是整句或空，不會是句中片段；證據仍停在 sent。"""
    speak = GatedSpeak()
    factory = counting_factory(SentOnlySink, realtime=True)
    r = rig(realtime=True, speak=speak, sink_factory=factory, responses=(LONG3,))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: factory.sinks and factory.sinks[0].chunks, what="首個 chunk")
    sink = factory.sinks[0]
    r.wait_for(lambda: sink.played_duration_s > 0.05, what="已有實播量")
    r.voice(seconds=3.0)  # 第一句還沒播完就被打斷
    speak.release.set()
    r.wait_terminal(turn_id)
    (committed,) = [c for c in r.think.committed if c[0] == turn_id]
    spoken = committed[1]
    assert spoken == "" or (LONG3.startswith(spoken) and spoken.endswith("。"))
    assert {d.status for d in r.deliveries()} <= {DeliveryStatus.SENT.value, DeliveryStatus.CANCELLED.value}


# ---- VC-4-5：佇列滿載時控制訊號不被擋 ----

def test_vc_4_5_control_stop_is_not_blocked_by_worker_backlog(rig):
    speak = GatedSpeak()
    factory = counting_factory()
    r = rig(realtime=True, speak=speak, sink_factory=factory, responses=(LONG3,))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: factory.sinks and factory.sinks[0].chunks, what="首個 chunk")
    gate = threading.Event()
    r.host._worker.submit(lambda: gate.wait(WAIT), "block")  # 工作緒卡住
    for i in range(50):
        r.host._worker.submit(lambda: None, f"backlog{i}")  # 佇列積壓
    t0 = time.perf_counter()
    control(r, ControlKind.STOP, ControlScope.TURN, turn_generation=result.ticket.turn_generation)
    r.wait_for(lambda: factory.sinks[0].stop_calls >= 1, timeout=2.0, what="inner.stop 在工作緒放行前被呼叫")
    assert time.perf_counter() - t0 < 2.0
    assert not gate.is_set()  # 工作緒仍卡住：控制路徑沒有經過它
    gate.set()
    speak.release.set()
    kinds = r.wait_terminal(turn_id)
    assert kinds[-1] == "turn.cancelled"
    cancelled = r.events(turn_id, ("turn.cancelled",))[0]
    assert cancelled.payload["reason_code"] == "control_stop"
    assert cancelled.fence["turn_generation"] > result.ticket.turn_generation


# ---- scope=turn 的 stop：以「<= 目標 generation」涵蓋較舊 live turn（與端點 _stopped_turn 一致）----

def test_turn_stop_covers_older_live_turns_but_not_newer_ones(rig):
    speak = GatedSpeak()
    factory = counting_factory()
    r = rig(realtime=True, speak=speak, sink_factory=factory, responses=(LONG3,))
    first = r.say("甲", speaker=SPEAKER).ticket
    second = r.say("乙", speaker="discord:222").ticket
    assert first and second and second.turn_generation > first.turn_generation
    r.wait_for(lambda: factory.sinks and factory.sinks[0].chunks, what="首輪開始出聲")
    control(r, ControlKind.STOP, ControlScope.TURN, turn_generation=first.turn_generation)
    speak.release.set()
    r.wait_terminal(first.turn_id)
    assert r.events(first.turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "control_stop"
    r.wait_terminal(second.turn_id, timeout=30.0)
    # 較新的輪不受影響（沒有被 control_stop 取消）
    reasons = [e.payload.get("reason_code") for e in r.events(second.turn_id, ("turn.cancelled",))]
    assert "control_stop" not in reasons


# ---- 全域停止與撤銷：Core 原因碼隨 ES cancel_turn 回來；歷史整輪撤回；舊輸出不復活 ----

@pytest.mark.parametrize("how", ["global_stop", "revoke"])
def test_global_stop_and_revoke_use_cancel_turn_retract_history_and_do_not_resurrect(rig, how):
    speak = GatedSpeak()
    factory = counting_factory()
    # eager：生成完成就 commit 全文，歷史撤回才看得出差別
    r = rig(
        realtime=True, speak=speak, sink_factory=factory, responses=(LONG3,),
        host_kwargs={"output": EchoOutputConfig(commit_mode="eager")},
    )
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: bool(r.events(turn_id, ("turn.generation_completed",))), what="生成完成")
    assert history_assistant(r.think) == [LONG3]  # eager：全文已在歷史
    if how == "global_stop":
        r.core.stop.engage(reason="test")
    else:
        r.core.presence.revoke("test_revoke")
    speak.release.set()
    kinds = r.wait_terminal(turn_id)
    assert kinds[-1] == "turn.cancelled"
    cancelled = r.events(turn_id, ("turn.cancelled",))[0]
    # 由 ES outcome 帶回，沒有 Core 覆寫。全域停止會同時撤銷 Lease，兩種通知先到者登記原因（冪等）
    allowed = {"global_stop", "presence_lost"} if how == "global_stop" else {"presence_lost"}
    assert cancelled.payload["reason_code"] in allowed
    assert cancelled.fence["turn_generation"] > result.ticket.turn_generation
    assert r.host.metrics["echo.cancel_turn_claimed"] >= 1
    assert history_assistant(r.think) == [] and not r.think.history  # 整輪撤出歷史（含使用者話語）
    # 不復活：晚到 chunk 只計 discard，不產生 intent
    wrapper = r.host.wrappers_of(turn_id)[-1]
    sink = factory.sinks[-1]
    chunks, intents, deliveries = len(sink.chunks), len(r.intents(turn_id)), len(r.deliveries())
    late = AudioChunk(pcm=b"\x00\x01" * 800, sample_rate=22050, turn_id=turn_id, sentence_index=7, chunk_index=1)
    r.run_on_es(wrapper.write(late))
    r.host._worker.drain(WAIT)
    assert len(sink.chunks) == chunks and len(r.intents(turn_id)) == intents and len(r.deliveries()) == deliveries
    # 撤銷後新輸入不開 turn；全域停止後重新 acquire 之前同樣不開
    assert r.say("還在嗎").ticket is None


def test_old_ticket_output_stays_dead_after_lease_is_reacquired(rig):
    speak = GatedSpeak()
    factory = counting_factory()
    r = rig(realtime=True, speak=speak, sink_factory=factory, responses=(LONG3,))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: factory.sinks and factory.sinks[0].chunks, what="首個 chunk")
    r.core.presence.revoke("reconnect_test")
    r.wait_for(lambda: r.host.generations.get(turn_id) > result.ticket.turn_generation, what="撤銷已登記")
    speak.release.set()
    r.wait_terminal(turn_id)
    r.host.acquire_lease()  # 斷線重連：取得新 Lease
    assert r.host.presence_active
    sink = factory.sinks[0]
    count, intents = len(sink.chunks), len(r.intents(turn_id))
    wrapper = r.host.wrappers_of(turn_id)[0]
    late = AudioChunk(pcm=b"\x00\x01" * 800, sample_rate=22050, turn_id=turn_id, sentence_index=0, chunk_index=5)
    r.run_on_es(wrapper.write(late))
    r.host._worker.drain(WAIT)
    assert len(sink.chunks) == count and len(r.intents(turn_id)) == intents  # 舊 ticket 的輸出不因新 Lease 復活


# ---- D5：被拒、dispatch 失敗改用 cancel_turn ----

def test_dispatch_failure_uses_cancel_turn_and_retracts_history(rig):
    speak = GatedSpeak()
    factory = counting_factory()
    r = rig(
        realtime=True, speak=speak, sink_factory=factory, responses=(LONG3,),
        host_kwargs={"output": EchoOutputConfig(commit_mode="eager")},
    )

    def boom(intent):
        raise RuntimeError("db down")

    r.host._services.delivery.dispatch = boom  # type: ignore[method-assign]
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: bool(r.events(turn_id, ("turn.generation_completed",))), what="生成完成")
    speak.release.set()
    kinds = r.wait_terminal(turn_id)
    assert kinds[-1] == "turn.cancelled"
    assert r.events(turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "dispatch_failed"
    assert r.host.metrics["echo.cancel_turn_claimed"] + r.host.metrics["echo.cancel_turn_missed"] >= 1
    r.wait_for(lambda: not r.think.history, what="歷史整輪撤回")
    assert r.host.metrics["echo.history_retract_failed"] == 0


def test_cancel_turn_reason_code_is_truncated_to_es_limit(rig):
    r = rig(realtime=True, responses=(LONG3,))
    result = r.say("講點什麼")
    r.wait_for(lambda: bool(r.host.wrappers_of(result.ticket.turn_id)), what="開始出聲")
    r.run_on_es(_call(lambda: r.host._cancel_in_es(result.ticket.turn_id, "x" * 200)))  # 不拋 ValueError
    r.wait_terminal(result.ticket.turn_id)


async def _call(fn):
    return fn()


# ---- D82 (b)：spoken-only 開關（eager 下才有差別；被拒／撤銷不受影響）----

@pytest.mark.parametrize("enabled", [False, True])
def test_d82_switch_controls_whether_eager_history_is_settled_to_spoken(rig, enabled):
    speak = GatedSpeak()
    factory = counting_factory()
    r = rig(
        realtime=True, speak=speak, sink_factory=factory, responses=(LONG3,),
        host_kwargs={"output": EchoOutputConfig(commit_mode="eager", history_spoken_only=enabled)},
    )
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: bool(r.events(turn_id, ("turn.generation_completed",))), what="生成完成")
    sink = factory.sinks[0]
    r.wait_for(lambda: sink.played_duration_s > 0.05, what="已有實播量")
    r.voice(seconds=3.0)
    speak.release.set()
    r.wait_terminal(turn_id)
    r.wait_for(lambda: len(r.think.history) >= 1, what="歷史")
    time.sleep(0.1)
    assistant = history_assistant(r.think)
    if enabled:
        assert len(assistant) == 1 and 0 < len(assistant[0]) < len(LONG3)  # 開：最終只留已播出的部分
    else:
        assert assistant == [LONG3]  # 關：維持現況（eager 全文留在歷史）


# ---- D73：dispatch 增量預算（只記錄）----

def test_d73_dispatch_budget_is_a_setting_and_only_records(rig):
    r = rig(host_kwargs={"output": EchoOutputConfig(dispatch_budget_ms=0.0)})
    result = r.say("你好，我是測試。今天天氣不錯。")
    r.wait_terminal(result.ticket.turn_id)
    assert r.host.metrics["echo.dispatch_over_budget"] == 2 and r.host.dispatch_ms_max > 0.0
    assert r.kinds(result.ticket.turn_id)[-1] == "turn.completed"  # 超出預算不丟棄、不降級
    r2 = rig(host_kwargs={"output": EchoOutputConfig(dispatch_budget_ms=60000.0)})
    r2.wait_terminal(r2.say().ticket.turn_id)
    assert r2.host.metrics["echo.dispatch_over_budget"] == 0
    assert EchoOutputConfig().dispatch_budget_ms == 150.0
    with pytest.raises(ValueError):
        EchoOutputConfig(commit_mode="bogus")


# ---- D79：工具白名單與 action 事件 ----

class ToolPort:
    """最小 MemoryPort 替身：記錄工具呼叫。"""

    def __init__(self, outcomes=None) -> None:
        self.stored: list[dict] = []
        self.calls: list[dict] = []
        self.outcomes = outcomes or {}

    def retrieve(self, scope, query, audience, **kw):
        raise AssertionError("不應被呼叫")

    def store(self, **kw):
        self.stored.append(kw)

    def profile(self, *, actor_id, audience, max_chars=800):
        return ""

    def set_session(self, session_id):
        pass

    def tool_definitions(self):
        return [
            {"type": "function", "function": {"name": "deep_recall"}},
            {"type": "function", "function": {"name": "member_recall"}},
        ]

    def execute_tool(self, name, args, *, actor_id, audience, session_id=None, context=None):
        self.calls.append({"name": name, "args": dict(args), "actor_id": actor_id, "audience": audience,
                           "session_id": session_id, "context": context})
        return dict(self.outcomes.get(name, {"success": True, "result": "ok"}))


class ToolThink(FakeThinkStage):
    """stream 開始時以 ES 的方式呼叫一次工具（tool_executor(name, args, context=tool_context)）。"""

    tools: list = None  # type: ignore[assignment]
    tool_executor = None
    memory_resolver = None
    memory = None

    def __init__(self, tool_name, tool_args, **kw) -> None:
        super().__init__(**kw)
        self.tools = []
        self.tool_name, self.tool_args = tool_name, tool_args
        self.tool_result = None

    async def stream(self, utterance, token):
        self.tool_result = await self.tool_executor(
            self.tool_name, self.tool_args, context=utterance.metadata["tool_context"]
        )
        async for sentence in super().stream(utterance, token):
            yield sentence


def _tool_think(r_tracer, name, args):
    return ToolThink(
        name, args, responses=["好的。"], first_token_delay_s=0.0, token_interval_s=0.0, memory_delay_s=0.0,
        tracer=r_tracer,
    )


def test_d79_host_whitelist_executes_via_access_layer_and_records_action_event(rig):
    from echo_stream.core.tracer import LatencyTracer

    tracer = LatencyTracer()
    secret = "這是引數裡的私人查詢字串"
    think = _tool_think(tracer, "deep_recall", {"query": secret})
    port = ToolPort({"deep_recall": {"success": True, "result": "命中的私人結果"}})
    r = rig(think=think, memory=port)
    assert [t["function"]["name"] for t in think.tools] == ["deep_recall"]  # 沒有 guild：不曝光 member_recall
    assert isinstance(think.tool_executor, CoreToolExecutor)
    result = r.say("查一下")
    r.wait_terminal(result.ticket.turn_id)
    assert think.tool_result == {"success": True, "result": "命中的私人結果"}
    (call,) = port.calls
    assert call["actor_id"] == "actor_a" and call["audience"] == "scope_private_owner"  # 取自 Core 寫入的 context
    events = r.events(result.ticket.turn_id, ("action.executed", "action.unknown"))
    assert [e.event_type for e in events] == ["action.executed"]
    assert events[0].payload == {"kind": "internal_read", "status": "executed", "ref_id": "tool:deep_recall"}
    assert secret not in str(events[0].payload) and "命中" not in str(events[0].payload)


def test_d79_non_whitelisted_tool_is_denied_without_event(rig):
    from echo_stream.core.tracer import LatencyTracer

    think = _tool_think(LatencyTracer(), "trigger_dream", {})
    port = ToolPort()
    r = rig(think=think, memory=port)
    result = r.say("做夢")
    r.wait_terminal(result.ticket.turn_id)
    assert think.tool_result["success"] is False and port.calls == []
    assert r.events(result.ticket.turn_id, ("action.executed", "action.unknown")) == []
    assert r.host.metrics["echo.tool.denied"] == 1


def test_d79_timeout_is_recorded_as_action_unknown(rig):
    from echo_stream.core.tracer import LatencyTracer

    think = _tool_think(LatencyTracer(), "deep_recall", {"query": "x"})
    port = ToolPort({"deep_recall": {"success": False, "result": "tool timeout", "code": "timeout"}})
    r = rig(think=think, memory=port)
    result = r.say("查")
    r.wait_terminal(result.ticket.turn_id)
    assert [e.event_type for e in r.events(result.ticket.turn_id, ("action.executed", "action.unknown"))] == [
        "action.unknown"
    ]


def test_d79_host_rejects_think_with_own_tool_executor():
    from echo_stream.core.tracer import LatencyTracer

    from uep.echo.host import EchoPipelineHost

    think = _tool_think(LatencyTracer(), "deep_recall", {})
    think.tool_executor = lambda *a, **k: None
    with pytest.raises(ValueError, match="tool_executor"):
        EchoPipelineHost(
            publisher=None, think=think, speak=None, sink_factory=lambda v, t: "x", session=make_session(),
        )


def test_d79_member_recall_resolves_name_through_roster_and_collects_ids():
    import asyncio

    roster = GuildRoster({"123": {"actor_a": "111", "actor_b": "222"}}, profiles={
        "123": {"222": {"display_name": "測試乙", "aliases": ["小乙"]}, "111": {"display_name": "測試甲"}}
    })
    port = ToolPort({"member_recall": {"success": True, "result": "投影", "shared_ids": ["p1", "p2"]}})
    seen: list = []
    executor = CoreToolExecutor(port, roster, on_action=lambda *a: seen.append(a))
    ctx = {"actor_id": "actor_a", "audience": "scope_group_g123", "segment_id": "seg_x", "ticket_ref": "turn_x",
           "guild_id": "123", "present_uids": ["111", "222", "999"]}
    out = asyncio.run(executor("member_recall", {"member": " 小乙 ", "query": "近況"}, context=ctx))
    assert out == {"success": True, "result": "投影"}
    assert ctx["member_recall_ids"] == ["p1", "p2"]
    call = port.calls[0]
    assert call["context"]["member_uid"] == "222" and call["args"]["member"] == " 小乙 "
    assert seen == [("turn_x", "member_recall", "internal_read", "executed")]
    # 不明或同名多人：不碰 engine、請模型先確認
    port.calls.clear()
    miss = asyncio.run(executor("member_recall", {"member": "不存在的人"}, context=dict(ctx)))
    assert miss["success"] is True and "確認" in miss["result"] and port.calls == []
    no_guild = {k: v for k, v in ctx.items() if k != "guild_id"}
    assert asyncio.run(executor("member_recall", {"member": "測試甲"}, context=no_guild))["success"] is False
    assert asyncio.run(executor("deep_recall", {}, context=None))["success"] is False  # 沒有 context：fail-closed


class _Runtime:
    def __init__(self, engines: dict) -> None:
        self.engines = engines

    def namespace_for_owner(self, owner_id):
        return f"ns-{owner_id}"

    def namespace_for_guild(self, guild_id):
        return f"scope-g{guild_id}"

    def call(self, namespace_id, fn, *, timeout_s, on_late=None):
        return fn(self.engines[namespace_id])

    def adapter(self, namespace_id):
        return None  # 最小組裝：沒有 adapter，工具退回 backend.execute


class _Backend:
    def __init__(self) -> None:
        self.calls: list = []

    def definitions(self):
        return [
            {"type": "function", "function": {
                "name": "deep_recall", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}},
            {"name": "member_recall"}, {"name": "trigger_dream"},
        ]

    def execute(self, engine, name, args, session_id):
        self.calls.append((name, dict(args)))
        return {"success": True, "result": "r"}


def _member_rig(tmp_path, *, engine_cls=FakeMemoryEngine):
    roster = GuildRoster(
        {"123": {"actor_a": "111", "actor_b": "222", "actor_c": "333"}},
        profiles={"123": {"222": {"display_name": "測試乙"}, "333": {"display_name": "測試丙"}}},
    )
    identity = IdentityDirectory(
        {"discord:111": "actor_a", "discord:222": "actor_b"}, {"actor_a": "owner", "actor_b": "ownerb"}
    )
    engine = engine_cls("scope-g123", tmp_path)
    if hasattr(engine, "shared_items"):
        engine.shared_items = [
            FakeSharedItem(owner="222", text="喜歡貓", id="p_cat"),
            FakeSharedItem(owner="333", text="喜歡狗", id="p_dog"),
            FakeSharedItem(owner="333", text="說乙很愛貓", id="p_about", about="222"),
        ]
    backend = _Backend()
    access = MemoryAccessLayer(
        _Runtime({"scope-g123": engine, "ns-owner": engine}), identity, tool_backend=backend, guild_membership=roster
    )
    return access, engine, backend, roster


def test_member_recall_schema_matches_es_schema():
    """與 ES 的 MEMBER_RECALL_SCHEMA 對照（防漂移）；Core 端程式碼不 import ES，只有測試對照。"""
    from echo_stream.adapters.memory_echo import MEMBER_RECALL_SCHEMA as ES_SCHEMA

    from uep.memory.access import MEMBER_RECALL_SCHEMA

    assert dict(MEMBER_RECALL_SCHEMA) == ES_SCHEMA


def test_d79_access_layer_member_recall_filters_by_owner_and_never_leaks_uid(tmp_path):
    access, engine, backend, _roster = _member_rig(tmp_path)
    assert [d["function"]["name"] for d in access.tool_definitions()] == ["deep_recall", "member_recall"]
    ctx = {"guild_id": "123", "member_uid": "222", "present_uids": ["111", "222", "999"]}
    ok = access.execute_tool("member_recall", {"query": "近況"}, actor_id="actor_a",
                             audience="scope_group_g123", context=ctx)
    assert ok["success"] is True
    text = ok["result"]
    assert "喜歡貓" in text and "說乙很愛貓" in text  # 本人段加旁證段
    assert "喜歡狗" not in text  # 其他成員自己的投影不回
    assert "測試乙" in text and "測試丙" in text
    assert not any(uid in text for uid in ("111", "222", "333", "999"))  # 原始 uid 不進 prompt
    assert ok["shared_ids"] == ["p_cat", "p_about"]
    call = engine.shared_calls[-1]
    assert call["owner_uid"] == "222" and call["viewer_uid"] == "111" and call["scope_id"] == "guild:123"
    assert call["present_uids"] == ("111", "222")  # 在場名單裁成成員子集
    assert backend.calls == []  # 不走通用 tool backend
    # viewer 等於 owner：engine 回空
    own = access.execute_tool("member_recall", {}, actor_id="actor_b", audience="scope_group_g123",
                              context={**ctx, "member_uid": "222"})
    assert own["success"] is True and "沒有可分享的內容" in own["result"] and "shared_ids" not in own


def test_d79_member_recall_fail_closed_cases_do_not_touch_engine(tmp_path):
    access, engine, _backend, roster = _member_rig(tmp_path)
    ctx = {"guild_id": "123", "member_uid": "222", "present_uids": []}
    before = len(engine.shared_calls)
    for kwargs in (
        {"actor_id": "actor_a", "audience": "scope_public", "context": ctx},  # audience 看不到共享層
        {"actor_id": "actor_zzz", "audience": "scope_group_g123", "context": ctx},  # 訪客
        {"actor_id": "actor_a", "audience": "scope_group_g123", "context": {**ctx, "member_uid": "999"}},  # 目標非成員
        {"actor_id": "actor_a", "audience": "scope_group_g123", "context": None},  # 沒有 Core 寫入的範圍
    ):
        out = access.execute_tool("member_recall", {"query": "q"}, **kwargs)
        assert out["success"] is False and out["code"] == "denied"
    assert len(engine.shared_calls) == before
    identity = IdentityDirectory({"discord:111": "actor_a"}, {"actor_a": "owner"})
    bare = MemoryAccessLayer(_Runtime({"scope-g123": engine}), identity, tool_backend=_Backend())
    assert [d["function"]["name"] for d in bare.tool_definitions()] == ["deep_recall"]  # 沒有成員資格 Port 不曝光
    assert bare.execute_tool("member_recall", {}, actor_id="actor_a", audience="scope_group_g123",
                             context=ctx)["code"] == "denied"
    assert roster.is_member("123", "actor_a")


def test_member_recall_engine_without_owner_uid_returns_empty_not_all_members(tmp_path):
    class OldEngine(FakeMemoryEngine):
        def retrieve_shared(self, query, *, scope_id, viewer_uid, present_uids=(), top_k=None, min_similarity=None):
            self.shared_calls.append({"query": query})
            return super().retrieve_shared(
                query, scope_id=scope_id, viewer_uid=viewer_uid, present_uids=present_uids
            )

    access, engine, _backend, _roster = _member_rig(tmp_path, engine_cls=OldEngine)
    out = access.execute_tool(
        "member_recall", {"query": "q"}, actor_id="actor_a", audience="scope_group_g123",
        context={"guild_id": "123", "member_uid": "222", "present_uids": []},
    )
    assert out["success"] is True and "沒有可分享的內容" in out["result"]
    assert engine.shared_calls == []  # 根本沒有呼叫（不會退化成全體查詢撈出別人的投影）
    assert "喜歡" not in out["result"]


def test_auto_injected_shared_summary_uses_display_names_not_raw_uids(tmp_path):
    access, _engine, _backend, _roster = _member_rig(tmp_path)
    out = access.retrieve("scope_group_g123", "近況", "scope_group_g123", actor_id="actor_a")
    assert "喜歡狗" in out.text and "測試丙" in out.text
    assert "333" not in out.text and "222" not in out.text


def test_deep_recall_args_are_filtered_by_declared_schema(tmp_path):
    access, _engine, backend, _roster = _member_rig(tmp_path)
    out = access.execute_tool(
        "deep_recall", {"query": "q", "namespace": "scope-g123", "session_id": "evil"}, actor_id="actor_a",
        audience="scope_private_owner",
    )
    assert out["success"] is True
    assert backend.calls == [("deep_recall", {"query": "q"})]  # 只剩 schema 宣告的參數


def test_executor_rechecks_audience_and_refetches_present_uids_at_call_time():
    roster = GuildRoster({"123": {"actor_a": "111", "actor_b": "222"}}, profiles={"123": {"222": {"display_name": "乙"}}})
    port = ToolPort({"member_recall": {"success": True, "result": "投影"}})
    allowed = {"ok": True}
    executor = CoreToolExecutor(port, roster, audience_ok=lambda a: allowed["ok"])
    ctx = {"actor_id": "actor_a", "audience": "scope_group_g123", "segment_id": "seg_x", "ticket_ref": "turn_x",
           "guild_id": "123", "present_uids": ["999"]}  # 建立 context 時的舊快照
    roster.join("123", "111")
    roster.join("123", "222")
    assert asyncio.run(executor("member_recall", {"member": "乙"}, context=dict(ctx)))["success"] is True
    assert port.calls[-1]["context"]["present_uids"] == ["111", "222"]  # 呼叫當下重取，不信快照
    allowed["ok"] = False  # Gate 的 audience 已收窄
    n = len(port.calls)
    assert asyncio.run(executor("member_recall", {"member": "乙"}, context=dict(ctx)))["success"] is False
    assert len(port.calls) == n


def test_metrics_counter_is_thread_safe():
    m = Metrics()
    threads = [threading.Thread(target=lambda: [m.inc("k") for _ in range(2000)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert m["k"] == 16000


# ---- D87 前置：GuildRoster.roster ----

def test_roster_lists_uid_display_name_and_aliases_from_adapter():
    roster = GuildRoster()
    roster.set_members(
        "123", {"actor_a": "111", "actor_b": "222", "actor_c": "333"},
        {"222": {"display_name": "測試乙", "aliases": ["小乙", "乙"]}, "111": {"display_name": "測試甲"},
         "888": {"display_name": "非成員"}},
    )
    entries = roster.roster("123")
    assert [(e.uid, e.display_name, e.aliases) for e in entries] == [
        ("111", "測試甲", ()), ("222", "測試乙", ("小乙", "乙")), ("333", "", ()),  # 沒有檔案的成員仍在名單內
    ]
    assert roster.is_member("123", "actor_c") and not roster.is_member("123", "actor_zzz")
    assert roster.resolve_member("123", "小乙") == "222" and roster.resolve_member("123", "非成員") is None
    assert roster.resolve_member("123", "") is None and roster.roster("nope") == []
    roster.set_members("123", {"actor_a": "111"})  # 整批取代：別名一併清掉
    assert [(e.uid, e.aliases) for e in roster.roster("123")] == [("111", ())]


# ---- D5 補測：各條撤回路徑 ----

class FlakyRetractThink(FakeThinkStage):
    """第一次 retract 拋例外（ES 的 cancel_turn 會回報 history_retract_error），之後正常。"""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.retract_calls = 0

    async def retract(self, turn_id):
        self.retract_calls += 1
        if self.retract_calls == 1:
            raise RuntimeError("撤回失敗")
        return await super().retract(turn_id)


def _wait_generated(r, turn_id):
    r.wait_for(lambda: bool(r.events(turn_id, ("turn.generation_completed",))), what="生成完成")


def test_d5_cancel_claimed_but_history_not_retracted_is_retried_by_host_and_counted(rig):
    from echo_stream.core.tracer import LatencyTracer

    speak = GatedSpeak()
    think = FlakyRetractThink(
        responses=[LONG3], first_token_delay_s=0.0, token_interval_s=0.0, memory_delay_s=0.0, tracer=LatencyTracer()
    )
    r = rig(
        realtime=True, speak=speak, think=think, sink_factory=counting_factory(),
        host_kwargs={"output": EchoOutputConfig(commit_mode="eager")},
    )
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    _wait_generated(r, turn_id)
    assert history_assistant(think) == [LONG3]
    r.core.stop.engage(reason="test")
    speak.release.set()
    r.wait_terminal(turn_id)
    r.wait_for(lambda: r.host.metrics["echo.history_retracted"] >= 1, what="宿主補撤成功")
    assert r.host.metrics["echo.history_retract_error"] == 1  # ES 回報的撤回失敗有記 metric
    assert think.retract_calls >= 2 and not think.history  # 歷史最終乾淨（含使用者話語）


def test_d5_global_stop_in_settled_mode_leaves_history_clean(rig):
    speak = GatedSpeak()
    r = rig(realtime=True, speak=speak, sink_factory=counting_factory(), responses=(LONG3,))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    _wait_generated(r, turn_id)
    r.core.stop.engage(reason="test")
    speak.release.set()
    r.wait_terminal(turn_id)
    cancelled = r.events(turn_id, ("turn.cancelled",))[0]
    assert cancelled.payload["reason_code"] in {"global_stop", "presence_lost"}
    assert not r.think.history and r.think.committed == []  # settled：不 commit，歷史沒有這一輪


def test_d5_queued_turn_is_cancelled_by_global_stop_without_running(rig):
    speak = GatedSpeak()
    r = rig(realtime=True, speak=speak, sink_factory=counting_factory(), responses=(LONG3,))
    first = r.say("甲", speaker=SPEAKER).ticket
    second = r.say("乙", speaker="discord:222").ticket
    r.wait_for(lambda: r.think.stream_calls == 1, what="首輪開始")
    r.core.stop.engage(reason="test")
    speak.release.set()
    r.wait_terminal(first.turn_id)
    r.wait_terminal(second.turn_id)
    # 隊首已在出聲：全域停止撤銷 Lease 的同時，它的下一句可能先被 Gate 或第二次接納拒絕（先登記者為準）
    head = r.events(first.turn_id, ("turn.cancelled",))[0].payload["reason_code"]
    assert head in {"global_stop", "presence_lost", "dispatch_refused", "admission_rejected"}
    # 排隊中的輪從沒出聲，原因只可能是停止／撤銷
    assert r.events(second.turn_id, ("turn.cancelled",))[0].payload["reason_code"] in {"global_stop", "presence_lost"}
    assert r.think.stream_calls == 1  # 排隊中的那輪沒有白跑生成
    assert not r.think.history


def test_d5_rejected_turn_history_is_cleared(rig):
    speak = GatedSpeak()
    r = rig(
        realtime=True, speak=speak, sink_factory=counting_factory(), responses=(LONG3,),
        host_kwargs={"output": EchoOutputConfig(commit_mode="eager")},
    )
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: any(0 in w.delivery_ids for w in r.host.wrappers_of(turn_id)), what="首句 dispatch")
    _wait_generated(r, turn_id)
    assert history_assistant(r.think) == [LONG3]
    r.host.generations.bump(turn_id)  # 舊 generation：下一個 chunk 被拒
    speak.release.set()
    r.wait_terminal(turn_id)
    assert r.events(turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "admission_rejected"
    r.wait_for(lambda: not r.think.history, what="被拒輪的歷史整輪撤回")


@pytest.mark.parametrize("how", ["revoke", "global_stop"])
def test_stopped_or_revoked_turn_does_not_store_memory(rig, how):
    speak = GatedSpeak()
    port = ToolPort()
    r = rig(realtime=True, speak=speak, sink_factory=counting_factory(), responses=(LONG3,), memory=port)
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: bool(r.host.wrappers_of(turn_id)), what="開始出聲")
    if how == "revoke":
        r.core.presence.revoke("test_revoke")
    else:
        r.core.stop.engage(reason="test")
    speak.release.set()
    r.wait_terminal(turn_id)
    assert r.events(turn_id, ("turn.cancelled",))
    assert port.stored == []  # 與歷史撤回一致：不寫記憶


def test_cancel_registered_during_submit_is_applied_after_submit(rig):
    r = rig(realtime=True, responses=(LONG3,))
    coordinator = r.host.coordinator
    original = coordinator.submit

    def submit_then_cancel_race(request):
        # 模擬 submit 的空窗裡 Core 已登記取消（端點通知找不到這一輪）
        r.host.orchestrator.cancel_turn(request.ticket.turn_id, "presence_lost")
        return original(request)

    coordinator.submit = submit_then_cancel_race  # type: ignore[method-assign]
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_terminal(turn_id)
    assert r.events(turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "presence_lost"
    assert "turn.generation_completed" not in r.kinds(turn_id)  # 不白跑生成


def test_dispatch_failure_cancels_directly_on_worker_thread(rig):
    speak = GatedSpeak()
    r = rig(realtime=True, speak=speak, sink_factory=counting_factory(), responses=(LONG3,))
    threads: list[str] = []
    original = r.host._cancel_in_es

    def spy(turn_id, reason):
        threads.append(threading.current_thread().name)
        return original(turn_id, reason)

    r.host._cancel_in_es = spy  # type: ignore[method-assign]

    def boom(intent):
        raise RuntimeError("db down")

    r.host._services.delivery.dispatch = boom  # type: ignore[method-assign]
    result = r.say("講點什麼")
    r.wait_for(lambda: bool(threads), what="取消")
    speak.release.set()
    r.wait_terminal(result.ticket.turn_id)
    assert threads[0] == "uep-echo-worker"  # 工作緒上直接取消，只有 retract 排回 loop
    assert not hasattr(r.host, "_refuse_turn_threadsafe")


def test_regen_attempt_clears_member_recall_ids(rig):
    speak = GatedSpeak()
    r = rig(realtime=True, speak=speak, sink_factory=counting_factory(), responses=(LONG3,))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: bool(r.host.wrappers_of(turn_id)), what="開始出聲")

    async def regen():
        runtime = r.host._live[turn_id]
        runtime.tool_context["member_recall_ids"] = ["p1"]
        await r.host._on_think_start(runtime.ticket, None, 2)
        return dict(runtime.tool_context)

    ctx = r.run_on_es(regen())
    assert "member_recall_ids" not in ctx
    speak.release.set()
    r.wait_terminal(turn_id, timeout=30.0)
