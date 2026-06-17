"""
辨識管線測試（TranscriptionQueue + STTWorker + STTWorkerPool）

使用 mock transcribe_fn，不需要真實 Whisper 模型。
"""

from __future__ import annotations

import time
import threading

import numpy as np
import pytest

from echo_stt.core.multi_channel_schemas import (
    AudioSegment,
    ChannelType,
    TranscriptEntry,
)
from echo_stt.pipeline.queue import TranscriptionQueue
from echo_stt.pipeline.worker import STTWorker, STTWorkerPool


# ──────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────


def make_segment(
    channel_id: str = "ch_0",
    start_ts: float = 100.0,
    duration: float = 2.0,
    sequence: int = 0,
    num_samples: int = 32000,
) -> AudioSegment:
    """建立假 AudioSegment（不含真實音訊）"""
    audio = np.random.randint(-1000, 1000, num_samples, dtype=np.int16)
    return AudioSegment.from_numpy(
        audio,
        channel_id=channel_id,
        channel_type=ChannelType.LOCAL_MIC,
        start_timestamp=start_ts,
        end_timestamp=start_ts + duration,
        sample_rate=16000,
        speaker_label="spk_test",
        speaker_name="Test",
        sequence_num=sequence,
    )


def mock_transcribe(audio_f32: np.ndarray, language=None):
    """假的辨識函式——回傳固定結果"""
    return {
        "text": f"mock text ({len(audio_f32)} samples)",
        "language": language or "zh",
        "confidence": 0.95,
    }


def slow_mock_transcribe(audio_f32: np.ndarray, language=None):
    """慢速假辨識——模擬 GPU 延遲"""
    time.sleep(0.1)
    return mock_transcribe(audio_f32, language)


# ──────────────────────────────────────────────────────────────
#  TranscriptionQueue 測試
# ──────────────────────────────────────────────────────────────


class TestTranscriptionQueue:

    def test_put_and_get(self):
        q = TranscriptionQueue(max_size=10)
        seg = make_segment(start_ts=100.0)
        assert q.put(seg) is True
        assert q.qsize() == 1

        out = q.get(timeout=1.0)
        assert out is not None
        assert out.segment_id == seg.segment_id

    def test_priority_order(self):
        """早說的先出來（start_timestamp 排序）"""
        q = TranscriptionQueue()

        q.put(make_segment(start_ts=103.0, sequence=3))
        q.put(make_segment(start_ts=101.0, sequence=1))
        q.put(make_segment(start_ts=102.0, sequence=2))

        s1 = q.get(timeout=1.0)
        s2 = q.get(timeout=1.0)
        s3 = q.get(timeout=1.0)

        assert s1.start_timestamp == 101.0
        assert s2.start_timestamp == 102.0
        assert s3.start_timestamp == 103.0

    def test_max_size_drops(self):
        q = TranscriptionQueue(max_size=2)

        assert q.put(make_segment(sequence=0)) is True
        assert q.put(make_segment(sequence=1)) is True
        assert q.put(make_segment(sequence=2)) is False  # 佇列滿

        metrics = q.get_metrics()
        assert metrics.total_dropped == 1
        assert metrics.total_enqueued == 2

    def test_get_timeout(self):
        q = TranscriptionQueue()
        result = q.get(timeout=0.1)
        assert result is None

    def test_metrics(self):
        q = TranscriptionQueue(max_size=10)
        q.put(make_segment(sequence=0))
        q.put(make_segment(sequence=1))
        q.get(timeout=1.0)

        m = q.get_metrics()
        assert m.total_enqueued == 2
        assert m.total_dequeued == 1
        assert m.queue_size == 1

    def test_clear(self):
        q = TranscriptionQueue()
        for i in range(5):
            q.put(make_segment(sequence=i))

        cleared = q.clear()
        assert cleared == 5
        assert q.is_empty()

    def test_thread_safety(self):
        """多執行緒同時 put 不應 crash"""
        q = TranscriptionQueue(max_size=100)
        errors = []

        def producer(pid):
            try:
                for i in range(20):
                    q.put(make_segment(sequence=pid * 20 + i))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=producer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert q.qsize() == 80


# ──────────────────────────────────────────────────────────────
#  STTWorker 測試
# ──────────────────────────────────────────────────────────────


