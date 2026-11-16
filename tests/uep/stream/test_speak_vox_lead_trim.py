"""VOX 句首冷啟動雜音與過長前導空白處理的測試。

用合成訊號與假 backend，不載 VOX。重點是保護規則：不符合特徵一律原樣，
且開口點之後的樣本逐點與原檔相同。
"""

from __future__ import annotations

import logging

import pytest

from uep.stream.adapters.speak_indextts import tensor_to_pcm16
from uep.stream.adapters.speak_vox import VOX_SAMPLE_RATE, VoxLeadTrimmer, VoxSpeakStage
from uep.stream.contracts.cancellation import CancellationToken
from tests.uep.stream.conftest import sentence_stream

np = pytest.importorskip("numpy", reason="adapter 的音訊轉換需要 numpy")
pytest.importorskip("soxr", reason="重採樣需要 soxr")

SR = VOX_SAMPLE_RATE
CHUNK = 7680  # 160ms @ 48kHz
PAD = int(SR * 0.015)  # 預設 segment_silence_ms=15 的句尾靜音墊
EDGE = 400  # 比較時避開句尾 4ms 淡出


def ms(n: float) -> int:
    return int(SR * n / 1000)


def make_signal(burst_ms=0.0, lead_ms=0.0, speech_ms=1000.0, burst_amp=600.0, quiet_amp=25.0):
    """句首爆發（1kHz）＋安靜前導（200Hz 低幅）＋語音（300Hz）；回傳 float32 與開口點樣本位置。"""
    parts = []
    if burst_ms:
        t = np.arange(ms(burst_ms)) / SR
        parts.append(burst_amp * np.sin(2 * np.pi * 1000 * t))
    if lead_ms:
        t = np.arange(ms(lead_ms)) / SR
        parts.append(quiet_amp * np.sin(2 * np.pi * 200 * t))
    onset = sum(len(p) for p in parts)
    t = np.arange(ms(speech_ms)) / SR
    parts.append(3000.0 * np.sin(2 * np.pi * 300 * t + 0.5))
    return (np.concatenate(parts) / 32767.0).astype("float32"), onset


class ScriptBackend:
    """每次呼叫都把同一段訊號切成 160ms chunk 吐出。"""

    sample_rate = SR

    def __init__(self, signal):
        self.signal = signal
        self.calls = 0

    def prepare(self) -> None:
        pass

    def unload(self) -> None:
        pass

    def stream(self, text, lang=None, speed=None, **kwargs):
        self.calls += 1
        return self._gen()

    def _gen(self):
        for i in range(0, len(self.signal), CHUNK):
            yield self.signal[i : i + CHUNK]


async def run(signal, n_sentences=1, **stage_kw):
    """跑 stage，回傳每句的 int16 輸出（含句尾靜音墊）與原檔 int16。"""
    stage = VoxSpeakStage(backend=ScriptBackend(signal), **stage_kw)
    texts = [f"第{i}句。" for i in range(n_sentences)]
    chunks = [c async for c in stage.stream(sentence_stream(texts), CancellationToken())]
    outs = []
    for i in range(n_sentences):
        pcm = b"".join(c.pcm for c in chunks if c.sentence_index == i)
        outs.append(np.frombuffer(pcm, dtype="<i2"))
    orig = np.frombuffer(tensor_to_pcm16(signal), dtype="<i2")
    return outs, orig


def onset_in_out(out, orig, onset):
    """開口點在輸出中的位置（由尾端對齊推回，不依賴實作的切點）。"""
    return len(out) - PAD - (len(orig) - onset)


def assert_main_audio_identical(out, orig, onset):
    """開口點之後（避開句尾淡出）逐樣本與原檔相同。"""
    idx = onset_in_out(out, orig, onset)
    assert idx >= 0
    assert np.array_equal(out[idx : len(out) - PAD - EDGE], orig[onset : len(orig) - EDGE])


def assert_unchanged(out, orig):
    """除了既有的 4ms 淡入、句尾淡出與靜音墊，與原檔完全相同（長度也一樣）。"""
    assert len(out) == len(orig) + PAD
    assert np.array_equal(out[EDGE : len(orig) - EDGE], orig[EDGE : len(orig) - EDGE])


# ---- 有爆發 ----

