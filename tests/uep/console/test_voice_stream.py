"""Console 語音輸入（/mic 整段辨識、/stream 即時串流、/wav 真實時間餵入）的單元測試。

前半（假 runtime／假 Host）純記憶體，不需要 PostgreSQL；後半的端到端情境（真 Host＋聚合器＋DB）需要
UEP_TEST_DATABASE_URL（未設時由 ``session_factory`` fixture skip）。
"""
from __future__ import annotations

import itertools
import threading
import time
import wave
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.uep.console.test_console import Capture
from uep.console import audio_io
from uep.console.audio_io import AudioIoError, SoundDeviceMicStream
from uep.console.commands import Console
from uep.console.rig import CONSOLE_SPEAKER
from uep.stream.adapters.input_stt import TranscriptionResult
from uep.turn.models import IntakeResult


def _pcm(seconds: float, *, loud: bool = True) -> bytes:
    import numpy as np

    n = int(16000 * seconds)
    if not loud:
        return bytes(n * 2)
    t = np.arange(n) / 16000
    return (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).tobytes()


def _wav_frames(path: Path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as w:
        return w.getnframes(), w.getframerate()


class StubHost:
    """只記錄 feed／submit_utterance 的假 Host；``on_feed`` 讓測試模擬管線在餵入過程中產生事件。"""

    location = "desktop"

    def __init__(self, on_feed=None, pending_wait_s: float = 0.0, clock=time.monotonic) -> None:
        self.fed: list[tuple[str, int, bool, str, float]] = []
        self.submitted: list = []
        self.on_feed = on_feed
        self._clock = clock
        self.orchestrator = SimpleNamespace(aggregator=SimpleNamespace(pending_wait_s=pending_wait_s))

    def feed(self, speaker: str, pcm: bytes, ts=None) -> bool:
        self.fed.append((speaker, len(pcm), any(pcm), threading.current_thread().name, self._clock()))
        if self.on_feed is not None:
            self.on_feed(len(self.fed))
        return True

    def submit_utterance(self, fact):
        self.submitted.append(fact)
        return IntakeResult(SimpleNamespace(turn_id=f"turn_{len(self.submitted)}"), "opened", fact=fact)


class FakeBackend:
    def __init__(self, text: str = "你好小橘", confidence: float = 0.9) -> None:
        self.calls: list[tuple[int, int]] = []
        self.text, self.confidence = text, confidence

    def transcribe(self, pcm: bytes, sample_rate: int):
        self.calls.append((len(pcm), sample_rate))
        return TranscriptionResult(text=self.text, language="zh", confidence=self.confidence)


def _runtime(tmp_path: Path, host: StubHost, *, backend=None, player=None):
    stage = SimpleNamespace(_backend=backend, min_confidence=0.35, fed=[])
    stage.feed = lambda *a, **k: stage.fed.append(a)
    return SimpleNamespace(
        host=host, input_stage=stage, session_dir=tmp_path / "session", intake_events=[], submissions=[], seq=0,
        player=player,
    )


def _console(rt, **kw):
    out = Capture()
    console = Console(rt, out=out, wait_s=1.0, stt_wait_s=2.0, feed_sleep=False, settle_s=0.1, input_fn=lambda _p: "", **kw)
    reported: list = []
    console._report_turn = reported.append  # type: ignore[method-assign]
    return console, out, reported


class FakeMic:
    def __init__(self, pcm: bytes) -> None:
        self._pcm = pcm
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> bytes:
        return self._pcm


class FakeStreamMic:
    """假的即時麥克風：依序吐出預先給定的塊，吐完設 ``exhausted``。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = deque(chunks)
        self.exhausted = threading.Event()
        self.started = self.stopped = False
        self.dropped = 0

    def start(self) -> None:
        self.started = True

    def read(self, timeout_s: float):
        try:
            return self._chunks.popleft()
        except IndexError:
            self.exhausted.set()
            time.sleep(min(timeout_s, 0.01))
            return None

    def stop(self) -> None:
        self.stopped = True


# ---- /mic：整段辨識，不經 VAD、不經聚合器 ----

def test_mic_整段交給_backend_只產生一句_utterance_且不餵_VAD(tmp_path):
    backend = FakeBackend()
    host = StubHost()
    rt = _runtime(tmp_path, host, backend=backend)
    pcm = _pcm(2.0)
    console, _out, reported = _console(rt, mic_factory=lambda: FakeMic(pcm))
    console.execute("/mic")
    assert backend.calls == [(len(pcm), 16000)]  # 整段一次辨識，沒有切段
    assert host.fed == [] and rt.input_stage.fed == []  # 沒有經過 host.feed／VAD
    assert len(host.submitted) == 1 and len(reported) == 1  # 只有一句、一個 turn
    fact = host.submitted[0]
    assert (fact.text, fact.duration_ms, fact.language) == ("你好小橘", 2000, "zh")
    assert fact.from_voice is False  # 走文字等價的提交路徑：永遠不被聚合器緩衝
    assert fact.source_location.value == "desktop"  # 本機 Host 的輸入場域
    assert fact.speaker_ref == CONSOLE_SPEAKER


def test_mic_錄音存成_wav_並印出路徑(tmp_path):
    rt = _runtime(tmp_path, StubHost(), backend=FakeBackend())
    pcm = _pcm(1.5)
    console, out, _ = _console(rt, mic_factory=lambda: FakeMic(pcm))
    console.execute("/mic")
    wavs = list((tmp_path / "session").glob("mic-*.wav"))
    assert len(wavs) == 1 and _wav_frames(wavs[0]) == (24000, 16000)
    assert str(wavs[0]) in out.text and "不經 VAD" in out.text


def test_mic_stage_宣告輸入場域時優先使用(tmp_path):
    from uep.contracts.presence import PresenceLocation

    host = StubHost()
    rt = _runtime(tmp_path, host, backend=FakeBackend())
    rt.input_stage.source_location = PresenceLocation.DISCORD
    console, _out, _ = _console(rt, mic_factory=lambda: FakeMic(_pcm(1.0)))
    console.execute("/mic")
    assert host.submitted[0].source_location is PresenceLocation.DISCORD


@pytest.mark.parametrize("text,confidence,hint", [("", 0.9, "辨識結果：（空"), ("雜訊", 0.1, "信心過低")])
def test_mic_空結果與低信心不開_turn(tmp_path, text, confidence, hint):
    host = StubHost()
    rt = _runtime(tmp_path, host, backend=FakeBackend(text, confidence))
    console, out, reported = _console(rt, mic_factory=lambda: FakeMic(_pcm(1.0)))
    console.execute("/mic")
    assert hint in out.text and host.submitted == [] and reported == []


def test_mic_backend_尚未載入時提示且不錄音(tmp_path):
    rt = _runtime(tmp_path, StubHost(), backend=None)
    mic = FakeMic(_pcm(1.0))
    console, out, _ = _console(rt, mic_factory=lambda: mic)
    console.execute("/mic")
    assert "尚未載入" in out.text and not mic.started


def test_mic_沒有真實_stt_時提示(tmp_path):
    rt = _runtime(tmp_path, StubHost())
    rt.input_stage = None
    console, out, _ = _console(rt)
    console.execute("/mic")
    assert "需要真實 STT" in out.text


# ---- /stream：即時串流 ----

def _stream_events(rt):
    """模擬管線：第 3、5 塊後各辨識出一句（回 buffered），第 6 塊後聚合器交出合併的 turn。"""
    ticket = SimpleNamespace(turn_id="turn_1")
    def buffered(text: str):
        return "utterance", SimpleNamespace(text=text), IntakeResult(None, "buffered")

    def on_feed(n: int) -> None:
        if n == 3:
            rt.intake_events.append(buffered("第一句"))
        elif n == 5:
            rt.intake_events.append(buffered("第二句"))
        elif n == 6:
            fact = SimpleNamespace(text="第一句 第二句")
            rt.intake_events.append(("turn", fact, IntakeResult(ticket, "opened", fact=fact)))

    return on_feed


def test_stream_以假麥克風跑通_即時印出_落地_wav_並摘要(tmp_path):
    host = StubHost()
    rt = _runtime(tmp_path, host, backend=FakeBackend())
    host.on_feed = _stream_events(rt)
    chunks = [_pcm(0.1)] * 8
    mic = FakeStreamMic(chunks)
    out = Capture()
    console = Console(
        rt, out=out, wait_s=1.0, stt_wait_s=3.0, feed_sleep=False, settle_s=0.1,
        input_fn=lambda _p: mic.exhausted.wait(5.0) and "", stream_mic_factory=lambda: mic,
    )
    reported: list = []
    console._report_turn = reported.append  # type: ignore[method-assign]
    console.execute("/stream")

    assert mic.started and mic.stopped
    # 麥克風 chunk 全部經 host.feed，且是在 pump 執行緒（不是主執行緒）呼叫
    loud = [f for f in host.fed if f[2]]
    assert len(loud) == 8 and all(f[0] == CONSOLE_SPEAKER for f in host.fed)
    assert {f[3] for f in loud} == {"console-stream-pump"}
    assert any(not f[2] for f in host.fed)  # 結束後補的尾端靜音讓最後一句收尾
    # 即時輸出：每句辨識、聚合後的 turn、turn 結果（turn 結果交給 _report_turn）
    assert "STT 辨識" in out.text and "第一句" in out.text and "（聚合中" in out.text
    assert "聚合 turn：第一句 第二句" in out.text
    assert len(reported) == 1 and reported[0].ticket.turn_id == "turn_1"  # buffered 的句子不另報告 turn
    # 輸入全程存成 wav（不含尾端補的靜音），摘要印出路徑、總秒數、utterance 數與 turn 數
    wavs = list((tmp_path / "session").glob("stream-*.wav"))
    assert len(wavs) == 1 and _wav_frames(wavs[0]) == (8 * 1600, 16000)
    assert f"輸入錄音 {wavs[0]}" in out.text and "共 0.8 秒" in out.text and "utterance 2 句" in out.text and "turn 1 個" in out.text
    assert "未帶 --play" in out.text  # 沒有播放器時照實提示只有文字沒有聲音


def test_stream_帶播放器時不提示只有文字(tmp_path):
    rt = _runtime(tmp_path, StubHost(), backend=FakeBackend(), player=object())
    mic = FakeStreamMic([_pcm(0.1)] * 2)
    console, out, _ = _console(rt, stream_mic_factory=lambda: mic)
    console.input_fn = lambda _p: mic.exhausted.wait(5.0) and ""
    console.execute("/stream")
    assert "未帶 --play" not in out.text and "utterance 0 句；turn 0 個" in out.text


def test_stream_沒有真實_stt_或裝置不可用時提示(tmp_path):
    rt = _runtime(tmp_path, StubHost())
    rt.input_stage = None
    console, out, _ = _console(rt)
    console.execute("/stream")
    assert "需要真實 STT" in out.text

    rt = _runtime(tmp_path, StubHost(), backend=FakeBackend())

    class Broken:
        def start(self):
            raise AudioIoError("缺少 sounddevice")

    console, out, _ = _console(rt, stream_mic_factory=Broken)
    console.execute("/stream")
    assert "缺少 sounddevice" in out.text and not list((tmp_path / "session").glob("stream-*.wav"))


def test_stream_single_模式沒有_feed_時提示(tmp_path):
    rt = _runtime(tmp_path, StubHost(), backend=FakeBackend())
    del rt.input_stage.feed
    console, out, _ = _console(rt)
    console.execute("/stream")
    assert "不支援即時餵入" in out.text


def test_stream_聚合等待納入收尾時間(tmp_path):
    """最後一句還在聚合器等待時，結束後要等到聚合逾時（納入 pending_wait_s）才收尾，不能提早結案。"""
    host = StubHost(pending_wait_s=0.4)
    rt = _runtime(tmp_path, host, backend=FakeBackend())
    ticket = SimpleNamespace(turn_id="turn_1")

    def on_feed(n: int) -> None:
        if n == 2:
            rt.intake_events.append(("utterance", SimpleNamespace(text="尾句"), IntakeResult(None, "buffered")))

            def later() -> None:  # 聚合逾時後 tick 才交出 turn
                time.sleep(0.6)
                fact = SimpleNamespace(text="尾句")
                rt.intake_events.append(("turn", fact, IntakeResult(ticket, "opened", fact=fact)))

            threading.Thread(target=later, daemon=True).start()

    host.on_feed = on_feed
    mic = FakeStreamMic([_pcm(0.1)] * 2)
    console, out, reported = _console(rt, stream_mic_factory=lambda: mic)
    console.input_fn = lambda _p: mic.exhausted.wait(5.0) and ""
    console.execute("/stream")
    assert "聚合 turn：尾句" in out.text and len(reported) == 1 and "turn 1 個" in out.text


def test_串流麥克風_callback_只入列_read_取出_佇列滿時計數(monkeypatch):
    holder: dict = {}

    class FakeRawInputStream:
        def __init__(self, **kwargs):
            holder["kwargs"] = kwargs

        def start(self):
            holder["started"] = True

        def stop(self):
            holder["stopped"] = True

        def close(self):
            holder["closed"] = True

    monkeypatch.setattr(audio_io, "_import_sounddevice", lambda: SimpleNamespace(RawInputStream=FakeRawInputStream))
    mic = SoundDeviceMicStream()
    assert mic.read(0.0) is None  # 逾時回 None
    mic.start()
    kwargs = holder["kwargs"]
    assert kwargs["samplerate"] == 16000 and kwargs["channels"] == 1 and kwargs["dtype"] == "int16"
    assert kwargs["blocksize"] == 1600
    kwargs["callback"](memoryview(b"\x01\x00" * 4), 4, None, None)
    assert mic.read(0.1) == b"\x01\x00" * 4
    for _ in range(audio_io.MIC_STREAM_QUEUE_MAX + 3):
        kwargs["callback"](b"\x00\x00", 1, None, None)
    assert mic.dropped == 3
    mic.stop()
    assert holder["stopped"] and holder["closed"]


# ---- /wav（real）：真實時間速度餵入 ----

class VirtualTime:
    """假時鐘：``sleep`` 只推進虛擬時間，用來驗證配速而不真的等。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    perf_counter = monotonic

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)

    def time(self) -> float:
        return time.time()

    def strftime(self, fmt: str) -> str:
        return time.strftime(fmt)


