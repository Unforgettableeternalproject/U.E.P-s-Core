"""P3 S5 審查修正的測試：audience 切換清狀態、否決先於 propose、採用時機（on_think_started）、
barge-in 頻道模式、卡住的 proposer、提議純讀、shadow 回饋、快取與 repr。"""
from __future__ import annotations

import dataclasses
import threading
import time

import pytest

pytest.importorskip("echo_stream")

from echo_stream.contracts.turn import VoiceEvent, VoiceState

from tests.uep.echo_support import PRIVATE, SPEAKER, WAIT, build_rig, make_session
from tests.uep.test_p3_s5_policy import View, active_source, voiced
from tests.uep.test_p3_s5_rules import (
    CHATTER,
    Clock,
    cand,
    feed_chatter,
    finish,
    joined,
    started,
)
from tests.uep.test_p3_s5_slot import (
    GROUP,
    OTHERS,
    Fixed,
    Recorder,
    Slow,
    fast,
    group_rig,
)
from uep.echo.endpoint import StopNotice
from uep.echo.host import _play_done
from uep.echo.policy import CoreModeRules, CoreYieldPolicy, HostPolicySource
from uep.turn.aggregator import BufferedAggregator, Segment
from uep.turn.decision_config import AggregatorConfig, DecisionConfig
from uep.turn.engagement import ChannelMode
from uep.turn.models import (
    Proposal,
    ReplyMode,
    TurnEndFacts,
    TurnOutcomeFacts,
    UtteranceFact,
)
from uep.turn.policy import PolicySnapshot, mode_flags
from uep.turn.proposer import Interjection, ProposerGuard, validate_proposal
from uep.turn.rule_proposer import RuleProposer
from uep.turn.wake import WakeDetector

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


# ---------------------------------------------------------------------------
# M1：audience 切換與成員離場清狀態
# ---------------------------------------------------------------------------


def test_rule_proposer_audience_change_clears_buffer_windows_and_responding():
    p, _ = joined(3)
    first = cand("小諾 你好嗎", "u1", n=1)
    p.propose(first)
    started(p, first)
    feed_chatter(p, CHATTER[:2], start=10)
    assert p._engagement.peek_buffer() and p._responding
    p.on_audience_changed()
    assert p._engagement.peek_buffer() == [] and not p._responding and not p._pre_windows
    assert p.propose(cand("今天你們有沒有看到那個影片", "u1", n=30)).reason_code == "observe"  # 窗口也清了
    assert p.tick() == []


def test_audience_change_then_tick_produces_no_interjection(rig):
    cfg = fast()
    proposer = RuleProposer(cfg)
    r = group_rig(rig, proposer, host_kwargs={"decision": cfg})
    # 把 tick 停住，累積一批旁聽 turn；換 audience 之後 tick 不可能插話
    r.host._tick_stop.set()
    time.sleep(0.15)
    for i, text in enumerate(CHATTER):
        assert r.say(text, OTHERS[i % 2]).ticket is None
    assert len(proposer._engagement.peek_buffer()) == 4
    r.host.set_session(make_session(audience_scope="scope_public", conversation_id=r.host.session.conversation_id))
    assert proposer._engagement.peek_buffer() == []
    assert r.host.orchestrator.tick(r.host.session) == []  # ctx 已清掉：不產生任何決策
    time.sleep(0.2)
    assert not r.events(types=("turn.started",))


def test_tick_with_a_session_that_differs_from_the_last_seen_one_is_dropped(rig):
    rec = Recorder()
    decision = fast()
    r = rig(host_kwargs={"proposer": rec, "aggregator": BufferedAggregator(decision.aggregator),
                         "decision": decision})
    r.host._tick_stop.set()
    time.sleep(0.15)
    assert r.say("舊內容").reason_code == "buffered"
    other = make_session(audience_scope=GROUP)
    assert r.host.orchestrator.tick(other) == []
    time.sleep(0.5)
    assert r.host.orchestrator.tick(r.host.session) == [] and rec.candidates == []


def test_participant_left_clears_their_buffered_text(rig):
    cfg = fast()
    proposer = RuleProposer(cfg)
    r = group_rig(rig, proposer, host_kwargs={"decision": cfg})
    r.host._tick_stop.set()
    time.sleep(0.15)
    r.say(CHATTER[0], "discord:222")
    r.say(CHATTER[1], "discord:333")
    assert {t.speaker_ref for t in proposer._engagement.peek_buffer()} == {"discord:222", "discord:333"}
    r.host.participant_left("discord:222")
    assert {t.speaker_ref for t in proposer._engagement.peek_buffer()} == {"discord:333"}


