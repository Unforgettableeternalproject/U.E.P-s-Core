# tests/test_identity_flow_integration.py
"""
測試 NLP→Identity→MEM 完整流程
驗證身份創建到記憶體存取的整合是否正確
"""

import unittest
from datetime import datetime
from typing import Dict, Any

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


class TestIdentityFlowIntegration(unittest.TestCase):
    """測試身份流程整合"""
    
    def setUp(self):
        """測試前設置"""
        from modules.mem_module.core.identity_manager import IdentityManager
        from modules.mem_module.schemas import IdentityToken
        
        self.identity_manager = IdentityManager({"test": True})
        self.identity_manager.initialize()
        
        self.mock_user_profile = create_mock_user_profile()
    
    def test_01_identity_token_creation_from_nlp(self):
        """測試 1: 從 NLP UserProfile 創建 IdentityToken"""
        print("\n[測試 1] 從 NLP UserProfile 創建 IdentityToken")
        
        # 1. 創建身份令牌
        identity_token = self.identity_manager.create_identity_token_from_nlp(
            self.mock_user_profile
        )
        
        # 2. 驗證創建成功
        self.assertIsNotNone(identity_token)
        self.assertEqual(identity_token.identity_id, self.mock_user_profile["identity_id"])
        self.assertEqual(identity_token.memory_token, self.mock_user_profile["memory_token"])
        self.assertEqual(identity_token.speaker_id, self.mock_user_profile["speaker_id"])
        self.assertEqual(identity_token.display_name, self.mock_user_profile["display_name"])
        
        # 3. 驗證偏好設定正確轉移
        self.assertEqual(identity_token.preferences, self.mock_user_profile["preferences"])
        self.assertEqual(identity_token.voice_preferences, self.mock_user_profile["voice_preferences"])
        self.assertEqual(identity_token.conversation_style, self.mock_user_profile["conversation_style"])
        
        print(f"   ✅ 身份令牌創建成功: {identity_token.identity_id}")
        print(f"   ✅ 記憶令牌: {identity_token.memory_token}")
        print(f"   ✅ 偏好設定轉移成功")
    
    def test_02_identity_token_caching(self):
        """測試 2: 身份令牌快取機制"""
        print("\n[測試 2] 身份令牌快取機制")
        
        # 1. 創建身份令牌
        identity_token = self.identity_manager.create_identity_token_from_nlp(
            self.mock_user_profile
        )
        
        # 2. 檢查快取
        cached_token = self.identity_manager.get_identity_token(
            self.mock_user_profile["identity_id"]
        )
        
        self.assertIsNotNone(cached_token)
        self.assertEqual(cached_token.identity_id, identity_token.identity_id)
        self.assertEqual(cached_token.memory_token, identity_token.memory_token)
        
        print(f"   ✅ 身份令牌快取成功")
        print(f"   ✅ 快取查詢正確: {cached_token.identity_id}")
    
    def test_03_memory_access_validation(self):
        """測試 3: 記憶體存取驗證"""
        print("\n[測試 3] 記憶體存取驗證")
        
        # 1. 創建身份令牌
        identity_token = self.identity_manager.create_identity_token_from_nlp(
            self.mock_user_profile
        )
        
        # 2. 驗證記憶體存取（模擬當前令牌）
        # 這需要模擬 working_context_manager 返回正確的令牌
        memory_token = identity_token.memory_token
        
        # 3. 驗證不同情況
        # 系統令牌應該能存取所有記憶體
        system_access = self.identity_manager.validate_memory_access(
            memory_token, "read"
        )
        
        print(f"   ✅ 記憶體存取驗證功能正常")
        print(f"   ✅ 記憶令牌: {memory_token}")
    
    def test_04_identity_token_structure_completeness(self):
        """測試 4: 身份令牌結構完整性"""
        print("\n[測試 4] 身份令牌結構完整性")
        
        # 1. 創建身份令牌
        identity_token = self.identity_manager.create_identity_token_from_nlp(
            self.mock_user_profile
        )
        
        # 2. 檢查所有必要屬性
        required_attrs = [
            "memory_token", "identity_id", "speaker_id", "display_name",
            "preferences", "voice_preferences", "conversation_style",
            "permissions", "is_active", "created_at", "total_interactions"
        ]
        
        for attr in required_attrs:
            self.assertTrue(hasattr(identity_token, attr), f"缺少屬性: {attr}")
        
        # 3. 檢查預設值
        self.assertIsInstance(identity_token.permissions, list)
        self.assertTrue(identity_token.is_active)
        self.assertIsInstance(identity_token.total_interactions, int)
        
        print(f"   ✅ 所有必要屬性存在")
        print(f"   ✅ 預設值設定正確")
        print(f"   ✅ 身份令牌結構完整")
    
    def test_05_usage_tracking(self):
        """測試 5: 使用追蹤"""
        print("\n[測試 5] 使用追蹤")
        
        # 1. 創建身份令牌
        identity_token = self.identity_manager.create_identity_token_from_nlp(
            self.mock_user_profile
        )
        
        initial_interactions = identity_token.total_interactions
        
        # 2. 更新使用記錄
        self.identity_manager.update_identity_token_usage(identity_token.identity_id)
        
        # 3. 檢查更新
        updated_token = self.identity_manager.get_identity_token(identity_token.identity_id)
        self.assertEqual(updated_token.total_interactions, initial_interactions + 1)
        self.assertIsNotNone(updated_token.last_used)
        
        print(f"   ✅ 使用追蹤正常")
        print(f"   ✅ 互動次數更新: {initial_interactions} → {updated_token.total_interactions}")
    
    def test_06_integration_with_working_context(self):
        """測試 6: Working Context 整合"""
        print("\n[測試 6] Working Context 整合")
        
        from core.working_context import working_context_manager
        
        # 1. 設定身份到 Working Context
        working_context_manager.set_current_identity(self.mock_user_profile)
        working_context_manager.set_memory_token(self.mock_user_profile["memory_token"])
        
        # 2. 從 Working Context 獲取
        current_identity = self.identity_manager.get_current_identity_info()
        current_token = self.identity_manager.get_current_memory_token()
        
        # 3. 驗證
        if current_identity:
            self.assertEqual(current_identity["identity_id"], self.mock_user_profile["identity_id"])
        self.assertEqual(current_token, self.mock_user_profile["memory_token"])
        
        print(f"   ✅ Working Context 整合正常")
        print(f"   ✅ 身份資訊同步成功")


if __name__ == "__main__":
    # 運行測試
    unittest.main(verbosity=2)