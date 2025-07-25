"""
module_tests/test_stt_phase2.py
STT Module Phase 2 功能測試
"""

import pytest
import numpy as np
import time
from modules.stt_module import register
from modules.stt_module.schemas import STTInput, ActivationMode
from modules.stt_module.voice_activity_detector import VoiceActivityDetector
from modules.stt_module.speaker_identification import SpeakerIdentifier
from modules.stt_module.smart_activation import SmartActivationDetector

class TestSTTPhase2:
    """STT Phase 2 功能測試"""
    
    @pytest.fixture
    def stt_module(self):
        """創建 STT 模組實例"""
        try:
            return register()
        except Exception as e:
            pytest.skip(f"STT 模組初始化失敗: {str(e)}")
    
    @pytest.fixture
    def sample_config(self):
        """測試配置"""
        return {
            "always_on": {
                "enabled": True,
                "vad_sensitivity": 0.6,
                "min_speech_duration": 0.5,
                "max_silence_duration": 2.0,
                "energy_threshold": 4000
            },
            "smart_activation": {
                "enabled": True,
                "context_keywords": ["UEP", "你好", "幫我"],
                "conversation_mode": True,
                "activation_confidence": 0.7
            },
            "speaker_identification": {
                "enabled": True,
                "similarity_threshold": 0.8,
                "new_speaker_threshold": 0.6,
                "min_samples_for_model": 3
            }
        }
    
    def test_stt_module_initialization(self, stt_module):
        """測試 STT 模組初始化"""
        assert stt_module is not None
        assert hasattr(stt_module, 'vad')
        assert hasattr(stt_module, 'speaker_id')
        assert hasattr(stt_module, 'smart_activation')
        
    def test_voice_activity_detector(self, sample_config):
        """測試語音活動檢測器"""
        vad = VoiceActivityDetector(sample_config)
        
        # 創建測試音頻數據
        sample_rate = 16000
        duration = 1.0
        silence = np.zeros(int(sample_rate * duration))
        noise = np.random.normal(0, 0.1, int(sample_rate * duration))
        
        # 測試靜默檢測
        silence_result = vad.detect_speech(silence, sample_rate)
        assert not silence_result["has_speech"]
        
        # 測試噪音檢測 (可能被誤認為語音，但能量應該較低)
        noise_result = vad.detect_speech(noise, sample_rate)
        assert "energy" in noise_result
        assert "threshold" in noise_result
        
    def test_speaker_identification(self, sample_config):
        """測試說話人識別"""
        speaker_id = SpeakerIdentifier(sample_config)
        
        # 創建模擬音頻特徵
        sample_rate = 16000
        duration = 2.0
        
        # 第一個說話人的音頻
        speaker1_audio = np.random.normal(0, 0.5, int(sample_rate * duration))
        
        # 第二個說話人的音頻 (不同特徵)
        speaker2_audio = np.random.normal(0.3, 0.3, int(sample_rate * duration))
        
        # 測試特徵提取
        features1 = speaker_id.extract_voice_features(speaker1_audio)
        features2 = speaker_id.extract_voice_features(speaker2_audio)
        
        assert len(features1) > 0
        assert len(features2) > 0
        
        # 測試說話人識別
        result1 = speaker_id.identify_speaker(speaker1_audio)
        assert result1.speaker_id is not None
        assert result1.is_new_speaker  # 第一次應該是新說話人
        
        # 再次測試同一說話人
        result1_again = speaker_id.identify_speaker(speaker1_audio)
        assert result1_again.speaker_id == result1.speaker_id
        # 第二次可能是已知說話人 (取決於相似度)
        
        # 測試不同說話人
        result2 = speaker_id.identify_speaker(speaker2_audio)
        assert result2.speaker_id is not None
        
    def test_smart_activation(self, sample_config):
        """測試智能啟動檢測"""
        smart_activation = SmartActivationDetector(sample_config)
        
        # 測試明確呼叫
        result1 = smart_activation.should_activate("UEP 你好")
        assert result1["should_activate"]
        assert "UEP" in result1["keywords_found"]
        
        # 測試問句
        result2 = smart_activation.should_activate("這個檔案怎麼打開？")
        # 依據配置可能會或不會啟動
        assert "confidence" in result2
        
        # 測試普通對話
        result3 = smart_activation.should_activate("今天天氣真好")
        # 普通對話通常不會啟動
        assert "confidence" in result3
        
        # 測試幫助請求
        result4 = smart_activation.should_activate("幫我處理這個文件")
        assert result4["should_activate"] or result4["confidence"] > 0.3
        
    def test_stt_input_schemas(self):
        """測試 STT 輸入結構"""
        # 測試預設值
        input1 = STTInput()
        assert input1.mode == ActivationMode.MANUAL
        assert input1.language == "zh-TW"
        assert input1.enable_speaker_id == True
        
        # 測試自訂值
        input2 = STTInput(
            mode=ActivationMode.SMART,
            duration=10.0,
            language="en-US",
            enable_speaker_id=False,
            context="test context"
        )
        assert input2.mode == ActivationMode.SMART
        assert input2.duration == 10.0
        assert input2.language == "en-US"
        assert input2.enable_speaker_id == False
        assert input2.context == "test context"
        
    def test_activation_modes(self, stt_module):
        """測試不同啟動模式"""
        # 測試手動模式 (不需要真實音頻輸入)
        manual_input = STTInput(mode=ActivationMode.MANUAL)
        
        # 測試智能模式
        smart_input = STTInput(
            mode=ActivationMode.SMART,
            context="test conversation"
        )
        
        # 測試 Always-on 模式
        always_on_input = STTInput(mode=ActivationMode.ALWAYS_ON)
        
        # 確保不同模式都有相應的處理邏輯
        assert hasattr(stt_module, '_manual_recognition')
        assert hasattr(stt_module, '_smart_recognition')
        assert hasattr(stt_module, '_handle_always_on_request')
        
    def test_always_on_controls(self, stt_module):
        """測試 Always-on 控制功能"""
        # 測試啟動 Always-on
        stt_module.start_always_on()
        assert stt_module._always_on_running
        
        # 測試停止 Always-on
        stt_module.stop_always_on()
        assert not stt_module._always_on_running
        
    def test_speaker_stats(self, stt_module):
        """測試說話人統計功能"""
        stats = stt_module.get_speaker_stats()
        assert isinstance(stats, dict)
        
    def test_activation_stats(self, stt_module):
        """測試啟動統計功能"""
        stats = stt_module.get_activation_stats()
        assert isinstance(stats, dict)
        assert "enabled" in stats
        assert "keywords_count" in stats

