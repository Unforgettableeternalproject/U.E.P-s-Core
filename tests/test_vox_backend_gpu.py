"""VoxBackend GPU 整合測試。預設跳過；設 VOX_GPU_TEST=1 並提供 VOX_MODEL_DIR、VOX_LORA_DIR 才跑。

模組內共用一個 backend（prepare 很慢）；測試順序即執行順序，第一個測試驗證 zh ULT 暖機。
"""
import os
import time

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("VOX_GPU_TEST") != "1" or not os.environ.get("VOX_MODEL_DIR") or not os.environ.get("VOX_LORA_DIR"),
    reason="需要 VOX_GPU_TEST=1、VOX_MODEL_DIR、VOX_LORA_DIR",
)

SENT = {
    "zh": "週末我想去市場買點菜，順便繞去書店逛逛，晚上再回家煮一鍋熱湯給大家喝。",
    "en": "The weather is nice today. I finished organizing everything on my desk.",
    "ja": "今日はいい天気ですね。机の上の仕事を全部片付けました。",
}


def _run(backend, text, lang=None, speed=None):
    t0 = time.perf_counter()
    first, chunks = None, []
    for c in backend.stream(text, lang=lang, speed=speed):
        if first is None:
            first = time.perf_counter() - t0
        chunks.append(c)
    el = time.perf_counter() - t0
    w = np.concatenate(chunks)
    return w, first, el / (len(w) / backend.sample_rate)


@pytest.fixture(scope="module")
def backend():
    from echo_tts.runtime.vox_backend import VoxBackend

    b = VoxBackend({"model_dir": os.environ["VOX_MODEL_DIR"], "lora_dir": os.environ["VOX_LORA_DIR"]})
    t0 = time.perf_counter()
    b.prepare()
    print(f"\nprepare_s={time.perf_counter() - t0:.1f}")
    yield b
    b.unload()


def test_first_zh_call_first_packet_under_1s(backend):
    """zh ULT 暖機有效：prepare 後第一次 zh 呼叫的首包 < 1 秒（未暖機約 9 秒）。"""
    w, first, rtf = _run(backend, SENT["zh"])
    print(f"zh first_call first_packet={first:.3f}s rtf={rtf:.3f}")
    assert first < 1.0
    assert np.isfinite(w).all() and w.dtype == np.float32


@pytest.mark.parametrize("lang", ["zh", "en", "ja"])
def test_each_lang(backend, lang):
    w, first, rtf = _run(backend, SENT[lang])
    dur = len(w) / backend.sample_rate
    print(f"{lang} first_packet={first:.3f}s rtf={rtf:.3f} audio_s={dur:.2f}")
    assert np.isfinite(w).all() and 1.0 < dur < 20.0 and float(np.abs(w).max()) > 0.01
    assert first < 1.0


@pytest.mark.parametrize("speed", [1.0, 1.1])
def test_zh_other_speed_tiers(backend, speed):
    w, first, rtf = _run(backend, SENT["zh"], speed=speed)
    print(f"zh speed={speed} first_packet={first:.3f}s rtf={rtf:.3f} audio_s={len(w) / backend.sample_rate:.2f}")
    assert np.isfinite(w).all() and first < 1.0


def test_close_midway_then_unload(backend):
    g = backend.stream(SENT["zh"])
    next(g)
    g.close()
    t0 = time.perf_counter()
    backend.unload()
    print(f"unload_s={time.perf_counter() - t0:.2f}")
    assert not backend.prepared


def test_cross_thread_stream_raises_clear_error():
    """thread A prepare、thread B stream：要得到清楚的 RuntimeError，而不是 inductor 的 AssertionError。
    獨立建立 backend（不用模組共用的）；thread A 保持存活，最後由它 unload。"""
    import threading

    from echo_tts.runtime.vox_backend import VoxBackend

    b = VoxBackend({"model_dir": os.environ["VOX_MODEL_DIR"], "lora_dir": os.environ["VOX_LORA_DIR"]})
    prepared, finish = threading.Event(), threading.Event()
    errors = []

    def thread_a():
        try:
            b.prepare()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        prepared.set()
        finish.wait(60)
        try:
            b.unload()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ta = threading.Thread(target=thread_a)
    ta.start()
    assert prepared.wait(600) and not errors and b.prepared

    result = {}

    def thread_b():
        try:
            list(b.stream(SENT["zh"]))
        except BaseException as exc:  # noqa: BLE001
            result["err"] = exc

    tb = threading.Thread(target=thread_b)
    tb.start()
    tb.join()
    finish.set()
    ta.join()
    assert not errors
    assert isinstance(result.get("err"), RuntimeError) and "同一條執行緒" in str(result["err"])
