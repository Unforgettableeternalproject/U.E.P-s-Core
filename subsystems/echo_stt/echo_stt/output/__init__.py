"""
echo_stt.output — Transcript 輸出與聚合

提供：
- TranscriptAggregator: 彙整多通道辨識結果
- TranscriptFormatter: 格式化輸出工具
- DiarizationBridge: Speaker ID 橋接（local_mic ↔ SpeakerIdentification）
"""

from echo_stt.output.aggregator import TranscriptAggregator
from echo_stt.output.formatter import TranscriptFormatter
from echo_stt.output.diarization_bridge import DiarizationBridge

__all__ = ["TranscriptAggregator", "TranscriptFormatter", "DiarizationBridge"]
