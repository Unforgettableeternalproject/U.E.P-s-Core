"""P3 S5 決策插槽的預設實作（不需資料庫）：bot 既有測試案例轉成 Core 的資料表。

基準是 bot 的 test_voice_mode／test_gate／test_aggregator／test_engagement／test_coupling_active_pass／
test_listener（名稱、id 一律虛構，不 import bot）。時間全部由假時鐘推進。
"""
from __future__ import annotations

import dataclasses

import pytest

from uep.turn.aggregator import (
    AggregateState,
    BufferedAggregator,
    PassThroughAggregator,
    Segment,
)
from uep.turn.decision_config import (
    AggregatorConfig,
    BotIdentityConfig,
    CouplingModelConfig,
    DecisionConfig,
    EngagementConfig,
    GateConfig,
    GroupEngagementConfig,
    ModeConfig,
)
from uep.turn.engagement import (
    ChannelMode,
    ContextTracker,
    CouplingModel,
    EngagementMonitor,
    ModeController,
    ObservedTurn,
)
from uep.turn.models import (
    Proposal,
    ReplyMode,
    SessionContext,
    TurnCandidate,
    TurnOutcomeFacts,
)
from uep.turn.proposer import (
    ProposerRegistry,
    RespondProposer,
    fallback_proposal,
    validate_proposal,
)
from uep.turn.registry import PROPOSERS, create_proposer
from uep.turn.rule_proposer import RuleProposer
from uep.turn.speech_filter import PassMarkerFilter
from uep.turn.wake import AddressConfidence, WakeDetector


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


NAMES = BotIdentityConfig(primary_name="Novalis", aliases=("小諾", "NVA"))
CTX = SessionContext(conversation_id="conv_t", segment_id="seg_t", audience_scope="scope_t", session_ref="sess_t")


def cand(text: str, speaker: str = "u1", *, n: int = 1, state: str | None = None, count: int | None = None,
         addressed: bool = False) -> TurnCandidate:
    return TurnCandidate(
        candidate_id=f"cand_{speaker}_{n}", utterance_refs=(f"evt_{speaker}_{n}",), speakers=(speaker,), ctx=CTX,
        interaction_state=state, interaction_version=0 if state else None, presence_active=True, text=text,
        participant_count=count, addressed_hint=addressed,
    )


def rules_cfg(**over) -> DecisionConfig:
    base = DecisionConfig(
        bot_identity=NAMES,
        gate=GateConfig(context_window_s=1.0),
        engagement=EngagementConfig(group=GroupEngagementConfig(engage_cooldown_s=0.5, silence_trigger_s=100.0)),
    )
    return dataclasses.replace(base, **over)


def joined(users: int, clock: Clock | None = None, cfg: DecisionConfig | None = None) -> tuple[RuleProposer, Clock]:
    clock = clock or Clock()
    p = RuleProposer(cfg or rules_cfg(), clock=clock)
    p.reset(user_count=users)
    return p, clock


# ---------------------------------------------------------------------------
# CouplingModel／ModeController（bot test_voice_mode）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("users, expected", [(1, 0.9), (2, 0.7), (3, 0.5), (4, 0.35)])
def test_coupling_headcount_is_absolute_and_floored(users, expected):
    model = CouplingModel(clock=Clock())
    model.reset(user_count=users)
    assert model.value() == pytest.approx(expected)


def test_coupling_unknown_headcount_has_no_bonus_and_changes_follow_count():
    model = CouplingModel(clock=Clock())
    model.reset(user_count=0)
    assert model.value() == pytest.approx(0.5)
    model.set_user_count(1)
    assert model.value() == pytest.approx(0.9)
    model.set_user_count(3)
    model.set_user_count(2)
    assert model.value() == pytest.approx(0.7)
    model.set_user_count(5)
    assert model.value() == pytest.approx(0.35)  # 人數項負向下限 -0.15


def test_coupling_coefficient_and_reference_come_from_config():
    model = CouplingModel(CouplingModelConfig(headcount_coefficient=0.05), clock=Clock())
    model.reset(user_count=1)
    assert model.value() == pytest.approx(0.6)
    model = CouplingModel(active_max_users=3, clock=Clock())
    model.reset(user_count=3)
    assert model.value() == pytest.approx(0.7)
    model.set_user_count(4)
    assert model.value() == pytest.approx(0.5)


def test_coupling_one_or_two_users_enter_active_three_do_not():
    cfg = ModeConfig()
    for users, active in ((1, True), (2, True), (3, False)):
        model = CouplingModel(active_max_users=cfg.active_max_users, clock=Clock())
        model.reset(user_count=users)
        assert (model.value() >= cfg.active_enter) is active


def test_coupling_topic_up_down_cap_and_clamp():
    model = CouplingModel(clock=Clock())
    model.reset(user_count=3)
    model.on_turn(topic_relevant=True)
    assert model.value() == pytest.approx(0.55)
    model.on_turn(topic_relevant=False)
    assert model.value() == pytest.approx(0.52)
    for _ in range(50):
        model.on_turn(topic_relevant=True)
    assert model.value() == pytest.approx(0.8)
    for _ in range(100):
        model.on_turn(topic_relevant=False)
    assert model.value() == pytest.approx(0.2)
    model.reset(user_count=1)
    for _ in range(50):
        model.on_turn(topic_relevant=True)
    assert model.value() == 1.0


