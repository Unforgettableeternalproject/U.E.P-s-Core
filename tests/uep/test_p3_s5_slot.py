"""P3 S5 決策插槽的整合行為（需 PostgreSQL 與已安裝的 echo_stream）。

涵蓋：proposer 預算逾時與例外回 D80 預設、Proposal 驗證、CC-7 提請與被拒回退、Behavior 軸被拒、
替換 proposer 不改 Core、shadow mode、Aggregator 與 tick、被動模式插話、PASS 熱路徑、Q-1 回饋通道、
政策快照由 proposer 的頻道模式驅動。
"""
from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("echo_stream")

from echo_stream.contracts.output import OutcomeStatus, TurnOutcome
from echo_stream.contracts.types import TurnPhase
from echo_stream.core.tracer import LatencyTracer
from sqlalchemy import select

from tests.uep.echo_support import (
    SPEAKER,
    WAIT,
    build_rig,
    fast_think,
    make_session,
)
from uep.echo.memory import GuildRoster
from uep.echo.outcomes import facts_from_outcome
from uep.state.coordinator import Axis, Decision, StateTransitionRequest
from uep.state.models import StateRequestRow
from uep.turn.aggregator import BufferedAggregator
from uep.turn.decision_config import (
    AggregatorConfig,
    BotIdentityConfig,
    DecisionConfig,
    EngagementConfig,
    GateConfig,
    GroupEngagementConfig,
)
from uep.turn.engagement import ChannelMode
from uep.turn.models import Proposal, ReplyMode, TurnEndFacts, TurnOutcomeFacts
from uep.turn.registry import create_proposer
from uep.turn.rule_proposer import RuleProposer

pytestmark = pytest.mark.usefixtures("session_factory")

GROUP = "scope_group_g123"
NAMES = BotIdentityConfig(primary_name="Novalis", aliases=("小諾",))
MEMBERS = {"123": {"actor_a": "111", "actor_b": "222", "actor_c": "333"}}
OTHERS = ("discord:222", "discord:333")


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


def fast(**over) -> DecisionConfig:
    base = DecisionConfig(
        bot_identity=NAMES, proposer_budget_ms=300, tick_interval_s=0.05,
        gate=GateConfig(context_window_s=5.0),
        aggregator=AggregatorConfig(merge_gap_ms=200, max_wait_ms=300, active_merge_gap_ms=100, active_max_wait_ms=100,
                                    min_text_length=1),
    )
    return dataclasses.replace(base, **over)


class Slow:
    proposer_id = "test.slow"

    def __init__(self, delay: float, mode: ReplyMode = ReplyMode.RESPOND) -> None:
        self.delay, self.mode = delay, mode

    def propose(self, candidate):
        time.sleep(self.delay)
        return Proposal(reply_mode=self.mode, reason_code="late", proposer_id=self.proposer_id)


class Boom:
    def propose(self, candidate):
        raise RuntimeError("壞了")


class Fixed:
    def __init__(self, **fields) -> None:
        self.fields = {"reply_mode": ReplyMode.RESPOND, "reason_code": "fixed", **fields}
        self.seen: list = []

    def propose(self, candidate):
        self.seen.append(candidate)
        return Proposal(**self.fields)


def group_rig(rig, proposer, *, joined=("discord:111", *OTHERS), **over):
    roster = GuildRoster(MEMBERS)
    r = rig(
        session=make_session(audience_scope=GROUP, guild_id="123", platform="discord"),
        presence_audience=lambda: GROUP,
        host_kwargs={"roster": roster, "proposer": proposer, **over.pop("host_kwargs", {})}, **over,
    )
    for ref in joined:
        r.host.participant_joined(ref)
    return r


def state_rows(r, proposer: str = "core.decision") -> list[StateRequestRow]:
    with r.session_factory() as s:
        return list(s.scalars(select(StateRequestRow).where(StateRequestRow.proposer == proposer)
                              .order_by(StateRequestRow.decided_at)))


# ---------------------------------------------------------------------------
# 逾時與例外 -> D80 預設
# ---------------------------------------------------------------------------


def test_timeout_falls_back_to_respond_for_a_solo_channel_and_does_not_wait_for_the_proposer(rig):
    r = rig(host_kwargs={"proposer": Slow(1.0), "decision": fast(proposer_budget_ms=50)})
    started = time.monotonic()
    result = r.say("你好")
    assert time.monotonic() - started < 0.6  # 不等 proposer 跑完（它睡 1.0s）
    assert result.ticket is not None and result.proposal.reason_code == "proposer_timeout"
    assert result.ticket.reply_mode == "respond"
    assert r.host.orchestrator.metrics["proposer.timeout"] == 1
    assert r.wait_terminal(result.ticket.turn_id)[-1] == "turn.completed"


