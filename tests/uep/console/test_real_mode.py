"""``--mode real`` 的 CPU／fake 自動化：組裝與 warmup 順序、GPU 守門、/mem 指令層、/mic 與 /wav 走 STT 路徑（假音源）。

不載入任何真實 GPU 模型、不打真實 API：STT 輸入 stage 用假 stage，記憶 Host 用 fake engine，embedding 暖機以 monkeypatch 計數。
"""
from __future__ import annotations

import asyncio
import types
import wave
from pathlib import Path

import pytest

from tests.uep.console.test_console import Capture
from uep.console import app, real_rig
from uep.console.commands import Console
from uep.console.rig import ConsoleError, build_fake_runtime, build_runtime
from uep.fakes.memory import FakeEngineFactory
from uep.memory import MemoryHost, NamespaceSpec
from uep.stream.contracts.types import Utterance


class FakeBackend:
    """假 STT backend：``/mic`` 整段辨識直接用它（不經 VAD）。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[int] = []

    def transcribe(self, pcm: bytes, sample_rate: int):
        from uep.stream.adapters.input_stt import TranscriptionResult

        self.calls.append(len(pcm))
        return TranscriptionResult(text=self.text, language="zh", confidence=0.9)


class FakeInputStage:
    """假 STT 輸入：收到「有聲後的靜音」就吐一句固定文字（模擬 VAD 斷句＋辨識）。"""

    def __init__(self, text: str = "你好小橘") -> None:
        self.text = text
        self._backend = FakeBackend(text)
        self.prepared = False
        self.fed: list[tuple[str, int]] = []
        self._heard = False
        self._done = False
        self._emitted = False

    async def prepare(self) -> None:
        self.prepared = True

    def feed(self, speaker_id: str, pcm: bytes, ts: float | None = None) -> None:
        self.fed.append((speaker_id, len(pcm)))
        silent = not any(pcm)
        if not silent:
            self._heard = True
        elif self._heard:
            self._done = True

    async def open(self, token):
        while not self._emitted:
            if self._done:
                self._emitted = True
                yield Utterance(text=self.text, speaker_id="console:owner", language="zh")
                return
            await asyncio.sleep(0.01)

    async def aclose(self) -> None:
        return None


class FakeMic:
    def __init__(self, pcm: bytes) -> None:
        self._pcm = pcm
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> bytes:
        return self._pcm


def _tone(seconds: float = 0.5) -> bytes:
    import numpy as np

    t = np.arange(int(16000 * seconds)) / 16000
    return (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).tobytes()


def _write_wav(path: Path, pcm: bytes) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return path


@pytest.fixture
def real_like(session_factory, tmp_path):
    """Think／Speak fake，但帶假 STT 輸入 stage 與 fake engine 的記憶 Host（real 模式的形狀）。"""
    made = []
    factory = FakeEngineFactory()

    def make(**over):
        warmups: dict[str, float] = {}
        stage = FakeInputStage()
        real_rig.timed_prepare(stage, "stt", warmups)

        def memory_factory(publisher, identity):
            def engine_factory(ns, root):
                engine = factory(ns, root)
                episodes = [types.SimpleNamespace(
                    timestamp=__import__("datetime").datetime(2026, 11, 2, 12, 0, 0), session_id="seg_abcdef12",
                    content_summary="使用者養了一隻叫小橘的貓")]
                engine.episodic = types.SimpleNamespace(count=lambda: 1, list_episodes=lambda limit=5: episodes)
                return engine

            return MemoryHost(
                engine_factory=engine_factory, data_root=tmp_path / "mem", namespaces=(NamespaceSpec(real_rig.MEMORY_NAMESPACE, owner_id="owner"),),
                identity=identity, publisher=publisher, llm_fn=lambda p: "ok", drain_timeout_s=1.0,
            )

        rt = build_fake_runtime(
            session_factory, tmp_path / "e2e", input_stage=stage, memory_factory=memory_factory,
            memory_root=tmp_path / "mem", warmups=warmups, **over,
        )
        out = Capture()
        made.append(rt)
        console = Console(
            rt, out=out, wait_s=10.0, stt_wait_s=10.0, feed_sleep=False, settle_s=0.2,
            input_fn=lambda _p: "", mic_factory=over.pop("mic_factory", None),
        )
        return console, rt, out, stage

    yield make
    for rt in made:
        rt.core.shutdown()


# ---- GPU 守門與組裝選項 ----

def test_real_模式未設_GPU_旗標清楚報錯且不組裝(monkeypatch, session_factory, tmp_path):
    monkeypatch.delenv("UEP_E2E_ALLOW_GPU", raising=False)
    monkeypatch.setattr("uep.console.rig.real_think_factory", lambda: (lambda tracer: pytest.fail("不應建 Think")))
    with pytest.raises(ConsoleError, match="UEP_E2E_ALLOW_GPU=1"):
        build_runtime("real", session_factory, tmp_path)
    with pytest.raises(ConsoleError, match="UEP_E2E_ALLOW_GPU=1"):
        build_runtime("fake", session_factory, tmp_path, stt="real")
    with pytest.raises(ConsoleError, match="未知的 stt"):
        build_runtime("fake", session_factory, tmp_path, stt="bogus")
    with pytest.raises(ConsoleError, match="未知的 memory"):
        build_runtime("fake", session_factory, tmp_path, memory="bogus")


def test_main_real_未設_GPU_旗標_exit2(monkeypatch, session_factory, tmp_path):
    monkeypatch.delenv("UEP_E2E_ALLOW_GPU", raising=False)
    monkeypatch.setattr("uep.console.rig.real_think_factory", lambda: None)
    monkeypatch.setattr(app, "connect_database", lambda: session_factory)
    out = Capture()
    code = app.main(["--mode", "real", "--session-dir", str(tmp_path)], input_fn=lambda _p: "/quit", out=out)
    assert code == 2 and "UEP_E2E_ALLOW_GPU=1" in out.text


def test_記憶資料根預設為全新開發目錄且可由設定覆寫(tmp_path):
    from uep.config import UepSettings

    default_settings = UepSettings(_env_file=None, UEP_MEMORY_DATA_ROOT=None)
    root, is_default = real_rig.resolve_memory_root(types.SimpleNamespace(memory_data_root=None))
    assert is_default and root.name == "console-memory-bgem3" and ".uep" in root.parts
    root, is_default = real_rig.resolve_memory_root(types.SimpleNamespace(memory_data_root=tmp_path / "x"))
    assert not is_default and root == tmp_path / "x"
    assert default_settings.memory_data_root is None


def test_tts_引擎預設_vox_明確設定才改(monkeypatch):
    from uep.config import UepSettings

    monkeypatch.delenv("UEP_INFER_TTS_ENGINE", raising=False)
    assert real_rig.tts_engine(UepSettings(_env_file=None)) == "vox"
    monkeypatch.setenv("UEP_INFER_TTS_ENGINE", "indextts2")
    assert real_rig.tts_engine(UepSettings(_env_file=None)) == "indextts2"


def test_host_啟動逾時沿用設定否則由_prepare_加總推得():
    base = dict(infer_speak_prepare_timeout_s=None, infer_think_prepare_timeout_s=8.0, infer_input_prepare_timeout_s=10.0)
    auto = real_rig.host_start_timeout_s(types.SimpleNamespace(infer_host_start_timeout_s=None, **base), "vox")
    assert auto == 240.0 + 8.0 + 10.0 + real_rig.START_MARGIN_S
    assert real_rig.host_start_timeout_s(types.SimpleNamespace(infer_host_start_timeout_s=500.0, **base), "vox") == 500.0


# ---- 組裝順序與 warmup ----

def test_warmup_耗時記錄且_embedding_先於_stage_prepare(monkeypatch, real_like):
    calls = []
    monkeypatch.setattr("uep.echo.memory_wiring.warmup_embedding", lambda model, device: calls.append((model, device)), raising=False)
    warmups: dict[str, float] = {}
    inner_calls = []
    factory = real_rig.WarmEngineFactory(
        lambda ns, root: inner_calls.append(ns) or object(), warmups, model="BAAI/bge-m3", device="cuda", backend="sentence-transformers",
    )
    factory("ns1", Path("x"))
    assert calls == [("BAAI/bge-m3", "cuda")] and inner_calls == ["ns1"] and "memory_embedding" in warmups
    console, rt, out, stage = real_like()
    assert stage.prepared and "stt" in rt.warmups and rt.host.started  # Host 啟動時 prepare 完成後才接受輸入
    assert rt.memory_host.started


def test_ollama_後端不走共用_warmup(monkeypatch):
    calls = []
    monkeypatch.setattr("uep.echo.memory_wiring.warmup_embedding", lambda m, d: calls.append(1), raising=False)
    real_rig.WarmEngineFactory(lambda ns, root: object(), {}, model="bge-m3", device="cpu", backend="ollama")("n", Path("x"))
    assert calls == []


def test_report_startup_印出各_warmup_與資料根(real_like):
    _console, rt, _out, _stage = real_like()
    rt.warmups.update({"tts": 52.1, "think": 0.2, "memory_embedding": 9.5})
    out = Capture()
    app.report_startup(rt, out)
    assert "52.10 秒" in out.text and "9.50 秒" in out.text and "STT" in out.text and str(rt.memory_root) in out.text


# ---- /mem ----

def test_mem_顯示_namespace_episode_數與最近寫入(real_like):
    console, _rt, out, _stage = real_like()
    console.execute("/mem")
    assert real_rig.MEMORY_NAMESPACE in out.text and "episode 數：1" in out.text and "小橘" in out.text


def test_mem_recall_走_access_layer(real_like):
    console, rt, out, _stage = real_like()
    console.execute("/mem recall 我的貓")
    assert "recall" in out.text and "狀態=" in out.text
    console.execute("/mem recall")
    assert "用法：/mem recall" in out.text


def test_mem_fake_模式提示未組裝(session_factory, tmp_path):
    rt = build_fake_runtime(session_factory, tmp_path / "e2e")
    try:
        out = Capture()
        Console(rt, out=out).execute("/mem 貓")
        assert "未組裝記憶" in out.text
    finally:
        rt.core.shutdown()


# ---- /wav 與 /mic 走 STT 路徑 ----

def test_wav_real_餵進_host_輸入_stage_並跑完整個_turn(real_like, tmp_path):
    console, rt, out, stage = real_like()
    wav = _write_wav(tmp_path / "in.wav", _tone())
    console.execute(f"/wav {wav}")
    assert stage.fed and all(speaker == "console:owner" for speaker, _ in stage.fed)
    assert "STT 辨識" in out.text and "你好小橘" in out.text and "turn.completed" in out.text
    assert rt.submissions and rt.submissions[0][0].text == "你好小橘"


def test_mic_假音源_enter_開始結束後整段辨識成一句並跑整條_turn(real_like):
    console, rt, out, stage = real_like()
    pcm = _tone()
    mic = FakeMic(pcm)
    console.mic_factory = lambda: mic
    prompts = []
    console.input_fn = lambda p: prompts.append(p) or ""
    console.execute("/mic")
    assert mic.started and len(prompts) == 2
    assert "錄到" in out.text and "你好小橘" in out.text and "turn.completed" in out.text
    assert stage.fed == [] and stage._backend.calls == [len(pcm)]  # 整段交給 backend，沒有經過 host.feed／VAD
    assert len(rt.submissions) == 1 and out.text.count("turn.completed") == 1  # 只有一句、一個 turn
    assert list((rt.session_dir).glob("mic-*.wav"))  # 錄音落地


def test_mic_沒有真實_stt_時提示(session_factory, tmp_path):
    rt = build_fake_runtime(session_factory, tmp_path / "e2e")
    try:
        out = Capture()
        Console(rt, out=out).execute("/mic")
        assert "需要真實 STT" in out.text
    finally:
        rt.core.shutdown()


def test_mic_裝置不可用時印原因不中止(real_like):
    from uep.console.audio_io import AudioIoError

    console, _rt, out, _stage = real_like()

    class Broken:
        def start(self):
            raise AudioIoError("缺少 sounddevice")

        def stop(self):
            return b""

    console.mic_factory = Broken
    assert console.execute("/mic") is True
    assert "缺少 sounddevice" in out.text


def test_session_new_換_conversation(real_like):
    console, rt, out, _stage = real_like()
    before = rt.host._session.segment_id
    console.execute("/session new")
    assert rt.host._session.segment_id != before and "已開新 session" in out.text


def test_play_缺_sounddevice_時清楚報錯(monkeypatch, session_factory, tmp_path):
    from uep.console import audio_io

    monkeypatch.setattr(audio_io, "_import_sounddevice", lambda: (_ for _ in ()).throw(audio_io.AudioIoError("缺少 sounddevice")))
    with pytest.raises(ConsoleError, match="sounddevice"):
        build_runtime("fake", session_factory, tmp_path, play=True)


def test_記憶_embedding_預設_cpu_可由設定覆寫():
    from uep.config import UepSettings

    assert real_rig.memory_device(UepSettings(_env_file=None, UEP_MEMORY_EMBEDDING_DEVICE=None)) == "cpu"
    assert real_rig.memory_device(types.SimpleNamespace(memory_embedding_device="cuda", memory_device="cpu")) == "cuda"
    assert real_rig.memory_device(types.SimpleNamespace(memory_embedding_device=None, memory_device="cuda")) == "cuda"


# ---- /wav 長音檔：VAD 切成多段 utterance ----

ZH_WAV = Path(__file__).resolve().parents[3] / "subsystems" / "echo_stt" / "tests" / "data" / "zh_test.wav"


class _SeqBackend:
    """假 STT backend：每段回「第 N 句」，記錄每段長度（秒）。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def transcribe(self, pcm: bytes, sample_rate: int):
        from uep.stream.adapters.input_stt import TranscriptionResult

        self.calls.append(len(pcm) / 2 / sample_rate)
        return TranscriptionResult(text=f"第{len(self.calls)}句", language="zh", confidence=0.9)