def test_participant_left_discards_the_speakers_unmerged_aggregator_text(rig):
    rec = Recorder()
    decision = fast(aggregator=AggregatorConfig(merge_gap_ms=5000, max_wait_ms=800, active_merge_gap_ms=5000,
                                                active_max_wait_ms=800, min_text_length=1))
    r = group_rig(rig, rec, host_kwargs={"aggregator": BufferedAggregator(decision.aggregator),
                                         "decision": decision})
    assert r.say("離場前說的話", "discord:222").reason_code == "buffered"
    r.host.participant_left("discord:222")
    time.sleep(1.2)
    assert rec.candidates == [] and not r.events(types=("turn.started",))


# ---------------------------------------------------------------------------
# M2：否決在 propose 之前
# ---------------------------------------------------------------------------


class Spy(Fixed):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def propose(self, candidate):
        self.calls += 1
        return super().propose(candidate)


def test_audience_not_allowed_is_vetoed_before_the_proposer_runs(rig):
    spy = Spy()
    r = rig(host_kwargs={"proposer": spy}, session=make_session(audience_scope="scope_public"),
            presence_audience=lambda: PRIVATE)  # 公開 audience 超出 Presence 目前允許的範圍
    result = r.say("你好")
    assert result.ticket is None and result.reason_code == "audience_not_allowed" and result.proposal is None
    assert spy.calls == 0
    assert [e.payload["reason_code"] for e in r.events(types=("turn.skipped",))] == ["audience_not_allowed"]


def test_global_stop_is_vetoed_before_the_proposer_runs_and_a_new_lease_lifts_it(rig):
    spy = Spy()
    r = rig(host_kwargs={"proposer": spy})
    r.host._on_endpoint_stop(StopNotice("global_stop"))
    result = r.say("你好")
    assert result.ticket is None and result.reason_code == "global_stop" and spy.calls == 0
    r.host._stopped_reason = None  # acquire_lease 取得新 Lease 時會做同樣的事
    assert r.say("再說一次").ticket is not None and spy.calls == 1


def test_presence_inactive_is_vetoed_before_the_proposer_runs(rig):
    spy = Spy()
    r = rig(host_kwargs={"proposer": spy}, start=False)  # 沒有啟動：沒有 Lease
    r.host.orchestrator._auth_provider = lambda: None
    result = r.host.orchestrator.on_utterance(r.host.session, UtteranceFact(SPEAKER, "你好", 1))
    assert result.reason_code == "presence_inactive" and result.ticket is None and spy.calls == 0


# ---------------------------------------------------------------------------
# M3 / m1：採用時機
# ---------------------------------------------------------------------------


class Observing(Fixed):
    def __init__(self, **fields) -> None:
        super().__init__(**fields)
        self.started: list = []
        self.outcomes: list[TurnOutcomeFacts] = []

    def on_turn_started(self, turn_id, candidate_id, speakers):
        self.started.append((turn_id, candidate_id, speakers))

    def on_turn_outcome(self, facts):
        self.outcomes.append(facts)


def test_started_hook_fires_at_think_start_and_never_for_a_dropped_turn(rig):
    obs = Observing()
    r = rig(host_kwargs={"proposer": obs})
    dropped = r.host.orchestrator.on_utterance(r.host.session, UtteranceFact(SPEAKER, "不會開始", 100))
    assert dropped.ticket is not None and obs.started == []  # 簽了 ticket 還不算被採用
    r.host.orchestrator.on_turn_ended(dropped.ticket.turn_id, TurnEndFacts("dropped_busy", "dropped_busy"))
    assert obs.started == [] and obs.outcomes[-1].started is False and obs.outcomes[-1].status == "dropped_busy"
    ran = r.say("會開始")
    r.wait_terminal(ran.ticket.turn_id)
    r.wait_for(lambda: len(obs.outcomes) == 2, what="outcome")
    assert [s[0] for s in obs.started] == [ran.ticket.turn_id]
    assert obs.outcomes[-1].started is True


def test_never_started_turn_leaves_no_responding_or_window_side_effects():
    p, _ = joined(3)
    c = cand("小諾 你好嗎", "u1", n=1)
    p.propose(c)  # 提議被簽成 ticket 但從未 think：沒有 on_turn_started
    finish_not_started = TurnOutcomeFacts(
        turn_id="t", candidate_id=c.candidate_id, speakers=c.speakers, reply_mode="respond", reason_code="called",
        status="dropped_busy", end_reason="dropped_busy", started=False,
    )
    p.on_turn_outcome(finish_not_started)
    assert not p._responding and not p._pre_windows
    assert p.propose(cand("今天你們有沒有看到那個影片", "u1", n=2)).reason_code == "observe"  # 沒有 context window