def test_coupling_topic_decays_with_half_life_on_fake_clock():
    clock = Clock()
    model = CouplingModel(CouplingModelConfig(topic_halflife_s=10.0), clock=clock)
    model.reset(user_count=3)
    for _ in range(6):
        model.on_turn(topic_relevant=True)
    high = model.topic_term()
    clock.advance(10.0)
    assert model.topic_term() == pytest.approx(high / 2)
    clock.advance(200.0)
    assert model.value() == pytest.approx(0.5, abs=0.001)


def test_mode_controller_hysteresis_sequence():
    """進入主動要 >= 0.55，但要掉到 < 0.45 才離開：中間地帶維持原狀。"""
    ctl = ModeController()
    assert ctl.mode is ChannelMode.PASSIVE
    steps = [(0.5, "passive"), (0.55, "active"), (0.5, "active"), (0.46, "active"), (0.44, "passive"), (0.5, "passive")]
    assert [ctl.update(coupling, 1).value for coupling, _ in steps] == [expected for _, expected in steps]


@pytest.mark.parametrize("coupling, users, expected", [(0.9, 3, "passive"), (0.9, 0, "passive"), (0.6, 2, "active")])
def test_mode_controller_user_count_limits(coupling, users, expected):
    assert ModeController().update(coupling, users).value == expected


def test_mode_controller_thresholds_come_from_config():
    ctl = ModeController(ModeConfig(active_enter=0.7, active_exit=0.6, active_max_users=1))
    assert ctl.update(0.6, 1) is ChannelMode.PASSIVE
    assert ctl.update(0.7, 1) is ChannelMode.ACTIVE
    assert ctl.update(0.9, 2) is ChannelMode.PASSIVE


@pytest.mark.parametrize("topic, expect_active", [(0.0, True), (-0.12, True), (-0.15, True), (-0.16, False), (-0.3, False)])
def test_two_users_back_to_active_needs_topic_at_least_minus_015(topic, expect_active):
    model = CouplingModel(active_max_users=2, clock=Clock())
    model.reset(user_count=2)
    model._topic = topic
    assert (ModeController(ModeConfig()).update(model.value(), 2) is ChannelMode.ACTIVE) is expect_active


# ---------------------------------------------------------------------------
# WakeDetector（bot test_gate；名字改為虛構）
# ---------------------------------------------------------------------------


def detector(primary="Novalis", aliases=("小諾", "NVA"), **gate) -> WakeDetector:
    return WakeDetector(GateConfig(**gate), BotIdentityConfig(primary_name=primary, aliases=tuple(aliases)))


@pytest.mark.parametrize("text, name", [("小諾你好", "小諾"), ("NVA 在嗎", "NVA"), ("nva 在嗎", "NVA"), ("novalis！", "Novalis")])
def test_wake_exact_match_is_certain_case_insensitive(text, name):
    result = detector().detect(text)
    assert result.confidence_level is AddressConfidence.CERTAIN
    assert result.matched_name == name and result.source == "wake_word"


@pytest.mark.parametrize("text, level", [
    ("Novalix 你在嗎", AddressConfidence.TENTATIVE),  # 距離 1
    ("Novalxx 你在嗎", AddressConfidence.TENTATIVE),  # 距離 2（7 字元名字容許 2）
    ("Novxxxx 你在嗎", AddressConfidence.NONE),  # 距離 4
])
def test_wake_fuzzy_distance_by_name_length(text, level):
    assert detector(aliases=()).detect(text).confidence_level is level


def test_wake_five_char_name_allows_only_distance_one():
    d = detector(primary="Orion", aliases=())
    assert d.detect("Orian 你好").confidence_level is AddressConfidence.TENTATIVE
    assert d.detect("Oriyz 你好").confidence_level is AddressConfidence.NONE


_ALIASES = ("小諾", "NVA", "諾瓦拉", "諾法拉")


@pytest.mark.parametrize("text", ["你可以跟我講一段故事嗎", "你可以跟我簡單介紹一下你自己嗎", "今天天氣不錯我想出去走走"])
def test_wake_ordinary_sentences_never_match_short_aliases(text):
    result = detector(aliases=_ALIASES).detect(text)
    assert result.confidence_level is AddressConfidence.NONE
    assert result.confidence_score == 0.0 and result.matched_name is None


def test_wake_three_char_alias_is_not_fuzzy_unless_config_lowers_threshold():
    assert detector(primary="NVA", aliases=()).detect("NVB 你在嗎").confidence_level is AddressConfidence.NONE
    lowered = detector(primary="NVA", aliases=(), fuzzy_min_name_length=3)
    assert lowered.detect("NVB 你在嗎").confidence_level is AddressConfidence.TENTATIVE


def test_wake_text_shorter_than_name_has_no_window_and_empty_text_is_none():
    d = detector(primary="Novalisation", aliases=())
    assert d.detect("nova").confidence_level is AddressConfidence.NONE
    assert detector().detect("").confidence_level is AddressConfidence.NONE


