"""多聲道 InputStage 測試——fake backend、無 GPU、無第三方套件。

音訊用常數振幅（DC）訊號：RMS 穩定高於 VAD 閾值，且第一個樣本值可當作
「這段音訊是誰的」標記，讓 backend 把它原樣吐回文字。
"""

from __future__ import annotations

import asyncio
import struct
import threading
import time

from uep.stream.adapters.input_multichannel import MultiChannelSttInputStage
from uep.stream.adapters.input_stt import TranscriptionResult
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.stages import MultiChannelInputStage
from uep.stream.contracts.turn import TurnPolicy
from uep.stream.audio.multichannel import TranscriberPool

SR = 16_000
CHUNK_S = 0.02


def dc(value: int, seconds: float) -> bytes:
    n = int(SR * seconds)
    return struct.pack(f"<{n}h", *([value] * n))


def quiet(seconds: float) -> bytes:
    return b"\x00\x00" * int(SR * seconds)


def split(pcm: bytes, chunk_s: float = CHUNK_S) -> list[bytes]:
    step = int(SR * chunk_s) * 2
    return [pcm[i : i + step] for i in range(0, len(pcm), step)]


class EchoBackend:
    """文字 = 音訊第一個樣本值；記錄呼叫順序與同時執行數。"""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls: list[int] = []
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0
        self.closed = False

    def transcribe(self, pcm: bytes, sample_rate: int) -> TranscriptionResult | None:
        assert sample_rate == SR
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            if self.delay_s:
                time.sleep(self.delay_s)
            value = max(memoryview(pcm).cast("h"))  # preroll 的靜音在前面，取最大值
            with self._lock:
                self.calls.append(value)
            return TranscriptionResult(text=str(value), language="zh")
        finally:
            with self._lock:
                self._active -= 1

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def make_stage(backend=None, **kwargs) -> MultiChannelSttInputStage:
    policy = kwargs.pop("policy", TurnPolicy(silence_threshold_s=0.3, min_utterance_s=0.2))
    return MultiChannelSttInputStage(backend=backend or EchoBackend(), policy=policy, **kwargs)


class Session:
    """開啟 stage 並在背景收集 Utterance。"""

    def __init__(self, stage: MultiChannelSttInputStage) -> None:
        self.stage = stage
        self.token = CancellationToken()
        self.results: list = []
        self.task = asyncio.ensure_future(self._consume())

    async def _consume(self) -> None:
        async for utterance in self.stage.open(self.token):
            self.results.append(utterance)

    async def ready(self) -> Session:
        for _ in range(200):
            if self.stage.engine._loop is not None:
                return self
            await asyncio.sleep(0.005)
        raise AssertionError("stage 沒有開啟")

    async def wait_for(self, n: int, timeout: float = 5.0) -> list:
        deadline = time.perf_counter() + timeout
        while len(self.results) < n:
            if time.perf_counter() > deadline:
                raise AssertionError(f"只收到 {len(self.results)}/{n} 則")
            await asyncio.sleep(0.01)
        return self.results

    async def close(self) -> None:
        self.token.cancel()
        await asyncio.wait_for(self.task, 5)


async def open_session(stage: MultiChannelSttInputStage) -> Session:
    return await Session(stage).ready()


def feed_all(stage: MultiChannelSttInputStage, sid: str, pcm: bytes) -> None:
    for chunk in split(pcm):
        stage.feed(sid, chunk)


def test_符合_MultiChannelInputStage_協定() -> None:
    assert isinstance(make_stage(), MultiChannelInputStage)


