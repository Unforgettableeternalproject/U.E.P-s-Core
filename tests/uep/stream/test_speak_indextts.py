"""IndexTTS2 adapter 的測試。

用**假 backend**——不需要 GPU、不需要 echo_tts。
Phase 1 的多數 bug 會出在 push→pull 橋接而不是模型，
橋接邏輯必須能在秒級迭代下驗證。

真模型的驗收走 `python -m echo_stream tts-check`（需要 GPU）。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from uep.stream.adapters.speak_indextts import IndexTTS2SpeakStage, tensor_to_pcm16
from uep.stream.contracts.cancellation import CancellationToken, CancelReason
from uep.stream.contracts.types import Sentence
from uep.stream.gap_filler import GapFiller, GapFillerPolicy
from tests.uep.stream.conftest import FakeTTSBackend as FakeBackend
from tests.uep.stream.conftest import sentence_stream

np = pytest.importorskip("numpy", reason="adapter 的音訊轉換需要 numpy")


# --- 音訊轉換 ---


def test_轉成_int16_pcm():
    arr = np.array([0.0, 1.0, -1.0], dtype="float32")
    pcm = tensor_to_pcm16(arr)
    assert len(pcm) == 6  # 3 samples × 2 bytes
    assert np.frombuffer(pcm, dtype="<i2").tolist() == [0, 32767, -32767]


def test_超出範圍會被夾住():
    """引擎偶爾會吐出略超過 ±1.0 的值，不夾會 wrap around 變成爆音。"""
    arr = np.array([2.5, -2.5], dtype="float32")
    assert np.frombuffer(tensor_to_pcm16(arr), dtype="<i2").tolist() == [32767, -32767]


def test_多聲道取第一軌():
    arr = np.zeros((2, 4), dtype="float32")
    arr[0] = 1.0
    assert np.frombuffer(tensor_to_pcm16(arr), dtype="<i2").tolist() == [32767] * 4


# --- 基本串流 ---


async def test_產出音訊塊():
    backend = FakeBackend(segments_per_sentence=2)
    stage = IndexTTS2SpeakStage(backend=backend)
    token = CancellationToken()

    chunks = [
        c async for c in stage.stream(sentence_stream(["第一句。", "第二句。"]), token)
    ]

    assert len(chunks) == 4  # 2 句 × 2 段
    assert backend.calls == ["第一句。", "第二句。"]


async def test_索引對得上():
    stage = IndexTTS2SpeakStage(backend=FakeBackend(segments_per_sentence=2))
    chunks = [
        c async for c in stage.stream(sentence_stream(["一。", "二。"]), CancellationToken())
    ]

    assert [c.sentence_index for c in chunks] == [0, 0, 1, 1]
    assert [c.chunk_index for c in chunks] == [0, 1, 0, 1]


async def test_最後一段標記_is_last():
    stage = IndexTTS2SpeakStage(backend=FakeBackend(segments_per_sentence=2))
    chunks = [
        c async for c in stage.stream(sentence_stream(["一。", "二。"]), CancellationToken())
    ]
    assert [c.is_last for c in chunks] == [False, False, False, True]


async def test_空句子被跳過():
    backend = FakeBackend()
    stage = IndexTTS2SpeakStage(backend=backend)

    async def stream_with_empty():
        yield Sentence(text="有內容。", turn_id="t", index=0, is_first=True)
        yield Sentence(text="", turn_id="t", index=1, is_last=True)

    _ = [c async for c in stage.stream(stream_with_empty(), CancellationToken())]
    assert backend.calls == ["有內容。"]


async def test_音訊時長正確():
    # 關掉段緣後處理——這裡驗的是原始時長換算，靜音墊另有專屬測試
    stage = IndexTTS2SpeakStage(
        backend=FakeBackend(segments_per_sentence=1, segment_seconds=0.5),
        edge_fade_ms=0,
        segment_silence_ms=0,
    )
    chunks = [c async for c in stage.stream(sentence_stream(["一。"]), CancellationToken())]
    assert abs(chunks[0].duration_s - 0.5) < 0.01


async def test_prepare_會載入_backend():
    backend = FakeBackend()
    stage = IndexTTS2SpeakStage(backend=backend)
    await stage.prepare()
    assert backend.loaded


class _LoraBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.merge_calls: list[float | None] = []

    def merge_lora(self, checkpoint_path=None, scale=None):
        assert self.loaded, "必須先 load 再 merge"
        self.merge_calls.append(scale)
        return {"accel_synced": True}


async def test_lora_scale_為_None_不呼叫_merge():
    backend = _LoraBackend()
    await IndexTTS2SpeakStage(backend=backend).prepare()
    assert backend.merge_calls == []


async def test_lora_scale_有值時_prepare_合併一次():
    backend = _LoraBackend()
    stage = IndexTTS2SpeakStage(backend=backend, lora_scale=0.85)
    await stage.prepare()
    await stage.prepare()   # 第二次 prepare 不重複合併
    assert backend.merge_calls == [0.85]


async def test_backend_不支援_merge_lora_時明確失敗():
    stage = IndexTTS2SpeakStage(backend=FakeBackend(), lora_scale=0.85)
    with pytest.raises(RuntimeError):
        await stage.prepare()


# --- 背壓 ---


async def test_有界通道把背壓傳到推理執行緒():
    """無界的話 TTS 會一路合成到底，barge-in 就省不到 GPU 時間。"""
    backend = FakeBackend(segments_per_sentence=6, segment_seconds=0.05)
    stage = IndexTTS2SpeakStage(backend=backend, queue_size=1)
    token = CancellationToken()

    stream = stage.stream(sentence_stream(["一句話。"]), token)
    first = await stream.__anext__()
    assert first is not None

    # 只拉一段就停手，讓通道塞住
    await asyncio.sleep(0.2)
    assert len(backend.calls) == 1

    with pytest.raises(Exception):  # noqa: B017 - 取消或關閉都可接受
        token.cancel(CancelReason.BARGE_IN)
        async for _ in stream:
            pass


# --- 取消 ---


async def test_取消會中止後續句子的合成():
    backend = FakeBackend(segments_per_sentence=1, synth_delay_s=0.05)
    stage = IndexTTS2SpeakStage(backend=backend, queue_size=2)
    token = CancellationToken()

    got = 0
    with pytest.raises(Exception):  # noqa: B017
        async for _ in stage.stream(
            sentence_stream(["一。", "二。", "三。", "四。", "五。"]), token
        ):
            got += 1
            if got == 1:
                token.cancel(CancelReason.BARGE_IN)

    await asyncio.sleep(0.2)
    assert len(backend.calls) < 5, "取消後不該把剩下的句子全部合成完"


async def test_取消能穿透到執行緒中的推理():
    """callback 是唯一的協作點——同步推理不在 asyncio 的取消範圍內。"""
    started = threading.Event()
    backend = FakeBackend(segments_per_sentence=20, synth_delay_s=0.02)
    original = backend.generate

    def tracked(*args, **kwargs):
        started.set()
        return original(*args, **kwargs)

    backend.generate = tracked
    stage = IndexTTS2SpeakStage(backend=backend, queue_size=1)
    token = CancellationToken()

    stream = stage.stream(sentence_stream(["很長的一句話。"]), token)
    await stream.__anext__()
    assert started.is_set()

    token.cancel(CancelReason.BARGE_IN)
    with pytest.raises(Exception):  # noqa: B017
        async for _ in stream:
            pass

    await asyncio.sleep(0.3)
    assert not backend.completed, "generate 應該被 callback 拋出的例外中止，而不是跑完"


async def test_取消時模型所有權保留到同步worker退出_並避免風格交錯():
    """取消消費端後立即返回，但同 backend 的下一句要等舊 generate 真正退出。"""
    from uep.stream.contracts.style import SpeechStyle

    first_started = threading.Event()
    release_first = threading.Event()

    class HeldBackend(FakeBackend):
        def __init__(self):
            super().__init__(segments_per_sentence=1)
            self.active = 0
            self.max_active = 0
            self.styles = []
            self.exited = threading.Event()

        def generate(self, text, output_path, on_segment_audio=None, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(text)
            self.styles.append(tuple(self.emotion_vector or []))
            try:
                if text == "嗯…":
                    first_started.set()
                    release_first.wait(2)
                if on_segment_audio is not None:
                    on_segment_audio(np.zeros(128, dtype="float32"), 0, 1)
            finally:
                self.active -= 1
                if text == "嗯…":
                    self.exited.set()

    backend = HeldBackend()
    stage = IndexTTS2SpeakStage(backend=backend, queue_size=1)

    class Sink:
        async def write(self, chunk):
            raise AssertionError("取消中的墊片不應寫入音訊")

    gap = GapFiller(
        GapFillerPolicy(enabled=True, start_delay_s=0, phrases={"zh": ["嗯…"]})
    )
    gap.attach(stage, lambda: SpeechStyle(emotion={"happy": 1.0}))
    gap.start(Sink(), "gap-turn", CancellationToken(), language="zh")
    assert await asyncio.to_thread(first_started.wait, 1)

    # stop 不等 thread；取消 token 後 callback 在下一個輸出點退出。
    await asyncio.wait_for(gap.stop(), timeout=0.2)
    formal = asyncio.create_task(
        _collect(stage.stream(sentence_stream(["正式回答。"]), CancellationToken()))
    )
    await asyncio.sleep(0.05)
    assert backend.calls == ["嗯…"], "正式回答須等墊片 worker 退出"

    release_first.set()
    chunks = await asyncio.wait_for(formal, timeout=2)
    assert chunks
    assert backend.exited.is_set()
    assert backend.calls == ["嗯…", "正式回答。"]
    assert backend.max_active == 1
    assert backend.styles[0][0] > 0, "墊片使用自己的風格"
    assert backend.styles[1] == (), "正式回答還原中性預設，不繼承墊片風格"


async def test_已過期墊片排在模型鎖後時不進入generate():
    from uep.stream.contracts.style import SpeechStyle

    first_started = threading.Event()
    release_first = threading.Event()
    gap_waiting_for_owner = threading.Event()

    class HeldBackend(FakeBackend):
        def generate(self, text, output_path, on_segment_audio=None, **kwargs):
            self.calls.append(text)
            if text == "正式回答。":
                first_started.set()
                release_first.wait(2)
            if on_segment_audio is not None:
                on_segment_audio(np.zeros(128, dtype="float32"), 0, 1)

    class TrackedStage(IndexTTS2SpeakStage):
        async def _synthesize_exclusive(self, sentence, audio_ch, loop, token):
            if sentence.turn_id.startswith("gap-"):
                gap_waiting_for_owner.set()
            await super()._synthesize_exclusive(sentence, audio_ch, loop, token)

    backend = HeldBackend()
    stage = TrackedStage(backend=backend, queue_size=1)
    formal = asyncio.create_task(
        _collect(stage.stream(sentence_stream(["正式回答。"]), CancellationToken()))
    )
    assert await asyncio.to_thread(first_started.wait, 1)

    class Sink:
        async def write(self, chunk):
            raise AssertionError("過期墊片不能排進 sink")

    gap = GapFiller(GapFillerPolicy(enabled=True, start_delay_s=0, phrases={"zh": ["嗯…"]}))
    gap.attach(stage, lambda: SpeechStyle(emotion={"happy": 1.0}))
    gap.start(Sink(), "gap-turn", CancellationToken(), language="zh")
    assert await asyncio.to_thread(gap_waiting_for_owner.wait, 1)
    await asyncio.wait_for(gap.stop(), timeout=0.2)

    release_first.set()
    assert await asyncio.wait_for(formal, timeout=2)
    await asyncio.sleep(0.05)
    assert backend.calls == ["正式回答。"], "已過期墊片不得進入 generate()"


async def _collect(stream):
    return [chunk async for chunk in stream]


async def test_佇列滿時aclose取消阻塞callback並讓同步worker退出(monkeypatch):
    third_put = threading.Event()
    worker_exited = threading.Event()

    from uep.stream.channel import StreamChannel

    original_put = StreamChannel.put_threadsafe

    def track_third_put(channel, item, loop):
        if item.chunk_index == 2:
            third_put.set()
        original_put(channel, item, loop)

    monkeypatch.setattr(StreamChannel, "put_threadsafe", track_third_put)

    class FullQueueBackend(FakeBackend):
        def generate(self, text, output_path, on_segment_audio=None, **kwargs):
            try:
                on_segment_audio(np.zeros(128, dtype="float32"), 0, 3)
                on_segment_audio(np.zeros(128, dtype="float32"), 1, 3)
                on_segment_audio(np.zeros(128, dtype="float32"), 2, 3)
            finally:
                worker_exited.set()

    stage = IndexTTS2SpeakStage(backend=FullQueueBackend(), queue_size=1)
    token = CancellationToken()
    stream = stage.stream(sentence_stream(["一句。"]), token)
    await stream.__anext__()  # 第二段填滿 queue，第三段會卡在 callback
    assert await asyncio.to_thread(third_put.wait, 1)
    assert not worker_exited.is_set(), "第三個 callback 應因 queue full 阻塞 worker"
    await stream.aclose()
    assert not token.is_cancelled, "關閉單一 stream 不得取消共用 turn token"
    assert await asyncio.to_thread(worker_exited.wait, 1), "滿佇列 callback 不得留下 worker"


def test_build_backend明確傳遞可配置的use_accel(monkeypatch):
    import sys
    import types

    captured = {}

    class BackendFactory:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    backend_module = types.ModuleType("echo_tts.runtime.indextts_backend")
    backend_module.IndexTTS2Backend = BackendFactory
    generate_module = types.ModuleType("echo_tts.runtime.indextts_generate")
    generate_module.default_generation_paths = lambda *a, **k: "fake-paths"
    for name in ("echo_tts", "echo_tts.runtime"):
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, "echo_tts.runtime.indextts_backend", backend_module)
    monkeypatch.setitem(sys.modules, "echo_tts.runtime.indextts_generate", generate_module)
    stage = IndexTTS2SpeakStage(use_accel=True)
    stage._build_backend()
    assert captured["use_accel"] is True

    captured.clear()
    IndexTTS2SpeakStage()._build_backend()
    assert captured["use_accel"] is False, "保留預設關閉，須由部署設定明確開啟"


# --- 錯誤傳播 ---


async def test_backend_爆炸會傳給消費端():
    class BoomBackend(FakeBackend):
        def generate(self, *args, **kwargs):
            raise RuntimeError("引擎爆了")

    stage = IndexTTS2SpeakStage(backend=BoomBackend())
    with pytest.raises(RuntimeError, match="引擎爆了"):
        async for _ in stage.stream(sentence_stream(["一。"]), CancellationToken()):
            pass


# --- 與 PipelineRunner 整合 ---


async def test_接進_PipelineRunner():
    from uep.stream.contracts.types import TurnPhase, Utterance
    from uep.stream.pipeline import PipelineRunner
    from uep.stream.splitter import SplitPolicy
    from uep.fakes.stream import FakeAudioSink, FakeThinkStage

    backend = FakeBackend(segments_per_sentence=1, segment_seconds=0.1)
    think = FakeThinkStage(
        responses=["第一句很短。第二句也不長。"],
        first_token_delay_s=0.0,
        token_interval_s=0.0,
        memory_delay_s=0.0,
        split_policy=SplitPolicy(),
    )
    runner = PipelineRunner(
        think_stage=think,
        speak_stage=IndexTTS2SpeakStage(backend=backend),
        sink=FakeAudioSink(realtime=False),
    )
    result = await runner.run_turn(Utterance(text="你好"))

    assert result.phase is TurnPhase.DONE
    assert result.spoken_text == result.generated_text
    assert backend.calls


# --- 段緣平滑（消爆音） ---


def test_段緣淡入淡出從零開始():
    from uep.stream.adapters.speak_indextts import smooth_segment_edges

    pcm = b"\x00\x40" * 1000  # 恆定振幅 16384
    out = smooth_segment_edges(pcm, 22050, fade_ms=4.0, tail_silence_ms=0)
    import struct

    samples = struct.unpack(f"<{len(out) // 2}h", out)
    assert samples[0] == 0  # 段首歸零——邊界不連續就是爆音來源
    assert samples[-1] == 0
    assert samples[500] == 16384  # 中段不受影響


def test_段尾靜音墊長度正確():
    from uep.stream.adapters.speak_indextts import smooth_segment_edges

    pcm = b"\x00\x40" * 1000
    out = smooth_segment_edges(pcm, 22050, fade_ms=0, tail_silence_ms=20.0)
    pad_samples = int(22050 * 20 / 1000)
    assert len(out) == len(pcm) + pad_samples * 2
    assert out[len(pcm):] == b"\x00\x00" * pad_samples


def test_過短的段不會因淡化壞掉():
    from uep.stream.adapters.speak_indextts import smooth_segment_edges

    pcm = b"\x00\x40" * 4  # 4 個樣本，比淡化窗還短
    out = smooth_segment_edges(pcm, 22050, fade_ms=4.0, tail_silence_ms=0)
    assert len(out) == len(pcm)


# --- 逐句 style 套用 ---


async def test_style_的情緒向量乘上強度(monkeypatch):
    """echo_tts 不可 import 時的退回路徑：向量 × 強度。

    明確擋掉 echo_tts——共用環境把它 editable 裝進來後，沒擋的話會走真換算，
    這個測試的結果就變成依賴環境。"""
    import sys

    from uep.stream.contracts.style import SpeechStyle

    monkeypatch.setitem(sys.modules, "echo_tts.config.emotion_presets", None)
    backend = FakeBackend(segments_per_sentence=1)
    stage = IndexTTS2SpeakStage(backend=backend)
    await stage.prepare()

    style = SpeechStyle(emotion={"happy": 0.8}, intensity=0.5)
    stage._apply_style(style)
    assert backend.emotion_vector[0] == pytest.approx(0.4)  # happy 是第 0 維


async def test_style_的情緒強度走_echo_tts_區間():
    """echo_tts 可 import 時：總和等於 echo_tts 對該刻度的強度（區間由 TTS 定案）。"""
    presets = pytest.importorskip("echo_tts.config.emotion_presets")
    from uep.stream.contracts.style import SpeechStyle

    backend = FakeBackend(segments_per_sentence=1)
    stage = IndexTTS2SpeakStage(backend=backend)
    await stage.prepare()

    stage._apply_style(SpeechStyle(emotion={"happy": 0.8}, intensity=0.5))
    assert sum(backend.emotion_vector) == pytest.approx(presets.user_to_strength(0.5))
    assert backend.emotion_vector[0] == max(backend.emotion_vector)


async def test_中性句還原角色檔預設情緒():
    """一句帶情緒之後的中性句**不能殘留**上一句的情緒。"""
    from uep.stream.contracts.style import SpeechStyle

    backend = FakeBackend(segments_per_sentence=1)
    backend.emotion_vector = [0.0] * 7 + [0.5]  # 角色檔預設：calm 0.5
    stage = IndexTTS2SpeakStage(backend=backend)
    await stage.prepare()

    stage._apply_style(SpeechStyle(emotion={"angry": 0.8}))
    assert backend.emotion_vector[1] > 0  # angry 已套上

    stage._apply_style(None)
    assert backend.emotion_vector == [0.0] * 7 + [0.5]  # 還原，不是殘留


async def test_style_語速退場時回到基準值():
    from uep.stream.contracts.style import SpeechStyle

    backend = FakeBackend(segments_per_sentence=1)
    stage = IndexTTS2SpeakStage(backend=backend, speed=0.2)
    await stage.prepare()
    assert backend.speed_offset == 0.2  # 啟動基準

    stage._apply_style(SpeechStyle(speed=1.3))
    assert backend.speed_offset != 0.2

    stage._apply_style(None)
    assert backend.speed_offset == 0.2


def test_sanitize_把列表破折號換成頓點():
    from uep.stream.adapters.speak_indextts import sanitize_for_tts

    assert sanitize_for_tts("小說 - 死亡擱淺") == "小說，死亡擱淺"
    # 連字號緊貼單字（well-known、2-3）不受影響
    assert sanitize_for_tts("a well-known game") == "a well-known game"


# --- 暫存目錄清掃 ---


def test_sweep_刪空目錄與過期目錄_留新的非空目錄(tmp_path):
    import os
    import time

    from uep.stream.adapters.speak_indextts import TEMP_PREFIX, sweep_stale_tempdirs

    empty = tmp_path / f"{TEMP_PREFIX}empty"
    empty.mkdir()
    os.utime(empty, (time.time() - 3600, time.time() - 3600))
    stale = tmp_path / f"{TEMP_PREFIX}stale"
    stale.mkdir()
    (stale / "x.wav").write_bytes(b"0")
    old = time.time() - 3 * 24 * 3600
    os.utime(stale, (old, old))
    fresh = tmp_path / f"{TEMP_PREFIX}fresh"
    fresh.mkdir()
    (fresh / "y.wav").write_bytes(b"0")
    other = tmp_path / "unrelated"
    other.mkdir()

    assert sweep_stale_tempdirs(tmp_path) == 2
    assert not empty.exists() and not stale.exists()
    assert fresh.exists() and other.exists()
    assert sweep_stale_tempdirs(tmp_path) == 0


def test_sweep_不刪另一個執行中_stage_的空目錄(tmp_path):
    """回歸：keep_wav=False 時兩句之間目錄是空的，舊版 sweep 會誤刪。"""
    import os
    import time

    from uep.stream.adapters.speak_indextts import PID_MARKER, TEMP_PREFIX, sweep_stale_tempdirs

    live = tmp_path / f"{TEMP_PREFIX}live"
    live.mkdir()
    (live / PID_MARKER).write_text(str(os.getpid()))
    old = time.time() - 3 * 24 * 3600
    os.utime(live, (old, old))  # 連過期也不能刪：擁有者還活著
    dead = tmp_path / f"{TEMP_PREFIX}dead"
    dead.mkdir()
    (dead / PID_MARKER).write_text("999999999")
    os.utime(dead, (old, old))
    fresh_empty = tmp_path / f"{TEMP_PREFIX}fresh_empty"
    fresh_empty.mkdir()

    assert sweep_stale_tempdirs(tmp_path) == 1
    assert live.exists() and fresh_empty.exists() and not dead.exists()


def test_next_output_path_目錄被刪後自我修復(tmp_path):
    import shutil

    from uep.stream.adapters.speak_indextts import IndexTTS2SpeakStage
    from uep.stream.contracts.types import Sentence

    stage = IndexTTS2SpeakStage.__new__(IndexTTS2SpeakStage)
    stage._output_dir = None
    stage._tempdir = None
    sentence = Sentence(turn_id="t", index=0, text="好")
    first = stage._next_output_path(sentence)
    shutil.rmtree(first.parent)
    second = stage._next_output_path(sentence)
    assert second.parent.is_dir()


def test_Aborted標記為取消而非失敗():
    from uep.stream.adapters.speak_indextts import _Aborted

    assert _Aborted.is_cancellation is True
    assert getattr(_Aborted(), "is_cancellation", False) is True