def test_wake_position_weight_start_beats_end():
    d = detector(primary="Novalis", aliases=())
    start = d.detect("Novalis 你今天過得怎麼樣呢朋友").confidence_score
    middle = d.detect("你今天過得怎麼樣呢朋友 Novalis 你覺得天氣如何呢朋友").confidence_score
    end = d.detect("你今天過得怎麼樣呢朋友你覺得天氣如何呢 Novalis").confidence_score
    assert start == pytest.approx(1.0) and middle == pytest.approx(0.95) and end == pytest.approx(0.90)
    assert start > middle > end


# ---------------------------------------------------------------------------
# ContextTracker（bot test_gate）
# ---------------------------------------------------------------------------


def test_context_window_opens_expires_and_is_sticky_per_speaker():
    clock = Clock()
    tracker = ContextTracker(GateConfig(context_window_s=8.0), clock)
    assert not tracker.is_in_context("u1")
    tracker.on_bot_responded("u1")
    assert tracker.is_in_context("u1") and not tracker.is_in_context("u2")
    clock.advance(7.9)
    assert tracker.is_in_context("u1")
    clock.advance(0.2)
    assert not tracker.is_in_context("u1")


def test_context_cleanup_removes_only_expired_and_snapshot_restore_round_trips():
    clock = Clock()
    tracker = ContextTracker(GateConfig(context_window_s=5.0), clock)
    tracker.on_bot_responded("u1")
    clock.advance(3.0)
    tracker.on_bot_responded("u2")
    saved = tracker.snapshot("u1")
    clock.advance(3.0)
    tracker.cleanup()
    assert not tracker.is_in_context("u1") and tracker.is_in_context("u2")
    tracker.restore("u1", saved)
    assert not tracker.is_in_context("u1")  # 還原的是原本已過期的窗口
    tracker.restore("u2", None)
    assert not tracker.is_in_context("u2")


# ---------------------------------------------------------------------------
# TurnAggregator（bot test_aggregator；merge_gap 1.5／max_wait 3／主動 0.25）
# ---------------------------------------------------------------------------


def seg(text: str, speaker: str = "u1", start: float = 0.0, end: float = 1.0, ref: str = "") -> Segment:
    return Segment(speaker_ref=speaker, text=text, ref=ref or f"evt_{text}", start_ts=start, end_ts=end)


def aggregator(clock: Clock, **over) -> BufferedAggregator:
    return BufferedAggregator(AggregatorConfig(**over), clock)


def test_aggregator_defaults_match_bot():
    cfg = AggregatorConfig()
    assert (cfg.merge_gap_ms, cfg.max_wait_ms, cfg.active_merge_gap_ms, cfg.active_max_wait_ms) == (1500, 3000, 250, 250)
    assert (cfg.max_duration_s, cfg.min_text_length) == (30.0, 2)


def test_aggregator_single_segment_completes_only_after_max_wait():
    clock = Clock()
    agg = aggregator(clock)
    assert agg.feed(seg("你好呀")) == []
    clock.advance(3.0)
    assert agg.tick() == []  # 恰好 3.0s 不算超過
    clock.advance(0.01)
    [turn] = agg.tick()
    assert turn.state is AggregateState.TIMEOUT and turn.speaker_ref == "u1" and "你好呀" in turn.text


def test_aggregator_merges_segments_within_merge_gap_in_order():
    clock = Clock()
    agg = aggregator(clock)
    for i, word in enumerate(["明天", "有", "空嗎"]):
        assert agg.feed(seg(word, start=float(i), end=float(i) + 1)) == []
        clock.advance(1.4)
    clock.advance(3.1)
    [turn] = agg.tick()
    assert turn.text == "明天 有 空嗎" and len(turn.segments) == 3
    assert turn.refs == ("evt_明天", "evt_有", "evt_空嗎")


def test_aggregator_gap_over_merge_gap_returns_previous_turn_and_starts_new():
    clock = Clock()
    agg = aggregator(clock)
    agg.feed(seg("舊的一段"))
    clock.advance(1.51)
    [turn] = agg.feed(seg("新的一段"))
    assert turn.state is AggregateState.COMPLETE and turn.text == "舊的一段"
    assert agg.active_speakers == ["u1"]


def test_aggregator_overflow_on_max_duration():
    clock = Clock()
    agg = aggregator(clock, max_duration_s=5.0)
    agg.feed(seg("這段很長", start=0.0, end=6.0))
    clock.advance(0.1)
    [turn] = agg.feed(seg("繼續說", start=6.0, end=7.0))
    assert turn.state is AggregateState.OVERFLOW and "這段很長" in turn.text


@pytest.mark.parametrize("text, kept", [("一", False), (" 一 ", False), ("好的", True)])
def test_aggregator_min_text_length(text, kept):
    clock = Clock()
    agg = aggregator(clock)
    agg.feed(seg(text))
    clock.advance(3.5)
    assert bool(agg.tick()) is kept


def test_aggregator_speakers_are_isolated_and_flush_all_empties():
    clock = Clock()
    agg = aggregator(clock)
    for speaker, text in (("ua", "甲說"), ("ub", "乙說"), ("uc", "丙說")):
        agg.feed(seg(text, speaker))
    assert sorted(agg.active_speakers) == ["ua", "ub", "uc"]
    turns = agg.flush_all()
    assert {t.speaker_ref for t in turns} == {"ua", "ub", "uc"} and all(t.state is AggregateState.FLUSHED for t in turns)
    assert agg.active_speakers == []
    agg.feed(seg("測試文字"))
    agg.clear()
    clock.advance(10)
    assert agg.tick() == []


