"""
T-12: VAD 測試
"""

import numpy as np
import pytest
from echo_stt.vad.energy_vad import VoiceActivityDetection


@pytest.fixture
def vad():
    v = VoiceActivityDetection(sample_rate=16000, sensitivity=0.7, min_speech_duration=0.3)
    v.initialize()
    return v


class TestVAD:
    def test_silence_detection(self, vad):
        """純靜音應不產生 speech_end 事件"""
        silence = np.zeros(16000, dtype=np.int16)  # 1 秒靜音
        events = vad.detect_voice_activity(silence)
        speech_ends = [e for e in events if e['event_type'] == 'speech_end']
        assert len(speech_ends) == 0

    def test_has_sufficient_speech_silence(self, vad):
        """純靜音不應有足夠語音"""
        silence = np.zeros(16000, dtype=np.int16)
        assert vad.has_sufficient_speech(silence) is False

    def test_speech_detection(self, vad):
        """有能量的音訊應被偵測為語音"""
        # 產生 1 秒的正弦波（440Hz）
        t = np.linspace(0, 1, 16000, dtype=np.float32)
        sine = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        events = vad.detect_voice_activity(sine)
        # 應有 speech_start 事件
        speech_starts = [e for e in events if e['event_type'] == 'speech_start']
        assert len(speech_starts) > 0

    def test_has_sufficient_speech_with_signal(self, vad):
        """有信號的音訊應有足夠語音"""
        t = np.linspace(0, 1, 16000, dtype=np.float32)
        sine = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        assert vad.has_sufficient_speech(sine) is True

    def test_audio_quality_score(self, vad):
        """品質分數應在 0-1 範圍"""
        t = np.linspace(0, 1, 16000, dtype=np.float32)
        sine = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        score = vad.get_audio_quality_score(sine)
        assert 0.0 <= score <= 1.0

    def test_extract_speech_segments(self, vad):
        """語音片段提取應回傳 list"""
        t = np.linspace(0, 1, 16000, dtype=np.float32)
        sine = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        segments = vad.extract_speech_segments(sine)
        assert isinstance(segments, list)

    def test_update_thresholds(self, vad):
        """閾值更新"""
        vad.update_thresholds(energy_threshold=0.01, speech_duration=0.5)
        assert vad.energy_threshold == 0.01
        assert vad.speech_duration_threshold == 0.5
