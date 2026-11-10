"""敘述中保持聆聽（narrative hold）的單元測試：行為對照 bot ``echo_decision/aggregator/turn_buffer.py``。

純記憶體（假時鐘），不需要 PostgreSQL。涵蓋：連續 2 段／講滿 6 秒觸發 hold、安靜 2.5 秒才交出、
聲音活動起算、喚醒詞不等、被動模式不生效、一般單句與回覆後重新起算不受影響，以及宿主的事件接線。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from uep.echo.host import EchoPipelineHost
from uep.turn.aggregator import AggregateState, BufferedAggregator, Segment
from uep.turn.decision_config import AggregatorConfig
from uep.turn.orchestrator import TurnOrchestrator

ALICE = "discord:alice"
BOB = "discord:bob"


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def seg(speaker: str = ALICE, text: str = "我今天去了很多地方", speech_s: float = 1.0, ref: str = "") -> Segment:
    """一段已定稿的 utterance；``speech_s`` 為音訊長度（0 = 沒有音訊長度，退回依文字估算）。"""
    return Segment(speaker_ref=speaker, text=text, ref=ref or text, start_ts=0.0, end_ts=speech_s)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def agg(clock: FakeClock) -> BufferedAggregator:
    a = BufferedAggregator(AggregatorConfig(), clock=clock)
    a.set_fast(True)
    return a


# ---- 預設值（bot echo_decision/config.py:40-45）----

def test_defaults_match_bot_config() -> None:
    cfg = AggregatorConfig()
    assert cfg.narrative_enabled is True  # config.py:40
    assert cfg.narrative_gap_s == 2.0  # config.py:41
    assert cfg.narrative_min_speech_s == 6.0  # config.py:42
    assert cfg.narrative_min_segments == 2  # config.py:43
    assert cfg.narrative_end_s == 2.5  # config.py:44
    assert cfg.narrative_max_duration_s == 120.0  # config.py:45


# ---- 觸發 ----

def test_two_consecutive_segments_hold_until_2_5s_quiet(agg: BufferedAggregator, clock: FakeClock) -> None:
    assert agg.feed(seg(text="第一段話")) == []
    clock.advance(1.2)
    assert agg.feed(seg(text="第二段話")) == []  # 段間停頓 0.2s <= 2.0：接續，同一串
    assert agg.is_narrating(ALICE)
    clock.advance(0.3)  # 一般主動模式 250ms 早該交出
    assert agg.tick() == []
    clock.advance(2.1)  # 距最後一段 2.4s
    assert agg.tick() == []
    clock.advance(0.2)  # 距最後一段 2.6s >= 2.5
    [turn] = agg.tick()
    assert turn.text == "第一段話 第二段話" and len(turn.segments) == 2
    assert turn.state is AggregateState.TIMEOUT and turn.narrative is True
    assert not agg.is_narrating(ALICE)


def test_single_segment_of_6s_enters_narrating(agg: BufferedAggregator, clock: FakeClock) -> None:
    assert agg.feed(seg(text="一口氣講了很久的一段話", speech_s=6.0)) == []
    assert agg.narrating_speakers() == [ALICE]
    clock.advance(0.5)
    assert agg.tick() == []
    clock.advance(2.1)  # 2.6s
    [turn] = agg.tick()
    assert turn.narrative is True and len(turn.segments) == 1


def test_just_under_6s_single_segment_is_a_normal_turn(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.feed(seg(speech_s=5.9))
    assert not agg.is_narrating(ALICE)
    clock.advance(0.3)  # 一般主動模式 250ms 門檻
    [turn] = agg.tick()
    assert turn.narrative is False and turn.state is AggregateState.TIMEOUT


def test_speech_accumulates_across_chain(agg: BufferedAggregator, clock: FakeClock) -> None:
    """min_segments 調高後，累積語音 >= 6s 仍會觸發（兩個條件是 OR）。"""
    a = BufferedAggregator(AggregatorConfig(narrative_min_segments=99), clock=clock)
    a.set_fast(True)
    a.feed(seg(speech_s=3.0))
    assert not a.is_narrating(ALICE)
    clock.advance(3.0)  # (now - 3.0) - 上次抵達 = 0 <= 2
    a.feed(seg(speech_s=3.0))
    assert a.is_narrating(ALICE)


def test_text_length_fallback_when_no_audio_duration(agg: BufferedAggregator) -> None:
    """沒有音訊長度時用文字估算（每秒 4 字）：24 字 = 6s。"""
    agg.feed(seg(text="一二三四五六七八九十" * 2 + "一二三四", speech_s=0.0))
    assert agg.is_narrating(ALICE)


def test_normal_single_sentence_unaffected(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.feed(seg(text="今天天氣好", speech_s=1.5))
    assert not agg.is_narrating(ALICE)
    clock.advance(0.2)
    assert agg.tick() == []
    clock.advance(0.1)  # 0.3s > active_max_wait 0.25s
    [turn] = agg.tick()
    assert turn.text == "今天天氣好" and turn.narrative is False


def test_pause_longer_than_gap_breaks_chain(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.feed(seg(text="第一段話", speech_s=1.0))
    clock.advance(5.0)  # (5.0 - 1.0) - 0 = 4 > 2.0：斷串
    done = agg.feed(seg(text="新的一句話", speech_s=1.0))
    assert [t.text for t in done] == ["第一段話"] and done[0].state is AggregateState.COMPLETE
    assert not agg.is_narrating(ALICE)


def test_reply_restarts_chain(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.feed(seg(text="第一段話"))
    clock.advance(0.5)
    agg.note_replied(ALICE)
    clock.advance(0.7)
    agg.feed(seg(text="回覆後的話"))  # 回覆之後是新的一輪：不接續
    assert not agg.is_narrating(ALICE)


def test_forgotten_reply_does_not_break_chain(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.feed(seg(text="第一段話"))
    clock.advance(0.5)
    agg.note_replied(ALICE)
    agg.forget_replied(ALICE)  # 回覆作廢：對方沒聽到
    clock.advance(0.7)
    agg.feed(seg(text="第二段話"))
    assert agg.is_narrating(ALICE)


def test_chain_is_per_speaker(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.feed(seg(ALICE, "愛麗絲說話"))
    clock.advance(1.0)
    agg.feed(seg(BOB, "鮑伯說話"))
    assert agg.narrating_speakers() == []  # 不同人不算連續
    clock.advance(0.3)
    assert {t.speaker_ref for t in agg.tick()} == {ALICE, BOB}


# ---- 喚醒詞 ----

def test_wake_word_in_latest_segment_releases_narrating_immediately(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.set_urgent_predicate(lambda text: "UEP" in text)
    agg.feed(seg(text="我今天去了很多地方"))
    clock.advance(1.0)
    [turn] = agg.feed(seg(text="UEP 你覺得呢"))
    assert turn.narrative is True and turn.state is AggregateState.COMPLETE and len(turn.segments) == 2


def test_wake_word_in_first_long_segment_releases_immediately(agg: BufferedAggregator) -> None:
    agg.set_urgent_predicate(lambda text: "UEP" in text)
    [turn] = agg.feed(seg(text="UEP 我想跟你說一長串事情", speech_s=7.0))
    assert turn.narrative is True


def test_wake_word_does_not_matter_for_normal_single_sentence(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.set_urgent_predicate(lambda text: "UEP" in text)
    assert agg.feed(seg(text="UEP 你好", speech_s=1.0)) == []  # 非敘述：走原本的短門檻
    clock.advance(0.3)
    [turn] = agg.tick()
    assert turn.narrative is False


def test_predicate_failure_is_treated_as_not_called(agg: BufferedAggregator) -> None:
    def boom(_text: str) -> bool:
        raise RuntimeError("x")

    agg.set_urgent_predicate(boom)
    assert agg.feed(seg(speech_s=7.0)) == [] and agg.is_narrating(ALICE)


# ---- 被動模式 ----

def test_passive_mode_has_no_narrating(clock: FakeClock) -> None:
    a = BufferedAggregator(AggregatorConfig(), clock=clock)  # 預設 fast=False
    a.feed(seg(text="第一段話", speech_s=7.0))
    clock.advance(1.0)
    a.feed(seg(text="第二段話", speech_s=1.0))
    assert not a.is_narrating(ALICE) and a.narrating_speakers() == []
    clock.advance(2.9)
    assert a.tick() == []  # 沿用被動 max_wait 3s
    clock.advance(0.2)
    [turn] = a.tick()
    assert turn.narrative is False and turn.text == "第一段話 第二段話"


def test_passive_mode_does_not_track_chain_before_switching(clock: FakeClock) -> None:
    a = BufferedAggregator(AggregatorConfig(), clock=clock)
    a.feed(seg(speech_s=1.0))
    clock.advance(1.0)
    a.set_fast(True)  # 切成主動：被動時沒有發言串，下一段是新串的第一段
    a.feed(seg(speech_s=1.0))
    assert not a.is_narrating(ALICE)


def test_switch_to_passive_while_narrating_falls_back_to_normal_wait(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.feed(seg(speech_s=7.0))
    assert agg.is_narrating(ALICE)
    agg.set_fast(False)
    assert not agg.is_narrating(ALICE)
    clock.advance(2.9)
    assert agg.tick() == []
    clock.advance(0.2)
    [turn] = agg.tick()
    assert turn.narrative is True  # buffer 仍是整串合併的結果


def test_narrative_disabled(clock: FakeClock) -> None:
    a = BufferedAggregator(AggregatorConfig(narrative_enabled=False), clock=clock)
    a.set_fast(True)
    a.feed(seg(speech_s=7.0))
    assert not a.is_narrating(ALICE)


# ---- 聲音活動與保險 ----

def test_quiet_is_measured_from_last_voice_not_last_segment(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.feed(seg(speech_s=7.0))
    clock.advance(1.5)
    agg.note_voice(ALICE)  # 段落文字抵達前，對方又開口了
    clock.advance(2.4)  # 距最後一段 3.9s，但距最後出聲 2.4s
    assert agg.tick() == []
    clock.advance(0.2)
    assert len(agg.tick()) == 1


def test_stale_voice_before_segment_is_ignored(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.note_voice(ALICE)
    clock.advance(30.0)  # 最後出聲早於最後一段抵達超過 15s：不可信
    agg.feed(seg(speech_s=7.0))
    clock.advance(2.6)
    assert len(agg.tick()) == 1


def test_hold_cap_releases_even_if_voice_never_stops(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.feed(seg(speech_s=7.0))
    for _ in range(22):  # 持續偵測到聲音
        clock.advance(1.0)
        agg.note_voice(ALICE)
        assert agg.tick() == []
    clock.advance(0.6)  # 22.6s >= 2.5 + 20
    agg.note_voice(ALICE)
    assert len(agg.tick()) == 1


def test_voice_of_other_speaker_does_not_extend_hold(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.feed(seg(ALICE, speech_s=7.0))
    clock.advance(1.0)
    agg.note_voice(BOB)
    clock.advance(1.6)
    assert [t.speaker_ref for t in agg.tick()] == [ALICE]


def test_narrative_duration_cap_overflows(clock: FakeClock) -> None:
    a = BufferedAggregator(AggregatorConfig(max_duration_s=5.0, narrative_max_duration_s=10.0), clock=clock)
    a.set_fast(True)
    a.feed(Segment(ALICE, "一段很長的話", "r1", 0.0, 9.0))
    clock.advance(1.0)
    assert a.feed(Segment(ALICE, "接著說", "r2", 9.0, 10.0)) == []  # 累積 10s 以前都還在同一串（含 narrative cap）
    clock.advance(1.0)
    done = a.feed(Segment(ALICE, "還在說", "r3", 10.0, 11.0))
    assert len(done) == 1 and done[0].state is AggregateState.OVERFLOW and done[0].narrative is True
    assert a.is_narrating(ALICE)  # 新 buffer 延續敘述


def test_discard_speaker_drops_chain_and_voice(agg: BufferedAggregator, clock: FakeClock) -> None:
    agg.feed(seg(speech_s=7.0))
    agg.note_voice(ALICE)
    agg.discard_speaker(ALICE)
    assert agg.narrating_speakers() == [] and agg.tick() == []
    clock.advance(0.5)
    agg.feed(seg(speech_s=1.0))
    assert not agg.is_narrating(ALICE)


def test_flush_all_returns_narrative_turn(agg: BufferedAggregator) -> None:
    agg.feed(seg(speech_s=7.0))
    [turn] = agg.flush_all()
    assert turn.state is AggregateState.FLUSHED and turn.narrative is True


# ---- 宿主接線 ----

def test_host_forwards_voiced_events_to_aggregator(agg: BufferedAggregator, clock: FakeClock) -> None:
    fake = SimpleNamespace(orchestrator=SimpleNamespace(aggregator=agg))
    agg.feed(seg("unknown", speech_s=7.0))
    clock.advance(1.5)
    EchoPipelineHost._note_voice_activity(fake, SimpleNamespace(is_voiced=True, speaker_id=None))  # 單聲道：對應 "unknown"
    clock.advance(2.4)
    assert agg.tick() == []  # 從最後出聲起算
    clock.advance(0.2)
    assert len(agg.tick()) == 1


def test_host_ignores_unvoiced_events_and_non_aware_aggregators(agg: BufferedAggregator, clock: FakeClock) -> None:
    fake = SimpleNamespace(orchestrator=SimpleNamespace(aggregator=agg))
    EchoPipelineHost._note_voice_activity(fake, SimpleNamespace(is_voiced=False, speaker_id=ALICE))
    assert ALICE not in agg._voice_last
    plain = SimpleNamespace(orchestrator=SimpleNamespace(aggregator=object()))
    EchoPipelineHost._note_voice_activity(plain, SimpleNamespace(is_voiced=True, speaker_id=ALICE))  # 不拋例外


def test_orchestrator_feeds_reply_outcome_back_to_aggregator(agg: BufferedAggregator, clock: FakeClock) -> None:
    fake = SimpleNamespace(_aggregator=agg)
    live = SimpleNamespace(speakers=(ALICE,), ticket=SimpleNamespace(speaker_ref=ALICE))
    TurnOrchestrator._notify_reply_to_aggregator(fake, live, "completed")
    assert ALICE in agg._replied_at
    TurnOrchestrator._notify_reply_to_aggregator(fake, live, "cancelled")
    assert ALICE not in agg._replied_at
    TurnOrchestrator._notify_reply_to_aggregator(fake, live, "skipped")  # 不動
    assert ALICE not in agg._replied_at