@pytest.mark.parametrize("lead_ms", [400.0, 700.0, 903.3])
async def test_有爆發加長空白_刪爆發並縮到_150ms_主音訊不變(lead_ms):
    signal, onset = make_signal(burst_ms=30.0, lead_ms=lead_ms)
    (out,), orig = await run(signal)
    idx = onset_in_out(out, orig, onset)
    # 開口點前保留 150ms（視窗對齊誤差 < 5ms），且絕不少於 150ms
    assert ms(150) <= idx <= ms(150) + ms(5)
    assert_main_audio_identical(out, orig, onset)
    # 保留的前導只剩底噪，沒有爆發
    assert np.abs(out[:idx]).max() <= 26


async def test_刪除後接點平順且只淡入一次():
    signal, onset = make_signal(burst_ms=30.0, lead_ms=400.0)
    (out,), orig = await run(signal)
    assert abs(int(out[0])) <= 6
    assert np.abs(np.diff(out[: ms(20)].astype(int))).max() <= 6
    # 8ms 餘弦淡入：中點約半幅；若再疊 4ms Hann 淡入，前 4ms 會被壓成平方（遠小於一次淡入）
    cut = onset - ms(150)
    n = ms(8)
    ramp = 0.5 * (1 - np.cos(np.pi * np.arange(n) / n))
    expect = orig[cut : cut + n].astype("float32") * ramp
    assert np.abs(out[:n].astype("float32") - expect).max() <= 1.5


async def test_有爆發但空白很短_只刪爆發不縮空白():
    signal, onset = make_signal(burst_ms=30.0, lead_ms=100.0)
    (out,), orig = await run(signal)
    assert len(out) == len(orig) - ms(30) + PAD
    assert onset_in_out(out, orig, onset) == ms(100)
    assert_main_audio_identical(out, orig, onset)
    assert np.abs(out[: ms(100)]).max() <= 26  # 爆發已不在


async def test_有爆發但安靜區不足_原樣():
    signal, _ = make_signal(burst_ms=30.0, lead_ms=40.0)
    (out,), orig = await run(signal)
    assert_unchanged(out, orig)


@pytest.mark.parametrize(
    "burst_ms, burst_amp",
    [(30.0, 3000.0), (30.0, 100.0), (30.0, 1500.0), (120.0, 600.0)],
    ids=["太大聲", "太小聲", "略超出峰值範圍", "持續太久"],
)
async def test_爆發特徵不符_原樣(burst_ms, burst_amp):
    signal, _ = make_signal(burst_ms=burst_ms, lead_ms=500.0, burst_amp=burst_amp)
    (out,), orig = await run(signal)
    assert_unchanged(out, orig)


# ---- 沒有爆發 ----

async def test_沒有爆發直接開口_完全不動():
    signal, _ = make_signal()
    (out,), orig = await run(signal)
    assert_unchanged(out, orig)
    assert out[0] == 0  # 沿用既有 4ms 淡入


async def test_沒有爆發且前導短_原樣():
    signal, _ = make_signal(lead_ms=100.0)
    (out,), orig = await run(signal)
    assert_unchanged(out, orig)


async def test_沒有爆發但前導很長_只縮到開口前_150ms():
    signal, onset = make_signal(lead_ms=600.0)
    (out,), orig = await run(signal)
    idx = onset_in_out(out, orig, onset)
    assert ms(150) <= idx <= ms(150) + ms(5)
    assert_main_audio_identical(out, orig, onset)


async def test_前導內有低於語音的雜訊_不越過它():
    """前導中段出現 100 LSB 的小聲音（高於安靜門檻）：以它為開口點，保留它之前 150ms，它本身完整保留。"""
    signal, onset = make_signal(lead_ms=800.0)
    signal = signal.copy()
    pos = ms(500)
    signal[pos : pos + ms(20)] = 100.0 / 32767.0
    (out,), orig = await run(signal)
    idx = onset_in_out(out, orig, onset)
    assert idx >= ms(450)  # 以小聲音為開口點：它前面留 150ms，它到語音之間（300ms）全留
    assert_main_audio_identical(out, orig, onset)
    assert 100 in set(out.tolist()) or 99 in set(out.tolist())


# ---- 串流與緩衝 ----

