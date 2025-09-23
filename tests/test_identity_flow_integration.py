# tests/test_identity_flow_integration.py
"""
測試 NLP→Identity→MEM 完整流程
驗證Working Context到記憶體存取的整合是否正確
"""

import unittest
from datetime import datetime
from typing import Dict, Any
from unittest.mock import patch, MagicMock

# 模擬 NLP UserProfile 數據
def create_mock_user_profile() -> Dict[str, Any]:
    """創建模擬的 NLP UserProfile 數據"""
    return {
        "identity_id": "user_1737845123_spk_test",
        "speaker_id": "spk_test_001",
        "display_name": "Test User",
        "memory_token": "mem_user_1737845123_spk_test_abcd1234",
        "preferences": {
            "language": "zh-tw",
            "interaction_style": "casual"
        },
        "voice_preferences": {
            "default_mood": "neutral",
            "speed": 1.0,
            "pitch": 1.0
        },
        "conversation_style": {
            "formality": "neutral",
            "verbosity": "moderate",
            "personality": "balanced"
        },
        "total_interactions": 5,
        "last_interaction": datetime.now(),
        "created_at": datetime.now()
    }

# 模擬 Identity 數據
def create_mock_identity():
    """創建模擬的 Identity 對象"""
    mock_identity = MagicMock()
    mock_identity.identity_id = "user_1737845123_spk_test"
    mock_identity.memory_token = "mem_user_1737845123_spk_test_abcd1234"
    mock_identity.speaker_id = "spk_test_001"
    mock_identity.display_name = "Test User"
    return mock_identity


class TestIdentityFlowIntegration(unittest.TestCase):
    """測試身份流程整合"""
    
    def setUp(self):
        """測試前設置"""
        from modules.mem_module.core.identity_manager import IdentityManager
        
        self.identity_manager = IdentityManager({"test": True})
        self.identity_manager.initialize()
        
        self.mock_user_profile = create_mock_user_profile()
        self.mock_identity = create_mock_identity()
    
    def test_01_memory_token_extraction_from_working_context(self):
        """測試 1: 從 Working Context 提取記憶令牌"""
        print("\n[測試 1] 從 Working Context 提取記憶令牌")
        
        with patch('core.working_context.working_context_manager.get_current_identity') as mock_get_identity:
            # 模擬 Working Context 返回 Identity
            mock_get_identity.return_value = self.mock_identity
            
            # 提取記憶令牌
            memory_token = self.identity_manager.get_current_memory_token()
            
            # 驗證提取成功
            self.assertEqual(memory_token, self.mock_identity.memory_token)
            
            print(f"   ✅ 記憶令牌提取成功: {memory_token}")
            print(f"   ✅ Working Context 整合正確")
    
    def test_02_memory_token_fallback_handling(self):
        """測試 2: 記憶令牌回退處理"""
        print("\n[測試 2] 記憶令牌回退處理")
        
        with patch('core.working_context.working_context_manager.get_current_identity') as mock_get_identity:
            # 模擬 Working Context 返回 None（無身份）
            mock_get_identity.return_value = None
            
            # 提取記憶令牌
            memory_token = self.identity_manager.get_current_memory_token()
            
            # 驗證回退到匿名令牌
            self.assertEqual(memory_token, "anonymous")
            
            print(f"   ✅ 回退處理正確: {memory_token}")
            print(f"   ✅ 匿名存取機制運作正常")
    
    def test_03_memory_access_validation(self):
        """測試 3: 記憶體存取驗證"""
        print("\n[測試 3] 記憶體存取驗證")
        
        with patch('core.working_context.working_context_manager.get_current_identity') as mock_get_identity:
            # 模擬 Working Context 返回 Identity
            mock_get_identity.return_value = self.mock_identity
            
            # 驗證記憶體存取（基於當前身份的記憶令牌）
            memory_token = self.identity_manager.get_current_memory_token()
            
            # 驗證記憶體存取權限
            has_access = self.identity_manager.validate_memory_access(memory_token, "read")
            self.assertTrue(has_access)
            
            print(f"   ✅ 記憶體存取驗證功能正常")
            print(f"   ✅ 記憶令牌: {memory_token}")
            print(f"   ✅ 存取權限驗證成功")
    
    def test_04_system_and_anonymous_tokens(self):
        """測試 4: 系統和匿名令牌"""
        print("\n[測試 4] 系統和匿名令牌")
        
        # 測試系統令牌
        system_token = self.identity_manager.get_system_token()
        self.assertEqual(system_token, "system")
        
        # 測試匿名令牌
        anonymous_token = self.identity_manager.get_anonymous_token()
        self.assertEqual(anonymous_token, "anonymous")
        
        # 驗證系統令牌的存取權限
        system_access = self.identity_manager.validate_memory_access(system_token, "read")
        self.assertTrue(system_access)
        
        print(f"   ✅ 系統令牌: {system_token}")
        print(f"   ✅ 匿名令牌: {anonymous_token}")
        print(f"   ✅ 系統令牌存取權限正確")
    
    def test_05_memory_access_stats_tracking(self):
        """測試 5: 記憶體存取統計追蹤"""
        print("\n[測試 5] 記憶體存取統計追蹤")
        
        with patch('core.working_context.working_context_manager.get_current_identity') as mock_get_identity:
            mock_get_identity.return_value = self.mock_identity
            
            # 獲取初始統計
            initial_stats = self.identity_manager.get_stats()
            
            # 執行幾次令牌提取
            for _ in range(3):
                self.identity_manager.get_current_memory_token()
            
            # 驗證統計更新
            updated_stats = self.identity_manager.get_stats()
            self.assertGreater(updated_stats["token_extractions"], initial_stats["token_extractions"])
            
            print(f"   ✅ 統計追蹤正常")
            print(f"   ✅ 令牌提取次數: {updated_stats['token_extractions']}")
    
    def test_06_integration_with_working_context(self):
        """測試 6: Working Context 整合"""
        print("\n[測試 6] Working Context 整合")
        
        with patch('core.working_context.working_context_manager.get_current_identity') as mock_get_identity:
            with patch('core.working_context.working_context_manager.get_memory_token') as mock_get_token:
                # 設定 Working Context 模擬
                mock_get_identity.return_value = self.mock_identity
                mock_get_token.return_value = self.mock_identity.memory_token
                
                # 從 IdentityManager 獲取記憶令牌
                current_token = self.identity_manager.get_current_memory_token()
                
                # 驗證整合
                self.assertEqual(current_token, self.mock_identity.memory_token)
                
                print(f"   ✅ Working Context 整合正常")
                print(f"   ✅ 記憶令牌獲取成功: {current_token}")


if __name__ == "__main__":
    # 運行測試
    unittest.main(verbosity=2)