class TestTurns:
    async def test_兩聲道交錯_turn_邊界獨立且_speaker_id_正確(self) -> None:
        stage = make_stage()
        session = await open_session(stage)
        try:
            a = split(dc(1000, 0.6) + quiet(0.6))
            b = split(dc(2000, 0.4) + quiet(0.6))
            for i in range(max(len(a), len(b))):
                if i < len(a):
                    stage.feed("A", a[i])
                if i < len(b):
                    stage.feed("B", b[i])
            results = await session.wait_for(2)
            by_speaker = {u.speaker_id: u for u in results}
            assert set(by_speaker) == {"A", "B"}
            assert by_speaker["A"].text == "1000"
            assert by_speaker["B"].text == "2000"
            assert stage.engine.stats["channels_rejected"] == 0
        finally:
            await session.close()

    async def test_同聲道多個_turn_順序不亂(self) -> None:
        backend = EchoBackend(delay_s=0.02)
        stage = make_stage(backend)
        session = await open_session(stage)
        try:
            for value in (1111, 1222, 1333):
                feed_all(stage, "A", dc(value, 0.5) + quiet(0.6))
            results = await session.wait_for(3)
            assert [u.text for u in results] == ["1111", "1222", "1333"]
            assert {u.speaker_id for u in results} == {"A"}
        finally:
            await session.close()

    async def test_增量切分的多段拼成一則_Utterance(self) -> None:
        parts: list[tuple[str, str, int]] = []
        stage = make_stage(
            on_partial=lambda sid, text, idx: parts.append((sid, text, idx)),
            partial_silence_s=0.2,
            partial_min_speech_s=0.4,
            policy=TurnPolicy(silence_threshold_s=0.6, min_utterance_s=0.2),
        )
        session = await open_session(stage)
        try:
            feed_all(stage, "A", dc(1700, 0.6) + quiet(0.3) + dc(1700, 0.5) + quiet(0.8))
            (utt,) = await session.wait_for(1)
            assert utt.metadata["channel_segments"] >= 2
            assert utt.text.split() == ["1700"] * utt.metadata["channel_segments"]
            assert parts and parts[0][0] == "A"
        finally:
            await session.close()

    async def test_metadata_時間順序(self) -> None:
        stage = make_stage(EchoBackend(delay_s=0.02))
        session = await open_session(stage)
        try:
            feed_all(stage, "A", dc(1500, 0.5) + quiet(0.6))
            (utt,) = await session.wait_for(1)
            md = utt.metadata
            assert md["speech_start_ts"] <= md["speech_end_ts"] <= md["turn_end_ts"]
            assert md["turn_end_ts"] <= md["stt_done_ts"]
            assert md["stt_ms"] >= 15
            assert md["queue_ms"] >= 0
            assert md["channel_segments"] == 1
            assert "temperature" not in md  # backend 沒給解碼品質就不放鍵
            assert utt.started_at == md["speech_start_ts"]
            assert utt.ended_at == md["speech_end_ts"]
        finally:
            await session.close()


class TestSilenceWithoutPackets:
    async def test_無封包時以補靜音結束_turn(self) -> None:
        """Discord 不說話就不送封包：只靠時鐘前進也要能結束 turn。"""
        clock = FakeClock()
        stage = make_stage(clock=clock, tick_interval_s=1000.0)
        session = await open_session(stage)
        try:
            feed_all(stage, "A", dc(1900, 0.5))
            await asyncio.sleep(0.1)
            assert stage.engine._channels["A"].core.active
            assert session.results == []

            # 時間過了 0.2s：靜音還不夠長（門檻 0.3s）
            clock.t += 0.5 + 0.2
            stage.engine._tick(clock())
            await asyncio.sleep(0.05)
            assert session.results == []

            clock.t += 0.3
            stage.engine._tick(clock())
            (utt,) = await session.wait_for(1)
            assert utt.speaker_id == "A" and utt.text == "1900"
            md = utt.metadata
            assert md["speech_end_ts"] <= md["turn_end_ts"] <= md["stt_done_ts"]
        finally:
            await session.close()

    async def test_背景_tick_迴圈真的會補靜音(self) -> None:
        stage = make_stage(tick_interval_s=0.02)
        session = await open_session(stage)
        try:
            feed_all(stage, "A", dc(1900, 0.4))  # 之後完全沒有封包
            (utt,) = await session.wait_for(1, timeout=4)
            assert utt.speaker_id == "A"
        finally:
            await session.close()

    async def test_封包缺口在下一個封包抵達時補靜音(self) -> None:
        clock = FakeClock()
        stage = make_stage(clock=clock, tick_interval_s=1000.0)
        session = await open_session(stage)
        try:
            feed_all(stage, "A", dc(1900, 0.5))
            # 靜默 1.0 秒後才又來一個封包（中間沒有 tick）
            stage.feed("A", dc(1901, CHUNK_S), ts=clock() + 1.5)
            (utt,) = await session.wait_for(1)
            assert utt.text == "1900"
        finally:
            await session.close()


