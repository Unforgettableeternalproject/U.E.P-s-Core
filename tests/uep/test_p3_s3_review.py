"""P3 S3 審查修正的回歸測試：audience 切換競態、輸出被拒的處理、Orchestrator 寫入失敗回收、關閉窗口、
冪等鍵命名空間、輸入驗證與輸入 stage 失敗（需 PostgreSQL 與已安裝的 echo_stream）。"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

pytest.importorskip("echo_stream")

from echo_stream.adapters.think_llm import EchoBackend, LLMThinkStage
from echo_stream.fakes.audio import FakeAudioSink
from sqlalchemy import select

from tests.uep.echo_support import PRIVATE, SPEAKER, WAIT, build_rig, make_session
from tests.uep.test_p3_s3_host import LONG, RecPort, StubInput
from uep.context.models import ContextSnapshotRow
from uep.echo.host import EchoPipelineHost, SessionBusyError
from uep.echo.memory import GuildRoster
from uep.echo.worker import CoreWorker
from uep.state.coordinator import Axis
from uep.turn.generations import TurnGenerations
from uep.turn.models import TurnEndFacts, UtteranceFact
from uep.turn.orchestrator import TurnOrchestrator

pytestmark = pytest.mark.usefixtures("session_factory")


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


class SlowReset(LLMThinkStage):
    """reset_context 卡住，讓測試在「切換進行中」插入 submit。"""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.entered = threading.Event()
        self.release = threading.Event()

    def reset_context(self, history=None) -> None:
        self.entered.set()
        self.release.wait(WAIT)
        super().reset_context(history)


class FlakyPublisher:
    """包住真 publisher：指定 event_type 的前 n 次 publish 拋例外。"""

    def __init__(self, real, fail_types: dict[str, int]) -> None:
        self._real = real
        self.fail_types = dict(fail_types)

    def publish(self, draft, trusted, **kw):
        remaining = self.fail_types.get(draft.event_type, 0)
        if remaining:
            self.fail_types[draft.event_type] = remaining - 1
            raise RuntimeError("journal 寫入失敗")
        return self._real.publish(draft, trusted, **kw)


# ---- C1：audience 切換競態 ----

def test_submit_during_session_switch_is_refused_not_queued_and_new_ticket_uses_new_audience(rig):
    think = SlowReset(EchoBackend(prefix="回："))
    r = rig(think=think, presence_audience=lambda: "scope_public", host_kwargs={"profile_provider": lambda c: "p"})
    first = r.say("先講一句。")
    r.wait_terminal(first.ticket.turn_id)
    assert first.ticket.audience_scope == PRIVATE
    new_ctx = make_session(audience_scope="scope_public", session_ref="sessPublic")
    errors: list[BaseException] = []

    def switch():
        try:
            r.host.set_session(new_ctx)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=switch)
    t.start()
    assert think.entered.wait(WAIT)
    during = r.say("切換中的話")
    assert during.ticket is None and during.reason_code == "session_switching"  # 不排隊、不以舊 audience 簽
    with pytest.raises(SessionBusyError):
        r.host.set_session(make_session(audience_scope=PRIVATE))  # 第二個切換也被拒
    think.release.set()
    t.join(WAIT)
    assert not errors and r.host.session is new_ctx
    after = r.say("切換後的話")
    assert after.ticket is not None and after.ticket.audience_scope == "scope_public"  # 私人→公開後不再以私人簽發
    assert r.wait_terminal(after.ticket.turn_id)[-1] == "turn.completed"
    assert len(r.events(types=("turn.started",))) == 2  # 切換中那句沒有開 turn


def test_signed_but_not_started_ticket_counts_as_busy(rig):
    r = rig()
    ticket = r.host.orchestrator.on_utterance(r.host.session, UtteranceFact(SPEAKER, "已簽未啟動", seq=40)).ticket
    with pytest.raises(SessionBusyError):
        r.host.set_session(make_session(audience_scope="scope_public"))
    assert r.host.session.audience_scope == PRIVATE and not r.host._switching
    r.host.orchestrator.on_turn_ended(ticket.turn_id, TurnEndFacts("cancelled", "shutdown"))
    r.host.set_session(make_session(audience_scope="scope_public"))  # ticket 結束後才可切換


# ---- M1：本地 audience 檢查 ----

def test_audience_wider_than_presence_is_rejected_before_first_chunk(rig):
    # 簽發當下 audience 還被允許（否則 S5 的否決會在 propose 之前直接 pass）；簽發之後 Presence 收窄，
    # 由 sink 的本地第二次接納在第一個 chunk 之前攔下
    allowed = {"v": "scope_public"}
    r = rig(session=make_session(audience_scope="scope_public"), presence_audience=lambda: allowed["v"],
            first_token_delay_s=0.4)
    result = r.say("公開場合的話")
    allowed["v"] = PRIVATE
    turn_id = result.ticket.turn_id
    kinds = r.wait_terminal(turn_id)
    assert kinds[-1] == "turn.cancelled"
    assert r.events(turn_id, ("output.rejected",))[0].payload["reason_code"] == "REJECT_AUDIENCE"
    assert all(s.chunks == [] for s in r.inner_sinks)  # 第一個 chunk 寫出之前就被攔下
    assert r.intents(turn_id) == []


# ---- M2：dispatch 失敗 fail-closed ----

def test_dispatch_exception_silences_and_cancels_turn(rig):
    r = rig(realtime=True, responses=(LONG,))

    def boom(intent):
        raise RuntimeError("DB 掛了")

    r.core.delivery.dispatch = boom  # type: ignore[method-assign]
    result = r.say("講點什麼")
    kinds = r.wait_terminal(result.ticket.turn_id)
    assert kinds[-1] == "turn.cancelled"
    assert r.events(result.ticket.turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "dispatch_failed"
    assert r.host.metrics["echo.dispatch_errors"] >= 1
    assert r.host.generations.get(result.ticket.turn_id) > result.ticket.turn_generation


# ---- M6：被拒輪的終結事件 ----

def test_rejected_turn_ends_cancelled_without_snapshot_or_memory_store(rig, session_factory):
    port = RecPort()
    r = rig(realtime=True, responses=(LONG,), memory=port)
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    r.wait_for(lambda: r.inner_sinks and r.inner_sinks[0].chunks, what="首個 chunk")
    r.host.generations.bump(turn_id)  # 舊 generation：下一個 chunk 被拒
    kinds = r.wait_terminal(turn_id)
    assert kinds[-1] == "turn.cancelled"  # 不再是 completed(skipped)
    assert r.events(turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "admission_rejected"
    with session_factory() as s:
        assert s.scalar(select(ContextSnapshotRow).where(ContextSnapshotRow.turn_id == turn_id)) is None
    assert port.stored == []


def test_cancel_voice_miss_is_counted(rig):
    r = rig()
    result = r.say()
    r.wait_terminal(result.ticket.turn_id)
    r.run_on_es(_call(lambda: r.host._refuse_turn(result.ticket.turn_id, "admission_rejected")))
    assert r.host.metrics["echo.cancel_voice_missed"] == 1


async def _call(fn):
    return fn()


# ---- M3：Orchestrator 寫入失敗回收 ----

def test_terminal_write_retries_once_then_succeeds(rig):
    r = rig()
    orch = r.host.orchestrator
    ticket = orch.on_utterance(r.host.session, UtteranceFact(SPEAKER, "重試", seq=50)).ticket
    orch._publisher = FlakyPublisher(r.host.publisher, {"turn.completed": 1})
    assert orch.on_turn_ended(ticket.turn_id, TurnEndFacts("completed", None, True)) == "turn.completed"
    assert orch.metrics["turn.terminal_retry"] == 1 and orch.dead_letters == []
    assert r.kinds(ticket.turn_id)[-1] == "turn.completed"


def test_terminal_write_failure_dead_letters_and_still_recovers_state(rig):
    r = rig()
    orch = r.host.orchestrator
    ticket = orch.on_utterance(r.host.session, UtteranceFact(SPEAKER, "死信", seq=51)).ticket
    assert r.core.state.current(Axis.INTERACTION).state == "THINKING"
    orch._publisher = FlakyPublisher(r.host.publisher, {"turn.completed": 99})
    orch.on_turn_ended(ticket.turn_id, TurnEndFacts("completed", None, True))
    assert orch.metrics["turn.terminal_dead_letter"] == 1
    assert orch.dead_letters == [(ticket.turn_id, "turn.completed", {"status": "completed"})]
    assert orch.live_tickets() == []  # live 已回收
    assert ticket.turn_id not in r.host.generations._live
    assert r.core.state.current(Axis.INTERACTION).state == "IDLE"  # IDLE 請求仍送出


def test_turn_started_write_failure_rolls_back_live_and_generation(rig):
    r = rig()
    orch = r.host.orchestrator
    before = dict(r.host.generations._by_turn)
    orch._publisher = FlakyPublisher(r.host.publisher, {"turn.started": 1})
    with pytest.raises(RuntimeError):
        orch.on_utterance(r.host.session, UtteranceFact(SPEAKER, "寫不進去", seq=52))
    assert orch.live_tickets() == [] and dict(r.host.generations._by_turn) == before
    assert orch.metrics["turn.issue_failed"] == 1


# ---- M4：關閉窗口 ----

def test_worker_submit_returns_false_when_not_running_or_closing():
    worker = CoreWorker()
    assert worker.submit(lambda: None) is False  # 尚未啟動
    done = threading.Event()
    worker.start()
    assert worker.submit(done.set) is True and done.wait(WAIT)
    worker.stop()
    assert worker.submit(lambda: None) is False  # 關閉旗標之後不再收任務


def test_enqueue_runs_inline_after_worker_stopped(rig):
    r = rig()
    r.host._worker.stop()
    ran = []
    r.host._enqueue(lambda: ran.append(1), "inline")
    assert ran == [1]


def test_teardown_waits_for_in_flight_intake_before_closing(rig):
    r = rig()
    r.host._intake_lock.acquire()
    t = threading.Thread(target=lambda: r.host._teardown(quiet=True))
    t.start()
    time.sleep(0.3)
    assert not r.host.closed and t.is_alive()
    r.host._intake_lock.release()
    t.join(WAIT)
    assert r.host.closed and not t.is_alive()


def test_shutdown_sweeps_signed_but_unstarted_tickets(rig):
    r = rig()
    ticket = r.host.orchestrator.on_utterance(r.host.session, UtteranceFact(SPEAKER, "孤兒", seq=60)).ticket
    r.core.shutdown()
    kinds = r.kinds(ticket.turn_id)
    assert kinds == ["turn.started", "turn.cancelled"]
    assert r.events(ticket.turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "shutdown"


def test_start_failure_cleans_worker(session_factory, tmp_path):
    r = build_rig(session_factory, tmp_path, start=False)

    def boom():
        raise RuntimeError("loop 起不來")

    r.host._es.start = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="loop"):
        r.core.start()
    assert not r.host._worker.running and not r.host.started


# ---- M5：冪等鍵命名空間 ----

def test_idempotency_key_includes_run_id_so_restart_does_not_collide(rig):
    r = rig()
    auth = r.host.endpoint.lease_auth()
    ctx = make_session(session_ref="sessSame")
    made = []
    for run in ("runa", "runb", "runa"):
        orch = TurnOrchestrator(r.host.publisher, TurnGenerations(), auth_provider=lambda: auth, run_id=run)
        made.append(orch.on_utterance(ctx, UtteranceFact(SPEAKER, "同一個 seq", seq=1)))
    assert made[0].ticket is not None and made[1].ticket is not None  # 重啟（不同 run）不撞鍵
    assert made[2].duplicate and made[2].ticket is None  # 同一次執行內才是重送
    keys = sorted(e.idempotency_key for e in r.events(types=("input.utterance_final",)))
    assert keys == ["es:runa:sessSame:1", "es:runb:sessSame:1"]
    assert r.host.run_id and r.host.orchestrator.run_id == r.host.run_id


# ---- Minor ----

def test_es_reason_codes_are_kept_and_only_shutdown_echo_is_overridden(rig):
    r = rig()
    orch = r.host.orchestrator
    t1 = orch.on_utterance(r.host.session, UtteranceFact(SPEAKER, "a", seq=70)).ticket
    orch.cancel_turn(t1.turn_id, "global_stop")
    orch.on_turn_ended(t1.turn_id, TurnEndFacts("cancelled", "barge_in"))  # ES 自己判定的原因保留
    assert r.events(t1.turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "barge_in"
    t2 = orch.on_utterance(r.host.session, UtteranceFact(SPEAKER, "b", seq=71)).ticket
    orch.cancel_turn(t2.turn_id, "presence_lost")
    orch.on_turn_ended(t2.turn_id, TurnEndFacts("voided", "yielded"))
    assert r.events(t2.turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "yielded"
    t3 = orch.on_utterance(r.host.session, UtteranceFact(SPEAKER, "c", seq=72)).ticket
    orch.cancel_turn(t3.turn_id, "admission_rejected")
    orch.on_turn_ended(t3.turn_id, TurnEndFacts("skipped", "voice_cancelled", True))  # cancel_voice 的回音才覆寫
    assert r.events(t3.turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "admission_rejected"


def test_live_turns_are_never_pruned_from_generations():
    gens = TurnGenerations(keep=2)
    for name in ("a", "b", "c", "d"):
        gens.begin(name)
    assert all(gens.get(n) is not None for n in "abcd")  # 全部仍 live：超過 keep 也不修剪
    for name in "abc":
        gens.finish(name)
    assert gens.get("d") is not None and sum(gens.get(n) is not None for n in "abc") <= 1
    gens.discard("d")
    assert gens.get("d") is None


def test_invalid_speaker_ref_and_language_are_dropped_and_counted(rig):
    r = rig()
    result = r.say("你好", speaker="bad speaker!<script>", language="zh_TW; DROP TABLE")
    assert result.ticket is not None and result.ticket.speaker_ref == "unknown:invalid"
    r.wait_terminal(result.ticket.turn_id)
    event = r.events(types=("input.utterance_final",))[0]
    assert event.payload["speaker_ref"] == "unknown:invalid" and "language" not in event.payload
    orch = r.host.orchestrator
    assert orch.metrics["intake.invalid_speaker_ref"] == 1 and orch.metrics["intake.invalid_language"] == 1
    ok = r.say("再一句", language="zh-TW")
    r.wait_terminal(ok.ticket.turn_id)
    assert r.events(types=("input.utterance_final",))[-1].payload["language"] == "zh-TW"


def test_participant_uses_given_session_guild_and_keeps_platform_prefix(rig):
    roster = GuildRoster({"123": {"actor_a": "111"}, "456": {"actor_b": "222"}})
    old = make_session(platform="discord", guild_id="123", audience_scope="scope_group_g123")
    r = rig(session=old, host_kwargs={"roster": roster}, presence_audience=lambda: "scope_group_g123")
    other = make_session(platform="discord", guild_id="456", audience_scope="scope_group_g456")
    r.host.participant_joined("discord:222", session=other)  # 事件屬於另一個 session 的 guild
    assert roster.present_uids("456") == ["222"] and roster.present_uids("123") == []
    r.host.participant_joined("web:111", session=old)  # 平台前綴與 session 不符：roster 不更新（避免跨平台同 uid 碰撞）
    assert roster.present_uids("123") == [] and r.host.metrics["echo.participant_platform_mismatch"] == 1
    r.host.participant_joined("bad ref!", session=old)
    assert r.host.metrics["echo.invalid_participant"] == 1
    r.host._worker.drain(WAIT)
    refs = [e.payload["speaker_ref"] for e in r.events(types=("conversation.participant_joined",))]
    assert refs == ["discord:222", "web:111"]  # 事件保留完整平台前綴


def test_think_with_own_memory_is_refused(session_factory):
    class Own:
        memory = object()
        memory_resolver = None

    class OwnResolver:
        memory = None
        memory_resolver = staticmethod(lambda scope: None)

    for think in (Own(), OwnResolver()):
        with pytest.raises(ValueError, match="Access Layer"):
            EchoPipelineHost(
                publisher=None, think=think, speak=None, sink_factory=lambda v, t: "x", session=make_session()
            )


def test_start_turn_skips_cancelled_or_presence_lost_tickets(rig):
    r = rig()
    orch = r.host.orchestrator
    t1 = orch.on_utterance(r.host.session, UtteranceFact(SPEAKER, "已取消", seq=80)).ticket
    orch.cancel_turn(t1.turn_id, "global_stop")
    r.host._es.call_soon(r.host._start_turn, t1, UtteranceFact(SPEAKER, "已取消", seq=80))
    assert r.wait_terminal(t1.turn_id)[-1] == "turn.cancelled"
    assert r.events(t1.turn_id, ("turn.cancelled",))[0].payload["reason_code"] == "global_stop"
    t2 = orch.on_utterance(r.host.session, UtteranceFact(SPEAKER, "Lease 沒了", seq=81)).ticket
    r.core.presence.revoke("gone")
    r.wait_for(lambda: not r.host.presence_active, what="端點丟棄 Lease")
    r.host._es.call_soon(r.host._start_turn, t2, UtteranceFact(SPEAKER, "Lease 沒了", seq=81))
    r.wait_for(lambda: "turn.cancelled" in r.kinds(t2.turn_id), what="t2 終結")
    assert r.think.stream_calls == 0  # 兩輪都沒有送進 coordinator


# ---- 輸入 stage 失敗 ----

class FlakyInput(StubInput):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.opens = 0
        self.fail_opens = 1

    async def open(self, token):
        self.opens += 1
        if self.opens <= self.fail_opens:
            raise RuntimeError("輸入流壞了")
        async for item in super().open(token):
            yield item


def test_input_stage_failure_restarts_once_and_publishes_health(rig):
    stub = FlakyInput()
    r = rig(host_kwargs={"input_stage": stub})
    r.wait_for(lambda: stub.opens >= 2 and stub.queue is not None, what="輸入 stage 重啟")
    assert r.host.started and r.host.metrics["echo.input_restarts"] == 1
    r.host._worker.drain(WAIT)
    health = [e for e in r.events(types=("core.component_health_changed",)) if e.payload.get("ref_id") == "echo_input"]
    assert [e.payload["status"] for e in health] == ["degraded"]


def test_input_stage_unrecoverable_failure_is_reflected_in_started(rig):
    stub = FlakyInput()
    stub.fail_opens = 99
    r = rig(host_kwargs={"input_stage": stub})
    r.wait_for(lambda: r.host._input_failed, what="輸入 stage 放棄")
    assert not r.host.started and r.host.metrics["echo.input_failures"] == 2
    r.host._worker.drain(WAIT)
    statuses = [e.payload["status"] for e in r.events(types=("core.component_health_changed",))
                if e.payload.get("ref_id") == "echo_input"]
    assert statuses == ["degraded", "degraded", "unavailable"]
    assert r.say("輸入壞了，直接 submit 仍可用").ticket is not None


# ---- V07：真實 ES pipeline 的晚到 chunk 與寫入期間被停止 ----

class BlockingSink(FakeAudioSink):
    """第 1 句的寫入卡住（模擬寫入進行中）；測試在這期間插話，再放行。"""

    def __init__(self) -> None:
        super().__init__(realtime=True)
        self.entered = threading.Event()
        self.gate: asyncio.Event | None = None

    async def write(self, chunk) -> None:
        if chunk.sentence_index == 1:
            self.gate = self.gate or asyncio.Event()
            self.entered.set()
            await self.gate.wait()
        await super().write(chunk)


def test_v07_stop_during_inner_write_through_real_pipeline_creates_no_intent_for_that_sentence(rig):
    sinks: list[BlockingSink] = []

    def factory(view, ticket):
        sink = BlockingSink()
        sinks.append(sink)
        return sink

    r = rig(sink_factory=factory, responses=("第一句話長長長長長長長長。第二句話也長長長長長長。第三句。",))
    result = r.say("講點什麼")
    turn_id = result.ticket.turn_id
    assert sinks or r.wait_for(lambda: bool(sinks), what="sink 建立") is None
    assert sinks[0].entered.wait(WAIT)  # 第 1 句寫入進行中
    r.voice(seconds=3.0)  # 插話：同步停 sink
    wrapper = r.host.wrappers_of(turn_id)[0]
    r.wait_for(lambda: wrapper.stopped, what="包裝已停止")

    async def release():
        sinks[0].gate.set()

    r.run_on_es(release())
    kinds = r.wait_terminal(turn_id)
    assert kinds[-1] == "turn.cancelled"
    assert {i.sequence for i in r.intents(turn_id)} <= {0}  # 寫入期間被停止的第 1 句沒有 intent
    assert all(e.sentence_index != 1 for e in wrapper.collect_evidence())
