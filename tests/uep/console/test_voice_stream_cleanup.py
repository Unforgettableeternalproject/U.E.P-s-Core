"""/mic 逾時提示、/stream 例外路徑收尾、tick 無 ticket 的顯示，以及語音聚合器依 proposer 能力啟用。

前半不需要 PostgreSQL；最後的 RuleProposer 模式翻轉為 DB 測試（未設 UEP_TEST_DATABASE_URL 時 skip）。
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from tests.uep.console.test_console import Capture
from tests.uep.console.test_voice_stream import (
    FakeBackend,
    FakeMic,
    FakeStreamMic,
    StubHost,
    _console,
    _pcm,
    _runtime,
    _wav_frames,
)
from uep.console.commands import Console
from uep.console.rig import CONSOLE_SPEAKER
from uep.turn.models import IntakeResult

# ---- /mic：逾時與無結果 ----


def test_mic_backend_回_None_時說明可能逾時(tmp_path):
    class NoneBackend:
        direct_timeout_s = None

        def transcribe(self, pcm, sample_rate):
            return None

    host = StubHost()
    rt = _runtime(tmp_path, host, backend=NoneBackend())
    console, out, reported = _console(rt, mic_factory=lambda: FakeMic(_pcm(1.0)))
    console.execute("/mic")
    assert "無結果（可能逾時、被過濾或純雜訊）" in out.text and host.submitted == [] and reported == []


def test_mic_錄音達到整段辨識逾時上限時提醒改用_stream(tmp_path):
    backend = FakeBackend()
    backend.direct_timeout_s = 2.0
    rt = _runtime(tmp_path, StubHost(), backend=backend)
    console, out, _ = _console(rt, mic_factory=lambda: FakeMic(_pcm(2.5)))
    console.execute("/mic")
    assert "逾時上限（2 秒" in out.text and "/stream" in out.text
    backend2 = FakeBackend()
    backend2.direct_timeout_s = 10.0
    rt2 = _runtime(tmp_path / "b", StubHost(), backend=backend2)
    console2, out2, _ = _console(rt2, mic_factory=lambda: FakeMic(_pcm(2.5)))
    console2.execute("/mic")
    assert "可能逾時而無結果" not in out2.text


# ---- /stream：例外路徑與收尾 ----


def _live_stream_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("console-stream-") and t.is_alive()]


def test_stream_mic_stop_丟例外時仍關_wav_停_reporter_並印摘要(tmp_path):
    class StopFails(FakeStreamMic):
        def stop(self) -> None:
            super().stop()
            raise RuntimeError("裝置已移除")

    rt = _runtime(tmp_path, StubHost(), backend=FakeBackend())
    mic = StopFails([_pcm(0.1)] * 3)
    console, out, _ = _console(rt, stream_mic_factory=lambda: mic)
    console.input_fn = lambda _p: mic.exhausted.wait(5.0) and ""
    console.execute("/stream")
    assert mic.stopped and "收尾「停止麥克風」失敗" in out.text and "裝置已移除" in out.text
    wavs = list((tmp_path / "session").glob("stream-*.wav"))
    assert len(wavs) == 1 and _wav_frames(wavs[0])[0] == 3 * 1600  # wav 仍正常關閉、內容完整
    assert "/stream 結束" in out.text and _live_stream_threads() == []


def test_stream_wav_開檔失敗時關麥克風且不留執行緒(monkeypatch, tmp_path):
    import wave as wave_mod

    def boom(*_a, **_k):
        raise OSError("磁碟已滿")

    monkeypatch.setattr(wave_mod, "open", boom)
    rt = _runtime(tmp_path, StubHost(), backend=FakeBackend())
    mic = FakeStreamMic([_pcm(0.1)] * 3)
    console, out, _ = _console(rt, stream_mic_factory=lambda: mic)
    console.execute("/stream")
    assert mic.started and mic.stopped  # 麥克風已關
    assert "指令失敗" in out.text and "磁碟已滿" in out.text and _live_stream_threads() == []


def test_stream_收音執行緒失敗時印出一行錯誤並停止收音(tmp_path):
    class ReadFails(FakeStreamMic):
        n = 0

        def read(self, timeout_s: float):
            self.n += 1
            if self.n > 2:
                raise RuntimeError("讀取失敗")
            return super().read(timeout_s)

    rt = _runtime(tmp_path, StubHost(), backend=FakeBackend())
    mic = ReadFails([_pcm(0.1)] * 5)
    console, out, _ = _console(rt, stream_mic_factory=lambda: mic)

    def wait_for_error(_p):
        deadline = time.monotonic() + 5.0
        while "收音執行緒失敗" not in out.text and time.monotonic() < deadline:
            time.sleep(0.01)
        return ""

    console.input_fn = wait_for_error
    console.execute("/stream")
    assert "收音執行緒失敗（RuntimeError：讀取失敗）" in out.text and "/stream 結束" in out.text
    assert _live_stream_threads() == []


def test_stream_等待時按_Ctrl_C_也走完收尾(tmp_path):
    rt = _runtime(tmp_path, StubHost(), backend=FakeBackend())
    mic = FakeStreamMic([_pcm(0.1)] * 3)
    console, out, _ = _console(rt, stream_mic_factory=lambda: mic)

    def interrupt(_p):
        mic.exhausted.wait(5.0)
        raise KeyboardInterrupt

    console.input_fn = interrupt
    console.execute("/stream")
    assert mic.stopped and "收到中斷" in out.text and "/stream 結束" in out.text and _live_stream_threads() == []


def test_stream_停止後先停麥克風再排乾佇列_不掉最後一段(tmp_path):
    class Late(FakeStreamMic):
        """stop() 時才把最後一塊放進佇列：必須在 mic.stop() 之後排乾才不會掉。"""

        def stop(self) -> None:
            self._chunks.append(_pcm(0.1))
            super().stop()

    rt = _runtime(tmp_path, StubHost(), backend=FakeBackend())
    mic = Late([_pcm(0.1)] * 2)
    console, _out, _ = _console(rt, stream_mic_factory=lambda: mic)
    console.input_fn = lambda _p: mic.exhausted.wait(5.0) and ""
    console.execute("/stream")
    wavs = list((tmp_path / "session").glob("stream-*.wav"))
    assert _wav_frames(wavs[0])[0] == 3 * 1600


def test_stream_tick_交出但沒有_ticket_的語音_turn_印出原因(tmp_path):
    host = StubHost()
    rt = _runtime(tmp_path, host, backend=FakeBackend())

    def on_feed(n: int) -> None:
        if n == 2:
            fact = SimpleNamespace(text="路人的話")
            rt.intake_events.append(("turn", fact, IntakeResult(None, "pass_not_addressed", fact=fact)))

    host.on_feed = on_feed
    mic = FakeStreamMic([_pcm(0.1)] * 3)
    out = Capture()
    console = Console(
        rt, out=out, wait_s=1.0, stt_wait_s=2.0, feed_sleep=False, settle_s=0.1,
        input_fn=lambda _p: mic.exhausted.wait(5.0) and "", stream_mic_factory=lambda: mic,
    )
    console.execute("/stream")
    assert "聚合 turn：路人的話" in out.text and "未開 turn：pass_not_addressed" in out.text
    assert "turn 0 個" in out.text


def test_feed_audio_tick_交出但沒有_ticket_時印出原因(tmp_path):
    host = StubHost()
    rt = _runtime(tmp_path, host, backend=FakeBackend())

    def on_feed(n: int) -> None:
        fact = SimpleNamespace(text="一句")
        if n == 3:
            rt.intake_events.append(("utterance", fact, IntakeResult(None, "buffered")))
        if n == 4:
            rt.intake_events.append(("turn", fact, IntakeResult(None, "pass_not_addressed", fact=fact)))

    host.on_feed = on_feed
    out = Capture()
    console = Console(rt, out=out, wait_s=1.0, stt_wait_s=2.0, feed_sleep=False, settle_s=0.1, input_fn=lambda _p: "")
    console._feed_audio(_pcm(0.5))  # 真實的 _report_turn：無 ticket 時只印原因，不查 DB
    assert "未開 turn：pass_not_addressed" in out.text


def test_proposer_不支援模式切換時不建語音聚合器並記_INFO(caplog):
    from uep.echo.inference_wiring import build_voice_aggregator
    from uep.turn.decision_config import DecisionConfig

    with caplog.at_level("INFO", logger="uep.echo.inference_wiring"):
        assert build_voice_aggregator(DecisionConfig(proposer_id="respond.minimal")) is None
    assert any("respond.minimal" in r.getMessage() and "不啟用語音聚合器" in r.getMessage() for r in caplog.records)
    assert build_voice_aggregator(DecisionConfig()) is not None  # rules.default 支援


def test_正式組裝在_proposer_不支援模式切換時不帶_aggregator(monkeypatch):
    from uep.config import get_settings
    from uep.echo.inference_wiring import InferenceConfig, build_inference_wiring

    class Stt:
        def transcribe(self, pcm, sample_rate):
            return None

    class Tts:
        def load(self):
            return None

    class Llm:
        async def stream_query(self, messages, system_instruction=None, **kwargs):
            yield "好"

    monkeypatch.setattr(get_settings(), "decision_proposer", "respond.minimal")
    wiring = build_inference_wiring(InferenceConfig(), tts_backend=Tts(), think_backend=Llm(), stt_backend=Stt())
    assert "aggregator" not in wiring.host_kwargs()


# ---- 端到端：真實 RuleProposer 的模式翻轉（DB）----


class _PushStage:
    """假 STT 輸入 stage：測試以 ``push`` 隨時送出一句語音 utterance。"""

    def __init__(self) -> None:
        self._backend = FakeBackend("x")
        self._items: list[str] = []

    def push(self, text: str) -> None:
        self._items.append(text)

    async def prepare(self) -> None:
        return None

    def feed(self, speaker_id, pcm, ts=None) -> None:
        return None

    async def open(self, token):
        import asyncio

        from uep.stream.contracts.types import Utterance

        sent = 0
        while True:
            while sent < len(self._items):
                yield Utterance(
                    text=self._items[sent], speaker_id=CONSOLE_SPEAKER, language="zh", started_at=0.0, ended_at=1.0,
                )
                sent += 1
            await asyncio.sleep(0.02)

    async def aclose(self) -> None:
        return None


def _wait_for(predicate, timeout: float = 10.0) -> float:
    t0 = time.monotonic()
    while not predicate():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("等待逾時")
        time.sleep(0.02)
    return time.monotonic() - t0


def test_e2e_第一個語音_turn_走被動門檻_之後翻主動套用短門檻(session_factory, tmp_path):
    from uep.console.rig import build_fake_runtime
    from uep.echo.inference_wiring import build_voice_aggregator
    from uep.turn.decision_config import AggregatorConfig, DecisionConfig

    cfg = AggregatorConfig(merge_gap_ms=1000, max_wait_ms=1500, active_merge_gap_ms=250, active_max_wait_ms=250)
    stage = _PushStage()
    rt = build_fake_runtime(
        session_factory, tmp_path / "e2e", input_stage=stage,
        host_kwargs={"aggregator": build_voice_aggregator(DecisionConfig(aggregator=cfg))},
    )
    try:
        voice = rt.host.orchestrator.aggregator.voice
        assert voice.max_wait_s == 1.5  # 啟動時被動門檻
        stage.push("第一句話")
        first = _wait_for(lambda: any(k == "turn" for k, _f, _r in rt.intake_events))
        assert first >= 1.3  # 第一個語音 turn 以被動門檻（max_wait 1.5 秒）等待
        assert voice.max_wait_s == 0.25 and voice.merge_gap_s == 0.25  # 第一次決策後 RuleProposer 翻主動
        stage.push("第二句話")
        second = _wait_for(lambda: sum(1 for k, _f, _r in rt.intake_events if k == "turn") >= 2)
        assert second < 1.0  # 之後套用主動短門檻
    finally:
        rt.core.shutdown()