class TestVoiceEvent:
    async def test_每個_chunk_觸發且帶_speaker_id_與_is_voiced(self) -> None:
        events = []

        def boom_then_record(event) -> None:
            events.append(event)
            raise RuntimeError("訂閱者壞掉")

        stage = make_stage(on_voice_event=boom_then_record)
        session = await open_session(stage)
        try:
            feed_all(stage, "A", dc(1800, 0.1))
            feed_all(stage, "B", quiet(0.1))
            await asyncio.sleep(0.2)
            assert len(events) == 10
            a = [e for e in events if e.speaker_id == "A"]
            b = [e for e in events if e.speaker_id == "B"]
            assert len(a) == 5 and all(e.is_voiced for e in a)
            assert len(b) == 5 and not any(e.is_voiced for e in b)
            assert all(isinstance(e.timestamp, float) for e in events)
            # callback 丟例外後管線仍活著
            feed_all(stage, "A", dc(1800, 0.3) + quiet(0.5))
            await session.wait_for(1)
        finally:
            await session.close()


class TestLifecycle:
    async def test_閒置聲道被回收_進行中的不回收(self) -> None:
        clock = FakeClock()
        stage = make_stage(clock=clock, tick_interval_s=1000.0, idle_evict_s=10.0)
        session = await open_session(stage)
        try:
            feed_all(stage, "idle", quiet(0.1))
            feed_all(stage, "busy", dc(1900, 0.4))
            await asyncio.sleep(0.1)
            clock.t += 30
            stage.engine._tick(clock())
            assert "idle" not in stage.engine.channel_ids
            assert "busy" in stage.engine.channel_ids  # 有進行中的語音，不能收
            assert stage.engine.stats["channels_evicted"] == 1
        finally:
            await session.close()

    async def test_leave_flush_送出殘餘並移除聲道(self) -> None:
        stage = make_stage()
        session = await open_session(stage)
        try:
            feed_all(stage, "A", dc(1600, 0.5))  # 還沒講完
            stage.leave("A", flush=True)
            (utt,) = await session.wait_for(1)
            assert utt.speaker_id == "A" and utt.text == "1600"
            assert "A" not in stage.engine.channel_ids
        finally:
            await session.close()

    async def test_leave_不_flush_不送出(self) -> None:
        stage = make_stage()
        session = await open_session(stage)
        try:
            feed_all(stage, "A", dc(1600, 0.5))
            stage.leave("A", flush=False)
            await asyncio.sleep(0.3)
            assert session.results == []
            assert "A" not in stage.engine.channel_ids
        finally:
            await session.close()

    async def test_斷線重連_同_id_是乾淨的新聲道(self) -> None:
        stage = make_stage()
        session = await open_session(stage)
        try:
            feed_all(stage, "A", dc(1600, 0.5))
            old = stage.engine._channels["A"]
            stage.leave("A", flush=True)
            await session.wait_for(1)
            assert "A" not in stage.engine.channel_ids

            stage.join("A")
            feed_all(stage, "A", dc(1650, 0.5) + quiet(0.6))
            new = stage.engine._channels["A"]
            assert new is not old
            assert new.core.gen == 0
            assert new.core.vad is not old.core.vad
            assert new.core.vad.clock_s <= 1.2  # 時鐘從頭算，不繼承舊聲道
            results = await session.wait_for(2)
            assert [u.text for u in results] == ["1600", "1650"]
        finally:
            await session.close()

    async def test_leave_與_discard_交錯不送出被丟棄的段(self) -> None:
        stage = make_stage()
        session = await open_session(stage)
        try:
            feed_all(stage, "A", dc(1600, 0.5))
            stage.leave("A", flush=True)
            stage.discard("A")
            await asyncio.sleep(0.4)
            assert session.results == []

            # 先讓 flush 跑完、段已在轉錄路上，再 discard：世代作廢
            feed_all(stage, "B", dc(1610, 0.5))
            stage.leave("B", flush=True)
            await asyncio.sleep(0)
            stage.discard(None)
            await asyncio.sleep(0.4)
            assert session.results == []
        finally:
            await session.close()

    async def test_discard_只影響該聲道(self) -> None:
        # min_utterance 拉高：被丟棄後殘留的尾巴（含 VAD hangover）不足以成 turn
        stage = make_stage(policy=TurnPolicy(silence_threshold_s=0.3, min_utterance_s=0.4))
        session = await open_session(stage)
        try:
            a = split(dc(1100, 0.5) + quiet(0.6))
            b = split(dc(1200, 0.5) + quiet(0.6))
            for i in range(len(a)):
                stage.feed("A", a[i])
                stage.feed("B", b[i])
                if i == 20:  # A 講到一半被丟棄
                    stage.discard("A")
            await asyncio.sleep(0.5)
            assert [u.speaker_id for u in session.results] == ["B"]
            assert stage.engine._channels["A"].core.gen == 1
            assert stage.engine._channels["B"].core.gen == 0
        finally:
            await session.close()

    async def test_discard_None_丟棄全部聲道(self) -> None:
        stage = make_stage()
        session = await open_session(stage)
        try:
            feed_all(stage, "A", dc(1100, 0.5))
            feed_all(stage, "B", dc(1200, 0.5))
            await asyncio.sleep(0.1)
            stage.discard()
            feed_all(stage, "A", quiet(0.6))
            feed_all(stage, "B", quiet(0.6))
            await asyncio.sleep(0.4)
            assert session.results == []
        finally:
            await session.close()

    async def test_max_channels_超過不崩潰(self) -> None:
        stage = make_stage(max_channels=2)
        session = await open_session(stage)
        try:
            for sid in ("A", "B", "C"):
                stage.feed(sid, dc(1100, CHUNK_S))
            await asyncio.sleep(0.1)
            assert sorted(stage.engine.channel_ids) == ["A", "B"]
            assert stage.engine.stats["channels_rejected"] == 1
        finally:
            await session.close()

    async def test_佇列滿了丟最舊並計數(self) -> None:
        stage = make_stage(chunk_queue_max=3)
        session = await open_session(stage)
        try:
            for _ in range(10):  # 同一個 loop tick 內連續 feed，pump 來不及消化
                stage.feed("A", dc(1100, CHUNK_S))
            assert stage.engine.stats["chunks_dropped"] == 7
        finally:
            await session.close()

    async def test_從其他執行緒_feed(self) -> None:
        stage = make_stage()
        session = await open_session(stage)
        try:
            def producer() -> None:
                for chunk in split(dc(1333, 0.5) + quiet(0.6)):
                    stage.feed("T", chunk)

            thread = threading.Thread(target=producer)
            thread.start()
            thread.join()
            (utt,) = await session.wait_for(1)
            assert utt.speaker_id == "T" and utt.text == "1333"
        finally:
            await session.close()

    async def test_token_取消後沒有懸掛_task(self) -> None:
        stage = make_stage()
        session = await open_session(stage)
        feed_all(stage, "A", dc(1100, 0.2))
        feed_all(stage, "B", dc(1100, 0.2))
        await asyncio.sleep(0.1)
        await session.close()
        await asyncio.sleep(0.05)
        current = asyncio.current_task()
        leftover = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        assert leftover == []
        assert stage.engine._loop is None
        # 關閉後 feed 不崩潰，只計數
        stage.feed("A", dc(1100, CHUNK_S))
        assert stage.engine.stats["feed_not_open"] >= 1

    async def test_prepare_不重載已注入的_backend(self, monkeypatch) -> None:
        backend = EchoBackend()
        stage = make_stage(backend)

        def boom(self):
            raise AssertionError("不該重載已注入的 backend")

        monkeypatch.setattr(MultiChannelSttInputStage, "_build_backend", boom)
        await stage.prepare()
        await stage.prepare()
        assert stage._backend is backend
        await stage.aclose()
        assert backend.closed is False  # 注入的 backend 屬於宿主