def test_timeout_in_a_group_unaddressed_becomes_passive_and_addressed_responds(rig):
    r = group_rig(rig, Slow(1.0), host_kwargs={"decision": fast(proposer_budget_ms=50)})
    quiet = r.say("今天你們有沒有看到那個影片")
    assert quiet.proposal.reason_code == "proposer_timeout" and quiet.ticket.reply_mode == "passive"
    assert r.wait_terminal(quiet.ticket.turn_id)[-1] == "turn.completed"
    done = r.events(quiet.ticket.turn_id, ("turn.completed",))[0]
    assert done.payload == {"status": "skipped", "reason_code": "mode_silent"}  # passive：LLM 跑一次、不出聲
    called = r.say("小諾 你覺得呢")  # 喚醒詞比對由 Core 做（與 proposer 無關）
    assert called.ticket.reply_mode == "respond" and called.proposal.reason_code == "proposer_timeout"


def test_exception_in_a_group_unaddressed_becomes_passive(rig):
    r = group_rig(rig, Boom())
    result = r.say("今天你們有沒有看到那個影片")
    assert result.proposal.reason_code == "proposer_error" and result.ticket.reply_mode == "passive"
    assert r.host.orchestrator.metrics["proposer.error"] == 1


def test_stuck_proposer_only_costs_the_budget_per_turn_and_later_turns_still_resolve(rig):
    r = rig(host_kwargs={"proposer": Slow(0.6), "decision": fast(proposer_budget_ms=50)})
    first = r.say("一")
    second = r.say("二")  # 單一決策執行緒仍被第一個占住：同樣在預算內放棄、回預設
    assert first.ticket is not None and second.ticket is not None
    assert r.host.orchestrator.metrics["proposer.timeout"] == 2


# ---------------------------------------------------------------------------
# Proposal 驗證、CC-7 提請與回退、Behavior 軸
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fields, why", [
    ({"route": "shell"}, "route"),
    ({"interaction_to": "WORK"}, "Behavior 軸的值"),
    ({"interaction_to": "MISCHIEF"}, "Behavior 軸的值"),
    ({"confidence": 7.0}, "confidence"),
    ({"reason_code": "含中文與 空白"}, "reason_code"),
])
def test_invalid_proposal_is_replaced_by_default_and_never_reaches_cc7(rig, fields, why):
    r = rig(host_kwargs={"proposer": Fixed(**fields)})
    result = r.say("你好")
    assert result.proposal.reason_code == "proposal_invalid", why
    assert result.ticket is not None and r.host.orchestrator.metrics["proposer.invalid"] == 1
    assert state_rows(r) == []  # 決策插槽沒有向 CC-7 提過任何請求


def test_non_enum_reply_mode_is_rejected(rig):
    r = rig(host_kwargs={"proposer": Fixed(reply_mode="respond")})
    assert r.say("你好").proposal.reason_code == "proposal_invalid"


def test_interaction_request_is_made_as_core_decision_and_accepted(rig):
    r = rig(host_kwargs={"proposer": Fixed(reply_mode=ReplyMode.PASS, reason_code="observe",
                                            interaction_to="LISTENING")})
    result = r.say("今天天氣不錯")
    assert result.ticket is None and result.reason_code == "observe"
    [row] = state_rows(r)
    assert (row.axis, row.from_state, row.to_state, row.decision) == ("Interaction", "IDLE", "LISTENING", "ACCEPTED")
    assert r.core.state.current(Axis.INTERACTION).state == "LISTENING"
    skipped = r.events(types=("turn.skipped",))
    assert [e.payload["reason_code"] for e in skipped] == ["observe"]
    assert r.host.orchestrator.metrics["decision.cc7_accepted"] == 1
    # 之後簽發 ticket 時 phase 仍能從 LISTENING 轉 THINKING
    r.host.orchestrator._proposer = Fixed()
    nxt = r.say("再說一句")
    assert r.wait_terminal(nxt.ticket.turn_id)[-1] == "turn.completed"


