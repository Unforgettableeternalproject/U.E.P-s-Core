"""P3 S3：EchoPipelineHost 與 Turn Orchestrator 的整合行為（需 PostgreSQL 與已安裝的 echo_stream）。

涵蓋組裝順序（echo 在 memory 之後啟動、之前停止）、批 B 繞道（B1 loop 綁定、B2 關閉後 submit）、
回呼不阻塞 ES loop、attempt／PASS／passive／FAILED／取消的事件對映、Core 否決權、audience 切換、
GuildMembership 接線、記憶 store 與 ContextSnapshot 登記。
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

import pytest

pytest.importorskip("echo_stream")

from echo_stream.adapters.think_llm import EchoBackend, LLMThinkStage
from echo_stream.contracts.types import Utterance
from echo_stream.fakes.stages import FakeThinkStage
from sqlalchemy import select

from tests.uep.echo_support import (
    PRIVATE,
    SPEAKER,
    WAIT,
    build_rig,
    fast_think,
    make_session,
)
from tests.uep.test_p3_s2_memory_host import OWNER_NS, SCOPE_NS
from tests.uep.test_p3_s2_memory_host import make_host as make_memory_host
from tests.uep.test_p3_s3_units import SpyPort
from uep.assembly import AssemblyError, assemble_core
from uep.context.models import ContextSnapshotRow
from uep.contracts.presence import PresenceStatus
from uep.echo.host import EchoPipelineHost, SessionBusyError
from uep.echo.memory import GuildRoster
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.state.coordinator import Axis
from uep.state.models import StateRequestRow
from uep.turn.models import Proposal, ReplyMode, TurnEndFacts, UtteranceFact

pytestmark = pytest.mark.usefixtures("session_factory")

LONG = "第一句話很長很長很長很長很長。第二句話也很長很長很長很長。第三句。"


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


class FixedProposer:
    def __init__(self, mode: ReplyMode, reason: str = "fixed") -> None:
        self.mode, self.reason, self.calls = mode, reason, 0

    def propose(self, candidate):
        self.calls += 1
        return Proposal(reply_mode=self.mode, reason_code=self.reason)


class BoomProposer:
    def propose(self, candidate):
        raise RuntimeError("proposer 壞了")


class FailingThink(FakeThinkStage):
    async def stream(self, utterance, token):
        self.stream_calls += 1
        raise RuntimeError("LLM 壞了")
        yield  # pragma: no cover


class RecPort(SpyPort):
    def __init__(self) -> None:
        super().__init__()
        self.stored: list[dict] = []
        self.sessions: list[str | None] = []

    def store(self, **kw):
        self.stored.append(kw)

    def set_session(self, session_id):
        self.sessions.append(session_id)

    def profile(self, *, actor_id, audience, max_chars=800):
        return f"profile:{actor_id}:{audience}"


# ---- 組裝順序 ----

def test_assembly_echo_after_memory_before_relay_and_stops_before_memory(session_factory, tmp_path):
    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    mem, factory = make_memory_host(tmp_path, publisher=publisher)
    order: list[str] = []
    r = build_rig(
        session_factory, tmp_path, bus=bus, publisher=publisher, memory_host=mem,
        assemble_over={"relay_starters": (lambda: None,)},
    )
    try:
        log = r.core.start_log
        idx = [log.index(x) for x in ("presence.start", "memory.start", "echo.start", "relay.start")]
        assert idx == sorted(idx) and r.host.started and r.host.presence_active
        orig_echo, orig_mem = r.host.stop, mem.stop
        r.host.stop = lambda: (order.append("echo"), orig_echo())[1]  # type: ignore[method-assign]
        mem.stop = lambda: (order.append("memory"), orig_mem())[1]  # type: ignore[method-assign]
    finally:
        r.core.shutdown()
    assert order == ["echo", "memory"]
    assert r.core.start_log.index("echo.stop") < r.core.start_log.index("memory.stop")
    assert not r.host.started and "stop_dream_scheduler" in factory.calls(OWNER_NS)


def test_echo_stop_failure_still_stops_memory(session_factory, tmp_path):
    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    mem, factory = make_memory_host(tmp_path, publisher=publisher)
    r = build_rig(session_factory, tmp_path, bus=bus, publisher=publisher, memory_host=mem)
    orig = r.host.stop

    def boom():
        raise RuntimeError("ES 停止失敗")

    r.host.stop = boom  # type: ignore[method-assign]
    try:
        r.core.shutdown()  # 不拋例外
        assert "stop_dream_scheduler" in factory.calls(OWNER_NS)
    finally:
        orig()


def test_assembly_refuses_mismatched_echo_wiring(session_factory, tmp_path):
    from tests.uep.test_assembly import kwargs as assembly_kwargs

    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)

    def host(**over):
        return EchoPipelineHost(
            publisher=over.pop("publisher", publisher), think=fast_think(None), speak=None,
            sink_factory=lambda v, t: "x", session=make_session(), **over,
        )

    def assemble(echo, **over):
        base = assembly_kwargs(session_factory, tmp_path, bus=bus, publisher=publisher, echo=echo)
        base.update(over)
        return assemble_core(session_factory, **base)

    other = EventPublisher(session_factory, EventBus())
    with pytest.raises(AssemblyError, match="publisher"):
        assemble(host(publisher=other))
    good = host()
    with pytest.raises(AssemblyError, match="adapter"):  # 預設 kwargs 是 FakeOutputAdapter
        assemble(good)
    with pytest.raises(AssemblyError, match="turn_generation_source"):
        assemble(good, adapter=good.adapter)  # 預設 ALLOW_ALL：舊 generation 不會被 Gate 拒絕


# ---- 批 B 繞道 ----

def test_b1_loop_is_bound_before_first_submit(rig):
    r = rig()
    coordinator = r.host.coordinator
    assert coordinator._loop is r.host._es.loop
    coordinator.cancel_all("no_live_turns")  # 非 loop 執行緒、首輪之前：不拋例外
    r.voice()  # 首輪之前的有聲事件：不拋例外
    result = r.say()
    assert r.wait_terminal(result.ticket.turn_id)[-1] == "turn.completed"


def test_b2_submit_after_stop_is_refused_by_core(session_factory, tmp_path):
    r = build_rig(session_factory, tmp_path)
    r.core.shutdown()
    before = len(r.events())
    result = r.say("關閉後才來")
    assert result.ticket is None and result.reason_code == "closed"
    assert len(r.events()) == before and r.think.stream_calls == 0


def test_submit_after_coordinator_aclose_is_voided_by_es_as_shutdown(rig):
    r = rig()
    intake = r.host.orchestrator.on_utterance(r.host.session, UtteranceFact(SPEAKER, "晚了一步", seq=500))
    ticket = intake.ticket
    r.run_on_es(r.host.coordinator.aclose())  # ES 正式語意：aclose 之後的 submit 回 VOIDED(shutdown)，不碰 think／sink
    r.host._es.call_soon(r.host._start_turn, ticket, UtteranceFact(SPEAKER, "晚了一步", seq=500))
    kinds = r.wait_terminal(ticket.turn_id)
    assert kinds == ["turn.started", "turn.cancelled"] and r.think.stream_calls == 0 and r.inner_sinks == []
    cancelled = r.events(ticket.turn_id, ("turn.cancelled",))[0]
    assert cancelled.payload["reason_code"] == "shutdown"
    assert r.host.generations.get(ticket.turn_id) == ticket.turn_generation  # shutdown 不前進 generation


def test_generation_completed_only_when_es_reports_ok(rig):
    r = rig()
    ticket = r.host.orchestrator.on_utterance(r.host.session, UtteranceFact(SPEAKER, "gen", seq=510)).ticket
    r.host._on_generation_done(ticket, None, False)
    r.host._worker.drain(WAIT)
    assert r.events(ticket.turn_id, ("turn.generation_completed",)) == []
    r.host._on_generation_done(ticket, None, True)
    r.host._on_generation_done(ticket, None, True)
    r.host._worker.drain(WAIT)
    assert len(r.events(ticket.turn_id, ("turn.generation_completed",))) == 1
    r.host.orchestrator.on_turn_ended(ticket.turn_id, TurnEndFacts("cancelled", "shutdown"))


# ---- 回呼不阻塞 ES loop ----

def test_callbacks_only_enqueue_and_db_work_runs_on_worker_thread(rig):
    r = rig()
    es_thread = r.host._es.thread_id
    seen: dict[str, object] = {}
    real_end = r.host.orchestrator.on_turn_ended

    def slow_end(turn_id, facts):
        seen["thread"] = threading.current_thread().name
        time.sleep(0.15)  # 模擬阻塞的 DB／Gateway 寫入
        return real_end(turn_id, facts)

    r.host.orchestrator.on_turn_ended = slow_end  # type: ignore[method-assign]
    durations: list[float] = []
    original = r.host.coordinator._on_turn_ended

    def timed(outcome):
        started = time.perf_counter()
        original(outcome)
        durations.append(time.perf_counter() - started)
        seen["callback_thread"] = threading.get_ident()

    r.host.coordinator._on_turn_ended = timed
    result = r.say()
    r.wait_terminal(result.ticket.turn_id)
    assert seen["callback_thread"] == es_thread and seen["thread"] == "uep-echo-worker"
    assert durations and max(durations) < 0.05  # 回呼本身 < 阻塞時間：Core 沒有在回呼內直接寫 DB


def test_submit_utterance_refuses_to_run_on_es_loop(rig):
    r = rig()

    async def call():
        return r.host.submit_utterance(UtteranceFact(SPEAKER, "不行", seq=900))

    with pytest.raises(Exception, match="ES loop"):
        r.run_on_es(call())


# ---- attempt／事件對映 ----

def test_attempt_started_only_from_attempt_two_and_generation_unchanged(rig):
    r = rig()
    ticket = r.host.orchestrator.on_utterance(r.host.session, UtteranceFact(SPEAKER, "regen", seq=600)).ticket
    gen = r.host.generations.get(ticket.turn_id)
    r.run_on_es(r.host._on_think_start(ticket, None, 1))
    r.run_on_es(r.host._on_think_start(ticket, None, 2))
    r.host._worker.drain(WAIT)
    events = r.events(ticket.turn_id, ("turn.attempt_started",))
    assert len(events) == 1 and events[0].payload == {"attempt_id": "att_2", "reason_code": "regen"}
    assert r.host.generations.get(ticket.turn_id) == gen  # regen 不增加 turn_generation
    r.host.orchestrator.on_turn_ended(ticket.turn_id, TurnEndFacts("cancelled", "shutdown"))


def test_passive_reply_mode_runs_llm_once_but_stays_silent(rig):
    r = rig(host_kwargs={"proposer": FixedProposer(ReplyMode.PASSIVE)})
    result = r.say()
    kinds = [k for k in r.wait_terminal(result.ticket.turn_id) if k.startswith("turn.")]
    assert kinds == ["turn.started", "turn.generation_completed", "turn.completed"]
    done = r.events(result.ticket.turn_id, ("turn.completed",))[0]
    assert done.payload == {"status": "skipped", "reason_code": "mode_silent"}
    assert r.think.stream_calls == 1 and r.inner_sinks == [] and r.deliveries() == []  # 沒有 sink、沒有 intent


def test_pass_proposal_writes_turn_skipped_and_never_calls_llm(rig):
    proposer = FixedProposer(ReplyMode.PASS, "not_addressed")
    r = rig(host_kwargs={"proposer": proposer})
    result = r.say()
    assert result.ticket is None and result.reason_code == "not_addressed"
    skipped = r.events(types=("turn.skipped",))
    assert [e.payload["reason_code"] for e in skipped] == ["not_addressed"]
    assert skipped[0].payload["reply_mode"] == "pass" and skipped[0].turn_id is None
    assert r.think.stream_calls == 0 and not r.events(types=("turn.started",))


def test_proposer_exception_falls_back_to_respond(rig):
    r = rig(host_kwargs={"proposer": BoomProposer()})
    result = r.say()
    assert result.ticket is not None and result.proposal.reason_code == "proposer_error"
    assert r.wait_terminal(result.ticket.turn_id)[-1] == "turn.completed"


def test_duplicate_utterance_is_idempotent(rig):
    r = rig()
    first = r.say("同一句", seq=7)
    second = r.say("同一句", seq=7)
    assert first.ticket is not None and second.ticket is None and second.duplicate
    r.wait_terminal(first.ticket.turn_id)
    assert len(r.events(types=("input.utterance_final",))) == 1 and len(r.events(types=("turn.started",))) == 1
    assert r.think.stream_calls == 1


def test_think_failure_maps_to_upstream_error_cancel_and_bumps_generation(rig):
    r = rig(think=FailingThink(memory_delay_s=0.0, first_token_delay_s=0.0))
    result = r.say()
    kinds = [k for k in r.wait_terminal(result.ticket.turn_id) if k.startswith("turn.")]
    assert kinds == ["turn.started", "turn.cancelled"]  # 生成沒成功：沒有 generation_completed
    cancelled = r.events(result.ticket.turn_id, ("turn.cancelled",))[0]
    assert cancelled.payload["reason_code"] == "upstream_error"  # TODO(批 B C2)：ES 補碼前由 Core 對映
    assert cancelled.fence["turn_generation"] > result.ticket.turn_generation


# ---- Orchestrator 對映（ES 各種結束路徑，exactly once）----

@pytest.mark.parametrize(("facts", "kind", "payload", "bumped"), [
    (TurnEndFacts("completed", None, True), "turn.completed", {"status": "completed"}, False),
    (TurnEndFacts("skipped", "pass", True), "turn.completed", {"status": "skipped", "reason_code": "pass"}, False),
    (TurnEndFacts("dropped_busy", "dropped_busy"), "turn.skipped",
     {"reason_code": "dropped_busy", "reply_mode": "respond"}, False),
    (TurnEndFacts("cancelled", "barge_in", True), "turn.cancelled", {"reason_code": "barge_in"}, True),
    (TurnEndFacts("cancelled", "superseded"), "turn.cancelled", {"reason_code": "superseded"}, True),
    (TurnEndFacts("voided", "yielded"), "turn.cancelled", {"reason_code": "yielded"}, True),
    (TurnEndFacts("cancelled", "timeout"), "turn.cancelled", {"reason_code": "timeout"}, True),
    (TurnEndFacts("failed", "upstream_error"), "turn.cancelled", {"reason_code": "upstream_error"}, True),
    (TurnEndFacts("cancelled", "shutdown"), "turn.cancelled", {"reason_code": "shutdown"}, False),
])
def test_orchestrator_terminal_mapping_exactly_once(rig, facts, kind, payload, bumped):
    r = rig()
    orch = r.host.orchestrator
    ticket = orch.on_utterance(r.host.session, UtteranceFact(SPEAKER, "對映", seq=701)).ticket
    assert orch.on_turn_ended(ticket.turn_id, facts) == kind
    assert orch.on_turn_ended(ticket.turn_id, facts) is None  # 每個 turn 恰好一個終結事件
    terminal = [e for e in r.events(ticket.turn_id) if e.event_type in ("turn.completed", "turn.cancelled", "turn.skipped")]
    assert [e.event_type for e in terminal] == [kind] and terminal[0].payload == payload
    now = r.host.generations.get(ticket.turn_id)
    assert (now > ticket.turn_generation) is bumped
    assert "turn.generation_completed" not in r.kinds(ticket.turn_id)  # 只由 ES 的 on_generation_done 觸發


def test_core_initiated_cancel_reason_wins_over_es_reason_only_when_es_was_cancelled(rig):
    r = rig()
    orch = r.host.orchestrator
    t1 = orch.on_utterance(r.host.session, UtteranceFact(SPEAKER, "a", seq=801)).ticket
    assert orch.cancel_turn(t1.turn_id, "global_stop") and not orch.cancel_turn(t1.turn_id, "global_stop")
    bumped = r.host.generations.get(t1.turn_id)
    assert bumped > t1.turn_generation
    orch.on_turn_ended(t1.turn_id, TurnEndFacts("cancelled", "shutdown"))  # ES 只能標 shutdown
    assert r.events(t1.turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "global_stop"
    assert r.host.generations.get(t1.turn_id) == bumped  # 已前進一次，不重複前進
    t2 = orch.on_utterance(r.host.session, UtteranceFact(SPEAKER, "b", seq=802)).ticket
    orch.cancel_turn(t2.turn_id, "presence_lost")
    orch.on_turn_ended(t2.turn_id, TurnEndFacts("completed", None, True))  # 取消來得太晚：輪已完成就不覆寫
    assert r.kinds(t2.turn_id)[-1] == "turn.completed"


# ---- Presence 與輸入 ----

@dataclass
class StubInput:
    name: str = "stub"
    on_voice_event: object = None

    def __post_init__(self) -> None:
        self.fed: list[str] = []
        self.queue: asyncio.Queue | None = None

    async def prepare(self) -> None: ...
    async def aclose(self) -> None: ...

    async def open(self, token):
        self.queue = asyncio.Queue()
        while True:
            item = await self.queue.get()
            yield item

    def feed(self, speaker_id, pcm, ts=None) -> None:
        self.fed.append(speaker_id)


def test_feed_is_dropped_before_input_stage_when_presence_inactive(rig):
    stub = StubInput()
    r = rig(host_kwargs={"input_stage": stub})
    assert r.host.feed(SPEAKER, b"\x00\x00") is True and stub.fed == [SPEAKER]
    calls = []
    r.host.coordinator.on_voice_event = calls.append  # type: ignore[method-assign]
    r.voice()
    assert len(calls) == 1
    r.core.presence.revoke("absent_test")
    r.wait_for(lambda: not r.host.presence_active, what="端點丟棄 Lease")
    assert r.host.feed(SPEAKER, b"\x00\x00") is False and stub.fed == [SPEAKER]  # 缺席：feed 之前丟棄（V18）
    assert r.host.metrics["echo.feed_dropped_absent"] == 1
    r.voice()
    assert len(calls) == 1  # 缺席時有聲事件也不交給 coordinator


def test_input_stage_utterance_flows_through_intake_pool(rig):
    stub = StubInput()
    r = rig(host_kwargs={"input_stage": stub})
    r.wait_for(lambda: stub.queue is not None, what="輸入 stage 已 open")
    utt = Utterance(text="口述的一句", speaker_id=SPEAKER)
    r.run_on_es(stub.queue.put(utt))
    r.wait_for(lambda: bool(r.events(types=("turn.started",))), what="turn.started")
    started = r.events(types=("turn.started",))[0]
    assert r.wait_terminal(started.turn_id)[-1] == "turn.completed"
    assert r.think.committed[0][0] == started.turn_id  # 不是輸入端的 uuid（A2）
    event = r.events(types=("input.utterance_final",))[0]
    assert event.idempotency_key.endswith(":1") and event.payload["speaker_ref"] == SPEAKER


# ---- 記憶、快照、Interaction 軸 ----

def test_think_retrieval_carries_actor_and_audience_from_core_not_speaker_ref(rig):
    port = RecPort()
    think = LLMThinkStage(EchoBackend(prefix="回："))
    r = rig(think=think, memory=port)
    assert think.memory_resolver is not None  # 轉接層把 per-scope resolver 掛上 Think
    result = r.say("你好。")
    r.wait_terminal(result.ticket.turn_id)
    r.wait_for(lambda: bool(port.calls), what="檢索被呼叫")
    kind, _query, audience, actor_id, kw = port.calls[0]
    assert (kind, audience, actor_id) == ("audience", PRIVATE, "actor_a")  # actor_id 取自身分映射，不是 discord:111
    assert kw["session_id"] == r.host.session.segment_id and kw["shared_turn"] is False
    assert port.sessions[0] == r.host.session.segment_id


def test_store_after_completed_turn_uses_turn_derived_idempotency_key(rig):
    port = RecPort()
    r = rig(memory=port)
    result = r.say("記住這句")
    r.wait_terminal(result.ticket.turn_id)
    r.wait_for(lambda: bool(port.stored), what="store")
    stored = port.stored[0]
    assert stored["actor_id"] == "actor_a" and stored["user_text"] == "記住這句" and stored["spoken_text"]
    assert stored["idempotency_key"] == f"turn:{result.ticket.turn_id}"
    assert stored["session_id"] == r.host.session.segment_id


def test_context_snapshot_registered_after_turn_with_sdk_store_owner(rig, session_factory):
    r = rig()
    result = r.say()
    r.wait_terminal(result.ticket.turn_id)
    with session_factory() as s:
        row = s.scalar(select(ContextSnapshotRow).where(ContextSnapshotRow.turn_id == result.ticket.turn_id))
    assert row is not None
    assert (row.purpose, row.history_owner, row.conversation_id) == (
        "conversation_turn", "sdk_store", r.host.session.conversation_id,
    )
    second = r.say("第二輪")
    r.wait_terminal(second.ticket.turn_id)
    with session_factory() as s:
        revisions = sorted(s.scalars(select(ContextSnapshotRow.context_revision)))
    assert revisions == [1, 2]  # 同 conversation／segment 的 revision 單調遞增


def test_interaction_axis_thinking_then_idle_via_core_turn_proposer(rig, session_factory):
    r = rig()
    result = r.say()
    r.wait_terminal(result.ticket.turn_id)
    with session_factory() as s:
        rows = list(s.scalars(select(StateRequestRow).where(StateRequestRow.proposer == "core.turn")
                              .order_by(StateRequestRow.decided_at)))
    assert [(x.to_state, x.decision) for x in rows] == [("THINKING", "ACCEPTED"), ("IDLE", "ACCEPTED")]
    assert r.core.state.current(Axis.INTERACTION).state == "IDLE"


# ---- shutdown 與 Lease ----

def test_shutdown_cancels_live_turn_without_generation_bump_and_keeps_lease(rig):
    r = rig(realtime=True, responses=(LONG,))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: r.inner_sinks and r.inner_sinks[0].chunks, what="首個 chunk")
    r.core.shutdown()
    kinds = r.kinds(turn_id)
    assert kinds[0] == "turn.started" and kinds[-1] == "turn.cancelled"  # stop 時事件已寫完（工作緒已排乾）
    assert r.events(turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "shutdown"
    assert r.host.generations.get(turn_id) == result.ticket.turn_generation
    assert r.core.presence.current().status is PresenceStatus.ACTIVE  # Core 關機不撤銷 Presence（T-11）


# ---- session／audience ----

def test_audience_switch_resets_think_then_profile_and_refuses_when_busy(rig):
    think = LLMThinkStage(EchoBackend(prefix="回："))
    r = rig(think=think, host_kwargs={"profile_provider": lambda ctx: f"profile:{ctx.audience_scope}"})
    result = r.say("你好。")
    r.wait_terminal(result.ticket.turn_id)
    assert think.history, "第一輪之後 Think 應有歷史"
    r.host.set_session(make_session(audience_scope="scope_public", conversation_id=r.host.session.conversation_id))
    assert think.history == [] and think.profile == "profile:scope_public"  # reset 後宿主重設 profile
    assert r.host.session.audience_scope == "scope_public"
    # 同 audience、同 conversation 的 session 更新不重置
    think.history.append({"role": "user", "content": "keep"})
    r.host.set_session(make_session(audience_scope="scope_public", conversation_id=r.host.session.conversation_id))
    assert think.history == [{"role": "user", "content": "keep"}]


def test_audience_switch_refused_while_turn_is_live(rig):
    think = LLMThinkStage(EchoBackend(prefix="回："))
    r = rig(think=think, realtime=True, host_kwargs={"profile_provider": lambda ctx: "p"})
    result = r.say("很長很長很長很長很長很長的一句話。第二句也很長很長很長很長。")
    r.wait_for(lambda: r.inner_sinks and r.inner_sinks[0].chunks, what="首個 chunk")
    with pytest.raises(SessionBusyError):
        r.host.set_session(make_session(audience_scope="scope_public"))
    assert r.host.session.audience_scope == PRIVATE
    r.wait_terminal(result.ticket.turn_id, timeout=30.0)


def test_participant_events_roster_snapshot_and_pending_memory_discard(rig):
    roster = GuildRoster({"123": {"actor_a": "111"}})
    think = LLMThinkStage(EchoBackend())
    session = make_session(audience_scope="scope_group_g123", guild_id="123", platform="discord")
    r = rig(think=think, session=session, host_kwargs={"roster": roster}, presence_audience=lambda: "scope_group_g123")
    v0 = r.host.policy.version
    r.host.participant_joined(SPEAKER)
    r.host.participant_joined("discord:999")  # 非成員
    assert roster.present_uids("123") == ["111"] and r.host.policy.version == v0 + 2  # 參與者變動推送新版快照
    assert think.seed_pending(SPEAKER, "待注入") is True and think.seed_pending(SPEAKER, "第二筆") is False
    r.host.participant_left(SPEAKER)
    r.wait_for(lambda: think.seed_pending(SPEAKER, "離場後") is True, what="離場丟棄待注入記憶")
    r.host._worker.drain(WAIT)
    events = r.events(types=("conversation.participant_joined", "conversation.participant_left"))
    assert [e.event_type for e in events] == [
        "conversation.participant_joined", "conversation.participant_joined", "conversation.participant_left",
    ]
    assert events[0].payload == {"speaker_ref": SPEAKER, "role": "speaker"}
    assert roster.present_uids("123") == []


def test_guild_roster_is_wired_into_access_layer_membership(session_factory, tmp_path):
    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    roster = GuildRoster({"123": {"actor_a": "111"}})  # actor_c 不是成員
    mem, factory = make_memory_host(tmp_path, publisher=publisher, guild_membership=roster)
    r = build_rig(
        session_factory, tmp_path, bus=bus, publisher=publisher, memory_host=mem, host_kwargs={"roster": roster},
        session=make_session(audience_scope="scope_group_g123", guild_id="123"),
    )
    try:
        group = "scope_group_g123"
        member = mem.access.retrieve(group, "q", group, actor_id="actor_a", present_uids=("111", "999"))
        assert member.excluded_summary == {}
        shared = factory.engines[SCOPE_NS].shared_calls
        assert shared and shared[0]["present_uids"] == ("111",)  # 在場名單被裁成成員子集
        outsider = mem.access.retrieve(group, "q", group, actor_id="actor_c")
        assert outsider.excluded_summary == {"not_member": 1}  # 成員資格由 session 的 guild 推導，非成員被擋
        assert len(shared) == 1
    finally:
        r.core.shutdown()


def test_each_turn_gets_its_own_tool_context_dict(rig):
    r = rig()
    seen = []
    original = r.host._build_request

    def spy(ticket, fact):
        req = original(ticket, fact)
        seen.append(req.utterance.metadata["tool_context"])
        return req

    r.host._build_request = spy  # type: ignore[method-assign]
    for i in range(2):
        r.wait_terminal(r.say(f"第{i}輪").ticket.turn_id)
    assert len(seen) == 2 and seen[0] is not seen[1]  # 不可跨輪共用（隱私）
