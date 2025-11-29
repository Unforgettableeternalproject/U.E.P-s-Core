"""
測試身分設定整合
測試範圍：
1. IdentityManager 讀取 user_settings
2. UI 創建/切換身分
3. allow_identity_creation 限制
4. 熱重載回調
5. Working Context 同步
"""

import sys
import os
from pathlib import Path

# 添加專案根目錄到 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import pytest
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

from modules.nlp_module.identity_manager import IdentityManager
from configs.user_settings_manager import user_settings_manager, set_user_setting, get_user_setting


class TestIdentityIntegration:
    """身分設定整合測試"""
    
    def setup_method(self):
        """測試前設置"""
        # 創建臨時存儲路徑
        self.test_storage = Path("memory/test_identities")
        self.test_storage.mkdir(parents=True, exist_ok=True)
        
        # 設置測試用的 user_settings
        set_user_setting("general.identity.user_name", "TestUser")
        set_user_setting("general.identity.uep_name", "TestUEP")
        set_user_setting("general.identity.allow_identity_creation", True)
        set_user_setting("general.identity.current_identity_id", None)
    
    def teardown_method(self):
        """測試後清理"""
        # 清理測試檔案
        if self.test_storage.exists():
            for file in self.test_storage.iterdir():
                file.unlink()
            self.test_storage.rmdir()
    
    def test_load_identity_settings(self):
        """測試載入身分設定"""
        identity_manager = IdentityManager(storage_path=str(self.test_storage))
        
        assert hasattr(identity_manager, 'default_user_name')
        assert identity_manager.default_user_name == "TestUser"
        assert identity_manager.default_uep_name == "TestUEP"
        assert identity_manager.allow_identity_creation == True
        assert identity_manager.current_identity_id is None
        
        print("✅ 測試 1 通過：IdentityManager 成功載入 user_settings")
    
    def test_create_identity_with_default_name(self):
        """測試使用預設名稱創建身分"""
        identity_manager = IdentityManager(storage_path=str(self.test_storage))
        
        # 創建身分時不指定名稱，應使用 user_settings 的 default_user_name
        profile = identity_manager.create_identity(speaker_id="test_speaker_001")
        
        assert profile is not None
        assert profile.display_name == "TestUser"
        assert profile.speaker_id == "test_speaker_001"
        
        print(f"✅ 測試 2 通過：創建身分使用預設名稱 '{profile.display_name}'")
    
    def test_allow_identity_creation_restriction(self):
        """測試 allow_identity_creation 限制"""
        # 禁止創建新身分
        set_user_setting("general.identity.allow_identity_creation", False)
        
        identity_manager = IdentityManager(storage_path=str(self.test_storage))
        
        # 嘗試創建新身分應失敗
        with pytest.raises(PermissionError, match="不允許創建新身分"):
            identity_manager.create_identity(speaker_id="test_speaker_002")
        
        # 但使用 force_new=True 應成功
        profile = identity_manager.create_identity(
            speaker_id="test_speaker_003", 
            force_new=True
        )
        assert profile is not None
        
        print("✅ 測試 3 通過：allow_identity_creation 限制生效")
    
    def test_reload_callback(self):
        """測試熱重載回調"""
        identity_manager = IdentityManager(storage_path=str(self.test_storage))
        
        # 初始狀態
        assert identity_manager.default_user_name == "TestUser"
        
        # 模擬設定變更
        set_user_setting("general.identity.user_name", "ChangedUser")
        
        # 手動觸發重載回調
        identity_manager._reload_from_user_settings(["general.identity.user_name"])
        
        # 檢查設定已更新
        assert identity_manager.default_user_name == "ChangedUser"
        
        print("✅ 測試 4 通過：熱重載回調成功更新設定")
    
    def test_current_identity_sync(self):
        """測試 current_identity_id 同步到 Working Context"""
        from core.working_context import working_context_manager
        
        identity_manager = IdentityManager(storage_path=str(self.test_storage))
        
        # 創建身分
        profile = identity_manager.create_identity(
            speaker_id="test_speaker_004",
            display_name="SyncTestUser"
        )
        
        # 設置為當前身分
        set_user_setting("general.identity.current_identity_id", profile.identity_id)
        
        # 觸發重載
        identity_manager._reload_from_user_settings(["general.identity.current_identity_id"])
        
        # 檢查 Working Context
        current_id = working_context_manager.global_context_data.get('current_identity_id')
        assert current_id == profile.identity_id
        
        declared = working_context_manager.global_context_data.get('declared_identity')
        assert declared == True
        
        print(f"✅ 測試 5 通過：current_identity_id 已同步到 Working Context: {current_id}")
    
    def test_identity_list_persistence(self):
        """測試身分清單持久化"""
        identity_manager = IdentityManager(storage_path=str(self.test_storage))
        
        # 創建多個身分
        profile1 = identity_manager.create_identity("speaker_001", "User1")
        profile2 = identity_manager.create_identity("speaker_002", "User2")
        profile3 = identity_manager.create_identity("speaker_003", "User3")
        
        # 檢查內存中的身分數量
        assert len(identity_manager.identities) == 3
        
        # 創建新的 IdentityManager 實例，應載入已保存的身分
        identity_manager2 = IdentityManager(storage_path=str(self.test_storage))
        
        assert len(identity_manager2.identities) == 3
        assert profile1.identity_id in identity_manager2.identities
        assert profile2.identity_id in identity_manager2.identities
        assert profile3.identity_id in identity_manager2.identities
        
        print("✅ 測試 6 通過：身分清單持久化成功")
    
    def test_speaker_sample_integration(self):
        """測試 Speaker 樣本累積"""
        identity_manager = IdentityManager(storage_path=str(self.test_storage))
        
        # 創建身分
        profile = identity_manager.create_identity("speaker_005", "SampleTestUser")
        
        # 添加 Speaker 樣本
        import numpy as np
        embedding = np.random.rand(128).tolist()
        
        success = identity_manager.add_speaker_sample(
            identity_id=profile.identity_id,
            embedding=embedding,
            confidence=0.85,
            audio_duration=2.5
        )
        
        assert success == True
        
        # 檢查樣本累積
        accumulation = identity_manager.get_speaker_accumulation(profile.identity_id)
        assert accumulation is not None
        assert accumulation['total_samples'] == 1
        
        print(f"✅ 測試 7 通過：Speaker 樣本累積成功 (樣本數: {accumulation['total_samples']})")


