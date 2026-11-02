"""VoxBackend 情緒路徑 GPU 整合測試。預設跳過；設 VOX_GPU_TEST=1 並提供 VOX_MODEL_DIR、VOX_LORA_DIR 才跑。

驗證：情緒路徑（COMBO）出聲、生成中 LoRA scale 為 0.85（從模型 LoRALinear 讀）、回中性為 1.0、
首包與 RTF 對照中性路徑、INFO log 內容。模組內共用一個 backend。
"""
import logging
import os
import statistics
import time

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("VOX_GPU_TEST") != "1" or not os.environ.get("VOX_MODEL_DIR") or not os.environ.get("VOX_LORA_DIR"),
    reason="需要 VOX_GPU_TEST=1、VOX_MODEL_DIR、VOX_LORA_DIR",
)

TEXT = "週末我想去市場買點菜，順便繞去書店逛逛，晚上再回家煮一鍋熱湯給大家喝。"
LOGGER = "echo_tts.runtime.vox_backend"


@pytest.fixture(scope="module")
def backend():
    from echo_tts.runtime.vox_backend import VoxBackend

    b = VoxBackend({"model_dir": os.environ["VOX_MODEL_DIR"], "lora_dir": os.environ["VOX_LORA_DIR"]})
    t0 = time.perf_counter()
    b.prepare()
    print(f"\nprepare_s={time.perf_counter() - t0:.1f}")
    yield b
    b.unload()


# scaling buffer 是 bfloat16，0.85 實際存成 0.8516（最近的 bf16 值），容許 bf16 量化誤差
def _is(ratio, target):
    return abs(ratio - target) <= 0.005


def _lora_ratios(backend):
    """每個 LoRALinear 的 scaling / base_scaling（1.0 = alpha/r）。"""
    from voxcpm.modules.layers.lora import LoRALinear

    return [float(m.scaling) / float(m._base_scaling) for m in backend._model.tts_model.modules() if isinstance(m, LoRALinear)]


def _run(backend, **kw):
    t0 = time.perf_counter()
    first, chunks = None, []
    for c in backend.stream(TEXT, **kw):
        if first is None:
            first = time.perf_counter() - t0
        chunks.append(c)
    el = time.perf_counter() - t0
    w = np.concatenate(chunks)
    return w, first, el / (len(w) / backend.sample_rate)


def test_emotion_combo_audible(backend):
    w, first, rtf = _run(backend, emotion="happy", intensity=0.5)
    dur = len(w) / backend.sample_rate
    rms = float(np.sqrt(np.mean(w**2)))
    print(f"combo happy@0.5 audio_s={dur:.2f} first={first:.3f} rtf={rtf:.3f} rms={rms:.4f} peak={float(np.abs(w).max()):.3f}")
    assert np.isfinite(w).all() and w.dtype == np.float32
    assert 2.0 < dur < 20.0 and rms > 0.005 and float(np.abs(w).max()) > 0.05


@pytest.mark.parametrize("intensity,tier", [(0.0, 0.57), (0.5, 0.61), (1.0, 0.65)])
def test_lora_scale_during_emotion_and_back(backend, intensity, tier, caplog):
    """生成中（第一個 chunk 已出來）讀模型的 LoRA scaling = 0.85；generator 結束後回 1.0。"""
    with caplog.at_level(logging.INFO, logger=LOGGER):
        g = backend.stream(TEXT, emotion="sad", intensity=intensity)
        try:
            next(g)
            during = _lora_ratios(backend)
            for _ in g:
                pass
        finally:
            g.close()
    after = _lora_ratios(backend)
    print(f"tier={tier} LoRALinear n={len(during)} during min/max={min(during):.4f}/{max(during):.4f} "
          f"after min/max={min(after):.4f}/{max(after):.4f}")
    assert len(during) > 0
    assert all(_is(r, 0.85) for r in during)
    assert all(_is(r, 1.0) for r in after)
    msgs = [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]
    assert msgs, "沒有 INFO log"
    print("INFO:", msgs[-1])
    assert "mode=COMBO" in msgs[-1] and "scale=0.85" in msgs[-1] and "emotion=sad" in msgs[-1]
    assert f"intensity_tier={tier}" in msgs[-1]


def test_neutral_scale_is_one_and_log(backend, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        g = backend.stream(TEXT)
        try:
            next(g)
            during = _lora_ratios(backend)
            for _ in g:
                pass
        finally:
            g.close()
    msgs = [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]
    print(f"neutral during min/max={min(during):.4f}/{max(during):.4f}")
    print("INFO:", msgs[-1])
    assert all(_is(r, 1.0) for r in during)
    assert "mode=ULT" in msgs[-1] and "scale=1.0" in msgs[-1] and "emotion=None" in msgs[-1]


def test_emotion_interrupted_restores_scale(backend):
    g = backend.stream(TEXT, emotion="angry", intensity=1.0)
    try:
        next(g)
        mid = _lora_ratios(backend)
    finally:
        g.close()  # 斷言失敗也要放掉 lock，不讓後面的測試卡死
    assert all(_is(r, 0.85) for r in mid)
    assert all(_is(r, 1.0) for r in _lora_ratios(backend))


def test_emotion_vs_neutral_latency(backend):
    """交錯 6 次，印出中位數；首包門檻沿用 < 1 秒。"""
    neu, emo = [], []
    for _ in range(6):
        _, f, r = _run(backend)
        neu.append((f, r))
        _, f, r = _run(backend, emotion="calm", intensity=0.5)
        emo.append((f, r))
    med = lambda xs, i: statistics.median(x[i] for x in xs)  # noqa: E731
    print(f"neutral first_med={med(neu, 0):.3f} rtf_med={med(neu, 1):.3f} | "
          f"emotion first_med={med(emo, 0):.3f} rtf_med={med(emo, 1):.3f}")
    print("neutral first:", [round(x[0], 3) for x in neu], "rtf:", [round(x[1], 3) for x in neu])
    print("emotion first:", [round(x[0], 3) for x in emo], "rtf:", [round(x[1], 3) for x in emo])
    assert med(neu, 0) < 1.0 and med(emo, 0) < 1.0