async def test_暫存上限放行_找不到開口點時原樣():
    signal, _ = make_signal(burst_ms=30.0, lead_ms=1500.0)
    (out,), orig = await run(signal)
    assert_unchanged(out, orig)
    signal2, _ = make_signal(lead_ms=1500.0)
    (out2,), orig2 = await run(signal2)
    assert_unchanged(out2, orig2)


async def test_自訂暫存上限():
    signal, onset = make_signal(burst_ms=30.0, lead_ms=400.0)
    (out,), orig = await run(signal, lead_trim_max_hold_s=0.3)
    assert_unchanged(out, orig)  # 0.3s 內看不到開口點 -> 放行
    (out,), orig = await run(signal, lead_trim_max_hold_s=0.6)
    assert onset_in_out(out, orig, onset) <= ms(150) + ms(5)


async def test_句子在判斷前結束_原樣():
    t = np.arange(ms(30)) / SR
    burst = 600.0 * np.sin(2 * np.pi * 1000 * t)
    quiet = 25.0 * np.sin(2 * np.pi * 200 * np.arange(ms(300)) / SR)
    signal = (np.concatenate([burst, quiet]) / 32767.0).astype("float32")
    (out,), orig = await run(signal)
    assert_unchanged(out, orig)


async def test_開關關閉_原樣輸出():
    signal, _ = make_signal(burst_ms=30.0, lead_ms=600.0)
    (out,), orig = await run(signal, lead_trim_enabled=False)
    assert_unchanged(out, orig)


async def test_每句各自處理():
    signal, onset = make_signal(burst_ms=30.0, lead_ms=500.0)
    outs, orig = await run(signal, n_sentences=2)
    for out in outs:
        assert ms(150) <= onset_in_out(out, orig, onset) <= ms(150) + ms(5)
        assert_main_audio_identical(out, orig, onset)


async def test_非_48k_輸出也能處理():
    signal, onset = make_signal(burst_ms=30.0, lead_ms=500.0)
    stage = VoxSpeakStage(backend=ScriptBackend(signal), sample_rate=24000)
    chunks = [c async for c in stage.stream(sentence_stream(["一句。"]), CancellationToken())]
    total = sum(c.duration_s for c in chunks)
    expected = (len(signal) - (onset - ms(150))) / SR + 0.015
    assert total == pytest.approx(expected, abs=0.01)


# ---- 計時欄位與 log ----

async def test_log_反映刪除的時間(caplog):
    signal, onset = make_signal(burst_ms=30.0, lead_ms=400.0)
    cut = onset - ms(150)
    with caplog.at_level(logging.DEBUG, logger="uep.stream.adapters.speak_vox"):
        await run(signal)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert f"audio_s={(len(signal) - cut) / SR:.2f}" in text
    assert f"trim_ms={cut * 1000 / SR:.0f}" in text
    assert "lead_trim" in text and "burst=True" in text
    assert "orig_lead_ms=400" in text and "kept_lead_ms=150" in text


async def test_未偵測到爆發的_log(caplog):
    signal, _ = make_signal()
    with caplog.at_level(logging.DEBUG, logger="uep.stream.adapters.speak_vox"):
        await run(signal)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "burst=False" in text and "cut_ms=0" in text and "trim_ms=0" in text


# ---- VoxLeadTrimmer 單元 ----

def test_trimmer_決定前不放出_決定後原樣穿透():
    signal, onset = make_signal(burst_ms=30.0, lead_ms=700.0)
    trimmer = VoxLeadTrimmer(SR)
    outs = []
    first_release = None
    for i in range(0, len(signal), CHUNK):
        got = trimmer.feed(signal[i : i + CHUNK])
        if len(got) and first_release is None:
            first_release = i // CHUNK
        outs.append(got)
    outs.append(trimmer.flush())
    out = np.concatenate(outs)
    assert first_release == onset // CHUNK  # 開口點所在的 chunk 才放出，之前都在暫存
    assert trimmer.info["burst"] is True
    assert trimmer.trimmed_samples == len(signal) - len(out)
    assert np.array_equal(out[onset - trimmer.trimmed_samples :], signal[onset:])


def test_trimmer_第一段就有持續能量_立刻放行():
    signal, _ = make_signal()
    trimmer = VoxLeadTrimmer(SR)
    got = trimmer.feed(signal[:CHUNK])
    assert len(got) == CHUNK and trimmer.trimmed_samples == 0
    assert trimmer.info["reason"] == "loud_start"