class TestTranscriberPool:
    async def test_同時執行數不超過上限(self) -> None:
        backend = EchoBackend(delay_s=0.03)
        pool = TranscriberPool(backend, max_concurrency=2)
        jobs = [pool.run(f"S{i}", struct.pack("<h", i + 1) * 100, SR) for i in range(5)]
        await asyncio.gather(*jobs)
        assert backend.peak == 2
        assert pool.peak_active == 2

    async def test_預設併發為_1(self) -> None:
        backend = EchoBackend(delay_s=0.02)
        pool = TranscriberPool(backend)
        await asyncio.gather(*(pool.run(f"S{i}", struct.pack("<h", 1) * 100, SR) for i in range(3)))
        assert backend.peak == 1

    async def test_同聲道不並行且順序固定(self) -> None:
        backend = EchoBackend(delay_s=0.02)
        pool = TranscriberPool(backend, max_concurrency=3)
        await asyncio.gather(
            *(pool.run("A", struct.pack("<h", v) * 100, SR) for v in (1, 2, 3, 4))
        )
        assert backend.calls == [1, 2, 3, 4]
        assert backend.peak == 1

    async def test_跨聲道_round_robin_不餓死(self) -> None:
        backend = EchoBackend(delay_s=0.02)
        pool = TranscriberPool(backend, max_concurrency=1)
        a = [pool.run("A", struct.pack("<h", v) * 100, SR) for v in (11, 12, 13, 14)]
        b = [pool.run("B", struct.pack("<h", 21) * 100, SR)]
        c = [pool.run("C", struct.pack("<h", 31) * 100, SR)]
        await asyncio.gather(*a, *b, *c)
        # A 的第一個先跑；B、C 插在 A 的後續任務之前輪流
        assert backend.calls[:3] == [11, 21, 31]
        assert backend.calls[3:] == [12, 13, 14]

    async def test_有_transcribe_for_時帶_speaker_id(self) -> None:
        seen: list[tuple[str, int]] = []

        class PerChannel:
            def transcribe(self, pcm, sample_rate):
                raise AssertionError("應該走 transcribe_for")

            def transcribe_for(self, speaker_id, pcm, sample_rate):
                seen.append((speaker_id, sample_rate))
                return TranscriptionResult(text=speaker_id)

        pool = TranscriberPool(PerChannel())
        result = await pool.run("alice", b"\x01\x00" * 10, SR)
        assert result.result.text == "alice"
        assert seen == [("alice", SR)]

    async def test_queue_ms_與_stt_ms_分開(self) -> None:
        backend = EchoBackend(delay_s=0.05)
        pool = TranscriberPool(backend, max_concurrency=1)
        first, second = await asyncio.gather(
            pool.run("A", struct.pack("<h", 1) * 100, SR),
            pool.run("B", struct.pack("<h", 2) * 100, SR),
        )
        assert first.queue_ms < 30
        assert second.queue_ms >= 40  # 等了第一個任務
        assert second.stt_ms >= 40
        assert second.stt_ms < second.queue_ms + second.stt_ms
        pool.close()

    async def test_discard_讓排隊中的任務以空結果完成(self) -> None:
        backend = EchoBackend(delay_s=0.05)
        pool = TranscriberPool(backend, max_concurrency=1)
        running = asyncio.ensure_future(pool.run("A", struct.pack("<h", 1) * 100, SR))
        queued = asyncio.ensure_future(pool.run("B", struct.pack("<h", 2) * 100, SR))
        await asyncio.sleep(0.01)
        pool.discard("B")
        assert (await queued).result is None
        assert (await running).result.text == "1"
        assert backend.calls == [1]


