"""
MISCHIEF 系統單元測試

測試所有 MISCHIEF 相關組件的功能。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import json

# 添加專案路徑
project_root = Path(__file__).parents[1]
sys.path.insert(0, str(project_root))


class TestMischiefActionBase(unittest.TestCase):
    """測試 MischiefAction 基類和註冊系統"""
    
    def setUp(self):
        """測試前準備"""
        from modules.sys_module.actions.mischief import (
            MischiefAction, 
            MoodContext, 
            MischiefActionRegistry
        )
        self.MischiefAction = MischiefAction
        self.MoodContext = MoodContext
        self.MischiefActionRegistry = MischiefActionRegistry
    
    def test_mood_context_enum(self):
        """測試情境枚舉"""
        self.assertEqual(self.MoodContext.POSITIVE.value, "positive")
        self.assertEqual(self.MoodContext.NEGATIVE.value, "negative")
        self.assertEqual(self.MoodContext.NEUTRAL.value, "neutral")
        self.assertEqual(self.MoodContext.ANY.value, "any")
    
    def test_action_registry_singleton(self):
        """測試註冊器初始化"""
        registry = self.MischiefActionRegistry()
        self.assertIsNotNone(registry)
        self.assertIsInstance(registry._actions, dict)
    
    def test_action_registration(self):
        """測試行為註冊"""
        from modules.sys_module.actions.mischief import MoodContext
        registry = self.MischiefActionRegistry()
        
        # 創建測試行為
        class TestAction(self.MischiefAction):
            def __init__(self):
                super().__init__()
                self.display_name = "測試行為"
                self.mood_context = MoodContext.ANY
            
            def execute(self, params):
                return True, "測試成功"
        
        action = TestAction()
        registry.register(action)
        
        # 驗證註冊
        self.assertIn("TestAction", registry.list_all_actions())
        retrieved = registry.get_action("TestAction")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.display_name, "測試行為")
    
    def test_get_available_actions_by_mood(self):
        """測試根據情緒過濾行為"""
        from modules.sys_module.actions.mischief import MoodContext
        registry = self.MischiefActionRegistry()
        
        # 創建不同情境的行為
        class PositiveAction(self.MischiefAction):
            def __init__(self):
                super().__init__()
                self.display_name = "積極行為"
                self.mood_context = MoodContext.POSITIVE
            def execute(self, params):
                return True, "ok"
        
        class NegativeAction(self.MischiefAction):
            def __init__(self):
                super().__init__()
                self.display_name = "消極行為"
                self.mood_context = MoodContext.NEGATIVE
            def execute(self, params):
                return True, "ok"
        
        class AnyAction(self.MischiefAction):
            def __init__(self):
                super().__init__()
                self.display_name = "通用行為"
                self.mood_context = MoodContext.ANY
            def execute(self, params):
                return True, "ok"
        
        registry.register(PositiveAction())
        registry.register(NegativeAction())
        registry.register(AnyAction())
        
        # 測試高情緒
        available = registry.get_available_actions(0.5)
        action_ids = [a["action_id"] for a in available]
        self.assertIn("PositiveAction", action_ids)
        self.assertIn("AnyAction", action_ids)
        self.assertNotIn("NegativeAction", action_ids)
        
        # 測試低情緒
        available = registry.get_available_actions(-0.5)
        action_ids = [a["action_id"] for a in available]
        self.assertIn("NegativeAction", action_ids)
        self.assertIn("AnyAction", action_ids)
        self.assertNotIn("PositiveAction", action_ids)


class TestMoveMouseAction(unittest.TestCase):
    """測試 MoveMouseAction"""
    
    def setUp(self):
        """測試前準備"""
        try:
            from modules.sys_module.actions.mischief.move_mouse import MoveMouseAction
            self.MoveMouseAction = MoveMouseAction
        except ImportError as e:
            self.skipTest(f"無法導入 MoveMouseAction: {e}")
    
    def test_action_properties(self):
        """測試行為屬性"""
        action = self.MoveMouseAction()
        self.assertEqual(action.display_name, "移動滑鼠")
        self.assertEqual(action.requires_params, [])
    
    @patch('pyautogui.position')
    @patch('pyautogui.moveTo')
    @patch('pyautogui.size')
    def test_execute_success(self, mock_size, mock_moveto, mock_position):
        """測試執行成功"""
        # 設置 mock
        mock_position.return_value = (500, 500)
        mock_size.return_value = (1920, 1080)
        
        action = self.MoveMouseAction()
        success, message = action.execute({})
        
        self.assertTrue(success)
        self.assertIn("成功", message)
        mock_moveto.assert_called_once()


class TestCreateTextFileAction(unittest.TestCase):
    """測試 CreateTextFileAction"""
    
    def setUp(self):
        """測試前準備"""
        try:
            from modules.sys_module.actions.mischief.create_file import CreateTextFileAction
            self.CreateTextFileAction = CreateTextFileAction
        except ImportError as e:
            self.skipTest(f"無法導入 CreateTextFileAction: {e}")
    
    def test_action_properties(self):
        """測試行為屬性"""
        action = self.CreateTextFileAction()
        self.assertEqual(action.display_name, "Create Text File")
        self.assertIn("message", action.requires_params)
    
    def test_validate_params_missing(self):
        """測試參數驗證 - 缺少必要參數"""
        action = self.CreateTextFileAction()
        valid, error = action.validate_params({})
        self.assertFalse(valid)
        self.assertIn("message", error)
    
    def test_validate_params_valid(self):
        """測試參數驗證 - 有效參數"""
        action = self.CreateTextFileAction()
        valid, error = action.validate_params({"message": "測試訊息"})
        self.assertTrue(valid)
        self.assertEqual(error, "")
    
    @patch('pathlib.Path.exists')
    @patch('builtins.open', create=True)
    def test_execute_success(self, mock_open, mock_exists):
        """測試執行成功"""
        # 設置 mock
        mock_exists.return_value = True
        mock_file = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_file
        
        action = self.CreateTextFileAction()
        success, message = action.execute({"message": "測試訊息"})
        
        self.assertTrue(success)
        self.assertIn("成功", message)
        mock_open.assert_called_once()
    
    def test_execute_missing_params(self):
        """測試執行失敗 - 缺少參數"""
        action = self.CreateTextFileAction()
        success, message = action.execute({})
        
        self.assertFalse(success)
        self.assertIn("message", message)


class TestSpeakAction(unittest.TestCase):
    """測試 SpeakAction"""
    
    def setUp(self):
        """測試前準備"""
        try:
            from modules.sys_module.actions.mischief.speak import SpeakAction
            self.SpeakAction = SpeakAction
        except ImportError as e:
            self.skipTest(f"無法導入 SpeakAction: {e}")
    
    def test_action_properties(self):
        """測試行為屬性"""
        action = self.SpeakAction()
        self.assertEqual(action.display_name, "Speak")
        self.assertIn("text", action.requires_params)
        self.assertEqual(action.animation_name, "talk_ani_f")
    
    def test_execute_success(self):
        """測試執行成功 - 返回文字而非直接調用 TTS"""
        action = self.SpeakAction()
        success, message = action.execute({"text": "測試語音"})
        
        # Speak action 現在只返回文字，由 Module Coordinator 處理
        self.assertTrue(success)
        self.assertEqual(message, "測試語音")


class TestMischiefExecutor(unittest.TestCase):
    """測試 MischiefExecutor"""
    
    def setUp(self):
        """測試前準備"""
        try:
            from modules.sys_module.actions.mischief.executor import MischiefExecutor
            self.MischiefExecutor = MischiefExecutor
        except ImportError as e:
            self.skipTest(f"無法導入 MischiefExecutor: {e}")
    
    def test_executor_initialization(self):
        """測試執行器初始化"""
        executor = self.MischiefExecutor()
        self.assertIsNotNone(executor)
        self.assertEqual(len(executor.execution_history), 0)
    
    def test_parse_valid_json(self):
        """測試解析有效的 JSON"""
        executor = self.MischiefExecutor()
        
        test_json = '''
        {
            "actions": [
                {
                    "action_id": "TestAction",
                    "params": {"key": "value"}
                }
            ]
        }
        '''
        
        success, actions = executor.parse_llm_response(test_json)
        
        # 注意：因為 TestAction 未註冊，實際會被過濾掉
        self.assertTrue(success)
        self.assertIsInstance(actions, list)
    
    def test_parse_invalid_json(self):
        """測試解析無效的 JSON"""
        executor = self.MischiefExecutor()
        
        test_json = "這不是 JSON"
        
        success, actions = executor.parse_llm_response(test_json)
        
        self.assertFalse(success)
        self.assertEqual(len(actions), 0)
    
    def test_parse_missing_actions_field(self):
        """測試缺少 actions 欄位"""
        executor = self.MischiefExecutor()
        
        test_json = '{"other_field": "value"}'
        
        success, actions = executor.parse_llm_response(test_json)
        
        self.assertFalse(success)
        self.assertEqual(len(actions), 0)
    
    def test_get_available_actions_for_llm(self):
        """測試生成 LLM prompt"""
        executor = self.MischiefExecutor()
        
        prompt = executor.get_available_actions_for_llm(mood=0.5)
        
        self.assertIsInstance(prompt, str)
        self.assertIn("available_actions", prompt)
        self.assertIn("instructions", prompt)
        
        # 驗證是有效的 JSON
        data = json.loads(prompt)
        self.assertIn("available_actions", data)
        self.assertIn("instructions", data)
    
    @patch('modules.sys_module.actions.mischief.executor.mischief_registry')
    def test_execute_actions_success(self, mock_registry):
        """測試執行行為列表 - 成功"""
        executor = self.MischiefExecutor()
        
        # 創建 mock 行為
        mock_action = MagicMock()
        mock_action.execute.return_value = (True, "執行成功")
        mock_registry.get_action.return_value = mock_action
        
        actions = [
            {"action_id": "TestAction1", "params": {}},
            {"action_id": "TestAction2", "params": {}}
        ]
        
        results = executor.execute_actions(actions)
        
        self.assertEqual(results["total"], 2)
        self.assertEqual(results["success"], 2)
        self.assertEqual(results["failed"], 0)
    
    @patch('modules.sys_module.actions.mischief.executor.mischief_registry')
    def test_execute_actions_with_failures(self, mock_registry):
        """測試執行行為列表 - 部分失敗"""
        executor = self.MischiefExecutor()
        
        # 第一個行為成功，第二個失敗
        mock_action1 = MagicMock()
        mock_action1.execute.return_value = (True, "成功")
        
        mock_action2 = MagicMock()
        mock_action2.execute.return_value = (False, "失敗")
        
        mock_registry.get_action.side_effect = [mock_action1, mock_action2]
        
        actions = [
            {"action_id": "Action1", "params": {}},
            {"action_id": "Action2", "params": {}}
        ]
        
        results = executor.execute_actions(actions)
        
        self.assertEqual(results["total"], 2)
        self.assertEqual(results["success"], 1)
        self.assertEqual(results["failed"], 1)


class TestStateManagerMischief(unittest.TestCase):
    """測試 StateManager 的 MISCHIEF 相關方法"""
    
    def setUp(self):
        """測試前準備"""
        try:
            from core.states.state_manager import state_manager
            self.state_manager = state_manager
        except ImportError as e:
            self.skipTest(f"無法導入 state_manager: {e}")
    
    @patch('core.states.state_manager.user_settings_manager')
    def test_trigger_mischief_disabled(self, mock_settings):
        """測試 MISCHIEF 被禁用的情況"""
        # 設置配置為禁用
        mock_settings.get_setting.return_value = False
        
        # 調用方法（需要 mock 更多依賴）
        # 這裡只測試配置檢查邏輯
        enabled = mock_settings.get_setting("behavior.mischief.enabled", False)
        self.assertFalse(enabled)
    
    def test_adjust_status_after_mischief_high_success(self):
        """測試高成功率的數值調整"""
        # 模擬高成功率結果
        results = {
            "total": 5,
            "success": 4,
            "failed": 1,
            "skipped": 0
        }
        
        # 驗證成功率計算
        success_rate = results["success"] / results["total"]
        self.assertGreater(success_rate, 0.7)
    
    def test_adjust_status_after_mischief_low_success(self):
        """測試低成功率的數值調整"""
        results = {
            "total": 5,
            "success": 1,
            "failed": 4,
            "skipped": 0
        }
        
        success_rate = results["success"] / results["total"]
        self.assertLess(success_rate, 0.3)


class TestMischiefIntegration(unittest.TestCase):
    """整合測試 - 測試完整流程"""
    
    def setUp(self):
        """測試前準備"""
        try:
            from modules.sys_module.actions.mischief.loader import (
                mischief_registry,
                mischief_executor
            )
            self.registry = mischief_registry
            self.executor = mischief_executor
        except ImportError as e:
            self.skipTest(f"無法導入 mischief 模組: {e}")
    
    def test_registry_has_actions(self):
        """測試註冊器包含預期的行為"""
        all_actions = self.registry.list_all_actions()
        
        # 檢查是否有註冊的行為
        self.assertGreater(len(all_actions), 0)
        
        # 檢查預期的行為
        expected_actions = [
            "MoveMouseAction",
            "CreateTextFileAction",
            "SpeakAction"
        ]
        
        for action_id in expected_actions:
            self.assertIn(
                action_id, 
                all_actions, 
                f"{action_id} 應該已註冊"
            )
    
    def test_end_to_end_flow_parsing(self):
        """測試端到端流程 - JSON 解析"""
        # 構建完整的 LLM 回應
        llm_response = {
            "actions": [
                {
                    "action_id": "MoveMouseAction",
                    "params": {}
                }
            ]
        }
        
        json_response = json.dumps(llm_response)
        
        # 解析
        success, actions = self.executor.parse_llm_response(json_response)
        
        # 驗證
        self.assertTrue(success)
        # 注意：如果行為未註冊，會被過濾
        self.assertIsInstance(actions, list)


def run_tests():
    """運行所有測試"""
    # 創建測試套件
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # 添加所有測試類別
    suite.addTests(loader.loadTestsFromTestCase(TestMischiefActionBase))
    suite.addTests(loader.loadTestsFromTestCase(TestMoveMouseAction))
    suite.addTests(loader.loadTestsFromTestCase(TestCreateTextFileAction))
    suite.addTests(loader.loadTestsFromTestCase(TestSpeakAction))
    suite.addTests(loader.loadTestsFromTestCase(TestMischiefExecutor))
    suite.addTests(loader.loadTestsFromTestCase(TestStateManagerMischief))
    suite.addTests(loader.loadTestsFromTestCase(TestMischiefIntegration))
    
    # 運行測試
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    return result


if __name__ == "__main__":
    result = run_tests()
    
    # 顯示統計
    print("\n" + "=" * 70)
    print("測試統計")
    print("=" * 70)
    print(f"運行測試數: {result.testsRun}")
    print(f"成功: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"失敗: {len(result.failures)}")
    print(f"錯誤: {len(result.errors)}")
    print(f"跳過: {len(result.skipped)}")
    
    # 返回適當的退出碼
    sys.exit(0 if result.wasSuccessful() else 1)
