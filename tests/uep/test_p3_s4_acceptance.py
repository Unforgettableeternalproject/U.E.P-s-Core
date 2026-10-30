"""P3 S4 驗收：播放證據與插話讓位（P3接入方案 §5 S4 列、§5.3）。

V06（插話停播、Sink 不 drain、歷史只含已交付）、V08（生成完成不等於交付完成）、VC-4-5（控制訊號不被工作緒
積壓擋住）、VC-5-2（Discord 只到 transport_sent）、斷線與撤銷後舊輸出不復活；另含 D5（cancel_turn）、D79（工具白名單與
action 事件）、D82（spoken-only 開關）、D73（dispatch 預算）。以 ES 的 fakes 驅動，需 PostgreSQL 與已安裝的 echo_stream。
"""
from __future__ import annotations

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
from uep.echo.tools import CoreToolExecutor
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


def test_playback_progress_is_reported_while_turn_is_still_playing(rig):
    speak = GatedSpeak()
    factory = counting_factory()
    r = rig(realtime=True, speak=speak, sink_factory=factory, responses=(LONG3,))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: factory.sinks and factory.sinks[0].chunks, what="首個 chunk")
    sink = factory.sinks[0]
    first = sink.chunks[0].duration_s
    r.wait_for(lambda: sink.played_duration_s >= first, timeout=first + WAIT, what="首句播畢")
    speak.release.set()  # 第二句寫入 -> 觸發首句的即時證據回報
    r.wait_for(
        lambda: any(d.sequence == 0 and d.status == DeliveryStatus.RENDERED_PLAYED.value for d in r.deliveries()),
        what="首句在整輪結束前即升到 rendered_played",
    )
    assert "turn.completed" not in r.kinds(turn_id)
    r.wait_terminal(turn_id, timeout=30.0)


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
    assert r.host.metrics["echo.cancel_turn_claimed"] >= 1 and r.host.metrics["echo.cancel_voice_missed"] == 0
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
        self.calls: list[dict] = []
        self.outcomes = outcomes or {}

    def retrieve(self, scope, query, audience, **kw):
        raise AssertionError("不應被呼叫")

    def store(self, **kw):
        return None

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
    def namespace_for_owner(self, owner_id):
        return f"ns-{owner_id}"

    def namespace_for_guild(self, guild_id):
        return f"scope-g{guild_id}"

    def call(self, namespace_id, fn, *, timeout_s, on_late=None):
        return fn(object())


class _Backend:
    def __init__(self) -> None:
        self.calls: list = []

    def definitions(self):
        return [{"name": "member_recall"}, {"name": "deep_recall"}, {"name": "trigger_dream"}]

    def execute(self, engine, name, args, session_id):
        self.calls.append((name, dict(args)))
        return {"success": True, "result": "r", "shared_ids": ["p1"]}


def test_d79_access_layer_member_recall_authorization_is_fail_closed():
    roster = GuildRoster({"123": {"actor_a": "111", "actor_b": "222"}})
    identity = IdentityDirectory({"discord:111": "actor_a"}, {"actor_a": "owner"})
    backend = _Backend()
    access = MemoryAccessLayer(_Runtime(), identity, tool_backend=backend, guild_membership=roster)
    assert [d["name"] for d in access.tool_definitions()] == ["member_recall", "deep_recall"]
    ctx = {"guild_id": "123", "member_uid": "222", "present_uids": ["111", "222", "999"]}
    ok = access.execute_tool("member_recall", {"query": "q"}, actor_id="actor_a",
                             audience="scope_group_g123", context=ctx)
    assert ok["success"] is True
    name, args = backend.calls[-1]
    assert name == "member_recall" and args["scope_id"] == "guild:123"
    assert args["present_uids"] == ["111", "222"] and args["member_uid"] == "222"  # 在場名單裁成成員子集
    n = len(backend.calls)
    for kwargs in (
        {"actor_id": "actor_a", "audience": "scope_public", "context": ctx},  # audience 看不到共享層
        {"actor_id": "actor_zzz", "audience": "scope_group_g123", "context": ctx},  # 訪客
        {"actor_id": "actor_a", "audience": "scope_group_g123", "context": {**ctx, "member_uid": "999"}},  # 目標非成員
        {"actor_id": "actor_a", "audience": "scope_group_g123", "context": None},  # 沒有 Core 寫入的範圍
    ):
        out = access.execute_tool("member_recall", {"query": "q"}, **kwargs)
        assert out["success"] is False and out["code"] == "denied"
    assert len(backend.calls) == n  # 全部不碰 engine
    no_membership = MemoryAccessLayer(_Runtime(), identity, tool_backend=backend)
    assert no_membership.execute_tool(
        "member_recall", {}, actor_id="actor_a", audience="scope_group_g123", context=ctx
    )["code"] == "denied"


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