class StaleVersion:
    """先讓 Interaction 軸前進一版，再用過期的 expected_version 提請求 -> REJECTED_VERSION。"""

    def __init__(self, core) -> None:
        self.core = core

    def propose(self, candidate):
        self.core.state.request_transition(StateTransitionRequest(
            request_id=f"bump:{candidate.candidate_id}", axis="Interaction", proposer="core.nlp",
            expected_version=candidate.interaction_version, from_state=candidate.interaction_state,
            to_state="LISTENING",
        ))
        return Proposal(reply_mode=ReplyMode.PASS, reason_code="observe", interaction_to="THINKING",
                        proposer_id="test.stale")


def test_cc7_rejection_falls_back_to_the_default_without_retry(rig):
    proposer = StaleVersion(None)
    r = rig(host_kwargs={"proposer": proposer})
    proposer.core = r.core
    result = r.say("你好")
    assert result.proposal.reason_code == "proposal_rejected"
    assert result.ticket is not None and result.ticket.reply_mode == "respond"  # D80 預設，不是 proposer 提議的 pass
    rows = state_rows(r)
    assert [(x.decision, x.reason_code) for x in rows] == [("REJECTED_VERSION", "version_mismatch")]
    assert r.host.orchestrator.metrics["decision.cc7_rejected"] == 1
    r.wait_terminal(result.ticket.turn_id)
    assert len(state_rows(r)) == 1  # 不重試


def test_illegal_transition_from_the_proposer_is_rejected_by_cc7_and_falls_back(rig):
    # IDLE -> SPEAKING 不在 Interaction 合法轉換表
    r = rig(host_kwargs={"proposer": Fixed(interaction_to="SPEAKING", reason_code="x")})
    result = r.say("你好")
    assert result.proposal.reason_code == "proposal_rejected" and result.ticket is not None
    assert [x.decision for x in state_rows(r)] == ["REJECTED_ILLEGAL"]


def test_core_decision_cannot_request_the_behavior_axis_at_the_coordinator(rig):
    r = rig()
    before = r.core.state.current(Axis.BEHAVIOR)
    decision = r.core.state.request_transition(StateTransitionRequest(
        request_id="req_behavior_by_decision", axis="Behavior", proposer="core.decision",
        expected_version=before.version, from_state=before.state, to_state="WORK",
    ))
    assert decision.decision is Decision.REJECTED_UNAUTHORIZED and decision.reason_code == "proposer_not_allowed"
    assert r.core.state.current(Axis.BEHAVIOR) == before  # 投影不變
    # core.decision 在 Interaction 軸則是授權的
    view = r.core.state.current(Axis.INTERACTION)
    ok = r.core.state.request_transition(StateTransitionRequest(
        request_id="req_interaction_by_decision", axis="Interaction", proposer="core.decision",
        expected_version=view.version, from_state=view.state, to_state="LISTENING",
    ))
    assert ok.decision is Decision.ACCEPTED


# ---------------------------------------------------------------------------
# 替換 proposer 不改 Core 其他程式
# ---------------------------------------------------------------------------


class OnlyPropose:
    """第三方 proposer 只需要 propose：沒有任何選用 Port。"""

    proposer_id = "third.party"

    def propose(self, candidate):
        reply = ReplyMode.PASS if "安靜" in candidate.text else ReplyMode.RESPOND
        return Proposal(reply_mode=reply, reason_code="third_party", proposer_id=self.proposer_id)


@pytest.mark.parametrize("make, text, issued", [
    (lambda: create_proposer("respond.minimal"), "隨便說說", True),
    (lambda: create_proposer("rules.default", config=fast()), "今天你們有沒有看到那個影片", False),
    (lambda: create_proposer("rules.default", config=fast()), "小諾 你覺得呢", True),
    (OnlyPropose, "請安靜", False),
    (OnlyPropose, "你好", True),
])
def test_swapping_the_proposer_needs_no_other_core_change(rig, make, text, issued):
    r = group_rig(rig, make(), host_kwargs={"decision": fast()})
    result = r.say(text)
    assert (result.ticket is not None) is issued
    if issued:
        assert r.wait_terminal(result.ticket.turn_id)[-1] == "turn.completed"


def test_orchestrator_and_host_do_not_depend_on_a_concrete_proposer():
    root = Path(__file__).resolve().parents[2] / "uep"
    for rel in ("turn/orchestrator.py", "echo/host.py"):
        source = (root / rel).read_text(encoding="utf-8")
        assert "rule_proposer" not in source and "RuleProposer" not in source, rel


# ---------------------------------------------------------------------------
# shadow mode
# ---------------------------------------------------------------------------


