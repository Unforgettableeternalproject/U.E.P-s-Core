"""P3 S6：推理 Host 組裝入口（uep/echo/inference_wiring.py）。

只用 fake backend：不載模型、不碰 GPU、不連資料庫、不啟動 Host。
涵蓋：建構不載模型、warmup 旗標、backend 注入共用、設定載入與 fail-closed、prepare 成功／失敗／逾時的降級、
關閉降級時沿用原行為、指標與 attach_metrics。
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

pytest.importorskip("echo_stream")

from echo_stream.contracts.cancellation import CancellationToken
from echo_stream.contracts.types import Sentence

from uep.config import UepSettings
from uep.echo.inference_wiring import (
    InferenceConfig,
    InferenceWiringError,
    StageNotReady,
    StageReadiness,
    _InputGuard,
    _ThinkGuard,
    build_inference_wiring,
)
from uep.echo.metrics import Metrics


class FakeTtsBackend:
    """只記錄 load／generate 次數；``load_error`` 例外、``block`` 阻塞到事件被設定（模擬載入過慢）。"""

    def __init__(self, *, load_error: Exception | None = None, block: threading.Event | None = None) -> None:
        self.load_calls = 0
        self.generate_calls = 0
        self.emotion_vector = None
        self._load_error = load_error
        self._block = block

    def load(self) -> None:
        self.load_calls += 1
        if self._block is not None:
            self._block.wait(timeout=5)
        if self._load_error is not None:
            raise self._load_error

    def generate(self, text, path, on_segment_audio=None, language=None, emotion_overrides=None):
        self.generate_calls += 1


class FakeLlm:
    async def stream_query(self, messages, system_instruction=None, **kwargs):
        yield "好"


class FakeStt:
    def transcribe(self, pcm, sample_rate):
        return None


def build(*, tts=None, cfg=None, **kw):
    cfg = cfg or InferenceConfig(stt_mode="single")
    return build_inference_wiring(
        cfg, tts_backend=tts or FakeTtsBackend(), think_backend=FakeLlm(), stt_backend=FakeStt(), **kw
    )


def run(coro):
    return asyncio.run(coro)


async def _drain(wiring_speak, texts=("你好。", "再見。")):
    async def sentences():
        for i, t in enumerate(texts):
            yield Sentence(text=t, turn_id="t1", index=i, is_first=i == 0, is_last=i == len(texts) - 1)

    return [c async for c in wiring_speak.stream(sentences(), CancellationToken())]


# ---- 建構 ----

def test_建構不載模型且共用同一個預建_backend():
    tts = FakeTtsBackend()
    w = build(tts=tts)
    assert tts.load_calls == 0 and tts.generate_calls == 0
    assert w.speak._backend is tts  # 預建一次、注入（所有 turn 共用）
    assert w.speak.warmup is True  # 預設在 prepare（Host 啟動）做暖機
    assert {k: r.ready for k, r in w.readiness.items()} == {"speak": None, "think": None, "input": None}
    assert set(w.host_kwargs()) == {"think", "speak", "input_stage"}
    assert not w.voice_ready


def test_設定傳到_speak_stage():
    cfg = InferenceConfig(
        stt_mode="single", tts_device="cpu", tts_fp16=False, tts_warmup=False, tts_speed=0.2, tts_lora_scale=0.5,
        tts_language="zh", system_prompt="你是 UEP",
    )
    w = build(cfg=cfg)
    assert (w.speak.device, w.speak.use_fp16, w.speak.warmup) == ("cpu", False, False)
    assert (w.speak.speed, w.speak.lora_scale, w.speak.language) == (0.2, 0.5, "zh")


def test_stt_模式選擇():
    from echo_stream.adapters.input_multichannel import MultiChannelSttInputStage
    from echo_stream.adapters.input_stt import SttInputStage

    assert isinstance(build(cfg=InferenceConfig(stt_mode="multichannel")).input_stage, MultiChannelSttInputStage)
    assert isinstance(build(cfg=InferenceConfig(stt_mode="single")).input_stage, SttInputStage)
    with pytest.raises(InferenceWiringError):
        InferenceConfig(stt_mode="bogus")


def test_缺子系統路徑_fail_closed():
    # 沒注入 backend 且沒設路徑：不猜同層目錄，直接拒絕組裝
    with pytest.raises(InferenceWiringError, match="UEP_INFER_TTS_REPO"):
        build_inference_wiring(InferenceConfig(), think_backend=FakeLlm(), stt_backend=FakeStt())
    with pytest.raises(InferenceWiringError, match="UEP_INFER_STT_REPO"):
        build_inference_wiring(InferenceConfig(), tts_backend=FakeTtsBackend(), think_backend=FakeLlm())
    with pytest.raises(InferenceWiringError, match="UEP_INFER_SESSION_REPO"):
        build_inference_wiring(InferenceConfig(), tts_backend=FakeTtsBackend(), stt_backend=FakeStt())
    with pytest.raises(InferenceWiringError, match="不存在"):
        build_inference_wiring(
            InferenceConfig(tts_repo=Path("Z:/不存在的路徑")), think_backend=FakeLlm(), stt_backend=FakeStt(),
        )


def test_from_settings_讀環境變數(monkeypatch, tmp_path):
    monkeypatch.setenv("UEP_INFER_TTS_REPO", str(tmp_path))
    monkeypatch.setenv("UEP_INFER_STT_MODE", "single")
    monkeypatch.setenv("UEP_INFER_TTS_WARMUP", "false")
    monkeypatch.setenv("UEP_INFER_SPEAK_PREPARE_TIMEOUT_S", "12.5")
    monkeypatch.setenv("UEP_INFER_DEGRADE_ON_FAILURE", "0")
    cfg = InferenceConfig.from_settings(UepSettings(_env_file=None))
    assert cfg.tts_repo == tmp_path and cfg.stt_mode == "single"
    assert cfg.tts_warmup is False and cfg.speak_prepare_timeout_s == 12.5 and cfg.degrade_on_failure is False
    assert cfg.stt_repo is None and cfg.tts_device == "cuda"


# ---- prepare 與降級 ----

def test_speak_prepare_成功只載入一次():
    tts = FakeTtsBackend()
    w = build(tts=tts)

    async def go():
        await w.speak.prepare()
        await w.speak.prepare()

    run(go())
    assert tts.load_calls == 1
    assert tts.generate_calls == 1  # warmup 在 prepare 內做一次
    assert w.voice_ready and w.readiness["speak"].elapsed_s is not None
    assert w.metrics["echo.speak_prepare_ok"] == 1


def test_speak_載入失敗_降級為無聲_不讓啟動失敗():
    tts = FakeTtsBackend(load_error=RuntimeError("CUDA OOM"))
    w = build(tts=tts)

    async def go():
        await w.speak.prepare()  # 不拋
        await w.speak.prepare()  # 失敗後不重試
        return await _drain(w.speak)

    chunks = run(go())
    assert chunks == []  # 句子被消耗、沒有音訊
    assert tts.load_calls == 1
    r = w.readiness["speak"]
    assert r.ready is False and "CUDA OOM" in (r.error or "") and not r.timed_out
    assert not w.voice_ready
    assert w.metrics["echo.speak_prepare_failed"] == 1 and w.metrics["echo.speak_not_ready_turns"] == 1


def test_speak_載入逾時_降級():
    gate = threading.Event()
    tts = FakeTtsBackend(block=gate)
    w = build(tts=tts, cfg=InferenceConfig(stt_mode="single", speak_prepare_timeout_s=0.05))

    async def go():
        try:
            await w.speak.prepare()
        finally:
            gate.set()  # 放行背景工作緒，測試才能乾淨結束
        return await _drain(w.speak)

    assert run(go()) == []
    r = w.readiness["speak"]
    assert r.ready is False and r.timed_out
    assert w.metrics["echo.speak_prepare_timeout"] == 1


def test_關閉降級_沿用原行為_例外上拋():
    w = build(
        tts=FakeTtsBackend(load_error=RuntimeError("boom")),
        cfg=InferenceConfig(stt_mode="single", degrade_on_failure=False),
    )
    with pytest.raises(RuntimeError, match="boom"):
        run(w.speak.prepare())
    assert w.readiness["speak"].ready is False  # 仍有紀錄與指標
    assert w.metrics["echo.speak_prepare_failed"] == 1


def test_取消不被當成失敗吞掉():
    gate = threading.Event()
    w = build(tts=FakeTtsBackend(block=gate))

    async def go():
        task = asyncio.ensure_future(w.speak.prepare())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        finally:
            gate.set()

    with pytest.raises(asyncio.CancelledError):
        run(go())
    assert w.readiness["speak"].ready is None  # 沒有被標成失敗，可再 prepare


class _BrokenStage:
    """think／input 的最小 stand-in：prepare 失敗。"""

    def __init__(self) -> None:
        self.prewarmed = False

    async def prepare(self) -> None:
        raise ConnectionError("no network")

    async def prewarm(self, prompt=None) -> bool:
        self.prewarmed = True
        return True


def test_think_prepare_失敗_stream_明確失敗_prewarm_略過():
    m = Metrics()
    stage = _BrokenStage()
    guard = _ThinkGuard(stage, StageReadiness("think"), timeout_s=1, metrics=lambda: m, degrade=True)

    async def go():
        await stage.prepare()
        assert await stage.prewarm("x") is False
        with pytest.raises(StageNotReady):
            async for _ in stage.stream(object(), CancellationToken()):
                pass

    run(go())
    assert not stage.prewarmed and guard.readiness.ready is False
    assert m["echo.think_prepare_failed"] == 1 and m["echo.think_not_ready_turns"] == 1


def test_input_prepare_失敗_open_明確失敗_交給_host_的輸入重啟():
    m = Metrics()
    stage = _BrokenStage()
    guard = _InputGuard(stage, StageReadiness("input"), timeout_s=1, metrics=lambda: m, degrade=True)

    async def go():
        await stage.prepare()
        with pytest.raises(StageNotReady):
            async for _ in stage.open(CancellationToken()):
                pass

    run(go())
    assert guard.readiness.ready is False and m["echo.input_not_ready_turns"] == 1


def test_attach_metrics_之後的指標進_host_metrics():
    w = build(tts=FakeTtsBackend(load_error=RuntimeError("x")))
    host_metrics = Metrics()
    w.attach_metrics(host_metrics)
    run(w.speak.prepare())
    assert host_metrics["echo.speak_prepare_failed"] == 1
    assert w.status()["speak"]["ready"] is False