def _write_wav(path: Path, pcm: bytes) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return path


def _wav_console(monkeypatch, tmp_path, *, feed_sleep: bool):
    vt = VirtualTime()
    monkeypatch.setattr("uep.console.commands.time", vt)
    host = StubHost(clock=vt.monotonic)
    rt = _runtime(tmp_path, host, backend=FakeBackend())

    def on_feed(n: int) -> None:
        if n == 20:
            fact = SimpleNamespace(text="辨識句")
            rt.intake_events.append(("utterance", fact, IntakeResult(SimpleNamespace(turn_id="t1"), "opened", fact=fact)))

    host.on_feed = on_feed
    console, out, reported = _console(rt)
    console.feed_sleep = feed_sleep
    return console, out, reported, host, vt


def test_wav_real_以真實時間速度餵入_並說明會花與音檔等長的時間(monkeypatch, tmp_path):
    console, out, reported, host, vt = _wav_console(monkeypatch, tmp_path, feed_sleep=True)
    wav = _write_wav(tmp_path / "in.wav", _pcm(1.0))
    start = vt.t
    console.execute(f"/wav {wav}")
    times = [f[4] for f in host.fed]
    assert len(host.fed) == 26  # 1.0 秒音訊 + 1.6 秒尾端靜音，每塊 0.1 秒
    # 依截止時間配速：每塊的間隔就是一塊的長度，總長 = 音訊 + 尾端靜音（約 2.6 秒），不是十倍速
    assert all(abs((b - a) - 0.1) < 1e-6 for a, b in itertools.pairwise(times))
    assert abs(times[-1] - start - 2.5) < 0.01  # 第 26 塊在 2.5 秒送出（餵完後再等滿最後一塊的長度）
    assert "與音檔等長" in out.text and "辨識句" in out.text and len(reported) == 1