def test_voided_after_started_restores_nothing_but_clears_responding():
    p, _ = joined(3)
    c = cand("小諾 你好嗎", "u1", n=1)
    p.propose(c)
    started(p, c)
    assert "u1" in p._responding
    finish(p, c, "voided", end_reason="yielded", ok=False)
    assert "u1" not in p._responding


def test_proposal_is_a_pure_read_for_adoption_side_effects():
    """被否決、逾時或被退回的提議（沒有 on_turn_started）不會吞掉 buffer，也不會套用冷卻。"""
    p, _ = joined(3)
    feed_chatter(p, CHATTER[:2])
    assert p.propose(cand("小諾 你覺得呢", "u1", n=9)).reply_mode is ReplyMode.RESPOND
    assert len(p._engagement.peek_buffer()) == 3 and p._engagement._last_engage is None
    started(p, cand("小諾 你覺得呢", "u1", n=9))
    assert p._engagement.peek_buffer() == [] and p._engagement._last_engage is not None


def test_unadopted_interjection_unblocks_after_expiry_and_a_failed_one_applies_cooldown():
    p, clock = joined(3)
    feed_chatter(p, [*CHATTER, "再補一句話"])  # 5 句：信心衰減一次後仍高於門檻
    [first] = p.tick()
    assert p.tick() == []
    clock.advance(10.1)  # 沒被採用也沒回報：自動解除，不會永遠擋住
    assert len(p.tick()) == 1
    p.on_turn_outcome(TurnOutcomeFacts(
        turn_id="t", candidate_id="tick:1", speakers=first.speakers, reply_mode="respond",
        reason_code="interject_context", status="dropped_busy", end_reason="dropped_busy", started=False,
    ))
    assert len(p._engagement.peek_buffer()) == 5  # buffer 保留
    assert p.tick() == []  # 但套了冷卻與重設新 turn 計數：不會每個 tick 重提


def test_interjection_adoption_only_takes_the_batch_it_proposed():
    p, _ = joined(3)
    feed_chatter(p, CHATTER)
    [ij] = p.tick()
    feed_chatter(p, ["提出之後才來的一句"], start=40)
    p.on_turn_started("t", "tick:1", ij.speakers)
    assert [t.text for t in p._engagement.peek_buffer()] == ["提出之後才來的一句"]


def test_on_participant_left_clears_window_and_responding_for_that_speaker():
    p, _ = joined(3)
    c = cand("小諾 你好嗎", "u1", n=1)
    p.propose(c)
    started(p, c)
    feed_chatter(p, CHATTER[:2], start=10)
    p.on_participant_left("u1")
    assert "u1" not in p._responding and not p._context.is_in_context("u1")
    assert all(t.speaker_ref != "u1" for t in p._engagement.peek_buffer())


# ---------------------------------------------------------------------------
# M4：barge-in 依頻道模式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel, reply_mode, barge", [
    (ChannelMode.ACTIVE, "respond", True),
    (ChannelMode.PASSIVE, "respond", False),  # 被動頻道：不可被打斷（bot 的 barge_in_modes=["active"]）
    (None, "respond", True),  # 沒有頻道模式的宿主維持既有行為
    (ChannelMode.ACTIVE, "passive", False),  # D80 預設本來就不可打斷
    (ChannelMode.PASSIVE, "passive", False),
    (ChannelMode.ACTIVE, None, True),
    (ChannelMode.PASSIVE, None, False),
])
def test_barge_in_flag_by_channel_mode_table(channel, reply_mode, barge):
    snap = PolicySnapshot(channel_mode=channel)
    assert mode_flags(snap, reply_mode)[1] is barge


def test_barge_in_channel_modes_are_configurable_and_follow_published_mode():
    both = HostPolicySource("discord", barge_in_channel_modes=frozenset({ChannelMode.ACTIVE, ChannelMode.PASSIVE}))
    both.publish_channel_mode(ChannelMode.PASSIVE)
    assert both.current().mode_rules.flags_for("respond").barge_in
    default = HostPolicySource("discord")
    rules = default.current().mode_rules
    default.publish_channel_mode(ChannelMode.PASSIVE)
    assert not rules.flags_for("respond").barge_in
    default.publish_channel_mode(ChannelMode.ACTIVE)
    assert rules.flags_for("respond").barge_in
    assert CoreModeRules().flags_for("respond").barge_in  # 預設快照沒有頻道模式