def test_aggregator_active_mode_uses_short_thresholds_and_applies_to_waiting_turns():
    clock = Clock()
    agg = aggregator(clock)
    agg.feed(seg("你可以跟我講一段故事嗎"))
    clock.advance(0.3)
    assert agg.tick() == []  # 被動：仍在等 3s
    agg.set_fast(True)  # 門檻在檢查當下才讀取：等待中的 turn 立刻套用新值
    [turn] = agg.tick()
    assert turn.state is AggregateState.TIMEOUT
    agg.feed(seg("第一句"))
    clock.advance(0.26)
    [prev] = agg.feed(seg("第二句"))  # 主動模式 merge_gap 0.25：超過就切開
    assert prev.text == "第一句"


def test_aggregator_called_turn_does_not_wait_in_passive_mode():
    clock = Clock()
    wake = detector()
    agg = aggregator(clock)
    agg.set_urgent_predicate(wake.is_called)
    agg.feed(seg("小諾你覺得呢", "u1"))
    agg.feed(seg("今天你們有沒有看到那個影片", "u2"))
    clock.advance(0.26)
    [turn] = agg.tick()  # 被叫到的 turn 只等 active_max_wait；沒叫到的繼續等 3s
    assert turn.speaker_ref == "u1"
    assert agg.active_speakers == ["u2"]


def test_aggregator_urgent_predicate_failure_falls_back_to_normal_wait():
    clock = Clock()
    agg = aggregator(clock)

    def boom(_text: str) -> bool:
        raise RuntimeError("壞了")

    agg.set_urgent_predicate(boom)
    agg.feed(seg("你好呀"))
    clock.advance(0.5)
    assert agg.tick() == []


def test_passthrough_aggregator_is_one_turn_per_segment():
    agg = PassThroughAggregator()
    [turn] = agg.feed(seg("你好"))
    assert turn.text == "你好" and agg.tick() == [] and agg.flush_all() == []


# ---------------------------------------------------------------------------
# EngagementMonitor（bot test_engagement 的 group 部分）
# ---------------------------------------------------------------------------


def observed(i: int, speaker: str = "u1") -> ObservedTurn:
    return ObservedTurn(f"k{i}", speaker, f"第{i}句", (f"e{i}",))


def monitor(clock: Clock) -> EngagementMonitor:
    return EngagementMonitor(EngagementConfig(group=GroupEngagementConfig(engage_cooldown_s=40.0)), clock)


def test_engagement_initial_confidence_zero_does_not_engage():
    mon = monitor(Clock())
    assert not mon.evaluate().should_engage and mon.evaluate().reason == "low_confidence"


def test_engagement_confidence_threshold_needs_enough_turns_and_cooldown():
    clock = Clock()
    mon = monitor(clock)
    for i in range(3):
        mon.on_turn(observed(i), is_addressed=False)
    assert mon.context_confidence == pytest.approx(0.45) and not mon.evaluate().should_engage
    mon.on_turn(observed(3), is_addressed=False)
    decision = mon.evaluate()
    assert decision.should_engage and decision.reason == "context_ready" and len(decision.context) == 4
    mon.mark_dispatched()
    for i in range(4):
        mon.on_turn(observed(10 + i), is_addressed=False)
    assert not mon.evaluate().should_engage  # 冷卻 40s
    clock.advance(40.0)
    assert mon.evaluate().should_engage


def test_engagement_addressed_turn_accumulates_more_and_bot_response_resets():
    mon = monitor(Clock())
    mon.on_turn(observed(0), is_addressed=True)
    assert mon.context_confidence == pytest.approx(0.25)
    mon.on_bot_responded()
    assert mon.context_confidence == 0.0


def test_engagement_switching_to_passive_resets_confidence_and_active_ignores_it():
    mon = monitor(Clock())
    mon.set_active(True)
    mon.on_turn(observed(0), is_addressed=False)
    assert mon.context_confidence == 0.0  # 主動模式沒有信心值
    mon.set_active(False)
    mon.on_turn(observed(1), is_addressed=False)
    assert mon.context_confidence == pytest.approx(0.15)
    mon.set_active(True)
    mon.set_active(False)
    assert mon.context_confidence == 0.0


def test_engagement_tick_decays_and_silence_trigger_and_buffer_cap():
    clock = Clock()
    mon = monitor(clock)
    mon.on_turn(observed(0), is_addressed=True)
    clock.advance(20.0)
    mon.tick()
    assert mon.context_confidence == pytest.approx(0.25 * 0.9 ** 2)
    clock.advance(10.0)  # silence_trigger_s 預設 10
    assert mon.evaluate().reason == "silence_trigger"
    for i in range(30):
        mon.on_turn(observed(i), is_addressed=False)
    assert len(mon.peek_buffer()) == 20


@pytest.mark.parametrize("text, meaningful", [
    ("嗯", False), ("哇", False), ("好棒", False), ("我今天遇到一件事", True), ("ok", False), ("你好嗎", True),
])
def test_engagement_meaningfulness_filter(text, meaningful):
    assert monitor(Clock()).is_meaningful(text) is meaningful