def test_wav_real_測試模式不等待(monkeypatch, tmp_path):
    console, out, _reported, host, vt = _wav_console(monkeypatch, tmp_path, feed_sleep=False)
    start = vt.t
    console.execute(f"/wav {_write_wav(tmp_path / 'in.wav', _pcm(1.0))}")
    assert host.fed[-1][4] - start < 0.01 and "與音檔等長" not in out.text


# ---- 端到端（真 Host＋聚合器＋DB）----

class _VoiceStage:
    """假 STT 輸入 stage：``feed`` 收到足夠的有聲塊就依序吐出 ``texts``（每句間隔 ``gap_s`` 秒），模擬 VAD 切成多段。"""

    def __init__(self, texts, gap_s: float = 0.05) -> None:
        self.texts, self.gap_s = list(texts), gap_s
        self._backend = FakeBackend("整段辨識")
        self.fed: list[bytes] = []
        self._trigger = False

    async def prepare(self) -> None:
        return None

    def feed(self, speaker_id, pcm, ts=None) -> None:
        self.fed.append(pcm)
        if any(pcm):
            self._trigger = True

    async def open(self, token):
        import asyncio

        from uep.stream.contracts.types import Utterance

        while not self._trigger:
            await asyncio.sleep(0.01)
        for text in self.texts:
            yield Utterance(text=text, speaker_id=CONSOLE_SPEAKER, language="zh", started_at=0.0, ended_at=1.0)
            await asyncio.sleep(self.gap_s)
        while True:
            await asyncio.sleep(0.05)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def voice_rt(session_factory, tmp_path):
    from uep.console.rig import build_fake_runtime
    from uep.echo.inference_wiring import build_voice_aggregator

    made = []

    def make(texts=("第一段話", "第二段話", "第三段話"), *, max_wait_ms=400, with_aggregator=True, **kw):
        from uep.turn.decision_config import AggregatorConfig, DecisionConfig

        stage = _VoiceStage(texts)
        decision = DecisionConfig(aggregator=AggregatorConfig(merge_gap_ms=1000, max_wait_ms=max_wait_ms))
        host_kwargs = {"aggregator": build_voice_aggregator(decision)} if with_aggregator else {}
        rt = build_fake_runtime(session_factory, tmp_path / "e2e", input_stage=stage, host_kwargs=host_kwargs, **kw)
        made.append(rt)
        out = Capture()
        console = Console(rt, out=out, wait_s=10.0, stt_wait_s=10.0, feed_sleep=False, settle_s=0.3, input_fn=lambda _p: "")
        return console, rt, out, stage

    yield make
    for rt in made:
        rt.core.shutdown()