def test_decision_config_default_barge_in_channel_modes_is_active_only():
    assert DecisionConfig().barge_in_channel_modes == ("active",)


# ---------------------------------------------------------------------------
# M5：卡住的 proposer
# ---------------------------------------------------------------------------


def test_guard_returns_busy_immediately_and_does_not_queue_while_a_call_is_stuck():
    guard = ProposerGuard("t")
    release = threading.Event()
    assert guard.call(lambda _: release.wait(5.0), None, 0.05) == (None, "timeout")
    assert guard.busy
    started_at = time.monotonic()
    assert guard.call(lambda _: 1, None, 5.0) == (None, "busy")
    assert guard.submit(lambda _: 1, None) is None  # 沒有入佇列
    assert time.monotonic() - started_at < 0.05
    release.set()
    deadline = time.monotonic() + 2.0
    while guard.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert guard.call(lambda _: 7, None, 1.0) == (7, "ok")
    guard.close()


def test_guard_close_does_not_wait_for_a_stuck_thread_and_thread_is_daemon():
    guard = ProposerGuard("t2")
    release = threading.Event()
    guard.call(lambda _: release.wait(10.0), None, 0.02)
    started_at = time.monotonic()
    guard.close()
    assert time.monotonic() - started_at < 0.2
    assert guard._thread is not None and guard._thread.daemon
    assert guard.call(lambda _: 1, None, 1.0)[1] == "error"
    release.set()


def test_stuck_primary_proposer_falls_back_immediately_for_later_utterances(rig):
    r = rig(host_kwargs={"proposer": Slow(1.5), "decision": fast(proposer_budget_ms=100)})
    first = r.say("一")
    assert first.proposal.reason_code == "proposer_timeout"
    started_at = time.monotonic()
    rest = [r.say(text) for text in ("二", "三", "四")]
    assert time.monotonic() - started_at < 0.5  # 不是每句都等滿 100ms 預算（再加 DB 寫入的時間）
    assert {x.proposal.reason_code for x in rest} == {"proposer_busy"}
    metrics = r.host.orchestrator.metrics
    assert metrics["proposer.timeout"] == 1 and metrics["proposer.busy"] == 3
    assert all(x.ticket is not None for x in rest)  # D80 預設仍然簽 ticket


def test_stuck_shadow_proposer_keeps_the_queue_bounded(rig):
    calls: list[int] = []

    class StuckShadow:
        proposer_id = "shadow.stuck"

        def propose(self, candidate):
            calls.append(1)
            time.sleep(1.0)
            return Proposal(reply_mode=ReplyMode.RESPOND, reason_code="x", proposer_id="shadow.stuck")

    r = rig(host_kwargs={"proposer": Fixed(), "shadow_proposers": [StuckShadow()]})
    for text in ("一", "二", "三", "四", "五"):
        assert r.say(text).ticket is not None
    time.sleep(0.2)
    assert len(calls) == 1  # 其餘都被跳過，沒有排隊
    assert r.host.orchestrator.metrics["shadow.skipped_busy"] == 4


def test_shutdown_is_not_dragged_by_a_stuck_proposer_thread(rig):
    r = rig(host_kwargs={"proposer": Slow(30.0), "decision": fast(proposer_budget_ms=50)})
    assert r.say("你好").proposal.reason_code == "proposer_timeout"
    started_at = time.monotonic()
    r.core.shutdown()
    assert time.monotonic() - started_at < 5.0


def test_submit_tick_skips_when_the_intake_lock_is_contended(rig):
    decision = fast()
    r = rig(host_kwargs={"aggregator": BufferedAggregator(decision.aggregator), "decision": decision})
    held = threading.Event()
    done = threading.Event()

    def hold():
        with r.host._intake_lock:
            held.set()
            done.wait(2.0)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(1.0)
    started_at = time.monotonic()
    assert r.host.submit_tick() == []
    assert time.monotonic() - started_at < 0.5
    assert r.host.metrics["echo.tick_skipped_contended"] >= 1
    done.set()
    t.join(2.0)


# ---------------------------------------------------------------------------
# m3／m4／m5／m7／m8
# ---------------------------------------------------------------------------


def test_participant_count_defaults_to_one_when_unknown(rig):
    rec = Recorder()
    r = rig(host_kwargs={"proposer": rec})
    r.say("你好")
    assert rec.candidates[0].participant_count == 1