def test_shadow_proposer_only_records_and_never_takes_effect(rig):
    seen = []
    shadow = Fixed(reply_mode=ReplyMode.PASS, reason_code="shadow_pass", interaction_to="LISTENING",
                   proposer_id="shadow.one")
    r = rig(host_kwargs={"proposer": Fixed(), "shadow_proposers": [shadow], "on_shadow": seen.append})
    result = r.say("你好")
    assert result.ticket is not None and result.ticket.reply_mode == "respond"  # 主決策生效，shadow 不影響
    assert r.host.orchestrator.wait_shadow(WAIT)
    [rec] = list(r.host.orchestrator.shadow_log)
    assert (rec.shadow_id, rec.primary_mode, rec.shadow_mode, rec.agree, rec.status) == (
        "shadow.one", "respond", "pass", False, "ok")
    assert rec.shadow_reason == "shadow_pass" and rec.candidate_id.endswith(f":{result.fact.seq}")
    assert seen == [rec] and r.host.orchestrator.metrics["shadow.disagree"] == 1
    assert state_rows(r) == []  # shadow 不提 state.transition_requested
    r.wait_terminal(result.ticket.turn_id)
    assert not r.events(types=("turn.skipped",))  # 也不寫 turn.skipped


def test_shadow_agreement_error_and_late_are_recorded(rig):
    class Late(Fixed):
        def propose(self, candidate):
            time.sleep(0.25)
            return super().propose(candidate)

    r = rig(host_kwargs={"proposer": Fixed(), "decision": fast(proposer_budget_ms=100),
                         "shadow_proposers": [Fixed(), Boom(), Late()]})
    result = r.say("你好")
    assert result.ticket is not None
    assert r.host.orchestrator.wait_shadow(WAIT)
    by_status = sorted((x.status, x.agree) for x in r.host.orchestrator.shadow_log)
    assert by_status == [("error", False), ("late", True), ("ok", True)]
    metrics = r.host.orchestrator.metrics
    assert metrics["shadow.agree"] == 2 and metrics["shadow.disagree"] == 1 and metrics["shadow.error"] == 1
    assert metrics["shadow.late"] == 1


# ---------------------------------------------------------------------------
# Aggregator 與 tick
# ---------------------------------------------------------------------------


class Recorder:
    proposer_id = "test.recorder"

    def __init__(self) -> None:
        self.candidates: list = []

    def propose(self, candidate):
        self.candidates.append(candidate)
        return Proposal(reply_mode=ReplyMode.RESPOND, reason_code="rec", proposer_id=self.proposer_id)


def test_default_aggregator_is_pass_through_and_no_tick_thread_runs(rig):
    r = rig()
    assert r.host._tick_thread is None and not r.host.needs_tick
    result = r.say("你好")
    assert result.ticket is not None and result.reason_code == "issued"


def test_buffered_aggregator_merges_segments_and_tick_completes_the_turn(rig):
    rec = Recorder()
    decision = fast()
    r = rig(host_kwargs={"proposer": rec, "aggregator": BufferedAggregator(decision.aggregator),
                         "decision": decision})
    assert r.host._tick_thread is not None and r.host._tick_thread.is_alive()
    first, second = r.say("今天"), r.say("天氣好")
    assert first.reason_code == "buffered" and second.reason_code == "buffered" and first.ticket is None
    r.wait_for(lambda: bool(rec.candidates), what="tick 完成合併的 turn")
    [cand] = rec.candidates
    assert cand.text == "今天 天氣好" and cand.segments == 2 and len(cand.utterance_refs) == 2
    r.wait_for(lambda: bool(r.events(types=("turn.started",))), what="turn.started")
    assert len(r.events(types=("input.utterance_final",))) == 2  # 兩句 utterance 都已記錄
    [started] = r.events(types=("turn.started",))
    assert r.wait_terminal(started.turn_id)[-1] == "turn.completed"
    assert len(rec.candidates) == 1  # 只決策一次


def test_buffered_gap_returns_the_previous_turn_on_the_next_utterance(rig):
    rec = Recorder()
    decision = fast(aggregator=AggregatorConfig(merge_gap_ms=100, max_wait_ms=10_000, active_merge_gap_ms=100,
                                                active_max_wait_ms=10_000, min_text_length=1))
    r = rig(host_kwargs={"proposer": rec, "aggregator": BufferedAggregator(decision.aggregator),
                         "decision": decision})
    assert r.say("前半句").reason_code == "buffered"
    time.sleep(0.25)
    result = r.say("後半句")  # 間隔超過 merge_gap：前一個 turn 在這次呼叫完成
    assert result.ticket is not None and rec.candidates[0].text == "前半句"
    assert result.fact is not None and result.fact.text == "前半句"
    r.wait_terminal(result.ticket.turn_id)


