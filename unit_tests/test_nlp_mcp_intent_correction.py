#!/usr/bin/env python3
"""
NLP MCP 意圖糾正機制單元測試

測試 NLP 模組使用 MCP 工具列表來糾正錯誤的意圖分類
"""

import sys
from pathlib import Path

# 添加項目根目錄到路徑
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import pytest
from unittest.mock import Mock, patch, MagicMock
from modules.nlp_module.intent_types import IntentType, IntentSegment


class TestNLPMCPIntentCorrection:
    """測試 NLP 的 MCP 意圖糾正功能"""
    
    @pytest.fixture
    def nlp_module(self):
        """創建 NLP 模組實例"""
        from modules.nlp_module.nlp_module import NLPModule
        
        # 使用最小配置，不初始化完整模組
        nlp = NLPModule({})
        return nlp
    
    @pytest.fixture
    def mock_sys_module(self):
        """模擬 SYS 模組"""
        sys_module = Mock()
        return sys_module
    
    def test_correct_chat_to_work_with_high_score(self, nlp_module, mock_sys_module):
        """測試：CHAT 意圖在高分匹配時被糾正為 WORK"""
        # 準備測試數據
        segment = IntentSegment(
            segment_text="What's the weather in Taipei?",
            intent_type=IntentType.CHAT,
            confidence=0.7,
            priority=50
        )
        
        # 模擬 SYS 查詢結果：高相關度的天氣查詢工作流
        mock_sys_module.query_function_info.return_value = [{
            'name': 'weather_query',
            'work_mode': 'direct',
            'relevance_score': 0.85
        }]
        
        # 執行糾正
        with patch('core.framework.core_framework.get_module', return_value=mock_sys_module):
            corrected_segment = nlp_module._correct_intent_with_mcp(segment)
        
        # 驗證結果
        assert corrected_segment.intent_type == IntentType.WORK, "意圖應該被糾正為 WORK"
        assert corrected_segment.metadata['work_mode'] == 'direct', "work_mode 應該是 direct"
        assert corrected_segment.metadata['mcp_corrected'] is True, "應該標記為 MCP 糾正"
        assert corrected_segment.metadata['matched_workflow'] == 'weather_query', "應該記錄匹配的工作流"
        assert corrected_segment.confidence > segment.confidence, "信心度應該增加"
        
        print(f"✅ CHAT -> WORK 糾正成功: {segment.segment_text}")
        print(f"   匹配工作流: {corrected_segment.metadata['matched_workflow']}")
        print(f"   work_mode: {corrected_segment.metadata['work_mode']}")
    
    def test_no_correction_with_low_score(self, nlp_module, mock_sys_module):
        """測試：低分匹配時不進行糾正"""
        segment = IntentSegment(
            segment_text="How are you today?",
            intent_type=IntentType.CHAT,
            confidence=0.8,
            priority=50
        )
        
        # 模擬 SYS 查詢結果：低相關度
        mock_sys_module.query_function_info.return_value = [{
            'name': 'some_workflow',
            'work_mode': 'background',
            'relevance_score': 0.15  # 低於閾值 0.3
        }]
        
        # 執行糾正
        with patch('core.framework.core_framework.get_module', return_value=mock_sys_module):
            corrected_segment = nlp_module._correct_intent_with_mcp(segment)
        
        # 驗證結果：不應該被糾正
        assert corrected_segment.intent_type == IntentType.CHAT, "低分時不應糾正意圖"
        assert corrected_segment is segment, "應該返回原始 segment"
        
        print(f"✅ 低分匹配不糾正: {segment.segment_text}")
        print(f"   保持原意圖: {corrected_segment.intent_type.name}")
    
    def test_work_mode_update_for_work_intent(self, nlp_module, mock_sys_module):
        """測試：已經是 WORK 意圖時，更新 work_mode"""
        segment = IntentSegment(
            segment_text="Check the weather",
            intent_type=IntentType.WORK,
            confidence=0.75,
            priority=50,
            metadata={'work_mode': 'background'}  # 錯誤的 work_mode
        )
        
        # 模擬 SYS 查詢結果：應該是 direct
        mock_sys_module.query_function_info.return_value = [{
            'name': 'weather_query',
            'work_mode': 'direct',
            'relevance_score': 0.92
        }]
        
        # 執行糾正
        with patch('core.framework.core_framework.get_module', return_value=mock_sys_module):
            corrected_segment = nlp_module._correct_intent_with_mcp(segment)
        
        # 驗證結果
        assert corrected_segment.intent_type == IntentType.WORK, "意圖類型應保持 WORK"
        assert corrected_segment.metadata['work_mode'] == 'direct', "work_mode 應該被更新為 direct"
        assert corrected_segment.metadata['mcp_verified'] is True, "應該標記為 MCP 驗證"
        
        print(f"✅ work_mode 更新成功: background -> direct")
        print(f"   工作流: {corrected_segment.metadata['matched_workflow']}")
    
    def test_unknown_intent_correction(self, nlp_module, mock_sys_module):
        """測試：UNKNOWN 意圖被糾正為 WORK"""
        segment = IntentSegment(
            segment_text="weather Taipei",
            intent_type=IntentType.UNKNOWN,
            confidence=0.5,
            priority=10
        )
        
        # 模擬 SYS 查詢結果
        mock_sys_module.query_function_info.return_value = [{
            'name': 'weather_query',
            'work_mode': 'direct',
            'relevance_score': 0.65
        }]
        
        # 執行糾正
        with patch('core.framework.core_framework.get_module', return_value=mock_sys_module):
            corrected_segment = nlp_module._correct_intent_with_mcp(segment)
        
        # 驗證結果
        assert corrected_segment.intent_type == IntentType.WORK, "UNKNOWN 應該被糾正為 WORK"
        assert corrected_segment.metadata['work_mode'] == 'direct'
        
        print(f"✅ UNKNOWN -> WORK 糾正成功: {segment.segment_text}")
    
    def test_no_sys_module_available(self, nlp_module):
        """測試：SYS 模組不可用時的容錯處理"""
        segment = IntentSegment(
            segment_text="What's the weather?",
            intent_type=IntentType.CHAT,
            confidence=0.7,
            priority=50
        )
        
        # 模擬 SYS 模組不可用
        with patch('core.framework.core_framework.get_module', return_value=None):
            corrected_segment = nlp_module._correct_intent_with_mcp(segment)
        
        # 驗證結果：應該返回原始 segment
        assert corrected_segment is segment, "SYS 不可用時應返回原始 segment"
        assert corrected_segment.intent_type == IntentType.CHAT
        
        print(f"✅ SYS 不可用時容錯處理正確")
    
    def test_multiple_corrections_in_batch(self, nlp_module, mock_sys_module):
        """測試：批量糾正多個 segment"""
        segments = [
            IntentSegment("What's the weather?", IntentType.CHAT, 0.7, 50),
            IntentSegment("Hello there", IntentType.CHAT, 0.9, 50),
            IntentSegment("Check time in Tokyo", IntentType.UNKNOWN, 0.5, 10)
        ]
        
        # 模擬不同的查詢結果
        def mock_query(text, top_k=1):
            if "weather" in text.lower():
                return [{'name': 'weather_query', 'work_mode': 'direct', 'relevance_score': 0.85}]
            elif "time" in text.lower():
                return [{'name': 'time_query', 'work_mode': 'direct', 'relevance_score': 0.75}]
            else:
                return [{'name': 'some_workflow', 'work_mode': 'background', 'relevance_score': 0.1}]
        
        mock_sys_module.query_function_info.side_effect = mock_query
        
        # 執行批量糾正
        corrected_segments = []
        with patch('core.framework.core_framework.get_module', return_value=mock_sys_module):
            for segment in segments:
                corrected = nlp_module._correct_intent_with_mcp(segment)
                corrected_segments.append(corrected)
        
        # 驗證結果
        assert corrected_segments[0].intent_type == IntentType.WORK, "weather 應該被糾正為 WORK"
        assert corrected_segments[1].intent_type == IntentType.CHAT, "Hello 應該保持 CHAT"
        assert corrected_segments[2].intent_type == IntentType.WORK, "time 應該被糾正為 WORK"
        
        print(f"✅ 批量糾正成功:")
        for i, (orig, corr) in enumerate(zip(segments, corrected_segments)):
            if orig.intent_type != corr.intent_type:
                print(f"   [{i+1}] {orig.segment_text}: {orig.intent_type.name} -> {corr.intent_type.name}")
            else:
                print(f"   [{i+1}] {orig.segment_text}: {orig.intent_type.name} (未變)")


if __name__ == "__main__":
    print("=" * 60)
    print("NLP MCP 意圖糾正機制測試")
    print("=" * 60)
    print()
    
    # 運行測試
    pytest.main([__file__, "-v", "-s"])
