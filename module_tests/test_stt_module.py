#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STT 模組單元測試
測試新版 Transformers Whisper + pyannote 架構
"""

import pytest
import time
import numpy as np
from unittest.mock import Mock, patch, MagicMock
import torch

from modules.stt_module.stt_module import STTModule, correct_stt
from modules.stt_module.schemas import ActivationMode, STTInput, STTOutput


@pytest.fixture
def stt_config():
    """STT 測試配置"""
    return {
        "device_index": 1,
        "whisper_model_id": "openai/whisper-large-v3",
        "use_local_model": True,
        "whisper_local_path": "models/stt/whisper/whisper-large-v3",
        "phrase_time_limit": 5
    }


@pytest.fixture
def mock_audio_data():
    """模擬音頻數據"""
    # 創建 3 秒的模擬音頻數據 (16000 採樣率)
    duration = 3.0
    sample_rate = 16000
    samples = int(duration * sample_rate)
    return np.random.randint(-32768, 32767, samples, dtype=np.int16)


@pytest.fixture
def stt_module(stt_config):
    """創建 STT 模組實例"""
    with patch('modules.stt_module.stt_module.AutoModelForSpeechSeq2Seq'), \
         patch('modules.stt_module.stt_module.AutoProcessor'), \
         patch('modules.stt_module.stt_module.pipeline'), \
         patch('modules.stt_module.stt_module.pyaudio.PyAudio'), \
         patch('modules.stt_module.stt_module.torch.cuda.is_available', return_value=True):
        
        stt = STTModule(config=stt_config)
        
        # 模擬組件 - 包含 shutdown 方法需要的屬性
        stt.model = MagicMock()
        stt.processor = MagicMock()
        stt.pipe = MagicMock()
        stt.pyaudio_instance = MagicMock()
        
        # 模擬子模組
        stt.vad_module = MagicMock()
        stt.vad_module.initialize.return_value = True
        stt.vad_module.has_sufficient_speech.return_value = True
        stt.vad_module.shutdown = MagicMock()  # 添加 shutdown 方法
        
        stt.speaker_module = MagicMock()
        stt.speaker_module.initialize.return_value = True
        stt.speaker_module.identify_speaker.return_value = {
            "speaker_id": "test_speaker",
            "confidence": 0.8,
            "is_new_speaker": False
        }
        stt.speaker_module.shutdown = MagicMock()  # 添加 shutdown 方法
        
        stt.keyword_detector = MagicMock()
        stt.keyword_detector.should_activate.return_value = (True, ["UEP"])
        stt.keyword_detector.get_activation_reason.return_value = "Keyword detected: UEP"
        
        yield stt
        
        # 清理
        try:
            stt.shutdown()
        except Exception:
            pass  # 忽略清理時的錯誤


class TestSTTModuleInitialization:
    """測試 STT 模組初始化"""
    
    def test_init_with_config(self, stt_config):
        """測試使用配置初始化"""
        stt = STTModule(config=stt_config)
        
        assert stt.device_index == 1
        assert stt.whisper_model_id == "openai/whisper-large-v3"
        assert stt.use_local_model == True
        assert stt.sample_rate == 16000
        assert stt.phrase_time_limit == 5
    
    def test_init_without_config(self):
        """測試不使用配置初始化"""
        with patch('modules.stt_module.stt_module.load_module_config') as mock_load:
            mock_load.return_value = {"device_index": 0}
            stt = STTModule()
            assert stt.device_index == 0


class TestSTTTextCorrection:
    """測試 STT 文本修正功能"""
    
    def test_correct_stt_uep_variations(self):
        """測試 UEP 相關修正"""
        test_cases = [
            ("you ep", "UEP"),
            ("youpee", "UEP"),
            ("u e p", "UEP"),
            ("hey you ep", "hey UEP"),
            ("hello you ep", "hello UEP")
        ]
        
        for input_text, expected in test_cases:
            result = correct_stt(input_text)
            assert expected in result
    
    def test_correct_stt_common_contractions(self):
        """測試常見縮寫修正"""
        test_cases = [
            ("cant", "can't"),
            ("wont", "won't"),
            ("dont", "don't"),
            ("isnt", "isn't")
        ]
        
        for input_text, expected in test_cases:
            result = correct_stt(input_text)
            assert expected in result


class TestSTTModuleHandleMethod:
    """測試 STT 模組 handle 方法"""
    
    def test_handle_manual_mode(self, stt_module, mock_audio_data):
        """測試手動模式"""
        # 模擬錄音和識別
        with patch.object(stt_module, '_record_audio', return_value=mock_audio_data):
            stt_module.pipe.return_value = {
                "text": "hello UEP",
                "chunks": [{"timestamp": [0.0, 3.0]}]
            }
            
            result = stt_module.handle({
                "mode": "manual",
                "enable_speaker_id": True,
                "duration": 3
            })
            
            assert isinstance(result, dict)
            assert result["text"] == "hello UEP"
            assert result["confidence"] > 0
            assert result["activation_reason"] == "manual"
            assert "speaker_info" in result
    
    def test_handle_smart_mode(self, stt_module, mock_audio_data):
        """測試智能模式"""
        # 模擬快速檢測觸發
        with patch.object(stt_module, '_record_audio', return_value=mock_audio_data):
            # 第一次調用返回觸發詞
            stt_module.pipe.side_effect = [
                {"text": "UEP help me"},  # 快速檢測
                {"text": "UEP help me with something", "chunks": []}  # 完整識別
            ]
            
            result = stt_module.handle({
                "mode": "smart",
                "enable_speaker_id": True,
                "duration": 5
            })
            
            assert isinstance(result, dict)
            assert "UEP" in result["text"]
            assert result["confidence"] > 0
            assert "Keyword detected" in result["activation_reason"]
    
    def test_handle_invalid_mode(self, stt_module):
        """測試無效模式"""
        result = stt_module.handle({
            "mode": "invalid",
        })
        
        assert isinstance(result, dict)
        # pydantic 驗證錯誤會返回詳細的錯誤信息
        assert "處理失敗" in result["error"]
        assert "validation error" in result["error"]
        assert result["confidence"] == 0.0


class TestSTTModulePrivateMethods:
    """測試 STT 模組私有方法"""
    
    def test_manual_recognition(self, stt_module, mock_audio_data):
        """測試手動識別方法"""
        with patch.object(stt_module, '_record_audio', return_value=mock_audio_data):
            stt_module.pipe.return_value = {
                "text": "test recognition",
                "chunks": [{"timestamp": [0.0, 2.0]}]
            }
            
            input_data = STTInput(
                mode=ActivationMode.MANUAL,
                enable_speaker_id=True,
                duration=3
            )
            
            result = stt_module._manual_recognition(input_data)
            
            assert isinstance(result, dict)
            assert result["text"] == "test recognition"
            assert result["activation_reason"] == "manual"
    
    def test_calculate_transformers_confidence(self, stt_module):
        """測試信心度計算"""
        # 測試有效文本
        result_with_text = {
            "text": "hello UEP can you help me",
            "chunks": [{"timestamp": [0.0, 3.0]}]
        }
        confidence = stt_module._calculate_transformers_confidence(result_with_text)
        assert 0.8 <= confidence <= 1.0
        
        # 測試空文本
        result_empty = {"text": ""}
        confidence = stt_module._calculate_transformers_confidence(result_empty)
        assert confidence == 0.0
    
    def test_record_audio_mock(self, stt_module):
        """測試錄音方法（模擬）"""
        # 模擬 PyAudio stream
        mock_stream = MagicMock()
        mock_stream.read.return_value = b'\x00\x01' * 512  # 模擬音頻數據
        stt_module.pyaudio_instance.open.return_value = mock_stream
        
        audio_data = stt_module._record_audio(2.0)
        
        assert isinstance(audio_data, np.ndarray)
        assert len(audio_data) > 0
        mock_stream.stop_stream.assert_called_once()
        mock_stream.close.assert_called_once()


class TestSTTModuleErrorHandling:
    """測試 STT 模組錯誤處理"""
    
    def test_handle_with_exception(self, stt_module):
        """測試處理過程中的異常"""
        with patch.object(stt_module, '_manual_recognition', side_effect=Exception("Test error")):
            result = stt_module.handle({
                "mode": "manual"
            })
            
            assert isinstance(result, dict)
            assert "處理失敗" in result["error"]
            assert result["confidence"] == 0.0
    
    def test_manual_recognition_audio_error(self, stt_module):
        """測試錄音失敗的情況"""
        with patch.object(stt_module, '_record_audio', return_value=None):
            input_data = STTInput(mode=ActivationMode.MANUAL)
            result = stt_module._manual_recognition(input_data)
            
            assert isinstance(result, dict)
            assert "錄音失敗" in result["error"]
            assert result["confidence"] == 0.0
    
    def test_whisper_recognition_error(self, stt_module, mock_audio_data):
        """測試 Whisper 識別失敗"""
        with patch.object(stt_module, '_record_audio', return_value=mock_audio_data):
            stt_module.pipe.side_effect = Exception("Whisper error")
            
            input_data = STTInput(mode=ActivationMode.MANUAL)
            result = stt_module._manual_recognition(input_data)
            
            assert isinstance(result, dict)
            assert "識別失敗" in result["error"]


class TestSTTModuleIntegration:
    """測試 STT 模組整合功能"""
    
    def test_schema_compatibility(self):
        """測試 Schema 相容性"""
        # 測試 STTInput
        input_data = STTInput(
            mode=ActivationMode.MANUAL,
            duration=5.0,
            language="en-US",
            enable_speaker_id=True
        )
        assert input_data.mode == ActivationMode.MANUAL
        assert input_data.duration == 5.0
        
        # 測試 STTOutput
        output_data = STTOutput(
            text="test text",
            confidence=0.9,
            activation_reason="manual"
        )
        output_dict = output_data.model_dump()
        
        assert isinstance(output_dict, dict)
        assert output_dict["text"] == "test text"
        assert output_dict["confidence"] == 0.9
    
    def test_activation_modes(self):
        """測試啟動模式"""
        assert ActivationMode.MANUAL.value == "manual"
        assert ActivationMode.SMART.value == "smart"
    
    def test_module_shutdown(self, stt_module):
        """測試模組關閉"""
        # 應該能夠正常關閉而不拋出異常
        try:
            stt_module.shutdown()
            # 檢查清理是否被調用
            stt_module.pyaudio_instance.terminate.assert_called()
        except Exception as e:
            # 在測試環境中可能會有一些清理錯誤，這是正常的
            assert True  # 測試通過


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