def test_audience_switch_drops_buffered_text_from_the_old_audience(rig):
    rec = Recorder()
    decision = fast(aggregator=AggregatorConfig(merge_gap_ms=5000, max_wait_ms=600, active_merge_gap_ms=5000,
                                                active_max_wait_ms=600, min_text_length=1))
    r = rig(host_kwargs={"proposer": rec, "aggregator": BufferedAggregator(decision.aggregator),
                         "decision": decision})
    assert r.say("舊 audience 的私密內容").reason_code == "buffered"
    r.host.set_session(make_session(audience_scope="scope_public", conversation_id=r.host.session.conversation_id))
    time.sleep(1.0)
    assert rec.candidates == [] and not r.events(types=("turn.started",))  # 不會併進新 session 的 turn


# ---------------------------------------------------------------------------
# 預設規則 proposer 接上宿主：被動模式插話、快照、PASS、Q-1
# ---------------------------------------------------------------------------


CHATTER = ["你們覺得這部電影怎麼樣", "我覺得劇情有點拖", "但是配樂真的很棒", "特別是最後那一段"]


def test_rule_proposer_channel_mode_drives_policy_snapshot_versions(rig):
    proposer = RuleProposer(fast())
    roster = GuildRoster(MEMBERS)
    r = rig(
        session=make_session(audience_scope=GROUP, guild_id="123", platform="discord"),
        presence_audience=lambda: GROUP, host_kwargs={"roster": roster, "proposer": proposer, "decision": fast()},
    )
    assert r.host.policy.core_current().channel_mode is ChannelMode.PASSIVE
    r.host.participant_joined("discord:111")
    assert proposer.channel_mode is ChannelMode.ACTIVE
    snap = r.host.policy.core_current()
    assert snap.channel_mode is ChannelMode.ACTIVE and snap.version == r.host.policy.version  # 同版推送
    for ref in OTHERS:
        r.host.participant_joined(ref)  # 三人：被動
    assert proposer.channel_mode is ChannelMode.PASSIVE
    assert r.host.policy.core_current().channel_mode is ChannelMode.PASSIVE
    assert r.host.policy.core_current().version == r.host.policy.version


def test_rule_proposer_group_observe_then_called_then_interject_via_tick(rig):
    proposer = RuleProposer(fast())
    r = group_rig(rig, proposer, host_kwargs={"decision": fast()})
    for i, text in enumerate(CHATTER[:2]):
        res = r.say(text, OTHERS[i % 2])
        assert res.ticket is None and res.reason_code == "observe"
    skipped = r.events(types=("turn.skipped",))
    assert [e.payload["reason_code"] for e in skipped] == ["observe", "observe"]
    assert r.say("小諾 你覺得呢").ticket is not None  # 被叫到：即使在被動頻道
    for i, text in enumerate(CHATTER):
        assert r.say(text, OTHERS[i % 2]).ticket is None
    # 被叫到的回覆套了冷卻（40s）：被動插話要等冷卻，這裡不會插
    time.sleep(0.3)
    assert len(r.events(types=("turn.started",))) == 1


def test_interjection_through_tick_opens_a_turn(rig):
    cfg = fast(engagement=EngagementConfig(group=GroupEngagementConfig(engage_cooldown_s=0.1,
                                                                      silence_trigger_s=1000.0)))
    proposer = RuleProposer(cfg)
    r = group_rig(rig, proposer, host_kwargs={"decision": cfg})
    for i, text in enumerate(CHATTER):
        res = r.say(text, OTHERS[i % 2])
        assert res.ticket is None
    r.wait_for(lambda: bool(r.events(types=("turn.started",))), what="tick 插話")
    [started] = r.events(types=("turn.started",))
    assert r.wait_terminal(started.turn_id)[-1] == "turn.completed"
    assert len(r.events(types=("turn.started",))) == 1 and r.think.stream_calls == 1


