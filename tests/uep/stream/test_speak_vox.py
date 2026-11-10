"""VOX adapter 的測試。

用**假 backend**——不需要 GPU、不載入真模型。假 generator 一律有限。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from uep.stream.adapters.speak_vox import VOX_SAMPLE_RATE, VoxSpeakStage
from uep.stream.contracts.cancellation import CancellationToken, CancelledError
from uep.stream.contracts.style import SpeechStyle
from tests.uep.stream.conftest import sentence_stream

np = pytest.importorskip("numpy", reason="adapter 的音訊轉換需要 numpy")
pytest.importorskip("soxr", reason="重採樣需要 soxr")

CHUNK_SAMPLES = 7680  # 160ms @ 48kHz


class FakeVoxBackend:
    """有限 generator 的假 VoxBackend，記錄呼叫與執行緒。"""

    sample_rate = VOX_SAMPLE_RATE

    def __init__(self, chunks_per_call: int = 3, delay: float = 0.0) -> None:
        self.chunks_per_call = chunks_per_call
        self.delay = delay
        self.prepared = 0
        self.closed = 0
        self.calls: list[dict] = []
        self.call_threads: list[int] = []
        self.close_threads: list[int] = []
        self.active = 0
        self.max_active = 0
        self.yielded = 0
        self._lock = threading.Lock()

    def prepare(self) -> None:
        self.prepared += 1

    def unload(self) -> None:
        self.closed += 1

    def stream(self, text, lang=None, speed=None, **kwargs):
        self.calls.append({"text": text, "lang": lang, "speed": speed, **kwargs})
        return self._gen()

    def _gen(self):
        self.call_threads.append(threading.get_ident())
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            for _ in range(self.chunks_per_call):
                if self.delay:
                    time.sleep(self.delay)
                self.yielded += 1
                yield np.full(CHUNK_SAMPLES, 0.5, dtype="float32")
        finally:
            self.close_threads.append(threading.get_ident())
            with self._lock:
                self.active -= 1


def test_預設_48k_直出_不建重採樣器():
    stage = VoxSpeakStage(backend=FakeVoxBackend())
    assert stage.sample_rate == VOX_SAMPLE_RATE == 48000
    assert stage._make_resampler() is None


async def test_明確指定_22050_仍可重採樣():
    stage = VoxSpeakStage(backend=FakeVoxBackend(chunks_per_call=3), sample_rate=22050)
    chunks = await _collect(stage, ["第一句。"])
    assert chunks and all(c.sample_rate == 22050 for c in chunks)


async def _collect(stage, texts, token=None, style=None):
    token = token or CancellationToken()
    return [c async for c in stage.stream(sentence_stream(texts, style=style), token)]


async def test_正常串流與格式():
    backend = FakeVoxBackend(chunks_per_call=3)
    stage = VoxSpeakStage(backend=backend)
    chunks = await _collect(stage, ["第一句。", "第二句。"])

    assert len(backend.calls) == 2
    assert all(c.sample_rate == 48000 and c.channels == 1 for c in chunks)
    assert all(len(c.pcm) % 2 == 0 for c in chunks)
    # 預設 48k 直出（不重採樣）：3 × 160ms，加上尾端靜音墊，每句約 480ms + 15ms
    for idx in (0, 1):
        total = sum(c.duration_s for c in chunks if c.sentence_index == idx)
        assert total == pytest.approx(0.495, abs=0.02)
    assert [c.is_last for c in chunks].count(True) == 1
    assert chunks[-1].is_last
    s0 = [c.chunk_index for c in chunks if c.sentence_index == 0]
    assert s0 == list(range(len(s0)))


async def test_句首淡入句尾淡出且中段連續():
    backend = FakeVoxBackend(chunks_per_call=3)
    stage = VoxSpeakStage(backend=backend)
    chunks = await _collect(stage, ["只有一句。"])
    pcm = np.frombuffer(b"".join(c.pcm for c in chunks), dtype="<i2")
    assert pcm[0] == 0  # 淡入
    body = pcm[len(pcm) // 4 : len(pcm) // 2]
    assert np.all(body > 16000)  # 0.5 約 16383，中段沒有被凹
    assert pcm[-1] == 0  # 靜音墊


async def test_句尾淡出長度等於_edge_fade_ms():
    """尾端保留不足時淡化會被砍成一半（11/01 TTS 量到實際只有 2ms）。"""
    stage = VoxSpeakStage(
        backend=FakeVoxBackend(chunks_per_call=3), sample_rate=48000, segment_silence_ms=0.0
    )
    chunks = await _collect(stage, ["只有一句。"])
    pcm = np.frombuffer(b"".join(c.pcm for c in chunks), dtype="<i2")
    n_fade = int(48000 * 4.0 / 1000)  # 預設 edge_fade_ms=4
    # 淡出區間從滿振幅單調降到接近 0；區間外仍是滿振幅
    assert np.all(pcm[-n_fade - 10 : -n_fade - 1] > 16000)
    tail = pcm[-n_fade:]
    assert tail[0] > 15000 and tail[-1] < 200
    assert 5000 < tail[n_fade // 2] < 12000  # 半 Hann 中點約半振幅；只淡一半時這裡仍是滿振幅
    assert np.all(np.diff(tail.astype(int)) <= 0)


async def test_取樣率_48k_不重採樣():
    stage = VoxSpeakStage(backend=FakeVoxBackend(chunks_per_call=2), sample_rate=48000)
    chunks = await _collect(stage, ["一句。"])
    assert all(c.sample_rate == 48000 for c in chunks)
    total = sum(c.duration_s for c in chunks)
    assert total == pytest.approx(0.32 + 0.015, abs=0.005)


async def test_取消後_generator_在工作執行緒內被關閉():
    backend = FakeVoxBackend(chunks_per_call=20, delay=0.01)
    stage = VoxSpeakStage(backend=backend, sentence_buffer=1)
    token = CancellationToken()
    got = 0
    async for _ in stage.stream(sentence_stream(["很長的一句。", "下一句。"]), token):
        got += 1
        if got == 2:
            token.cancel()
            break
    await asyncio.sleep(0.3)

    assert backend.yielded < 20  # 沒有一路算完
    assert len(backend.calls) == 1  # 第二句沒有開始
    assert backend.close_threads == backend.call_threads  # 同一條工作執行緒內 close
    assert backend.call_threads[0] != threading.get_ident()
    assert backend.active == 0


async def test_sanitize_為空時跳過():
    backend = FakeVoxBackend()
    stage = VoxSpeakStage(backend=backend)
    chunks = await _collect(stage, ["「」", "有內容。"])
    assert [c["text"] for c in backend.calls] == ["有內容。"]
    assert {c.sentence_index for c in chunks} == {1}


async def test_送進_backend_的是清理過的文字():
    backend = FakeVoxBackend()
    stage = VoxSpeakStage(backend=backend)
    await _collect(stage, ["你好*"])
    assert backend.calls[0]["text"] == "你好"


async def test_speed_與_lang_傳遞():
    backend = FakeVoxBackend(chunks_per_call=1)
    stage = VoxSpeakStage(backend=backend, speed=1.1, language="zh")
    await _collect(stage, ["預設。"])
    await _collect(stage, ["風格。"], style=SpeechStyle(speed=1.2))
    await _collect(stage, ["中性。"], style=SpeechStyle())
    assert [c["speed"] for c in backend.calls] == [1.1, 1.2, 1.1]
    assert all(c["lang"] == "zh" for c in backend.calls)

    none_backend = FakeVoxBackend(chunks_per_call=1)
    await _collect(VoxSpeakStage(backend=none_backend), ["無。"])
    assert none_backend.calls[0]["speed"] is None
    assert none_backend.calls[0]["lang"] is None


async def test_不會並行呼叫_backend():
    backend = FakeVoxBackend(chunks_per_call=3, delay=0.01)
    stage = VoxSpeakStage(backend=backend)
    # 兩個 turn 同時跑，共用同一個 stage / backend
    await asyncio.gather(
        _collect(stage, ["甲一。", "甲二。"]),
        _collect(stage, ["乙一。", "乙二。"]),
    )
    assert len(backend.calls) == 4
    assert backend.max_active == 1


async def test_prepare_與_aclose():
    backend = FakeVoxBackend()
    stage = VoxSpeakStage(backend=backend)
    await stage.prepare()
    await stage.prepare()  # 冪等
    assert backend.prepared == 1
    await _collect(stage, ["一句。"])
    assert backend.prepared == 1  # stream 不重複 prepare
    await stage.aclose()
    await stage.aclose()  # 可重複呼叫不拋錯
    assert backend.closed == 2


async def test_backend_為_None_時延遲建立(monkeypatch):
    backend = FakeVoxBackend()
    stage = VoxSpeakStage(backend_config={"x": 1})
    seen = {}

    def fake_build():
        seen["config"] = stage.backend_config
        return backend

    monkeypatch.setattr(stage, "_build_backend", fake_build)
    await stage.prepare()
    assert seen["config"] == {"x": 1}
    assert backend.prepared == 1


async def test_backend_例外會傳給消費端():
    class Boom(FakeVoxBackend):
        def _gen(self):
            yield np.zeros(CHUNK_SAMPLES, dtype="float32")
            raise RuntimeError("boom")

    stage = VoxSpeakStage(backend=Boom())
    with pytest.raises(RuntimeError, match="boom"):
        await _collect(stage, ["一句。"])


async def test_消費端提早離開不會永久握住_lock():
    """只讀一個 chunk 就 aclose、token 不取消，下一個 turn 仍要能拿到 backend。"""
    backend = FakeVoxBackend(chunks_per_call=20)
    stage = VoxSpeakStage(backend=backend, sentence_buffer=1, queue_size=1)
    token = CancellationToken()

    agen = stage.stream(sentence_stream(["很長的一句。"]), token)
    await agen.__anext__()
    await asyncio.sleep(0.1)  # 讓工作執行緒塞滿通道、卡在 put
    await agen.aclose()
    assert not token.is_cancelled

    chunks = await asyncio.wait_for(_collect(stage, ["下一句。"]), timeout=10)
    assert chunks
    assert backend.active == 0
    assert backend.max_active == 1


async def test_取得_lock_後已取消就不啟動推理():
    backend = FakeVoxBackend()
    stage = VoxSpeakStage(backend=backend)
    token = CancellationToken()
    stage._backend_lock.acquire()  # 模擬上一個 turn 還在推理
    task = asyncio.ensure_future(_collect(stage, ["等待中。"], token=token))
    await asyncio.sleep(0.2)
    token.cancel()
    await asyncio.sleep(0.2)
    stage._backend_lock.release()
    with pytest.raises(CancelledError):  # 消費端讀通道時本來就會拿到取消
        await asyncio.wait_for(task, timeout=10)
    await asyncio.sleep(0.2)  # 讓工作執行緒有機會（錯誤地）啟動推理
    assert backend.calls == []


async def test_淡化與靜音皆為零時仍送出_is_last():
    class Empty(FakeVoxBackend):
        def _gen(self):
            return iter(())  # 有限且不吐任何 chunk

    stage = VoxSpeakStage(backend=Empty(), edge_fade_ms=0, segment_silence_ms=0)
    chunks = await _collect(stage, ["一句。"])
    assert len(chunks) == 1
    assert chunks[0].is_last and chunks[0].pcm == b""

    stage = VoxSpeakStage(
        backend=FakeVoxBackend(chunks_per_call=2), edge_fade_ms=0, segment_silence_ms=0
    )
    chunks = await _collect(stage, ["一句。"])
    assert chunks[-1].is_last
    assert [c.is_last for c in chunks].count(True) == 1


def test_建構子拒絕未知參數():
    with pytest.raises(TypeError):
        VoxSpeakStage(backend=FakeVoxBackend(), warmup=True)  # type: ignore[call-arg]


async def test_所有_backend_呼叫都在同一條執行緒():
    """torch 的 CUDA graph tree 是執行緒區域的：prepare（compile／暖機）與
    之後每次合成、unload 必須同一條執行緒，否則真 VOX 會 assert 失敗。"""

    class ThreadRecordingBackend(FakeVoxBackend):
        def __init__(self) -> None:
            super().__init__(chunks_per_call=2)
            self.prepare_threads: list[int] = []
            self.unload_threads: list[int] = []

        def prepare(self) -> None:
            super().prepare()
            self.prepare_threads.append(threading.get_ident())

        def unload(self) -> None:
            super().unload()
            self.unload_threads.append(threading.get_ident())

    backend = ThreadRecordingBackend()
    stage = VoxSpeakStage(backend=backend)
    await stage.prepare()
    # 多個 turn、且中間穿插其他 executor 工作，預設執行緒池會換執行緒
    for i in range(4):
        await asyncio.gather(
            _collect(stage, [f"第 {i} 句。", "下一句。"]),
            *[asyncio.get_running_loop().run_in_executor(None, time.sleep, 0.01)
              for _ in range(4)],
        )
    await stage.aclose()

    threads = set(
        backend.prepare_threads + backend.call_threads + backend.close_threads
        + backend.unload_threads
    )
    assert len(threads) == 1
    assert threading.get_ident() not in threads


async def test_逐句_log_每句一行且欄位齊全(caplog):
    backend = FakeVoxBackend(chunks_per_call=3, delay=0.01)
    stage = VoxSpeakStage(backend=backend)
    with caplog.at_level("INFO", logger="uep.stream.adapters.speak_vox"):
        await _collect(stage, ["第一句。", "第二句。"])

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("vox sentence")]
    assert len(lines) == 2
    for key in (
        "turn=t1", "idx=", "chars=", "reason=", "start=", "first_chunk_ms=", "synth_ms=",
        "audio_s=", "rtf=", "gap_prev_ms=", "chunks=3", "max_chunk_gap_ms=",
    ):
        assert all(key in line for line in lines), key
    assert "gap_prev_ms=-" in lines[0]  # 首句沒有上一句
    assert "gap_prev_ms=-" not in lines[1]  # 同 turn 第二句有間隔
    assert "audio_s=0.48" in lines[0]  # 3 chunk x 160ms
    assert "lead_ms=0" in lines[0]  # turn 第一個 chunk
    lead = int(lines[1].split("lead_ms=")[1].split()[0])
    assert lead > 0  # 合成快於即時，接縫有餘裕


async def test_lead_ms_第二句延遲時為負值(caplog):
    class SlowSecond(FakeVoxBackend):
        def stream(self, text, lang=None, speed=None):
            self.calls.append({"text": text})
            return self._slow() if len(self.calls) == 2 else self._gen()

        def _slow(self):
            time.sleep(0.8)  # 第一句只有 480ms 音訊，第二句晚到 -> 欠流
            yield from self._gen()

    stage = VoxSpeakStage(backend=SlowSecond(chunks_per_call=3))
    with caplog.at_level("INFO", logger="uep.stream.adapters.speak_vox"):
        await _collect(stage, ["第一句。", "第二句。"])
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("vox sentence")]
    assert int(lines[1].split("lead_ms=")[1].split()[0]) < 0


def test_recommended_split_policy_數值():
    policy = VoxSpeakStage.recommended_split_policy()
    assert (policy.first_min_weight, policy.first_max_weight) == (10.0, 24.0)
    assert (policy.min_weight, policy.max_weight) == (30.0, 80.0)
    assert policy.hard_cap_weight is None  # 沿用 2 x max


async def test_句尾停頓依切點性質():
    """句末切長停、逗號切短停、最後一句與 flush 用 segment_silence_ms。"""
    from uep.stream.contracts.types import Sentence

    async def stream():
        specs = [("一。", "terminal"), ("二，", "secondary"), ("三", "flush"), ("四。", "terminal")]
        for i, (text, reason) in enumerate(specs):
            yield Sentence(
                text=text, turn_id="t", index=i, is_first=i == 0,
                is_last=i == len(specs) - 1, split_reason=reason,
            )

    stage = VoxSpeakStage(
        backend=FakeVoxBackend(chunks_per_call=1), sample_rate=48000,
        terminal_pause_ms=250.0, secondary_pause_ms=80.0,
    )
    chunks = [c async for c in stage.stream(stream(), CancellationToken())]
    dur = {i: sum(c.duration_s for c in chunks if c.sentence_index == i) for i in range(4)}
    base = 0.16  # 一個 chunk 的語音長度
    assert dur[0] == pytest.approx(base + 0.250, abs=0.002)
    assert dur[1] == pytest.approx(base + 0.080, abs=0.002)
    assert dur[2] == pytest.approx(base + 0.015, abs=0.002)  # flush：沿用 segment_silence_ms
    assert dur[3] == pytest.approx(base + 0.015, abs=0.002)  # 最後一句：不拖長 turn


async def test_停頓未設定時行為不變():
    stage = VoxSpeakStage(backend=FakeVoxBackend(chunks_per_call=1), sample_rate=48000)
    chunks = await _collect(stage, ["一。", "二。"])
    assert sum(c.duration_s for c in chunks if c.sentence_index == 0) == pytest.approx(
        0.16 + 0.015, abs=0.002
    )


# ---- 情緒映射 ----

_HAPPY = SpeechStyle(emotion={"happy": 1.0}, intensity=0.8, style="happy")
_EXCITED = SpeechStyle(emotion={"happy": 0.7, "surprised": 0.3}, intensity=0.3, style="excited")


async def test_情緒_預設對照表全_neutral_開關開了也不帶參數():
    plain, enabled = FakeVoxBackend(chunks_per_call=1), FakeVoxBackend(chunks_per_call=1)
    await _collect(VoxSpeakStage(backend=plain), ["你好。"], style=_HAPPY)
    await _collect(VoxSpeakStage(backend=enabled, emotion_enabled=True), ["你好。"], style=_HAPPY)
    assert plain.calls == enabled.calls
    assert "emotion" not in enabled.calls[0] and "intensity" not in enabled.calls[0]


async def test_情緒_開關關閉時即使對照表非預設也不帶參數():
    backend = FakeVoxBackend(chunks_per_call=1)
    stage = VoxSpeakStage(backend=backend, emotion_map={"happy": "happy"})
    await _collect(stage, ["你好。"], style=_HAPPY)
    assert "emotion" not in backend.calls[0]


async def test_情緒_非預設對照表帶出_emotion_與_intensity():
    backend = FakeVoxBackend(chunks_per_call=1)
    stage = VoxSpeakStage(
        backend=backend,
        emotion_enabled=True,
        emotion_map={"happy": "happy", "excited": {"emotion": "surprised", "intensity": 0.9}},
    )
    await _collect(stage, ["開心。"], style=_HAPPY)
    await _collect(stage, ["興奮。"], style=_EXCITED)
    await _collect(stage, ["中性。"], style=SpeechStyle())
    await _collect(stage, ["沒風格。"])
    assert backend.calls[0]["emotion"] == "happy" and backend.calls[0]["intensity"] == 0.8  # 沿用 style.intensity
    assert backend.calls[1]["emotion"] == "surprised" and backend.calls[1]["intensity"] == 0.9  # 表內覆寫
    assert "emotion" not in backend.calls[2] and "emotion" not in backend.calls[3]


async def test_情緒_沒有_preset_名時改查主導維度():
    backend = FakeVoxBackend(chunks_per_call=1)
    stage = VoxSpeakStage(backend=backend, emotion_enabled=True, emotion_map={"sad": "sad"})
    await _collect(stage, ["難過。"], style=SpeechStyle(emotion={"sad": 0.9}, intensity=0.4))
    assert (backend.calls[0]["emotion"], backend.calls[0]["intensity"]) == ("sad", 0.4)


def test_情緒_對照表值域錯誤_fail_closed():
    with pytest.raises(ValueError):
        VoxSpeakStage(backend=FakeVoxBackend(), emotion_map={"happy": "joyful"})
    with pytest.raises(ValueError):
        VoxSpeakStage(backend=FakeVoxBackend(), emotion_map={"happy": {"emotion": "happy", "intensity": 1.5}})


def test_build_backend_預設權重目錄依_UEP_TTS_MODELS_DIR(monkeypatch, tmp_path):
    # echo_tts 已遷入 Core：直接 import，model_dir／lora_dir 未給時補預設；明確給的值優先
    monkeypatch.setenv("UEP_TTS_MODELS_DIR", str(tmp_path))
    backend = VoxSpeakStage()._build_backend()
    assert backend.model_dir == tmp_path / "VoxCPM2"
    assert backend.lora_dir == tmp_path / "lora_tw" / "step_0000600"
    explicit = VoxSpeakStage(backend_config={"model_dir": "m", "lora_dir": "l"})._build_backend()
    assert str(explicit.model_dir) == "m" and str(explicit.lora_dir) == "l"