def run_tests():
    """運行所有測試"""
    print("\n" + "=" * 60)
    print("身分設定整合測試")
    print("=" * 60 + "\n")
    
    test_suite = TestIdentityIntegration()
    
    try:
        # 測試 1: 載入設定
        test_suite.setup_method()
        test_suite.test_load_identity_settings()
        test_suite.teardown_method()
        
        # 測試 2: 預設名稱
        test_suite.setup_method()
        test_suite.test_create_identity_with_default_name()
        test_suite.teardown_method()
        
        # 測試 3: 創建限制
        test_suite.setup_method()
        test_suite.test_allow_identity_creation_restriction()
        test_suite.teardown_method()
        
        # 測試 4: 熱重載
        test_suite.setup_method()
        test_suite.test_reload_callback()
        test_suite.teardown_method()
        
        # 測試 5: Working Context 同步
        test_suite.setup_method()
        test_suite.test_current_identity_sync()
        test_suite.teardown_method()
        
        # 測試 6: 持久化
        test_suite.setup_method()
        test_suite.test_identity_list_persistence()
        test_suite.teardown_method()
        
        # 測試 7: Speaker 樣本
        test_suite.setup_method()
        test_suite.test_speaker_sample_integration()
        test_suite.teardown_method()
        
        print("\n" + "=" * 60)
        print("✅ 所有測試通過 (7/7)")
        print("=" * 60 + "\n")
        
        return True
        
    except Exception as e:
        print(f"\n❌ 測試失敗: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