# ---------------------------------------------------------------------------
# RuleProposer：reply_mode 對照（bot trigger -> Core Proposal）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("users, mode", [(1, ChannelMode.ACTIVE), (2, ChannelMode.ACTIVE), (3, ChannelMode.PASSIVE)])
def test_join_mode_by_headcount_and_listener_is_notified(users, mode):
    seen: list[ChannelMode] = []
    p = RuleProposer(rules_cfg(), clock=Clock(), on_mode_change=seen.append)
    p.reset(user_count=users)
    assert p.channel_mode is mode
    assert seen == ([mode] if mode is ChannelMode.ACTIVE else [])


def test_active_mode_responds_to_every_meaningful_turn_without_name():
    p, _ = joined(1)
    proposal = p.propose(cand("你可以跟我講一段故事嗎"))
    assert (proposal.reply_mode, proposal.reason_code, proposal.addressed) == (ReplyMode.RESPOND, "active", True)
    assert proposal.proposer_id == "rules.default" and validate_proposal(proposal) is None


def test_active_mode_filler_is_pass_and_does_not_lower_coupling():
    p, _ = joined(1)
    before = p.coupling_value
    for text in ("嗯", "哈", "好棒"):
        proposal = p.propose(cand(text))
        assert (proposal.reply_mode, proposal.reason_code) == (ReplyMode.PASS, "not_meaningful")
    assert p.coupling_value == pytest.approx(before)


def test_active_mode_fuzzy_name_is_addressed_not_a_probe():
    p, _ = joined(1)
    proposal = p.propose(cand("Novalix 你有聽到嗎"))
    assert (proposal.reply_mode, proposal.reason_code) == (ReplyMode.RESPOND, "active")


def test_active_mode_turn_does_not_gain_coupling_before_the_reply_result():
    p, _ = joined(2)
    before = p.topic_term
    p.propose(cand("我們今天晚上去吃拉麵好不好"))
    assert p.topic_term == before and p.pending_topics == 1


def test_called_in_passive_channel_responds_without_active_mode():
    p, _ = joined(3)
    proposal = p.propose(cand("小諾 你好，今天天氣如何？"))
    assert (proposal.reply_mode, proposal.reason_code) == (ReplyMode.RESPOND, "called")
    assert p.channel_mode is ChannelMode.PASSIVE


def test_passive_unaddressed_turns_are_pass_observe_and_only_buffer():
    p, _ = joined(3)
    for i, speaker in enumerate(("u2", "u3")):
        proposal = p.propose(cand("今天你們有沒有看到那個影片", speaker, n=i))
        assert (proposal.reply_mode, proposal.reason_code) == (ReplyMode.PASS, "observe")
    assert len(p._engagement.peek_buffer()) == 2


def test_called_hands_over_the_whole_buffer_once():
    p, _ = joined(3)
    p.propose(cand("今天你們有沒有看到那個影片", "u2", n=1))
    p.propose(cand("我看到了，真的很好笑", "u3", n=2))
    called = cand("小諾 你覺得呢？", "u1", n=3)
    assert p.propose(called).reply_mode is ReplyMode.RESPOND
    assert len(p._engagement.peek_buffer()) == 3  # 提議本身是純讀：還沒被採用，buffer 不會被吞掉
    started(p, called)  # 被採用（ES 開始 think）才取走 buffer 並套用冷卻
    assert p._engagement.peek_buffer() == []  # 交出去的 turn 不會再被交一次


def test_tentative_name_in_passive_is_pass_not_a_probe():
    """偏離 bot：bot 在這裡 probe（「剛剛有叫我嗎？」）；Core 沒有 probe 機制，當成旁聽。"""
    p, _ = joined(3)
    proposal = p.propose(cand("Novalix 那你覺得接下來呢", "u1"))
    assert (proposal.reply_mode, proposal.reason_code) == (ReplyMode.PASS, "tentative_unconfirmed")


def test_third_user_forces_passive_and_decrease_recovers_active():
    p, _ = joined(1)
    assert p.channel_mode is ChannelMode.ACTIVE
    p.set_participant_count(3)
    assert p.channel_mode is ChannelMode.PASSIVE
    p.set_participant_count(1)
    assert p.channel_mode is ChannelMode.ACTIVE


def test_candidate_participant_count_syncs_mode():
    p, _ = joined(1)
    p.propose(cand("今天天氣不錯我想出去走走", count=3))
    assert p.channel_mode is ChannelMode.PASSIVE


def test_three_to_two_users_returns_to_active_after_passive_chatter():
    p, _ = joined(3)
    for _ in range(4):
        p._coupling.on_turn(topic_relevant=False)  # 旁人閒聊：話題項 -0.12
    p.set_participant_count(2)  # 0.5 + 0.2 - 0.12 = 0.58
    assert p.channel_mode is ChannelMode.ACTIVE


def test_mode_override_locks_and_releases():
    p, _ = joined(3)
    assert p.set_mode_override(ChannelMode.ACTIVE) is ChannelMode.ACTIVE
    p.set_participant_count(5)
    assert p.channel_mode is ChannelMode.ACTIVE  # 鎖定忽略人數
    assert p.set_mode_override(None) is ChannelMode.PASSIVE


def started(p: RuleProposer, c: TurnCandidate, turn: str = "t") -> None:
    p.on_turn_started(turn, c.candidate_id, c.speakers)


