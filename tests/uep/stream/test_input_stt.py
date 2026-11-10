"""SttInputStage 的橋接測試——無 GPU、無 echo_stt。

Phase 3 的風險在 turn 切分、時間標記、背景 task 的生命週期，
不在 Whisper 本身。所以 backend 全部注入假的。
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
from collections.abc import AsyncIterator

import pytest

from uep.stream.adapters.input_stt import (
    SttInputStage,
    TranscriptionResult,
)
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.turn import TurnPolicy, VoiceState
from uep.stream.audio.vad import EnergyVad

SR = 16_000


def tone(seconds: float, amplitude: float = 0.3) -> bytes:
    n = int(SR * seconds)
    return struct.pack(
        f"<{n}h",
        *(
            int(amplitude * 32767 * math.sin(2 * math.pi * 220.0 * i / SR))
            for i in range(n)
        ),
    )


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SR * seconds)


class ScriptedSource:
    """依腳本推送 PCM 的 AudioSource。不節流——VAD 的時間軸靠樣本數。"""

    def __init__(self, pcm: bytes, chunk_ms: int = 50) -> None:
        self.pcm = pcm
        self.chunk_ms = chunk_ms

    @property
    def sample_rate(self) -> int:
        return SR

    async def stream(self, token: CancellationToken) -> AsyncIterator[bytes]:
        step = int(SR * self.chunk_ms / 1000) * 2
        for i in range(0, len(self.pcm), step):
            if token.is_cancelled:
                return
            yield self.pcm[i : i + step]
            await asyncio.sleep(0)  # 讓 worker 有機會跑


class FakeBackend:
    """記錄收到的音訊，回傳固定文字。"""

    def __init__(self, text: str = "你好", delay_s: float = 0.0) -> None:
        self.text = text
        self.delay_s = delay_s
        self.received: list[bytes] = []
        self.closed = False

    def transcribe(self, pcm: bytes, sample_rate: int) -> TranscriptionResult | None:
        assert sample_rate == SR
        if self.delay_s:
            time.sleep(self.delay_s)
        self.received.append(pcm)
        return TranscriptionResult(text=f"{self.text}{len(self.received)}", language="zh")

    def close(self) -> None:
        self.closed = True


def make_stage(backend=None, **kwargs) -> SttInputStage:
    policy = kwargs.pop("policy", TurnPolicy(silence_threshold_s=0.7, min_utterance_s=0.3))
    return SttInputStage(
        backend=backend or FakeBackend(),
        policy=policy,
        vad=EnergyVad(sample_rate=SR),
        **kwargs,
    )


async def collect(stage: SttInputStage, source, token=None):
    token = token or CancellationToken()
    results = []
    async for utterance in stage.stream(source, token):
        results.append(utterance)
    return results


class TestTurnSegmentation:
    @pytest.mark.asyncio
    async def test_單一_turn(self) -> None:
        backend = FakeBackend()
        stage = make_stage(backend)
        source = ScriptedSource(tone(1.0) + silence(1.2))
        results = await collect(stage, source)

        assert len(results) == 1
        assert results[0].text == "你好1"
        assert results[0].language == "zh"
        assert results[0].is_final

    @pytest.mark.asyncio
    async def test_兩個_turn_依序切出(self) -> None:
        stage = make_stage()
        source = ScriptedSource(
            tone(0.8) + silence(1.0) + tone(0.6) + silence(1.0)
        )
        results = await collect(stage, source)
        assert [u.text for u in results] == ["你好1", "你好2"]

    @pytest.mark.asyncio
    async def test_太短的噪音被丟棄(self) -> None:
        backend = FakeBackend()
        stage = make_stage(backend)
        # 0.1s 咳嗽 < min_utterance_s=0.3，之後的正常語音不該黏到它
        source = ScriptedSource(
            tone(0.1) + silence(1.0) + tone(0.8) + silence(1.0)
        )
        results = await collect(stage, source)
        assert len(results) == 1
        # 送去轉錄的音訊量應接近 0.8s 的語音（含 preroll），
        # 不含 1.9s 前的咳嗽與大段靜音
        assert len(backend.received) == 1
        seconds = len(backend.received[0]) / 2 / SR
        assert seconds < 1.4

    @pytest.mark.asyncio
    async def test_source_結束時_flush_殘餘語音(self) -> None:
        stage = make_stage()
        # 語音結尾沒有足夠靜音讓 turn 判定觸發——靠 flush
        source = ScriptedSource(tone(0.8) + silence(0.2))
        results = await collect(stage, source)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_短句內停頓不切斷_turn(self) -> None:
        backend = FakeBackend()
        stage = make_stage(backend)
        # 0.4s 停頓 < 0.7s 靜音門檻：仍是同一個 turn
        source = ScriptedSource(
            tone(0.5) + silence(0.4) + tone(0.5) + silence(1.0)
        )
        results = await collect(stage, source)
        assert len(results) == 1
        assert len(backend.received) == 1


class TestTimestamps:
    @pytest.mark.asyncio
    async def test_ended_at_早於轉錄完成時刻(self) -> None:
        """TTFA 從 ended_at 起算，轉錄耗時必須算進使用者的等待。"""
        stage = make_stage(FakeBackend(delay_s=0.15))
        source = ScriptedSource(tone(0.8) + silence(1.0))
        results = await collect(stage, source)
        after = time.perf_counter()

        assert len(results) == 1
        utt = results[0]
        # 注意：ScriptedSource 比即時快得多，started_at/ended_at 的回推
        # 用的是音訊時間軸，所以只驗證順序與「轉錄耗時不算進 ended_at」，
        # 不驗證絕對值（那要 realtime source 才有意義）
        assert utt.started_at <= utt.ended_at <= after
        # 轉錄 delay 0.15s：ended_at 應明顯早於整體結束
        assert after - utt.ended_at >= 0.14


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_取消時停止產出(self) -> None:
        stage = make_stage()
        token = CancellationToken()
        source = ScriptedSource(
            (tone(0.8) + silence(1.0)) * 5
        )
        results = []
        async for utterance in stage.stream(source, token):
            results.append(utterance)
            token.cancel()
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_aclose_關閉_backend(self) -> None:
        backend = FakeBackend()
        stage = make_stage(backend)
        source = ScriptedSource(tone(0.5) + silence(1.0))
        await collect(stage, source)
        await stage.aclose()
        assert backend.closed

    @pytest.mark.asyncio
    async def test_取樣率不符直接拒絕(self) -> None:
        stage = make_stage()

        class WrongRateSource(ScriptedSource):
            @property
            def sample_rate(self) -> int:
                return 44_100

        with pytest.raises(ValueError, match="取樣率"):
            async for _ in stage.stream(WrongRateSource(tone(0.1)), CancellationToken()):
                pass

    @pytest.mark.asyncio
    async def test_backend_錯誤傳給消費端(self) -> None:
        class BrokenBackend:
            def transcribe(self, pcm: bytes, sample_rate: int):
                raise RuntimeError("模型爆了")

        stage = make_stage(BrokenBackend())
        source = ScriptedSource(tone(0.8) + silence(1.0))
        with pytest.raises(RuntimeError, match="模型爆了"):
            await collect(stage, source)


class TestVoiceEventHook:
    @pytest.mark.asyncio
    async def test_on_voice_event_收到事件流(self) -> None:
        states = []
        stage = make_stage(on_voice_event=lambda e: states.append(e.state))
        source = ScriptedSource(tone(0.5) + silence(1.0))
        await collect(stage, source)
        assert VoiceState.SPEECH in states
        assert VoiceState.SILENCE in states

    @pytest.mark.asyncio
    async def test_壞掉的訂閱者不斷麥克風(self) -> None:
        def boom(event) -> None:
            raise RuntimeError("訂閱者爆了")

        stage = make_stage(on_voice_event=boom)
        source = ScriptedSource(tone(0.8) + silence(1.0))
        results = await collect(stage, source)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_轉錄期間_VAD_不中斷(self) -> None:
        """轉錄在 worker 跑，讀取端照常消化音訊——插話偵測不能有空窗。"""
        states_during: list[VoiceState] = []
        backend = FakeBackend(delay_s=0.2)
        stage = make_stage(backend, on_voice_event=lambda e: states_during.append(e.state))
        # 第一個 turn 轉錄的 0.2s 期間，第二段語音持續進來
        source = ScriptedSource(
            tone(0.8) + silence(1.0) + tone(0.8) + silence(1.0)
        )
        results = await collect(stage, source)
        assert len(results) == 2


class TestConfidenceGate:
    @pytest.mark.asyncio
    async def test_低信心轉錄被擋下(self) -> None:
        class NoisyBackend:
            def transcribe(self, pcm: bytes, sample_rate: int):
                return TranscriptionResult(text="Hello!", confidence=0.2)

        stage = SttInputStage(
            backend=NoisyBackend(),
            policy=TurnPolicy(silence_threshold_s=0.7, min_utterance_s=0.3),
            vad=EnergyVad(sample_rate=SR),
            min_confidence=0.35,
        )
        results = await collect(stage, ScriptedSource(tone(0.8) + silence(1.0)))
        assert results == []


# --- 增量轉錄（STT 端的逐句串流，2026-08-21） ---


async def test_停頓處切出增量段並在_turn_結束拼裝():
    backend = FakeBackend()
    partials: list[tuple[int, str]] = []
    stage = make_stage(
        backend,
        on_partial=lambda t, i: partials.append((i, t)),
        partial_silence_s=0.35,
        partial_min_speech_s=2.0,
    )
    # 2.5s 語音 → 0.5s 停頓（觸發增量）→ 2.5s 語音 → 1.0s 靜音（turn 結束）
    script = tone(2.5) + silence(0.5) + tone(2.5) + silence(1.0)
    utterances = [
        u async for u in stage.stream(ScriptedSource(script), CancellationToken())
    ]

    assert len(utterances) == 1
    assert utterances[0].text == "你好1 你好2"
    assert [t for _, t in partials] == ["你好1", "你好2"]
    assert [i for i, _ in partials] == [0, 1]
    # 已切出去的段不重轉——長獨白的最終轉錄成本被攤平
    assert len(backend.received) == 2


async def test_不設_on_partial_時行為不變():
    backend = FakeBackend()
    stage = make_stage(backend, partial_silence_s=0.35, partial_min_speech_s=2.0)
    script = tone(2.5) + silence(0.5) + tone(2.5) + silence(1.0)
    utterances = [
        u async for u in stage.stream(ScriptedSource(script), CancellationToken())
    ]
    assert len(utterances) == 1
    assert len(backend.received) == 1  # 整個 turn 一次轉


async def test_短句不觸發增量切分():
    backend = FakeBackend()
    partials: list[str] = []
    stage = make_stage(
        backend,
        on_partial=lambda t, i: partials.append(t),
        partial_silence_s=0.35,
        partial_min_speech_s=2.0,
    )
    script = tone(1.0) + silence(1.0)
    utterances = [
        u async for u in stage.stream(ScriptedSource(script), CancellationToken())
    ]
    assert len(utterances) == 1
    # 殘餘段也走 on_partial（讓前端的條列完整），但只該有一段
    assert partials == ["你好1"]
    assert len(backend.received) == 1


async def test_低信心的增量段被剔除不污染整段():
    class NoisyBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.confidences = [0.9, 0.1, 0.9]  # 中段是雜訊

        def transcribe(self, pcm, sample_rate):
            result = super().transcribe(pcm, sample_rate)
            conf = self.confidences[min(len(self.received) - 1, 2)]
            return TranscriptionResult(text=result.text, language="zh", confidence=conf)

    backend = NoisyBackend()
    stage = make_stage(
        backend,
        on_partial=lambda t, i: None,
        partial_silence_s=0.35,
        partial_min_speech_s=2.0,
    )
    script = (
        tone(2.5) + silence(0.5) + tone(2.5) + silence(0.5) + tone(2.5) + silence(1.0)
    )
    utterances = [
        u async for u in stage.stream(ScriptedSource(script), CancellationToken())
    ]
    assert len(utterances) == 1
    assert utterances[0].text == "你好1 你好3"


# --- 捨棄輸入（discard_current，2026-08-21） ---


class DiscardingSource(ScriptedSource):
    """推完指定位元組後呼叫 discard 的來源——模擬「講到一半按捨棄」。"""

    def __init__(self, pcm: bytes, discard_after: int, chunk_ms: int = 50) -> None:
        super().__init__(pcm, chunk_ms)
        self.discard_after = discard_after
        self.stage: SttInputStage | None = None
        self._fired = False

    async def stream(self, token):
        sent = 0
        step = int(SR * self.chunk_ms / 1000) * 2
        for i in range(0, len(self.pcm), step):
            if token.is_cancelled:
                return
            yield self.pcm[i : i + step]
            sent += step
            if not self._fired and sent >= self.discard_after:
                self._fired = True
                assert self.stage is not None
                self.stage.discard_current()
            await asyncio.sleep(0)


async def test_discard_丟棄累積中的輸入():
    """捨棄後，捨棄前的語音不能變成 turn 或黏進下一個 turn。"""
    backend = FakeBackend()
    stage = make_stage(backend)
    # 1.2s 語音（被捨棄）→ 0.6s 語音 + 1.0s 靜音（正常 turn）
    pcm = tone(1.2) + tone(0.6) + silence(1.0)
    source = DiscardingSource(pcm, discard_after=len(tone(1.2)))
    source.stage = stage

    utterances = [u async for u in stage.stream(source, CancellationToken())]

    assert len(utterances) == 1
    assert utterances[0].text == "你好1"
    assert len(backend.received) == 1
    # 收到的音訊裡不含被捨棄那 1.2s（0.6s + preroll 遠小於 1.2s）
    assert len(backend.received[0]) < len(tone(1.0))


async def test_discard_後殘餘不會在_source_結束時_flush():
    backend = FakeBackend()
    stage = make_stage(backend)
    pcm = tone(1.5)  # 講到一半直接停（沒有靜音收尾）
    source = DiscardingSource(pcm, discard_after=len(pcm) - 3200)
    source.stage = stage

    utterances = [u async for u in stage.stream(source, CancellationToken())]

    assert utterances == []
    assert backend.received == []


def test_echo_backend_把_avg_logprob_與壓縮比帶進結果():
    """echo_stt 的 TranscriptEntry 帶的解碼品質要傳到 TranscriptionResult（結尾套語過濾在 echo_stt 內依此判斷）。"""
    from types import SimpleNamespace

    from uep.stream.adapters.input_stt import EchoSttBackend

    entry = SimpleNamespace(
        text="你好", language="zh", confidence=0.8, avg_logprob=-0.2, compression_ratio=1.1
    )
    backend = EchoSttBackend.__new__(EchoSttBackend)
    backend._language = None
    backend._engine = SimpleNamespace(transcribe_segment=lambda *a, **k: entry)
    result = backend.transcribe(b"\x01\x00" * 1600, SR)
    assert result is not None
    assert result.avg_logprob == -0.2
    assert result.compression_ratio == 1.1
