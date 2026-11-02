"""turn / barge-in 判定的測試。

兩者刻意分開實作——barge-in 要保守、turn-end 要靈敏，
合併就只能取一組閾值，必然犧牲其中一邊。
"""

from __future__ import annotations

from uep.stream.contracts.turn import TurnPolicy, VoiceEvent, VoiceState
from uep.stream.contracts.types import Utterance
from uep.stream.turn_detector import (
    AlwaysAddressed,
    DurationInterruptionDetector,
    SilenceTurnDetector,
)


def speech(duration: float, text: str = "") -> VoiceEvent:
    return VoiceEvent(
        state=VoiceState.SPEECH, timestamp=0.0, duration_s=duration, partial_text=text
    )


def silence(duration: float) -> VoiceEvent:
    return VoiceEvent(state=VoiceState.SILENCE, timestamp=0.0, duration_s=duration)


# --- TurnDetector ---


def test_講話中不算結束():
    detector = SilenceTurnDetector()
    decision = detector.evaluate(speech(1.0))
    assert not decision.is_end_of_turn
    assert decision.reason == "speaking"


def test_靜音達門檻判定結束():
    detector = SilenceTurnDetector(TurnPolicy(silence_threshold_s=0.7))
    detector.evaluate(speech(1.0))
    assert detector.evaluate(silence(0.7)).is_end_of_turn


def test_靜音未達門檻回報還要等多久():
    """回傳建議等待時間而不是 bool——Phase 1 換語意模型時介面不用改。"""
    detector = SilenceTurnDetector(TurnPolicy(silence_threshold_s=0.7))
    detector.evaluate(speech(1.0))
    decision = detector.evaluate(silence(0.3))
    assert not decision.is_end_of_turn
    assert abs(decision.suggested_wait_s - 0.4) < 1e-6


def test_太短的語音不成一個_turn():
    """咳嗽、關門聲、鍵盤敲擊。"""
    detector = SilenceTurnDetector(TurnPolicy(min_utterance_s=0.3))
    detector.evaluate(speech(0.1))
    decision = detector.evaluate(silence(5.0))
    assert not decision.is_end_of_turn
    assert decision.reason == "too_short"


def test_reset_清掉累積狀態():
    detector = SilenceTurnDetector()
    detector.evaluate(speech(2.0))
    detector.reset()
    assert detector.evaluate(silence(5.0)).reason == "too_short"


# --- InterruptionDetector ---


def test_短促附和聲不算插話():
    """「嗯」「對」撐不到時長門檻。"""
    detector = DurationInterruptionDetector(TurnPolicy(interruption_min_speech_s=0.4))
    decision = detector.evaluate(speech(0.15))
    assert not decision.should_interrupt
    assert decision.reason == "below_duration_threshold"


def test_持續語音算插話():
    detector = DurationInterruptionDetector(TurnPolicy(interruption_min_speech_s=0.4))
    assert detector.evaluate(speech(0.5)).should_interrupt


def test_靜音不算插話():
    detector = DurationInterruptionDetector()
    assert not detector.evaluate(silence(2.0)).should_interrupt


def test_全域關閉插話():
    """用來測「無插話」的基準延遲。"""
    detector = DurationInterruptionDetector(TurnPolicy(allow_interruptions=False))
    decision = detector.evaluate(speech(5.0))
    assert not decision.should_interrupt
    assert decision.reason == "disabled"


def test_字數門檻與時長門檻是_AND_關係():
    """LiveKit 的實作是覆蓋關係（issue #3515），會讓人以為設了兩層保護
    但實際只有一層生效。我們明確定義成 AND。"""
    policy = TurnPolicy(interruption_min_speech_s=0.4, interruption_min_words=3)
    detector = DurationInterruptionDetector(policy)

    # 時長夠但字數不夠 → 不插話
    assert not detector.evaluate(speech(1.0, "嗯 對")).should_interrupt
    # 兩個都夠 → 插話
    assert detector.evaluate(speech(1.0, "等 一 下")).should_interrupt


def test_字數門檻為零時停用():
    policy = TurnPolicy(interruption_min_speech_s=0.4, interruption_min_words=0)
    detector = DurationInterruptionDetector(policy)
    assert detector.evaluate(speech(1.0, "")).should_interrupt


# --- Addressing ---


def test_phase0到4_永遠視為對機器人講話():
    """單人本機測試沒有第二個人可以講話，這是正確行為不是 stub。"""
    classifier = AlwaysAddressed()
    assert classifier.is_addressed_to_bot(Utterance(text="隨便什麼"))