def finish(p: RuleProposer, c: TurnCandidate, status: str = "completed", *, end_reason=None, barge_in=False,
           ok: bool = True, reason: str = "x") -> None:
    p.on_turn_outcome(TurnOutcomeFacts(
        turn_id="t", candidate_id=c.candidate_id, speakers=c.speakers, reply_mode="respond", reason_code=reason,
        status=status, end_reason=end_reason, generation_ok=ok, barge_in=barge_in,
    ))


def test_followup_during_generation_is_context_passive_not_a_probe():
    p, _ = joined(3)
    first = cand("小諾 你好嗎", "u1", n=1)
    p.propose(first)
    started(p, first)
    proposal = p.propose(cand("Novalix 那你覺得接下來呢", "u1", n=2))
    assert (proposal.reply_mode, proposal.reason_code) == (ReplyMode.RESPOND, "context_passive")


def test_followup_within_window_after_reply_is_context_then_window_expires():
    p, clock = joined(3)
    first = cand("小諾 你好嗎", "u1", n=1)
    p.propose(first)
    started(p, first)
    finish(p, first)
    assert p.propose(cand("Novalix 那你覺得接下來呢", "u1", n=2)).reason_code == "context_passive"
    clock.advance(1.1)  # context_window_s=1.0（測試設定）
    assert p.propose(cand("Novalix 那你覺得接下來呢", "u1", n=3)).reason_code == "tentative_unconfirmed"


def test_context_in_passive_mode_still_called_by_name_is_called():
    p, _ = joined(3)
    first = cand("小諾 你好嗎", "u1", n=1)
    p.propose(first)
    started(p, first)
    assert p.propose(cand("小諾 那你覺得呢", "u1", n=2)).reason_code == "called"


def test_other_speaker_is_not_in_context_window():
    p, _ = joined(3)
    first = cand("小諾 你好嗎", "u1", n=1)
    p.propose(first)
    started(p, first)
    assert p.propose(cand("今天你們有沒有看到那個影片", "u2", n=2)).reason_code == "observe"


def test_active_mode_context_responds_as_context_and_defers_topic():
    p, _ = joined(1)
    first = cand("小諾 你好嗎", "u1", n=1)
    p.propose(first)
    started(p, first)
    proposal = p.propose(cand("我今天遇到一件很有趣的事情", "u1", n=2))
    assert (proposal.reply_mode, proposal.reason_code) == (ReplyMode.RESPOND, "context")


def test_response_failed_clears_the_responding_flag():
    p, clock = joined(3)
    first = cand("小諾 你好嗎", "u1", n=1)
    p.propose(first)
    started(p, first)
    finish(p, first, "failed", ok=False)
    assert "u1" not in p._responding and p.pending_topics == 0
    clock.advance(1.1)  # 失敗不還原窗口（與 bot 相同）；窗口過期後就不再是延續對話
    assert p.propose(cand("今天你們有沒有看到那個影片", "u1", n=2)).reason_code == "observe"


def test_pass_restores_the_context_window_opened_at_start():
    p, _ = joined(3)
    first = cand("小諾 你好嗎", "u1", n=1)
    p.propose(first)
    started(p, first)
    finish(p, first, "skipped", end_reason="pass")  # LLM PASS：還原開始時開的窗口（原本沒有）
    # 沒有窗口、沒叫名字 -> 旁聽
    assert p.propose(cand("今天你們有沒有看到那個影片", "u1", n=2)).reason_code == "observe"


# ---- 耦合度結算（bot test_coupling_active_pass）----


def chat(p: RuleProposer, n: int, speaker: str = "u1") -> TurnCandidate:
    c = cand("我們今天晚上去吃拉麵好不好", speaker, n=n)
    assert p.propose(c).reason_code in ("active", "context")  # 回覆過的人在窗口內是 context，其餘是 active
    return c


def pass_it(p: RuleProposer, c: TurnCandidate) -> None:
    started(p, c)
    finish(p, c, "skipped", end_reason="pass", ok=True)


def test_real_reply_adds_topic():
    p, _ = joined(2)
    c = chat(p, 1)
    started(p, c)
    finish(p, c)
    assert p.topic_term > 0.04  # +topic_up（0.05），扣掉極短衰減


def test_two_users_repeated_pass_drops_to_passive():
    p, _ = joined(2)
    for i in range(8):
        pass_it(p, chat(p, i, "u1" if i % 2 else "u2"))
    assert p.channel_mode is ChannelMode.ACTIVE  # 8 次 PASS 仍高於 active_exit(0.45)
    for i in range(8, 12):
        if p.channel_mode is not ChannelMode.ACTIVE:
            break
        pass_it(p, chat(p, i))
    assert p.channel_mode is ChannelMode.PASSIVE and p.coupling_value < 0.45


def test_reply_turns_offset_pass_turns_and_single_user_stays_active():
    p, _ = joined(2)
    for i in range(20):
        c = chat(p, i)
        if i % 2:
            pass_it(p, c)
        else:
            started(p, c)
            finish(p, c)
    assert p.channel_mode is ChannelMode.ACTIVE
    solo, _ = joined(1)
    for i in range(12):
        pass_it(solo, chat(solo, i))
    assert solo.channel_mode is ChannelMode.ACTIVE and solo.coupling_value >= 0.6 - 1e-6


