"""
STTWorker / STTWorkerPool — 辨識工作者

STTWorker: 從佇列取 AudioSegment → 呼叫 WhisperModel.transcribe() → 產生 TranscriptEntry
STTWorkerPool: 管理 N 個 worker，共用同一 WhisperModel 和佇列
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from echo_stt.core.multi_channel_schemas import (
    AudioSegment,
    TranscriptEntry,
)
from echo_stt.pipeline.queue import TranscriptionQueue
from echo_stt.vad.noise_filter import NoiseFilter

logger = logging.getLogger(__name__)


class STTWorker:
    """單一辨識 worker。

    從 TranscriptionQueue 取 segment，呼叫 WhisperModel.transcribe()，
    產生 TranscriptEntry 並觸發 result_callback。

    可搭配 NoiseFilter 做辨識前的音訊前處理。
    """

    def __init__(
        self,
        worker_id: str,
        transcribe_fn: Callable[[np.ndarray, Optional[str]], Dict[str, Any]],
        queue: TranscriptionQueue,
        result_callback: Callable[[TranscriptEntry, AudioSegment], None],
        noise_filter: Optional[NoiseFilter] = None,
        default_language: Optional[str] = None,
    ):
        """
        Args:
            worker_id: worker 識別碼
            transcribe_fn: 辨識函式，簽名 (audio_f32, language) -> {text, language, confidence, ...}
            queue: 共用的 TranscriptionQueue
            result_callback: 辨識結果的 callback，簽名 (entry, segment) -> None
            noise_filter: 可選的前處理濾波器
            default_language: 預設語言（None = 自動偵測）
        """
        self.worker_id = worker_id
        self.transcribe_fn = transcribe_fn
        self.queue = queue
        self.result_callback = result_callback
        self.noise_filter = noise_filter
        self.default_language = default_language

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._processed = 0
        self._total_processing_time = 0.0

    def start(self) -> None:
        """啟動 worker 執行緒。"""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"STTWorker-{self.worker_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """停止 worker。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None

    def _run_loop(self) -> None:
        """主迴圈：get → transcribe → callback。"""
        logger.info("[Worker:%s] 啟動", self.worker_id)

        while not self._stop_event.is_set():
            segment = self.queue.get(timeout=0.5)
            if segment is None:
                continue  # 超時，檢查 stop_event 後重試

            try:
                entry = self._process_segment(segment)
                if entry is not None:
                    self.result_callback(entry, segment)
            except Exception as e:
                logger.error(
                    "[Worker:%s] 處理 segment %s 失敗: %s",
                    self.worker_id, segment.segment_id, e,
                )

        logger.info("[Worker:%s] 停止 (processed=%d)", self.worker_id, self._processed)

    def _process_segment(self, segment: AudioSegment) -> Optional[TranscriptEntry]:
        """處理單一 segment → TranscriptEntry。"""
        t0 = time.perf_counter()

        # 轉為 float32
        audio_f32 = segment.to_float32()

        # 前處理（可選）
        if self.noise_filter is not None:
            audio_f32 = self.noise_filter.process(audio_f32)

        if len(audio_f32) == 0:
            return None

        # 辨識
        result = self.transcribe_fn(audio_f32, self.default_language)

        processing_time = time.perf_counter() - t0
        self._processed += 1
        self._total_processing_time += processing_time

        text = result.get("text", "").strip()
        if not text:
            return None

        return TranscriptEntry(
            segment_id=segment.segment_id,
            text=text,
            language=result.get("language"),
            confidence=result.get("confidence", 0.0),
            channel_id=segment.channel_id,
            channel_type=segment.channel_type,
            speaker_label=segment.speaker_label or "unknown",
            speaker_name=segment.speaker_name or "Unknown",
            start_ts=segment.start_timestamp,
            end_ts=segment.end_timestamp,
            processing_time=round(processing_time, 3),
        )

    @property
    def processed_count(self) -> int:
        return self._processed

    @property
    def avg_processing_time(self) -> float:
        if self._processed == 0:
            return 0.0
        return self._total_processing_time / self._processed


class STTWorkerPool:
    """Worker 池——管理 N 個 STTWorker，共用同一辨識函式和佇列。

    Usage:
        pool = STTWorkerPool(
            transcribe_fn=my_transcribe,
            result_callback=on_entry,
            num_workers=2,
        )
        pool.start()
        pool.submit(segment)  # 從 ChannelManager 來的 segment
        ...
        pool.stop()
    """

    def __init__(
        self,
        transcribe_fn: Callable[[np.ndarray, Optional[str]], Dict[str, Any]],
        result_callback: Callable[[TranscriptEntry, AudioSegment], None],
        num_workers: int = 2,
        max_queue_size: int = 50,
        noise_filter: Optional[NoiseFilter] = None,
        default_language: Optional[str] = None,
    ):
        self.transcribe_fn = transcribe_fn
        self.result_callback = result_callback
        self.num_workers = num_workers
        self.noise_filter = noise_filter
        self.default_language = default_language

        self.queue = TranscriptionQueue(max_size=max_queue_size)
        self._workers: List[STTWorker] = []
        self._running = False

    def start(self) -> None:
        """啟動所有 worker。"""
        if self._running:
            return

        self._workers = [
            STTWorker(
                worker_id=f"w{i}",
                transcribe_fn=self.transcribe_fn,
                queue=self.queue,
                result_callback=self.result_callback,
                noise_filter=self.noise_filter,
                default_language=self.default_language,
            )
            for i in range(self.num_workers)
        ]

        for w in self._workers:
            w.start()

        self._running = True
        logger.info(
            "[WorkerPool] 啟動 %d workers, queue_max=%d",
            self.num_workers, self.queue.max_size,
        )

    def stop(self) -> None:
        """停止所有 worker。"""
        for w in self._workers:
            w.stop()
        self._workers.clear()
        self._running = False
        logger.info("[WorkerPool] 已停止")

    def submit(self, segment: AudioSegment) -> bool:
        """提交 segment 到佇列。

        Returns:
            True 若成功放入佇列，False 若佇列滿。
        """
        return self.queue.put(segment)

    def drain(self, timeout: float = 5.0) -> None:
        """等待佇列排空（用於優雅關閉）。"""
        deadline = time.time() + timeout
        while not self.queue.is_empty() and time.time() < deadline:
            time.sleep(0.1)

    @property
    def is_running(self) -> bool:
        return self._running

    def get_metrics(self) -> Dict[str, Any]:
        """取得 pool 和 queue 的統計。"""
        queue_metrics = self.queue.get_metrics()
        worker_stats = [
            {
                "id": w.worker_id,
                "processed": w.processed_count,
                "avg_time": round(w.avg_processing_time, 3),
            }
            for w in self._workers
        ]
        return {
            "queue": queue_metrics.model_dump(),
            "workers": worker_stats,
            "num_workers": self.num_workers,
            "running": self._running,
        }