class _GateBackend:
    """value 在 ``slow`` 內的任務會卡在 gate 上，直到測試放行；其餘立即完成。"""

    def __init__(self, slow: set[int]) -> None:
        self.slow = slow
        self.gate = threading.Event()
        self.started: list[int] = []
        self.finished: list[int] = []

    def transcribe(self, pcm: bytes, sample_rate: int) -> TranscriptionResult | None:
        value = struct.unpack("<h", pcm[:2])[0]
        self.started.append(value)
        if value in self.slow:
            self.gate.wait(5.0)
        self.finished.append(value)
        return TranscriptionResult(text=str(value))


def _pcm(value: int) -> bytes:
    return struct.pack("<h", value) * 100


class TestTranscriberPoolTimeout:
    async def test_停用時行為不變(self) -> None:
        backend = _GateBackend(slow=set())
        pool = TranscriberPool(backend)
        assert pool.job_timeout_s is None
        result = await pool.run("A", _pcm(1), SR)
        assert result.result.text == "1"
        assert result.timed_out is False
        assert pool.timeouts == 0

    async def test_停用時慢任務照常等完(self) -> None:
        backend = _GateBackend(slow={1})
        pool = TranscriberPool(backend)
        slow = asyncio.ensure_future(pool.run("A", _pcm(1), SR))
        nxt = asyncio.ensure_future(pool.run("B", _pcm(2), SR))
        await asyncio.sleep(0.15)
        assert not nxt.done()  # 沒設逾時：B 一直排隊
        backend.gate.set()
        assert (await slow).result.text == "1"
        assert (await nxt).result.text == "2"

    async def test_逾時後下一個任務不再等滿(self) -> None:
        backend = _GateBackend(slow={1})
        pool = TranscriberPool(backend, job_timeout_s=0.1)
        try:
            slow = asyncio.ensure_future(pool.run("A", _pcm(1), SR))
            nxt = asyncio.ensure_future(pool.run("B", _pcm(2), SR))
            t0 = time.perf_counter()
            second = await asyncio.wait_for(nxt, 2.0)
            assert second.result.text == "2"
            assert second.timed_out is False
            assert time.perf_counter() - t0 < 1.0  # gate 未放行：不是等慢任務完成
            assert (await slow).timed_out is True
            assert pool.timeouts == 1
        finally:
            backend.gate.set()
            await asyncio.sleep(0.05)

    async def test_逾時結果被丟棄且記_warning(self, caplog) -> None:
        backend = _GateBackend(slow={1})
        pool = TranscriberPool(backend, job_timeout_s=0.1)
        try:
            with caplog.at_level("WARNING", logger="uep.stream.audio.multichannel"):
                result = await pool.run("alice", _pcm(1), SR)
            assert result.result is None
            assert result.timed_out is True
            assert any(
                "alice" in r.getMessage() and "逾時" in r.getMessage() for r in caplog.records
            )
        finally:
            backend.gate.set()
            await asyncio.sleep(0.05)

    async def test_晚到結果不外洩且不影響後續(self) -> None:
        backend = _GateBackend(slow={1})
        pool = TranscriberPool(backend, job_timeout_s=0.1)
        timed_out = await pool.run("A", _pcm(1), SR)
        assert timed_out.result is None
        backend.gate.set()  # 孤兒執行緒此時才完成
        await asyncio.sleep(0.1)
        assert 1 in backend.finished
        assert pool._orphans == 0  # 額度歸還
        # 同聲道的後續任務順序、狀態正常，拿到的是自己的結果
        after = await pool.run("A", _pcm(3), SR)
        assert after.result.text == "3"
        assert after.timed_out is False
        assert pool.timeouts == 1

    async def test_孤兒額滿時不再放棄(self) -> None:
        backend = _GateBackend(slow={1, 2})
        pool = TranscriberPool(backend, job_timeout_s=0.1, max_orphans=1)
        try:
            first = await pool.run("A", _pcm(1), SR)
            assert first.timed_out is True
            second = asyncio.ensure_future(pool.run("B", _pcm(2), SR))
            await asyncio.sleep(0.3)
            assert not second.done()  # 孤兒額滿：照常等，不疊第三條 GPU 工作
            assert pool.timeouts == 1
            backend.gate.set()
            result = await asyncio.wait_for(second, 2.0)
            assert result.result.text == "2"
            assert result.timed_out is False
        finally:
            backend.gate.set()
            await asyncio.sleep(0.05)

    async def test_逾時值必須為正(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            TranscriberPool(_GateBackend(set()), job_timeout_s=0)

    async def test_長音訊的上限依音訊長度縮放(self) -> None:
        backend = _GateBackend(slow={1})
        pool = TranscriberPool(backend, job_timeout_s=0.1, job_timeout_per_audio_s=0.2)
        loop = asyncio.get_running_loop()
        pcm = struct.pack("<h", 1) * (SR * 3)  # 3 秒 → 上限 0.1 + 0.2 × 3 = 0.7s
        loop.call_later(0.3, backend.gate.set)  # 超過固定部分 0.1s，但未達 0.7s
        result = await pool.run("A", pcm, SR)
        assert result.timed_out is False
        assert result.result.text == "1"
        assert pool.timeouts == 0

    async def test_縮放上限仍會逾時且_log_記實際上限(self, caplog) -> None:
        backend = _GateBackend(slow={1})
        pool = TranscriberPool(backend, job_timeout_s=0.1, job_timeout_per_audio_s=0.1)
        try:
            pcm = struct.pack("<h", 1) * (SR * 1)  # 上限 0.2s
            with caplog.at_level("WARNING", logger="uep.stream.audio.multichannel"):
                result = await pool.run("A", pcm, SR)
            assert result.timed_out is True
            assert any("0.20s" in r.getMessage() for r in caplog.records)
        finally:
            backend.gate.set()
            await asyncio.sleep(0.05)

    def test_loop_關閉後孤兒才結束不拋例外且額度歸還(self, recwarn) -> None:
        """孤兒執行緒在 loop 關閉後才結束：不可有未處理例外，額度仍要歸還。"""
        import gc

        backend = _GateBackend(slow={1})
        loop = asyncio.new_event_loop()
        pool = TranscriberPool(backend, job_timeout_s=0.1)
        try:
            result = loop.run_until_complete(pool.run("A", _pcm(1), SR))
            assert result.timed_out is True
            assert pool._orphans == 1
            pool.close()
        finally:
            loop.close()
        backend.gate.set()  # loop 已關閉，孤兒此時才結束
        deadline = time.perf_counter() + 2.0
        while 1 not in backend.finished and time.perf_counter() < deadline:
            time.sleep(0.01)
        while pool._orphans and time.perf_counter() < deadline:
            time.sleep(0.01)
        gc.collect()
        assert pool._orphans == 0
