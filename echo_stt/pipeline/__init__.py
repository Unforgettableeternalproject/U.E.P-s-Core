"""
echo_stt.pipeline — 辨識排程管線

提供：
- TranscriptionQueue: 帶優先權的 AudioSegment 佇列
- STTWorker: 單一辨識 worker
- STTWorkerPool: Worker 池，管理佇列→辨識→callback 流程
"""

from echo_stt.pipeline.queue import TranscriptionQueue
from echo_stt.pipeline.worker import STTWorker, STTWorkerPool

__all__ = ["TranscriptionQueue", "STTWorker", "STTWorkerPool"]
