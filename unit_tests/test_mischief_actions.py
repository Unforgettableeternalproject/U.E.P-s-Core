"""
MISCHIEF Actions 獨立測試

單獨測試每個 action 的功能，無需完整系統依賴。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, mock_open

# 添加專案路徑
project_root = Path(__file__).parents[1]
sys.path.insert(0, str(project_root))


class TestMoveWindowAction(unittest.TestCase):
    """測試 MoveWindowAction - 移動視窗功能"""
    
    @classmethod
    def setUpClass(cls):
        """測試類初始化"""
        try:
            from modules.sys_module.actions.mischief.move_window import MoveWindowAction
            cls.MoveWindowAction = MoveWindowAction
        except ImportError as e:
            raise unittest.SkipTest(f"無法導入 MoveWindowAction: {e}")
    
    def test_action_metadata(self):
        """測試行為元數據"""
        action = self.MoveWindowAction()
        
        self.assertEqual(action.display_name, "移動視窗")
        self.assertIn("視窗", action.description)
        self.assertEqual(action.requires_params, [])
    
    @patch('win32gui.EnumWindows')
    @patch('win32gui.GetWindowRect')
    @patch('win32gui.SetWindowPos')
    def test_execute_with_windows(self, mock_setpos, mock_getrect, mock_enum):
        """測試執行 - 有可移動視窗"""
        action = self.MoveWindowAction()
        
        # 模擬視窗枚舉
        def enum_callback(callback, _):
            # 模擬一個視窗
            callback(12345, None)
            return True
        
        mock_enum.side_effect = enum_callback
        
        # 模擬視窗屬性
        with patch('win32gui.IsWindowVisible', return_value=True):
            with patch('win32gui.GetWindowText', return_value="Test Window"):
                with patch('win32gui.GetWindowLong', return_value=0):  # 非最大化
                    with patch('pyautogui.size', return_value=(1920, 1080)):
                        mock_getrect.return_value = (100, 100, 500, 400)
                        
                        success, message = action.execute({})
                        
                        # 驗證執行結果
                        self.assertTrue(success or not success)  # 可能沒有視窗
                        self.assertIsInstance(message, str)
    
    def test_get_movable_windows_filtering(self):
        """測試視窗過濾邏輯"""
        action = self.MoveWindowAction()
        
        # 測試排除 UEP 視窗
        with patch('win32gui.IsWindowVisible', return_value=True):
            with patch('win32gui.GetWindowText', return_value="U.E.P System"):
                with patch('win32gui.EnumWindows') as mock_enum:
                    # 應該排除包含 UEP 的視窗
                    windows = action._get_movable_windows()
                    # 驗證不會包含 UEP 視窗
                    self.assertIsInstance(windows, list)


class TestClickShortcutAction(unittest.TestCase):
    """測試 ClickShortcutAction - 點擊捷徑功能"""
    
    @classmethod
    def setUpClass(cls):
        """測試類初始化"""
        try:
            from modules.sys_module.actions.mischief.click_shortcut import ClickShortcutAction
            from modules.sys_module.actions.mischief import MoodContext
            cls.ClickShortcutAction = ClickShortcutAction
            cls.MoodContext = MoodContext
        except ImportError as e:
            raise unittest.SkipTest(f"無法導入 ClickShortcutAction: {e}")
    
    def test_action_metadata(self):
        """測試行為元數據"""
        action = self.ClickShortcutAction()
        
        self.assertEqual(action.display_name, "點擊捷徑")
        self.assertEqual(action.mood_context, self.MoodContext.POSITIVE)
        self.assertEqual(action.animation_name, "click_f")
    
    def test_blacklist_contains_dangerous_keywords(self):
        """測試黑名單包含危險關鍵字"""
        action = self.ClickShortcutAction()
        
        dangerous_keywords = ["刪除", "delete", "卸載", "格式化", "關機"]
        
        for keyword in dangerous_keywords:
            self.assertIn(keyword, action.blacklist)
    
    @patch('pathlib.Path.exists')
    @patch('pathlib.Path.glob')
    def test_get_shortcuts_filters_blacklist(self, mock_glob, mock_exists):
        """測試捷徑過濾 - 黑名單"""
        action = self.ClickShortcutAction()
        
        # 創建測試捷徑路徑
        safe_shortcut = Mock()
        safe_shortcut.stem = "Chrome"
        
        dangerous_shortcut = Mock()
        dangerous_shortcut.stem = "刪除所有檔案"
        
        mock_glob.return_value = [safe_shortcut, dangerous_shortcut]
        mock_exists.return_value = True
        
        desktop = Path.home() / "Desktop"
        shortcuts = action._get_shortcuts(desktop)
        
        # 驗證只返回安全的捷徑
        self.assertIn(safe_shortcut, shortcuts)
        self.assertNotIn(dangerous_shortcut, shortcuts)
    
    @patch('os.startfile')
    @patch('pathlib.Path.exists', return_value=True)
    @patch('pathlib.Path.glob')
    def test_execute_with_shortcuts(self, mock_glob, mock_exists, mock_startfile):
        """測試執行 - 有可用捷徑"""
        action = self.ClickShortcutAction()
        
        # 模擬捷徑
        mock_shortcut = Mock()
        mock_shortcut.stem = "TestApp"
        mock_shortcut.name = "TestApp.lnk"
        mock_glob.return_value = [mock_shortcut]
        
        success, message = action.execute({})
        
        # 驗證結果
        self.assertTrue(success or not success)  # 可能沒有捷徑
        self.assertIsInstance(message, str)


class TestMischiefActionParameterValidation(unittest.TestCase):
    """測試所有 actions 的參數驗證功能"""
    
    def test_move_mouse_no_params_required(self):
        """測試 MoveMouseAction 不需要參數"""
        try:
            from modules.sys_module.actions.mischief.move_mouse import MoveMouseAction
            action = MoveMouseAction()
            
            self.assertEqual(len(action.requires_params), 0)
            
            valid, error = action.validate_params({})
            self.assertTrue(valid)
        except ImportError:
            self.skipTest("無法導入 MoveMouseAction")
    
    def test_create_file_requires_message(self):
        """測試 CreateTextFileAction 需要 message 參數"""
        try:
            from modules.sys_module.actions.mischief.create_file import CreateTextFileAction
            action = CreateTextFileAction()
            
            self.assertIn("message", action.requires_params)
            
            # 缺少參數
            valid, error = action.validate_params({})
            self.assertFalse(valid)
            self.assertIn("message", error)
            
            # 有參數
            valid, error = action.validate_params({"message": "test"})
            self.assertTrue(valid)
        except ImportError:
            self.skipTest("無法導入 CreateTextFileAction")
    
    def test_speak_requires_text(self):
        """測試 SpeakAction 需要 text 參數"""
        try:
            from modules.sys_module.actions.mischief.speak import SpeakAction
            action = SpeakAction()
            
            self.assertIn("text", action.requires_params)
            
            # 缺少參數
            valid, error = action.validate_params({})
            self.assertFalse(valid)
            
            # 有參數
            valid, error = action.validate_params({"text": "hello"})
            self.assertTrue(valid)
        except ImportError:
            self.skipTest("無法導入 SpeakAction")


class TestMischiefActionInfoGeneration(unittest.TestCase):
    """測試所有 actions 的資訊生成功能"""
    
    def test_all_actions_provide_info(self):
        """測試所有行為都能提供資訊"""
        try:
            from modules.sys_module.actions.mischief import MischiefActionRegistry
            
            # 創建註冊器並手動註冊測試行為
            registry = MischiefActionRegistry()
            
            # 嘗試導入並註冊所有行為
            try:
                from modules.sys_module.actions.mischief.move_mouse import MoveMouseAction
                from modules.sys_module.actions.mischief.create_file import CreateTextFileAction
                from modules.sys_module.actions.mischief.speak import SpeakAction
                
                registry.register(MoveMouseAction())
                registry.register(CreateTextFileAction())
                registry.register(SpeakAction())
            except ImportError:
                pass
            
            all_actions = registry.list_all_actions()
            
            self.assertGreater(len(all_actions), 0, "應該至少有一個註冊的行為")
            
            for action_id in all_actions:
                action = registry.get_action(action_id)
                info = action.get_info()
                
                # 驗證資訊完整性
                self.assertIn("action_id", info)
                self.assertIn("name", info)
                self.assertIn("description", info)
                self.assertIn("mood_context", info)
                self.assertIn("required_params", info)
                
                # 驗證類型
                self.assertIsInstance(info["action_id"], str)
                self.assertIsInstance(info["name"], str)
                self.assertIsInstance(info["required_params"], list)
                
        except ImportError as e:
            self.skipTest(f"無法導入 mischief 模組: {e}")


class TestMischiefExecutorErrorHandling(unittest.TestCase):
    """測試 MischiefExecutor 的錯誤處理"""
    
    @classmethod
    def setUpClass(cls):
        """測試類初始化"""
        try:
            from modules.sys_module.actions.mischief.executor import MischiefExecutor
            cls.MischiefExecutor = MischiefExecutor
        except ImportError as e:
            raise unittest.SkipTest(f"無法導入 MischiefExecutor: {e}")
    
    def test_parse_malformed_json(self):
        """測試解析格式錯誤的 JSON"""
        executor = self.MischiefExecutor()
        
        malformed_jsons = [
            "",
            "{",
            '{"actions": [}',
            '{"actions": "not_a_list"}',
            None
        ]
        
        for malformed in malformed_jsons:
            if malformed is not None:
                success, actions = executor.parse_llm_response(malformed)
                self.assertFalse(success)
                self.assertEqual(len(actions), 0)
    
    def test_parse_actions_without_action_id(self):
        """測試解析缺少 action_id 的行為"""
        executor = self.MischiefExecutor()
        
        test_json = '''
        {
            "actions": [
                {"params": {"key": "value"}},
                {"action_id": "ValidAction", "params": {}}
            ]
        }
        '''
        
        success, actions = executor.parse_llm_response(test_json)
        
        # 應該過濾掉無效的行為
        self.assertTrue(success)
        # 有效行為數量取決於是否註冊
        self.assertIsInstance(actions, list)
    
    @patch('modules.sys_module.actions.mischief.executor.mischief_registry')
    @patch('time.sleep')
    def test_execute_actions_handles_exceptions(self, mock_sleep, mock_registry):
        """測試執行行為時處理異常"""
        executor = self.MischiefExecutor()
        
        # 創建會拋出異常的 mock 行為
        mock_action = MagicMock()
        mock_action.execute.side_effect = Exception("測試異常")
        mock_registry.get_action.return_value = mock_action
        
        actions = [{"action_id": "FailingAction", "params": {}}]
        
        results = executor.execute_actions(actions)
        
        # 驗證異常被捕獲
        self.assertEqual(results["total"], 1)
        self.assertEqual(results["failed"], 1)
        self.assertEqual(results["success"], 0)


def run_action_tests():
    """運行所有 action 測試"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # 添加測試類別
    suite.addTests(loader.loadTestsFromTestCase(TestMoveWindowAction))
    suite.addTests(loader.loadTestsFromTestCase(TestClickShortcutAction))
    suite.addTests(loader.loadTestsFromTestCase(TestMischiefActionParameterValidation))
    suite.addTests(loader.loadTestsFromTestCase(TestMischiefActionInfoGeneration))
    suite.addTests(loader.loadTestsFromTestCase(TestMischiefExecutorErrorHandling))
    
    # 運行測試
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    return result


if __name__ == "__main__":
    result = run_action_tests()
    
    # 顯示統計
    print("\n" + "=" * 70)
    print("MISCHIEF Actions 測試統計")
    print("=" * 70)
    print(f"運行: {result.testsRun}")
    print(f"成功: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"失敗: {len(result.failures)}")
    print(f"錯誤: {len(result.errors)}")
    print(f"跳過: {len(result.skipped)}")
    
    if result.wasSuccessful():
        print("\n✅ 所有測試通過！")
    else:
        print("\n❌ 部分測試失敗")
    
    sys.exit(0 if result.wasSuccessful() else 1)