@pytest.mark.skipif(not ZH_WAV.exists(), reason="缺 zh_test.wav")
def test_wav_長音檔被切成多句時追到最後一個_turn(session_factory, tmp_path):
    """回歸：zh_test.wav 在 2.9–3.6 秒有 0.7 秒停頓，被切成兩句；後句開的 turn 依「同 owner 新回覆取代」
    取消前句（設計行為）。原本 console 只追第一個 turn，因此回報「turn.cancelled」而跑不完整一輪。"""
    from uep.fakes.stream.stages import FakeThinkStage
    from uep.stream.adapters.input_multichannel import MultiChannelSttInputStage

    backend = _SeqBackend()

    def slow_think(tracer):
        return FakeThinkStage(
            responses=["好的，我來回答。"], first_token_delay_s=0.0, token_interval_s=0.5, memory_delay_s=0.0, tracer=tracer,
        )

    rt = build_fake_runtime(
        session_factory, tmp_path / "e2e", input_stage=MultiChannelSttInputStage(backend=backend), think_factory=slow_think,
    )
    try:
        out = Capture()
        console = Console(rt, out=out, wait_s=20.0, stt_wait_s=20.0, feed_sleep=False, settle_s=3.0, input_fn=lambda _p: "")
        console.execute(f"/wav {ZH_WAV}")
        assert len(backend.calls) == 2 and len(rt.submissions) == 2  # VAD 把音檔切成兩段 utterance
        first, second = (result.ticket.turn_id for _fact, result in rt.submissions)
        assert f"turn {first}：turn.cancelled" in out.text  # 前句被後句取代（設計行為）
        assert f"turn {second}：turn.completed" in out.text and "回應：好的，我來回答。" in out.text
        assert console.last_turn_id == second
        assert console._turn_kinds(first)[-1] == "turn.cancelled"
    finally:
        rt.core.shutdown()