def test_reprs_never_carry_the_spoken_text(rig):
    secret = "這是不該出現在日誌的私人文字"
    r = rig(host_kwargs={"proposer": Fixed()})
    result = r.say(secret)
    assert secret not in repr(result) and secret not in repr(result.fact) and secret not in repr(result.ticket)
    ij = Interjection("u1", secret, ("e1",), Proposal(reply_mode=ReplyMode.RESPOND))
    assert secret not in repr(ij)
    buffered = rig(host_kwargs={"aggregator": BufferedAggregator(fast().aggregator), "decision": fast()}).say(secret)
    assert secret not in repr(buffered)


def test_wake_detection_is_cached_per_text():
    d = WakeDetector()
    first = d.detect("今天天氣不錯我想出去走走")
    assert d.detect("今天天氣不錯我想出去走走") is first
    for i in range(400):
        d.detect(f"文字{i}")
    assert len(d._cache) <= 256


def test_urgent_predicate_is_only_reevaluated_when_the_buffer_grows():
    clock = Clock()
    agg = BufferedAggregator(AggregatorConfig(), clock)
    calls: list[str] = []

    def predicate(text: str) -> bool:
        calls.append(text)
        return False

    agg.set_urgent_predicate(predicate)
    agg.feed(Segment(speaker_ref="u1", text="第一句", ref="e1", start_ts=0, end_ts=1))
    for _ in range(5):
        clock.advance(0.3)
        agg.tick()
    assert len(calls) == 1  # buffer 沒長就不重判
    agg.feed(Segment(speaker_ref="u1", text="第二句", ref="e2", start_ts=1, end_ts=2))
    clock.advance(0.3)
    agg.tick()
    assert len(calls) == 2


def test_play_done_filter_matches_the_coordinator_selection():
    class V:
        pass

    done, finished, live = V(), V(), V()
    done._done = True
    finished._play_done = threading.Event()
    finished._play_done.set()
    live._play_done = threading.Event()
    assert _play_done(done) and _play_done(finished) and not _play_done(live) and not _play_done(V())


def test_voice_feed_with_only_finished_turns_still_feeds_the_accumulator(rig):
    r = rig(host_kwargs={"proposer": Fixed()})
    seen = []
    r.host._yield_policy = type("P", (), {"on_voiced": lambda self, ev, live: seen.append(list(live))})()
    ev = VoiceEvent(state=VoiceState.SPEECH, timestamp=time.perf_counter(), duration_s=0.1, speaker_id=SPEAKER,
                    is_voiced=True, chunk_s=0.1)
    r.host.on_voice_event(ev)
    assert seen == [[]]


def test_shadow_proposers_receive_started_and_outcome_feedback_with_failure_isolation(rig):
    shadow = Observing(reply_mode=ReplyMode.RESPOND, proposer_id="shadow.obs")

    class BadHooks(Fixed):
        def on_turn_started(self, *a):
            raise RuntimeError("壞了")

        def on_turn_outcome(self, facts):
            raise RuntimeError("壞了")

    r = rig(host_kwargs={"proposer": Observing(), "shadow_proposers": [shadow, BadHooks()]})
    result = r.say("你好")
    r.wait_terminal(result.ticket.turn_id)
    r.wait_for(lambda: bool(shadow.outcomes), what="shadow outcome")
    assert shadow.started and shadow.started[0][0] == result.ticket.turn_id
    assert shadow.outcomes[0].status == "completed" and shadow.outcomes[0].started is True
    assert r.host.orchestrator.wait_shadow(WAIT)


def test_shadow_feedback_is_skipped_when_the_shadow_is_busy_and_counted(rig):
    class SlowObserver(Fixed):
        def propose(self, candidate):
            time.sleep(0.6)
            return super().propose(candidate)

        def on_turn_started(self, *a):
            pass

    r = rig(host_kwargs={"proposer": Fixed(), "shadow_proposers": [SlowObserver()]})
    result = r.say("你好")
    r.wait_terminal(result.ticket.turn_id)
    assert r.host.orchestrator.metrics["shadow.feedback_skipped_busy"] >= 1


def test_voiced_helper_and_snapshot_still_drive_yield_after_review_changes():
    policy = CoreYieldPolicy(active_source())
    assert [a.turn_id for a in policy.on_voiced(voiced(1.0, 0.8), [View()])] == ["turn_a"]


def test_validate_proposal_whitelists_only_listening():
    base = Proposal(reply_mode=ReplyMode.PASS, reason_code="x", proposer_id="p")
    assert validate_proposal(dataclasses.replace(base, interaction_to="LISTENING")) is None
    for bad in ("THINKING", "SPEAKING", "IDLE", "WAITING_USER", "WORK", "listening"):
        assert validate_proposal(dataclasses.replace(base, interaction_to=bad)) == "interaction_to"