@pytest.mark.parametrize("status, end_reason", [("failed", None), ("cancelled", "presence_lost"), ("voided", "yielded"),
                                                ("dropped_busy", "dropped_busy"), ("skipped", "mode_silent")])
def test_dropped_or_failed_reply_is_neutral(status, end_reason):
    p, _ = joined(2)
    c = chat(p, 1)
    started(p, c)
    finish(p, c, status, end_reason=end_reason, ok=False)
    assert p.topic_term == 0.0 and p.pending_topics == 0


# ---- 被動模式插話（bot TestPassiveMode）----


def feed_chatter(p: RuleProposer, texts, start: int = 0) -> None:
    for i, text in enumerate(texts):
        speaker = f"u{(start + i) % 3 + 1}"
        assert p.propose(cand(text, speaker, n=start + i)).reason_code == "observe"


CHATTER = ["你們覺得這部電影怎麼樣", "我覺得劇情有點拖", "但是配樂真的很棒", "特別是最後那一段"]


def test_interjects_once_then_cooldown_and_clears_buffer():
    p, clock = joined(3)
    feed_chatter(p, CHATTER)
    [ij] = p.tick()
    assert ij.proposal.reply_mode is ReplyMode.RESPOND and ij.proposal.reason_code == "interject_context"
    assert ij.text == CHATTER[-1] and set(ij.speakers) == {"u1", "u2", "u3"}
    assert len(p._engagement.peek_buffer()) == 4  # 還沒被採用：buffer 與冷卻都沒動
    assert p.tick() == []  # 已提出、尚未採用：不重複提出
    p.on_turn_started("t_ij", "tick:1", ij.speakers)
    assert p._engagement.peek_buffer() == []  # 被採用後才取走這批、套用冷卻
    assert p.tick() == []  # 回到觀察狀態；不會對同一批 turn 再觸發
    p.propose(cand("我也這麼覺得，下次再一起看", "u2", n=99))  # 插話對象都有 context window，這句算接話
    assert p.tick() == []
    clock.advance(0.6)  # 冷卻過後但新 turn 不足
    assert p.tick() == []


def test_no_repeat_interjection_while_generating():
    p, clock = joined(3)
    feed_chatter(p, CHATTER)
    [ij] = p.tick()
    for _ in range(5):  # 插話提出後、開始 think 前：tick 不會對同一批 turn 再觸發
        clock.advance(0.05)
        assert p.tick() == []
    p.on_turn_started("t_ij", "tick:1", ij.speakers)
    for _ in range(5):  # 生成期間同樣不會
        clock.advance(0.05)
        assert p.tick() == []


def test_interjection_silence_trigger_after_quiet_period():
    cfg = rules_cfg(engagement=EngagementConfig(group=GroupEngagementConfig(engage_cooldown_s=0.5,
                                                                            silence_trigger_s=10.0,
                                                                            interject_min_new_turns=1)))
    p, clock = joined(3, cfg=cfg)
    feed_chatter(p, ["今天天氣不錯我想出去走走"])
    assert p.tick() == []
    clock.advance(10.0)
    [ij] = p.tick()
    assert ij.proposal.reason_code == "interject_silence"


def test_interjection_prefilter_skips_when_new_turns_are_too_few():
    cfg = rules_cfg(engagement=EngagementConfig(group=GroupEngagementConfig(engage_cooldown_s=0.5,
                                                                            silence_trigger_s=10.0)))
    p, clock = joined(3, cfg=cfg)
    feed_chatter(p, ["今天天氣不錯我想出去走走"])
    clock.advance(10.0)
    assert p.tick() == []  # 沉默觸發成立但只有 1 個新 turn（< interject_min_new_turns=3）：不插話、不套冷卻
    feed_chatter(p, ["然後我們去走走吧", "好啊那就這樣"], start=5)
    clock.advance(10.0)
    assert len(p.tick()) == 1  # 新 turn 夠了，下個 tick 就放行


def test_interjection_opens_a_window_for_every_buffered_speaker():
    p, _ = joined(3)
    feed_chatter(p, CHATTER)
    [ij] = p.tick()
    p.on_turn_started("t_ij", "tick:1", ij.speakers)
    proposal = p.propose(cand("Novalix 你剛剛說的是什麼意思", "u3", n=50))
    assert proposal.reason_code == "context_passive"


def test_active_mode_has_no_interjection():
    p, _ = joined(1)
    assert p.tick() == []


# ---- Q-1：插話後冷卻 ----


def test_barge_in_cooldown_blocks_passive_class_triggers_but_not_called_or_active():
    cfg = rules_cfg(barge_in_cooldown_s=8.0)
    p, clock = joined(3, cfg=cfg)
    first = cand("小諾 你好嗎", "u1", n=1)
    p.propose(first)
    started(p, first)
    finish(p, first, "cancelled", end_reason="barge_in", barge_in=True, ok=True)
    follow = p.propose(cand("Novalix 那你覺得呢", "u1", n=2))
    assert follow.reason_code in ("context_passive", "barge_in_cooldown")
    # 重新開窗口後（回覆過），冷卻內的 context_passive 改為 pass
    second = cand("小諾 再問一件事", "u1", n=3)
    assert p.propose(second).reason_code == "called"  # 被點名不受冷卻影響
    started(p, second, "t2")
    assert p.propose(cand("然後呢", "u1", n=4)).reason_code == "barge_in_cooldown"
    for i, text in enumerate(CHATTER):
        assert p.propose(cand(text, f"u{i % 2 + 2}", n=20 + i)).reason_code == "observe"  # 只有 u2、u3，不在窗口內
    assert p.tick() == []  # 冷卻內不插話
    clock.advance(8.1)
    assert len(p.tick()) == 1  # 冷卻過後同一批 turn 就能插話：剛才被擋的原因確實是冷卻