def test_pass_marker_from_the_llm_skips_the_voice_and_feeds_back_to_the_proposer(rig):
    class Observing(Fixed):
        def __init__(self) -> None:
            super().__init__()
            self.started: list = []
            self.outcomes: list[TurnOutcomeFacts] = []

        def on_turn_started(self, turn_id, candidate_id, speakers):
            self.started.append((turn_id, candidate_id, speakers))

        def on_turn_outcome(self, facts):
            self.outcomes.append(facts)

    obs = Observing()
    r = rig(host_kwargs={"proposer": obs}, responses=("[PASS]",))
    result = r.say("你好")
    r.wait_terminal(result.ticket.turn_id)
    done = r.events(result.ticket.turn_id, ("turn.completed",))[0]
    assert done.payload == {"status": "skipped", "reason_code": "pass"}
    assert r.inner_sinks == [] or all(not s.chunks for s in r.inner_sinks)  # 沒有任何語音
    assert r.deliveries() == []
    r.wait_for(lambda: bool(obs.outcomes), what="outcome 回饋")
    assert obs.started == [(result.ticket.turn_id, obs.seen[0].candidate_id, (SPEAKER,))]
    [facts] = obs.outcomes
    assert (facts.status, facts.end_reason, facts.candidate_id) == ("skipped", "pass", obs.seen[0].candidate_id)


def test_barge_in_flag_flows_from_es_outcome_to_proposer_feedback(rig):
    class Observing(Fixed):
        def __init__(self) -> None:
            super().__init__()
            self.outcomes: list[TurnOutcomeFacts] = []

        def on_turn_outcome(self, facts):
            self.outcomes.append(facts)

    obs = Observing()
    r = rig(host_kwargs={"proposer": obs})
    result = r.say("你好")
    r.host.orchestrator.on_turn_ended(result.ticket.turn_id, TurnEndFacts("cancelled", "barge_in", True, barge_in=True))
    [facts] = obs.outcomes
    assert facts.barge_in is True and facts.status == "cancelled" and facts.end_reason == "barge_in"


def test_es_outcome_barge_in_is_mapped_into_turn_end_facts():
    base = TurnOutcome(turn_id="turn_x", phase=TurnPhase.DONE, status=OutcomeStatus.CANCELLED, barge_in=True,
                       cancel_reason="barge_in")
    assert facts_from_outcome(base).barge_in is True
    assert facts_from_outcome(dataclasses.replace(base, barge_in=False)).barge_in is False


def test_proposer_without_optional_ports_is_never_called_for_feedback(rig):
    r = rig(host_kwargs={"proposer": OnlyPropose()})
    result = r.say("你好")
    assert r.wait_terminal(result.ticket.turn_id)[-1] == "turn.completed"  # 沒有 OutcomeObserver 也能正常收尾


def test_listener_continuation_voids_the_unstarted_turn_of_the_same_speaker(rig):
    """主動模式：同一人又講了有意義的新內容（listener_new_segment）-> 他尚未開播的輪讓位。"""
    proposer = RuleProposer(fast())
    roster = GuildRoster(MEMBERS)
    r = rig(
        session=make_session(audience_scope=GROUP, guild_id="123", platform="discord"),
        presence_audience=lambda: GROUP,
        think=fast_think(LatencyTracer(), first_token_delay_s=1.0),
        host_kwargs={"roster": roster, "proposer": proposer, "decision": fast()},
    )
    r.host.participant_joined("discord:111")
    assert proposer.channel_mode is ChannelMode.ACTIVE
    first = r.say("我今天遇到一件很有趣的事情")
    assert first.ticket is not None and first.yield_for == "discord:111"
    r.wait_for(lambda: bool(r.host.coordinator.live_turns()), what="第一輪進入 coordinator")
    second = r.say("後來我就回家了然後睡覺")
    assert second.yield_for == "discord:111"
    kinds = r.wait_terminal(first.ticket.turn_id, timeout=30.0)
    assert kinds[-1] == "turn.cancelled", kinds  # 第一輪在開播前被讓位作廢
    assert r.wait_terminal(second.ticket.turn_id, timeout=30.0)[-1] == "turn.completed"


def test_submit_tick_refuses_to_run_on_the_es_loop_and_after_close(rig):
    r = rig(host_kwargs={"aggregator": BufferedAggregator(fast().aggregator), "decision": fast()})
    assert r.host.submit_tick() == []  # 沒有緩衝：什麼都不做
    errors: list[Exception] = []

    async def on_loop():
        try:
            r.host.submit_tick()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    r.run_on_es(on_loop())
    assert errors and "ES loop" in str(errors[0])
    t = threading.Thread(target=r.host.stop)
    t.start()
    t.join(WAIT)
    assert r.host._tick_thread is None