def test_e2e_語音多段經聚合器併成一個_turn(voice_rt):
    console, rt, out, _stage = voice_rt()
    console.execute(f"/wav {_write_wav(Path(rt.session_dir) / 'in.wav', _pcm(0.5))}")
    utterances = [e for e in rt.intake_events if e[0] == "utterance"]
    turns = [e for e in rt.intake_events if e[0] == "turn"]
    assert len(utterances) == 3 and all(r.reason_code == "buffered" for _k, _f, r in utterances)
    assert len(turns) == 1 and turns[0][1].text == "第一段話 第二段話 第三段話"
    assert "聚合 turn" in out.text and out.text.count("turn.completed") == 1 and "turn.cancelled" not in out.text


def test_e2e_文字路徑不被聚合器緩衝(voice_rt):
    console, rt, out, _stage = voice_rt(max_wait_ms=3000)
    t0 = time.perf_counter()
    console.execute("/say 你好")
    assert time.perf_counter() - t0 < 2.5  # 不必等語音聚合器的 3 秒
    fact, result = rt.submissions[-1]
    assert result.ticket is not None and result.reason_code != "buffered" and fact.from_voice is False
    assert "turn.completed" in out.text


def test_e2e_沒有聚合器時語音一句一個_turn(voice_rt):
    console, rt, _out, _stage = voice_rt(("只有一句",), with_aggregator=False)
    console.execute(f"/wav {_write_wav(Path(rt.session_dir) / 'in.wav', _pcm(0.5))}")
    results = [r for k, _f, r in rt.intake_events if k == "utterance"]
    assert len(results) == 1 and results[0].ticket is not None and results[0].reason_code != "buffered"


def test_e2e_mic_只產生一個_turn_且不經_VAD(voice_rt):
    console, rt, out, stage = voice_rt(("不該出現",))
    pcm = _pcm(1.0)
    console.mic_factory = lambda: FakeMic(pcm)
    console.execute("/mic")
    assert stage.fed == [] and stage._backend.calls == [(len(pcm), 16000)]
    assert [f.text for f, _r in rt.submissions] == ["整段辨識"] and out.text.count("turn.completed") == 1
    assert rt.submissions[0][1].reason_code != "buffered"  # 不經聚合器等待


def test_e2e_stream_以假麥克風跑通並落地_wav(voice_rt):
    console, rt, out, _stage = voice_rt()
    mic = FakeStreamMic([_pcm(0.1)] * 5)
    console.stream_mic_factory = lambda: mic
    console.input_fn = lambda _p: mic.exhausted.wait(5.0) and ""
    console.execute("/stream")
    wavs = list(Path(rt.session_dir).glob("stream-*.wav"))
    assert len(wavs) == 1 and _wav_frames(wavs[0]) == (5 * 1600, 16000)
    assert "utterance 3 句；turn 1 個" in out.text and "turn.completed" in out.text