class TestSTTWorker:

    def test_processes_segment(self):
        """Worker 從佇列取 segment 並呼叫 transcribe_fn"""
        q = TranscriptionQueue()
        results = []

        worker = STTWorker(
            worker_id="w0",
            transcribe_fn=mock_transcribe,
            queue=q,
            result_callback=lambda entry: results.append(entry),
        )

        q.put(make_segment())
        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert len(results) == 1
        assert isinstance(results[0], TranscriptEntry)
        assert "mock text" in results[0].text
        assert results[0].confidence == 0.95
        assert worker.processed_count == 1

    def test_empty_text_filtered(self):
        """辨識結果為空時不觸發 callback"""
        def empty_transcribe(audio, lang=None):
            return {"text": "   ", "language": "zh", "confidence": 0.0}

        q = TranscriptionQueue()
        results = []

        worker = STTWorker(
            worker_id="w0",
            transcribe_fn=empty_transcribe,
            queue=q,
            result_callback=lambda entry: results.append(entry),
        )

        q.put(make_segment())
        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert len(results) == 0

    def test_entry_has_correct_metadata(self):
        """TranscriptEntry 應包含正確的 channel/speaker/timestamp"""
        q = TranscriptionQueue()
        results = []

        worker = STTWorker(
            worker_id="w0",
            transcribe_fn=mock_transcribe,
            queue=q,
            result_callback=lambda entry: results.append(entry),
        )

        seg = make_segment(channel_id="mic_alice", start_ts=200.0)
        q.put(seg)
        worker.start()
        time.sleep(0.5)
        worker.stop()

        entry = results[0]
        assert entry.channel_id == "mic_alice"
        assert entry.start_ts == 200.0
        assert entry.speaker_label == "spk_test"
        assert entry.processing_time >= 0  # mock 太快可能 round 到 0.000


# ──────────────────────────────────────────────────────────────
#  STTWorkerPool 測試
# ──────────────────────────────────────────────────────────────


class TestSTTWorkerPool:

    def test_processes_multiple_segments(self):
        results = []
        pool = STTWorkerPool(
            transcribe_fn=mock_transcribe,
            result_callback=lambda entry: results.append(entry),
            num_workers=2,
        )
        pool.start()

        for i in range(5):
            pool.submit(make_segment(start_ts=100 + i, sequence=i))

        pool.drain(timeout=5.0)
        pool.stop()

        assert len(results) == 5

    def test_maintains_timestamp_in_entries(self):
        """結果應保留 segment 的時間戳"""
        results = []
        pool = STTWorkerPool(
            transcribe_fn=mock_transcribe,
            result_callback=lambda entry: results.append(entry),
            num_workers=1,
        )
        pool.start()

        pool.submit(make_segment(start_ts=300.0, sequence=0))
        pool.submit(make_segment(start_ts=200.0, sequence=1))

        pool.drain(timeout=5.0)
        pool.stop()

        # 佇列按 timestamp 排序，但結果 list 順序取決於 worker 競爭
        assert len(results) == 2
        timestamps = sorted(r.start_ts for r in results)
        assert timestamps == [200.0, 300.0]

    def test_get_metrics(self):
        pool = STTWorkerPool(
            transcribe_fn=mock_transcribe,
            result_callback=lambda entry: None,
            num_workers=2,
            max_queue_size=20,
        )
        pool.start()

        pool.submit(make_segment(sequence=0))
        pool.drain(timeout=3.0)

        metrics = pool.get_metrics()
        assert metrics["num_workers"] == 2
        assert metrics["running"] is True
        assert metrics["queue"]["total_enqueued"] >= 1

        pool.stop()
        assert not pool.is_running

    def test_submit_when_full(self):
        pool = STTWorkerPool(
            transcribe_fn=slow_mock_transcribe,
            result_callback=lambda entry: None,
            num_workers=1,
            max_queue_size=2,
        )
        # 不啟動 worker（佇列不會被消費）
        assert pool.submit(make_segment(sequence=0)) is True
        assert pool.submit(make_segment(sequence=1)) is True
        assert pool.submit(make_segment(sequence=2)) is False  # 佇列滿

    def test_multiple_channels_interleaved(self):
        """多通道的 segment 混合提交，應全部被處理"""
        results = []
        pool = STTWorkerPool(
            transcribe_fn=mock_transcribe,
            result_callback=lambda entry: results.append(entry),
            num_workers=2,
        )
        pool.start()

        for i in range(3):
            pool.submit(make_segment(channel_id="alice", start_ts=100 + i * 2, sequence=i * 2))
            pool.submit(make_segment(channel_id="bob", start_ts=101 + i * 2, sequence=i * 2 + 1))

        pool.drain(timeout=5.0)
        pool.stop()

        assert len(results) == 6
        alice_results = [r for r in results if r.channel_id == "alice"]
        bob_results = [r for r in results if r.channel_id == "bob"]
        assert len(alice_results) == 3
        assert len(bob_results) == 3