def test_barge_in_counts_as_responded_for_coupling_and_cooldown_only_applies_to_passive_channel():
    p, _ = joined(1)
    c = chat(p, 1)
    started(p, c)
    finish(p, c, "cancelled", end_reason="barge_in", barge_in=True)
    assert p.topic_term > 0.04
    assert p.propose(cand("我今天遇到一件很有趣的事情", "u1", n=2)).reason_code == "context"  # 主動頻道不受冷卻影響


# ---- 介面：interaction_to 與驗證 ----


def test_pass_in_idle_requests_listening_and_other_states_do_not():
    p, _ = joined(3)
    assert p.propose(cand("今天你們有沒有看到那個影片", "u2", n=1, state="IDLE")).interaction_to == "LISTENING"
    assert p.propose(cand("今天你們有沒有看到那個影片", "u2", n=2, state="THINKING")).interaction_to is None
    assert p.propose(cand("小諾 你好", "u2", n=3, state="IDLE")).interaction_to is None


def test_listener_continuation_is_active_mode_and_meaningful_only():
    p, _ = joined(1)
    assert p.is_listener_continuation("我今天遇到一件很有趣的事情") and not p.is_listener_continuation("嗯")
    p.set_participant_count(3)
    assert not p.is_listener_continuation("我今天遇到一件很有趣的事情")


# ---------------------------------------------------------------------------
# validate_proposal／fallback_proposal／registry／PASS filter
# ---------------------------------------------------------------------------


GOOD = Proposal(reply_mode=ReplyMode.RESPOND, reason_code="called", proposer_id="x.y", proposer_version="1")


@pytest.mark.parametrize("change, bad", [
    ({}, None),
    ({"reply_mode": "respond"}, "reply_mode"),
    ({"route": "shell"}, "route"),
    ({"interaction_to": "WORK"}, "interaction_to"),  # Behavior 軸的值
    ({"interaction_to": "MISCHIEF"}, "interaction_to"),
    ({"interaction_to": "LISTENING"}, None),
    ({"confidence": 1.5}, "confidence"),
    ({"confidence": True}, "confidence"),
    ({"addressed": "yes"}, "addressed"),
    ({"reason_code": "含 中文 與空白"}, "identifier"),
    ({"reason_code": "x" * 65}, "identifier"),
    ({"proposer_id": ""}, "identifier"),
])
def test_validate_proposal_table(change, bad):
    assert validate_proposal(dataclasses.replace(GOOD, **change)) == bad


def test_validate_proposal_rejects_non_proposal():
    assert validate_proposal({"reply_mode": "respond"}) == "not_a_proposal"
    assert validate_proposal(None) == "not_a_proposal"


@pytest.mark.parametrize("count, addressed, mode", [
    (None, False, ReplyMode.RESPOND),  # 人數未知視為單人
    (1, False, ReplyMode.RESPOND),
    (3, True, ReplyMode.RESPOND),
    (3, False, ReplyMode.PASSIVE),  # 多人且未被點名 -> passive
    (2, False, ReplyMode.PASSIVE),
])
def test_fallback_proposal_d80_table(count, addressed, mode):
    proposal = fallback_proposal(cand("你好", count=count, addressed=addressed), "proposer_timeout")
    assert proposal.reply_mode is mode and proposal.reason_code == "proposer_timeout"
    assert validate_proposal(proposal) is None


def test_registry_creates_by_id_and_rejects_duplicates_and_unknown():
    assert {"respond.minimal", "rules.default"} <= set(PROPOSERS.ids())
    assert isinstance(create_proposer("respond.minimal"), RespondProposer)
    assert isinstance(create_proposer("rules.default", clock=Clock()), RuleProposer)
    reg = ProposerRegistry()
    reg.register("x.one", lambda **_: RespondProposer())
    with pytest.raises(ValueError):
        reg.register("x.one", lambda **_: RespondProposer())
    reg.register("x.one", lambda **_: RespondProposer(), replace=True)
    with pytest.raises(KeyError):
        reg.create("missing")
    with pytest.raises(ValueError):
        reg.register("含 空白", lambda **_: RespondProposer())


@pytest.mark.parametrize("sentences, spoken, passed", [
    (["[PASS]"], [None], True),
    (["  [pass] 我沒有要說", "後面的句子"], [None, None], True),
    (["你好。", "[PASS]"], ["你好。", None], False),  # 已經唸出字之後不再當成 PASS；標記單獨成句，剝掉後沒有可唸的文字
    (["好的 [PASS] 再見"], ["好的  再見"], False),
    ([""], [None], False),
])
def test_pass_marker_filter(sentences, spoken, passed):
    flt = PassMarkerFilter()
    assert [flt.feed(s) for s in sentences] == spoken
    assert flt.passed is passed
