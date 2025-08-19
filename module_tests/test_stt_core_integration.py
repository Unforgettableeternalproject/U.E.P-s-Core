# module_tests/test_stt_core_integration.py
"""
測試 STT 模組與核心框架的集成
"""

import sys
import os
import pytest
from unittest.mock import MagicMock, patch
import numpy as np

# 添加根目錄到路徑，以便導入模組
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.working_context import WorkingContextManager, ContextType
from core.schema_adapter import STTSchemaAdapter
from core.schemas import STTModuleData
from modules.stt_module.stt_module import STTModule
from modules.stt_module.schemas import STTInput, STTOutput, ActivationMode, SpeakerInfo

# 使用 pytest fixture 來準備測試環境
@pytest.fixture
def mock_stt_environment():
    """建立模擬的 STT 測試環境"""
    # 模擬配置
    mock_config = {
        "device_index": 0,
        "whisper_model_id": "test_model",
        "use_local_model": False
    }
    
    # 創建工作上下文管理器
    working_context_manager = MagicMock(spec=WorkingContextManager)
    working_context_manager.create_context.return_value = "test-context-123"
    
    # 創建 STT 模組
    with patch('modules.stt_module.stt_module.VoiceActivityDetection'), \
         patch('modules.stt_module.stt_module.SpeakerIdentification'), \
         patch('modules.stt_module.stt_module.SmartKeywordDetector'), \
         patch('modules.stt_module.stt_module.pyaudio'), \
         patch('modules.stt_module.stt_module.AutoModelForSpeechSeq2Seq'), \
         patch('modules.stt_module.stt_module.AutoProcessor'), \
         patch('modules.stt_module.stt_module.pipeline'):
        stt_module = STTModule(config=mock_config, working_context_manager=working_context_manager)
        yield stt_module, working_context_manager
def test_schema_adapter_integration():
    """測試 schema 適配器集成"""
    # 創建適配器
    adapter = STTSchemaAdapter()
    
    # 測試輸入適配
    unified_input = {
        "mode": "manual",
        "language": "en-US",
        "context": "test context"
    }
    
    adapted_input = adapter.adapt_input(unified_input)
    assert adapted_input["language"] == "en-US"
    
    # 測試輸出適配
    stt_output = {
        "text": "test result",
        "confidence": 0.95,
        "speaker_info": {"speaker_id": "user1", "confidence": 0.9},
        "activation_reason": "keyword trigger"
    }
    
    adapted_output = adapter.adapt_output(stt_output)
    assert adapted_output["status"] == "success"
    assert adapted_output["data"]["text"] == "test result"

def test_stt_module_data_conversion():
    """測試 STT 內部格式與統一格式的轉換"""
    # 創建 SpeakerInfo
    speaker_info = SpeakerInfo(
        speaker_id="user1",
        confidence=0.9,
        is_new_speaker=False
    )
    
    # 創建 STTOutput
    stt_output = STTOutput(
        text="test content",
        confidence=0.85,
        speaker_info=speaker_info,
        activation_reason="keyword trigger"
    )
    
    # 轉換為統一格式
    unified_data = stt_output.to_unified_format()
    
    assert unified_data.text == "test content"
    assert unified_data.source_module == "stt"
    assert unified_data.confidence == 0.85
    assert unified_data.activation_reason == "keyword trigger"
    
def test_working_context_integration(mock_stt_environment):
    """測試工作上下文集成"""
    stt_module, working_context_manager = mock_stt_environment
    
    # 模擬 handle 方法調用
    with patch.object(stt_module, '_smart_recognition_v2') as mock_smart:
        # 設置模擬返回值
        mock_smart.return_value = {
            "text": "test result",
            "confidence": 0.9,
            "activation_reason": "test"
        }
        
        # 呼叫 handle
        input_data = {
            "mode": "smart",
            "language": "en-US",
            "duration": 5.0
        }
        
        stt_module.handle(input_data)
        
        # 驗證是否創建了上下文
        working_context_manager.create_context.assert_called_once_with(
            ContextType.SPEAKER_ACCUMULATION,
            threshold=5,
            timeout=300.0
        )

# pytest 不需要 main 函數
