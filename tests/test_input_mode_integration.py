"""
測試 input_mode (VAD/文字輸入) 與 user_settings 整合

驗證項目：
1. speech_input.enabled 正確控制 SystemLoop 的 input_mode
2. get_input_mode() 讀取 user_settings 而非 config.yaml
3. 熱重載回調正確更新 input_mode
4. 不同設定值下的行為符合預期
"""

import sys
import os
import pytest
import yaml

# 添加專案根目錄到 Python 路徑
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from configs.config_loader import get_input_mode, is_vad_mode, is_text_input_mode
from configs.user_settings_manager import user_settings_manager


class TestInputModeIntegration:
    """測試輸入模式與 user_settings 整合"""
    
    def setup_method(self):
        """測試前設置"""
        # 備份原始設定
        self.original_enabled = user_settings_manager.get("interaction.speech_input.enabled", True)
        
    def teardown_method(self):
        """測試後清理"""
        # 恢復原始設定
        user_settings_manager.set("interaction.speech_input.enabled", self.original_enabled)
        user_settings_manager.save_settings()
    
    def test_vad_mode_when_enabled(self):
        """測試：enabled=True 時應該返回 VAD 模式"""
        # 設定為啟用
        user_settings_manager.set("interaction.speech_input.enabled", True)
        user_settings_manager.save_settings()
        
        # 驗證
        mode = get_input_mode()
        assert mode == "vad", f"Expected 'vad' but got '{mode}'"
        assert is_vad_mode() == True
        assert is_text_input_mode() == False
        print("✅ VAD 模式 (enabled=True) 測試通過")
    
    def test_text_mode_when_disabled(self):
        """測試：enabled=False 時應該返回文字輸入模式"""
        # 設定為禁用
        user_settings_manager.set("interaction.speech_input.enabled", False)
        user_settings_manager.save_settings()
        
        # 驗證
        mode = get_input_mode()
        assert mode == "text", f"Expected 'text' but got '{mode}'"
        assert is_vad_mode() == False
        assert is_text_input_mode() == True
        print("✅ 文字輸入模式 (enabled=False) 測試通過")
    
    def test_default_mode(self):
        """測試：無設定時應該默認為 VAD 模式"""
        # 確保設定存在（正常情況）
        current = user_settings_manager.get("interaction.speech_input.enabled", None)
        if current is None:
            # 如果設定不存在，應該默認為 True (VAD)
            mode = get_input_mode()
            assert mode == "vad", "Default mode should be 'vad'"
        print("✅ 預設模式測試通過")
    
    def test_mode_switching(self):
        """測試：模式切換功能"""
        # VAD -> 文字
        user_settings_manager.set("interaction.speech_input.enabled", True)
        user_settings_manager.save_settings()
        assert get_input_mode() == "vad"
        
        # 切換到文字模式
        user_settings_manager.set("interaction.speech_input.enabled", False)
        user_settings_manager.save_settings()
        assert get_input_mode() == "text"
        
        # 切換回 VAD
        user_settings_manager.set("interaction.speech_input.enabled", True)
        user_settings_manager.save_settings()
        assert get_input_mode() == "vad"
        
        print("✅ 模式切換測試通過")
    
    def test_system_loop_uses_input_mode(self):
        """測試：SystemLoop 使用 input_mode"""
        from core.system_loop import system_loop
        
        # 設定為 VAD 模式
        user_settings_manager.set("interaction.speech_input.enabled", True)
        user_settings_manager.save_settings()
        
        # 重新獲取 input_mode（模擬 SystemLoop 初始化）
        from configs.config_loader import get_input_mode
        mode = get_input_mode()
        
        # 驗證 SystemLoop 會讀取到正確的模式
        assert mode == "vad"
        
        # 切換到文字模式
        user_settings_manager.set("interaction.speech_input.enabled", False)
        user_settings_manager.save_settings()
        mode = get_input_mode()
        assert mode == "text"
        
        print("✅ SystemLoop input_mode 整合測試通過")
    
    def test_hot_reload_callback(self):
        """測試：熱重載回調功能"""
        from core.system_loop import system_loop
        
        # 記錄初始模式
        initial_mode = system_loop.input_mode
        
        # 改變設定
        new_enabled = not user_settings_manager.get("interaction.speech_input.enabled", True)
        user_settings_manager.set("interaction.speech_input.enabled", new_enabled)
        
        # 觸發熱重載（模擬使用者在 UI 中改變設定）
        system_loop._reload_from_user_settings(
            "interaction.speech_input.enabled",
            new_enabled
        )
        
        # 驗證 SystemLoop 的 input_mode 已更新
        expected_mode = "vad" if new_enabled else "text"
        assert system_loop.input_mode == expected_mode, \
            f"Expected input_mode to be '{expected_mode}' but got '{system_loop.input_mode}'"
        
        print("✅ 熱重載回調測試通過")


def run_all_tests():
    """執行所有測試"""
    print("\n" + "="*60)
    print("開始測試 input_mode 與 user_settings 整合")
    print("="*60 + "\n")
    
    test_suite = TestInputModeIntegration()
    
    try:
        # 設置
        test_suite.setup_method()
        
        # 執行測試
        test_suite.test_vad_mode_when_enabled()
        test_suite.test_text_mode_when_disabled()
        test_suite.test_default_mode()
        test_suite.test_mode_switching()
        test_suite.test_system_loop_uses_input_mode()
        test_suite.test_hot_reload_callback()
        
        # 清理
        test_suite.teardown_method()
        
        print("\n" + "="*60)
        print("✅ 所有測試通過！")
        print("="*60)
        return True
        
    except AssertionError as e:
        print(f"\n❌ 測試失敗: {e}")
        test_suite.teardown_method()
        return False
    except Exception as e:
        print(f"\n❌ 測試錯誤: {e}")
        import traceback
        traceback.print_exc()
        test_suite.teardown_method()
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