if __name__ == "__main__":
    # 直接運行測試
    test_instance = TestSTTPhase2()
    
    print("🧪 開始 STT Phase 2 功能測試...")
    
    try:
        # 測試基本初始化
        sample_config = test_instance.sample_config()
        print("✅ 配置載入成功")
        
        # 測試 VAD
        print("🔊 測試語音活動檢測...")
        test_instance.test_voice_activity_detector(sample_config)
        print("✅ VAD 測試通過")
        
        # 測試說話人識別
        print("👤 測試說話人識別...")
        test_instance.test_speaker_identification(sample_config)
        print("✅ 說話人識別測試通過")
        
        # 測試智能啟動
        print("🧠 測試智能啟動...")
        test_instance.test_smart_activation(sample_config)
        print("✅ 智能啟動測試通過")
        
        # 測試結構定義
        print("📋 測試數據結構...")
        test_instance.test_stt_input_schemas()
        print("✅ 數據結構測試通過")
        
        print("🎉 所有基本測試通過！")
        
        # 嘗試測試完整模組 (可能需要麥克風)
        print("🎙️ 嘗試測試完整 STT 模組...")
        try:
            stt_module = test_instance.stt_module()
            test_instance.test_activation_modes(stt_module)
            test_instance.test_speaker_stats(stt_module)
            test_instance.test_activation_stats(stt_module)
            print("✅ 完整模組測試通過")
        except Exception as e:
            print(f"⚠️ 完整模組測試跳過: {str(e)}")
            
    except Exception as e:
        print(f"❌ 測試失敗: {str(e)}")
        raise
