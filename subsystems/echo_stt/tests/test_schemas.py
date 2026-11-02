"""
T-12: Schemas 測試

包含 Bug #2 修復驗證：duration=0.0 不被 falsy 判定誤吃。
"""

import pytest
from echo_stt.core.schemas import (
    ActivationMode, SpeakerInfo, STTInput, STTOutput, STTModuleData,
    VoiceActivityEvent, SpeakerModel,
)


class TestSTTInput:
    def test_default_mode(self):
        inp = STTInput()
        assert inp.mode == ActivationMode.CONTINUOUS

    def test_duration_zero_is_valid(self):
        """Bug #2: duration=0.0 不應被視為 None"""
        inp = STTInput(duration=0.0)
        assert inp.duration == 0.0
        assert inp.duration is not None

    def test_duration_none(self):
        inp = STTInput()
        assert inp.duration is None


class TestSTTOutput:
    def test_to_unified_format_success(self):
        output = STTOutput(text="hello", confidence=0.9)
        unified = output.to_unified_format()
        assert isinstance(unified, STTModuleData)
        assert unified.text == "hello"
        assert unified.status == "success"
        assert unified.source_module == "stt"

    def test_to_unified_format_error(self):
        output = STTOutput(text="", confidence=0.0, error="some error")
        unified = output.to_unified_format()
        assert unified.status == "error"

    def test_to_unified_format_listening_completed(self):
        """持續監聽完成時，即使 text 為空也應為 success"""
        output = STTOutput(
            text="", confidence=1.0,
            activation_reason="continuous_listening_completed",
        )
        unified = output.to_unified_format()
        assert unified.status == "success"

    def test_to_unified_format_with_speaker(self):
        speaker = SpeakerInfo(
            speaker_id="spk_001", confidence=0.85,
            is_new_speaker=False,
        )
        output = STTOutput(text="test", confidence=0.9, speaker_info=speaker)
        unified = output.to_unified_format()
        assert unified.speaker_info is not None
        assert unified.speaker_info["speaker_id"] == "spk_001"

    def test_metadata_is_mutable(self):
        """STTModuleData.metadata 必須是 mutable dict"""
        unified = STTOutput(text="x", confidence=0.5).to_unified_format()
        unified.metadata["test_key"] = "test_value"
        assert unified.metadata["test_key"] == "test_value"


class TestSpeakerInfo:
    def test_basic_creation(self):
        info = SpeakerInfo(
            speaker_id="test", confidence=0.9,
            is_new_speaker=True,
        )
        assert info.speaker_id == "test"
        assert info.voice_features is None

    def test_with_features(self):
        info = SpeakerInfo(
            speaker_id="test", confidence=0.9,
            is_new_speaker=False,
            voice_features={"method": "pyannote"},
        )
        assert info.voice_features["method"] == "pyannote"
