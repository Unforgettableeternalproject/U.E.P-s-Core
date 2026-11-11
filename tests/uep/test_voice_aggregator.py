"""語音路徑接上 BufferedAggregator 的單元測試（純記憶體，不需要 PostgreSQL）。

涵蓋：分流（語音緩衝、文字逐句通過）、選用入口（set_fast／urgent／note_voice／note_replied／discard）轉給語音聚合器、
Host 只在輸入 stage 消費點標記語音、needs_tick、正式組裝（InferenceWiring）建立聚合器，以及 STT 斷句／VAD 設定傳到 stage。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from uep.config import UepSettings
from uep.console import real_rig
from uep.echo.host import EchoPipelineHost
from uep.echo.inference_wiring import (
    InferenceConfig,
    build_inference_wiring,
    build_voice_aggregator,
)
from uep.stream.audio.vad import EnergyVad
from uep.stream.contracts.types import Utterance
from uep.turn.aggregator import (
    AggregateState,
    BufferedAggregator,
    ModeAwareAggregator,
    PassThroughAggregator,
    Segment,
    VoiceRoutedAggregator,
)
from uep.turn.decision_config import AggregatorConfig, DecisionConfig
from uep.turn.models import UtteranceFact

SPEAKER = "console:owner"


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def seg(text: str, *, voice: bool, speech_s: float = 1.0) -> Segment:
    return Segment(speaker_ref=SPEAKER, text=text, ref=text, start_ts=0.0, end_ts=speech_s, voice=voice)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def router(clock: FakeClock) -> VoiceRoutedAggregator:
    return VoiceRoutedAggregator(BufferedAggregator(AggregatorConfig(), clock=clock))


# ---- 分流 ----

def test_文字逐句通過_不被緩衝(router: VoiceRoutedAggregator) -> None:
    done = router.feed(seg("你好小橘", voice=False))
    assert [t.text for t in done] == ["你好小橘"]  # 立刻交出，不等被動模式的 3 秒
    assert router.voice.active_speakers == []


def test_語音多段在合併間隔內併成一個_turn(router: VoiceRoutedAggregator, clock: FakeClock) -> None:
    for text in ("第一句", "第二句", "第三句"):
        assert router.feed(seg(text, voice=True)) == []  # 全部緩衝
        clock.advance(1.0)  # 段落間隔 1.0s <= merge_gap 1.5s
    assert router.tick() == []  # 最後一段後才過 1.0s，還沒到 max_wait 3s
    clock.advance(2.5)
    turns = router.tick()
    assert len(turns) == 1 and turns[0].text == "第一句 第二句 第三句"
    assert turns[0].state is AggregateState.TIMEOUT and len(turns[0].segments) == 3


def test_語音與文字混用時文字不受語音緩衝影響(router: VoiceRoutedAggregator) -> None:
    router.feed(seg("語音一", voice=True))
    done = router.feed(seg("文字", voice=False))
    assert [t.text for t in done] == ["文字"]
    assert router.voice.active_speakers == [SPEAKER]  # 語音那句仍在緩衝


def test_主動模式敘述中_多段長語音併成一個_turn(clock: FakeClock) -> None:
    """17.5 秒、切成多段的情境（主動模式 + narrative hold）：整串合併，等說話者安靜 2.5 秒才交出。"""
    router = VoiceRoutedAggregator(BufferedAggregator(AggregatorConfig(), clock=clock))
    router.set_fast(True)
    for i in range(5):
        clock.advance(3.0)  # 每段約 2 秒語音 + 辨識延遲；段間停頓 <= narrative_gap_s
        assert router.feed(seg(f"第{i}段", voice=True, speech_s=2.0)) == []
        router.note_voice(SPEAKER)
    clock.advance(1.0)
    assert router.tick() == []  # 說話者安靜未滿 2.5s
    clock.advance(2.0)
    turns = router.tick()
    assert len(turns) == 1 and turns[0].narrative and len(turns[0].segments) == 5


# ---- 選用入口轉給語音聚合器 ----

def test_路由器符合_ModeAware_且不是_PassThrough(router: VoiceRoutedAggregator) -> None:
    assert isinstance(router, ModeAwareAggregator)
    assert not isinstance(router, PassThroughAggregator)  # needs_tick 才會成立


def test_set_fast_與_urgent_生效(router: VoiceRoutedAggregator, clock: FakeClock) -> None:
    voice = router.voice
    assert voice.merge_gap_s == 1.5 and voice.max_wait_s == 3.0
    router.set_fast(True)
    assert voice.merge_gap_s == 0.25 and voice.max_wait_s == 0.25
    router.set_fast(False)
    router.set_urgent_predicate(lambda text: "小U" in text)
    router.feed(seg("小U 幫我查", voice=True))
    clock.advance(0.3)  # 被叫到的 turn 只等 active_max_wait（0.25s），不等 3 秒
    assert [t.text for t in router.tick()] == ["小U 幫我查"]


def test_note_voice_與回覆回饋與離場轉給語音聚合器() -> None:
    calls: list[tuple] = []

    class Recorder:
        def feed(self, segment):
            return []

        def tick(self):
            return []

        def flush_all(self):
            return []

        def note_voice(self, speaker, now=None):
            calls.append(("voice", speaker))

        def note_replied(self, speaker, now=None):
            calls.append(("replied", speaker))

        def forget_replied(self, speaker):
            calls.append(("forget", speaker))

        def discard_speaker(self, speaker):
            calls.append(("discard", speaker))

    router = VoiceRoutedAggregator(Recorder())
    router.note_voice("a")
    router.note_replied("a")
    router.forget_replied("a")
    router.discard_speaker("a")
    assert calls == [("voice", "a"), ("replied", "a"), ("forget", "a"), ("discard", "a")]
    router.set_fast(True)  # 語音聚合器沒有的入口視為無操作，不拋例外
    router.set_urgent_predicate(None)


def test_語音聚合器不支援選用入口時不拋例外() -> None:
    router = VoiceRoutedAggregator(PassThroughAggregator())
    router.note_voice("a")
    router.note_replied("a")
    assert router.pending_wait_s == 0.0


def test_pending_wait_s_隨模式變化(router: VoiceRoutedAggregator) -> None:
    assert router.pending_wait_s == 3.0
    router.set_fast(True)
    assert router.pending_wait_s == 2.5  # 主動模式含敘述中的安靜時間 2.5s > active_max_wait 0.25s


def test_flush_all_涵蓋語音緩衝(router: VoiceRoutedAggregator) -> None:
    router.feed(seg("語音一", voice=True))
    assert [t.state for t in router.flush_all()] == [AggregateState.FLUSHED]


# ---- Host ----

def test_Host_只在輸入_stage_消費點標記語音() -> None:
    host = object.__new__(EchoPipelineHost)
    host._seq = 0
    host._input_stage = SimpleNamespace()
    captured: list[UtteranceFact] = []
    host.submit_utterance = captured.append  # type: ignore[method-assign]
    host._intake_stage_utterance(Utterance(text="你好", speaker_id=SPEAKER))
    assert captured[0].from_voice is True
    assert UtteranceFact(speaker_ref=SPEAKER, text="文字", seq=1).from_voice is False  # 文字／Console 預設不是語音


@pytest.mark.parametrize("aggregator,expected", [(PassThroughAggregator(), False), (None, True)])
def test_needs_tick_隨聚合器而定(aggregator, expected) -> None:
    host = object.__new__(EchoPipelineHost)
    agg = aggregator if aggregator is not None else VoiceRoutedAggregator(BufferedAggregator())
    host.orchestrator = SimpleNamespace(proposer=object(), aggregator=agg)
    assert host.needs_tick is expected


# ---- 正式組裝 ----

class _FakeStt:
    def transcribe(self, pcm, sample_rate):
        return None


class _FakeTts:
    def __init__(self) -> None:
        self.load_calls = 0

    def load(self) -> None:
        self.load_calls += 1


class _FakeLlm:
    async def stream_query(self, messages, system_instruction=None, **kwargs):
        yield "好"


def _wiring(**over):
    cfg = InferenceConfig(**{"stt_mode": "multichannel", **over})
    return build_inference_wiring(cfg, tts_backend=_FakeTts(), think_backend=_FakeLlm(), stt_backend=_FakeStt())


def test_正式組裝預設帶入語音聚合器_每次一份新的() -> None:
    wiring = _wiring()
    first, second = wiring.host_kwargs()["aggregator"], wiring.host_kwargs()["aggregator"]
    assert isinstance(first, VoiceRoutedAggregator) and isinstance(first.voice, BufferedAggregator)
    assert first is not second  # 聚合器有狀態，不能共用


def test_關閉語音聚合器時不帶_aggregator() -> None:
    assert "aggregator" not in _wiring(voice_aggregator=False).host_kwargs()


def test_語音聚合器沿用_bot_的_AggregatorConfig() -> None:
    cfg = AggregatorConfig(max_wait_ms=1234)
    agg = build_voice_aggregator(DecisionConfig(aggregator=cfg))
    assert agg.voice.max_wait_s == 1.234


def test_設定預設對齊_bot_現行值() -> None:
    s = UepSettings(_env_file=None)
    assert (s.stt_silence_threshold_s, s.stt_min_utterance_s) == (0.8, 0.3)
    assert (s.stt_vad_threshold, s.stt_vad_hysteresis, s.stt_vad_hangover_s) == (0.015, 0.6, 0.15)
    assert s.voice_aggregator is True
    cfg = InferenceConfig.from_settings(s)
    assert cfg.stt_silence_threshold_s == 0.8 and cfg.voice_aggregator is True


def test_UEP_環境變數覆寫斷句與_VAD() -> None:
    s = UepSettings(
        _env_file=None, UEP_STT_SILENCE_THRESHOLD_S=1.2, UEP_STT_MIN_UTTERANCE_S=0.5, UEP_STT_VAD_THRESHOLD=0.03,
        UEP_STT_VAD_HYSTERESIS=0.5, UEP_STT_VAD_HANGOVER_S=0.3, UEP_VOICE_AGGREGATOR=False,
    )
    assert (s.stt_silence_threshold_s, s.stt_min_utterance_s, s.stt_vad_threshold) == (1.2, 0.5, 0.03)
    assert (s.stt_vad_hysteresis, s.stt_vad_hangover_s, s.voice_aggregator) == (0.5, 0.3, False)


def test_斷句與_VAD_設定傳到_multichannel_stage() -> None:
    wiring = _wiring(
        stt_silence_threshold_s=1.2, stt_min_utterance_s=0.5, stt_vad_threshold=0.03, stt_vad_hysteresis=0.5,
        stt_vad_hangover_s=0.3,
    )
    stage = wiring.input_stage
    assert (stage.policy.silence_threshold_s, stage.policy.min_utterance_s) == (1.2, 0.5)
    vad = stage.engine._vad_factory()
    assert isinstance(vad, EnergyVad) and (vad.threshold, vad.hysteresis, vad.hangover_s) == (0.03, 0.5, 0.3)


def test_斷句與_VAD_設定傳到_single_stage() -> None:
    stage = _wiring(stt_mode="single", stt_silence_threshold_s=1.2, stt_vad_threshold=0.03).input_stage
    assert stage.policy.silence_threshold_s == 1.2 and stage.turn_detector.policy.silence_threshold_s == 1.2
    assert stage.vad.threshold == 0.03


@pytest.mark.parametrize("mode", ["multichannel", "single"])
def test_Console_的_STT_stage_吃同一組設定(mode: str) -> None:
    settings = UepSettings(
        _env_file=None, UEP_INFER_STT_MODE=mode, UEP_STT_SILENCE_THRESHOLD_S=1.1, UEP_STT_VAD_THRESHOLD=0.02,
        UEP_STT_VAD_HANGOVER_S=0.25,
    )
    stage = real_rig.build_stt_stage(settings, {})
    assert stage.policy.silence_threshold_s == 1.1
    vad = stage.engine._vad_factory() if mode == "multichannel" else stage.vad
    assert (vad.threshold, vad.hangover_s) == (0.02, 0.25)
