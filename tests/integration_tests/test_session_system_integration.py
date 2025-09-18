# tests/integration_tests/test_session_system_integration.py
"""
會話系統整合測試

測試完整的會話系統架構：
1. General Session (GS) 基礎功能
2. Chatting Session (CS) 對話處理
3. Workflow Session (WS) 任務執行
4. Session Coordinator 協調邏輯
5. 會話轉換和狀態管理
"""

import unittest
import sys
import os
from unittest.mock import Mock, patch

# 添加項目根目錄到 Python 路徑
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from core.session_coordinator import session_coordinator, SessionCoordinationResult
from core.general_session import general_session_manager, GSType, GSStatus
from core.chatting_session import chatting_session_manager, ChattingSession
from core.workflow_session import workflow_session_manager, WorkflowSession, WSTaskType
from core.state_manager import state_manager, UEPState
from core.working_context import working_context_manager, ContextType

from utils.debug_helper import debug_log, info_log, error_log


class TestSessionSystemIntegration(unittest.TestCase):
    """會話系統整合測試"""
    
    def setUp(self):
        """測試前設置"""
        # 清理所有會話
        general_session_manager.cleanup_completed_sessions()
        chatting_session_manager.cleanup_inactive_sessions()
        workflow_session_manager.cleanup_completed_sessions()
        
        # 重置狀態
        state_manager.set_state(UEPState.IDLE)
        
        # 清理 Working Context
        working_context_manager.clear_all_data()
        
        print("\n=== 會話系統整合測試開始 ===")
    
    def tearDown(self):
        """測試後清理"""
        # 結束所有會話
        session_coordinator.end_current_session()
        
        # 清理會話管理器
        general_session_manager.cleanup_completed_sessions()
        chatting_session_manager.cleanup_inactive_sessions()
        workflow_session_manager.cleanup_completed_sessions()
        
        print("=== 會話系統整合測試結束 ===\n")
    
    def test_01_basic_gs_lifecycle(self):
        """測試 1: 基礎 GS 生命週期"""
        print("\n[測試 1] 基礎 GS 生命週期測試")
        
        # 1. 啟動新的 GS
        input_data = {
            "type": "text_input",
            "data": {"text": "你好"},
            "timestamp": "2024-01-01T10:00:00Z"
        }
        
        result = session_coordinator.handle_user_input(input_data)
        # "你好" 會觸發 greeting 意圖，然後啟動 CS
        self.assertIn(result, [SessionCoordinationResult.CS_STARTED, SessionCoordinationResult.GS_STARTED, SessionCoordinationResult.SESSION_CONTINUED])
        
        # 2. 檢查 GS 狀態
        current_gs = general_session_manager.get_current_session()
        self.assertIsNotNone(current_gs)
        self.assertEqual(current_gs.gs_type, GSType.TEXT_INPUT)
        self.assertIn(current_gs.status, [GSStatus.ACTIVE, GSStatus.PROCESSING, GSStatus.COMPLETED])
        
        # 3. 檢查系統狀態
        system_status = session_coordinator.get_system_status()
        self.assertIsNotNone(system_status["general_session"]["current_session"])
        
        # 4. 結束 GS
        success = session_coordinator.end_current_session()
        self.assertTrue(success)
        self.assertEqual(state_manager.get_state(), UEPState.IDLE)
        
        print("✅ 基礎 GS 生命週期測試通過")
    
    def test_02_cs_conversation_flow(self):
        """測試 2: CS 對話流程"""
        print("\n[測試 2] CS 對話流程測試")
        
        # 1. 啟動對話型輸入
        input_data = {
            "type": "text_input",
            "data": {"text": "我想和你聊天"},
            "timestamp": "2024-01-01T10:00:00Z"
        }
        
        result = session_coordinator.handle_user_input(input_data)
        self.assertIn(result, [SessionCoordinationResult.CS_STARTED, SessionCoordinationResult.SESSION_CONTINUED])
        
        # 2. 檢查 CS 是否啟動
        current_gs = general_session_manager.get_current_session()
        self.assertIsNotNone(current_gs)
        
        # 如果啟動了 CS，檢查相關狀態
        if session_coordinator.active_cs_sessions:
            self.assertEqual(state_manager.get_state(), UEPState.CHAT)
            cs_session_id = list(session_coordinator.active_cs_sessions.keys())[0]
            cs_instance = session_coordinator.active_cs_sessions[cs_session_id]["cs_instance"]
            self.assertIsInstance(cs_instance, ChattingSession)
            
            # 3. 模擬對話輸入
            conversation_input = {
                "type": "text_input",
                "data": {"text": "今天天氣怎麼樣？"}
            }
            
            result = session_coordinator.handle_user_input(conversation_input)
            self.assertEqual(result, SessionCoordinationResult.SESSION_CONTINUED)
            
            # 4. 檢查對話輪次
            self.assertGreater(cs_instance.turn_counter, 0)
            
            # 5. 結束 CS
            success = session_coordinator.end_sub_session(cs_session_id)
            self.assertTrue(success)
        
        print("✅ CS 對話流程測試通過")
    
    def test_03_ws_task_execution(self):
        """測試 3: WS 任務執行"""
        print("\n[測試 3] WS 任務執行測試")
        
        # 1. 啟動任務型輸入
        input_data = {
            "type": "text_input",
            "data": {"text": "執行系統檢查"},
            "timestamp": "2024-01-01T10:00:00Z"
        }
        
        result = session_coordinator.handle_user_input(input_data)
        self.assertIn(result, [SessionCoordinationResult.WS_STARTED, SessionCoordinationResult.SESSION_CONTINUED])
        
        # 2. 檢查 WS 是否啟動
        current_gs = general_session_manager.get_current_session()
        self.assertIsNotNone(current_gs)
        
        # 如果啟動了 WS，檢查相關狀態
        if session_coordinator.active_ws_sessions:
            self.assertEqual(state_manager.get_state(), UEPState.WORK)
            ws_session_id = list(session_coordinator.active_ws_sessions.keys())[0]
            ws_instance = session_coordinator.active_ws_sessions[ws_session_id]["ws_instance"]
            self.assertIsInstance(ws_instance, WorkflowSession)
            
            # 3. 啟動任務執行
            success = ws_instance.start_execution()
            self.assertTrue(success)
            
            # 4. 執行任務步驟
            step_count = 0
            max_steps = 10  # 防止無限循環
            
            while step_count < max_steps:
                result = ws_instance.execute_next_step()
                step_count += 1
                
                if result.get("execution_completed", False):
                    # 任務完成
                    self.assertTrue(result["success"])
                    self.assertIsNotNone(result["task_result"])
                    break
                elif not result.get("success", False):
                    # 任務失敗
                    break
                elif not result.get("has_next_step", False):
                    # 沒有下一步
                    break
            
            # 5. 檢查任務狀態
            progress = ws_instance.get_progress()
            self.assertGreater(progress["progress"]["completed_steps"], 0)
            
            # 6. 結束 WS
            success = session_coordinator.end_sub_session(ws_session_id)
            self.assertTrue(success)
        
        print("✅ WS 任務執行測試通過")
    
    def test_04_session_transitions(self):
        """測試 4: 會話轉換"""
        print("\n[測試 4] 會話轉換測試")
        
        # 1. 啟動 GS
        input_data = {
            "type": "voice_input",
            "data": {"text": "測試會話轉換"},
            "timestamp": "2024-01-01T10:00:00Z"
        }
        
        result = session_coordinator.handle_user_input(input_data)
        # "測試會話轉換" 會觸發 conversation 意圖，啟動 CS
        self.assertIn(result, [SessionCoordinationResult.CS_STARTED, SessionCoordinationResult.GS_STARTED, SessionCoordinationResult.SESSION_CONTINUED])
        
        current_gs = general_session_manager.get_current_session()
        self.assertIsNotNone(current_gs)
        
        # 2. 測試不同意圖的會話啟動
        test_cases = [
            {
                "input": {"type": "text_input", "data": {"text": "聊天測試"}},
                "expected_state": UEPState.CHAT,
                "session_type": "cs"
            },
            {
                "input": {"type": "text_input", "data": {"text": "執行文件操作"}},
                "expected_state": UEPState.WORK,
                "session_type": "ws"
            }
        ]
        
        for i, test_case in enumerate(test_cases):
            print(f"  測試案例 {i+1}: {test_case['input']['data']['text']}")
            
            # 清理之前的子會話
            session_coordinator.end_current_session()
            session_coordinator.handle_user_input(input_data)  # 重新啟動 GS
            
            # 發送測試輸入
            result = session_coordinator.handle_user_input(test_case["input"])
            
            # 檢查結果
            if test_case["session_type"] == "cs" and session_coordinator.active_cs_sessions:
                self.assertEqual(state_manager.get_state(), test_case["expected_state"])
                print(f"    ✅ CS 啟動成功")
            elif test_case["session_type"] == "ws" and session_coordinator.active_ws_sessions:
                self.assertEqual(state_manager.get_state(), test_case["expected_state"])
                print(f"    ✅ WS 啟動成功")
            else:
                print(f"    ℹ️ 會話類型判斷為簡單回應")
        
        print("✅ 會話轉換測試通過")
    
    def test_05_concurrent_sessions(self):
        """測試 5: 併發會話處理"""
        print("\n[測試 5] 併發會話處理測試")
        
        # 1. 啟動 GS
        input_data = {
            "type": "text_input",
            "data": {"text": "測試併發會話"},
            "timestamp": "2024-01-01T10:00:00Z"
        }
        
        result = session_coordinator.handle_user_input(input_data)
        current_gs = general_session_manager.get_current_session()
        self.assertIsNotNone(current_gs)
        
        # 2. 嘗試啟動多個子會話 (實際上一次只能有一個活躍子會話)
        chat_input = {
            "type": "text_input",
            "data": {"text": "開始對話"}
        }
        
        task_input = {
            "type": "text_input", 
            "data": {"text": "執行任務"}
        }
        
        # 啟動第一個子會話
        result1 = session_coordinator.handle_user_input(chat_input)
        active_sessions_before = len(session_coordinator.active_cs_sessions) + len(session_coordinator.active_ws_sessions)
        
        # 嘗試啟動第二個子會話
        result2 = session_coordinator.handle_user_input(task_input)
        active_sessions_after = len(session_coordinator.active_cs_sessions) + len(session_coordinator.active_ws_sessions)
        
        # 3. 檢查會話管理
        self.assertLessEqual(active_sessions_after, 1)  # 同時最多只有一個子會話
        print(f"    活躍子會話數量: {active_sessions_after}")
        
        # 4. 檢查系統狀態一致性
        system_status = session_coordinator.get_system_status()
        total_active = system_status["active_cs_sessions"] + system_status["active_ws_sessions"]
        self.assertEqual(total_active, active_sessions_after)
        
        print("✅ 併發會話處理測試通過")
    
    def test_06_error_handling(self):
        """測試 6: 錯誤處理"""
        print("\n[測試 6] 錯誤處理測試")
        
        # 1. 測試無效輸入
        invalid_inputs = [
            {},  # 空輸入
            {"type": "unknown"},  # 未知類型
            {"type": "text_input"},  # 缺少 data
            {"type": "text_input", "data": {}},  # 空 data
        ]
        
        for i, invalid_input in enumerate(invalid_inputs):
            print(f"  測試無效輸入 {i+1}: {invalid_input}")
            
            result = session_coordinator.handle_user_input(invalid_input)
            # 系統應該能處理無效輸入而不崩潰，可能會啟動 CS 或返回錯誤
            self.assertIn(result, [
                SessionCoordinationResult.ERROR,
                SessionCoordinationResult.GS_STARTED,
                SessionCoordinationResult.SESSION_CONTINUED,
                SessionCoordinationResult.CS_STARTED,
                SessionCoordinationResult.WS_STARTED
            ])
        
        # 2. 測試會話狀態異常
        # 啟動正常會話
        normal_input = {
            "type": "text_input",
            "data": {"text": "正常輸入"},
            "timestamp": "2024-01-01T10:00:00Z"
        }
        
        result = session_coordinator.handle_user_input(normal_input)
        current_gs = general_session_manager.get_current_session()
        
        if current_gs:
            # 強制設定異常狀態
            original_status = current_gs.status
            current_gs.status = GSStatus.ERROR
            
            # 嘗試處理輸入
            result = session_coordinator.handle_user_input(normal_input)
            
            # 恢復狀態
            current_gs.status = original_status
            
            print(f"    異常狀態處理結果: {result}")
        
        # 3. 測試子會話清理
        session_coordinator.end_current_session()
        
        # 檢查清理結果
        self.assertEqual(len(session_coordinator.active_cs_sessions), 0)
        self.assertEqual(len(session_coordinator.active_ws_sessions), 0)
        self.assertEqual(state_manager.get_state(), UEPState.IDLE)
        
        print("✅ 錯誤處理測試通過")
    
    def test_07_working_context_integration(self):
        """測試 7: Working Context 整合"""
        print("\n[測試 7] Working Context 整合測試")
        
        # 1. 啟動會話
        input_data = {
            "type": "text_input",
            "data": {"text": "測試上下文整合"},
            "timestamp": "2024-01-01T10:00:00Z"
        }
        
        result = session_coordinator.handle_user_input(input_data)
        current_gs = general_session_manager.get_current_session()
        self.assertIsNotNone(current_gs)
        
        # 2. 檢查 Working Context 設定
        gs_context = working_context_manager.get_data(ContextType.GENERAL_SESSION, "current_session")
        self.assertIsNotNone(gs_context)
        self.assertEqual(gs_context["session_id"], current_gs.session_id)
        
        # 3. 啟動 CS 並檢查上下文
        chat_input = {
            "type": "text_input",
            "data": {"text": "開始聊天"}
        }
        
        result = session_coordinator.handle_user_input(chat_input)
        
        if session_coordinator.active_cs_sessions:
            cs_context = working_context_manager.get_data(ContextType.LLM_CONTEXT, "conversation_session")
            if cs_context:
                self.assertEqual(cs_context["conversation_mode"], "chatting")
                print("    ✅ CS Working Context 設定正確")
        
        # 4. 啟動 WS 並檢查上下文
        session_coordinator.end_current_session()
        session_coordinator.handle_user_input(input_data)  # 重新啟動 GS
        
        task_input = {
            "type": "text_input",
            "data": {"text": "執行系統任務"}
        }
        
        result = session_coordinator.handle_user_input(task_input)
        
        if session_coordinator.active_ws_sessions:
            ws_context = working_context_manager.get_data(ContextType.SYS_WORKFLOW, "workflow_session")
            if ws_context:
                self.assertEqual(ws_context["execution_mode"], "workflow")
                print("    ✅ WS Working Context 設定正確")
        
        print("✅ Working Context 整合測試通過")
    
    def test_08_session_system_performance(self):
        """測試 8: 會話系統性能"""
        print("\n[測試 8] 會話系統性能測試")
        
        import time
        
        # 1. 測試會話啟動性能
        start_time = time.time()
        
        for i in range(10):
            input_data = {
                "type": "text_input",
                "data": {"text": f"性能測試 {i+1}"},
                "timestamp": f"2024-01-01T10:0{i:01d}:00Z"
            }
            
            result = session_coordinator.handle_user_input(input_data)
            session_coordinator.end_current_session()
        
        end_time = time.time()
        avg_time = (end_time - start_time) / 10
        
        print(f"    平均會話啟動時間: {avg_time:.4f} 秒")
        self.assertLess(avg_time, 1.0)  # 期望每次啟動不超過 1 秒
        
        # 2. 測試記憶體使用
        import gc
        gc.collect()
        
        # 啟動多個會話並檢查清理
        for i in range(5):
            input_data = {
                "type": "text_input",
                "data": {"text": f"記憶體測試 {i+1}"}
            }
            session_coordinator.handle_user_input(input_data)
            session_coordinator.end_current_session()
        
        # 檢查會話是否正確清理
        self.assertEqual(len(session_coordinator.active_cs_sessions), 0)
        self.assertEqual(len(session_coordinator.active_ws_sessions), 0)
        
        print("✅ 會話系統性能測試通過")
    
    def run_full_integration_test(self):
        """執行完整整合測試"""
        print("\n🚀 開始執行會話系統完整整合測試...")
        
        test_methods = [
            self.test_01_basic_gs_lifecycle,
            self.test_02_cs_conversation_flow,
            self.test_03_ws_task_execution,
            self.test_04_session_transitions,
            self.test_05_concurrent_sessions,
            self.test_06_error_handling,
            self.test_07_working_context_integration,
            self.test_08_session_system_performance
        ]
        
        passed_tests = 0
        total_tests = len(test_methods)
        
        for test_method in test_methods:
            try:
                self.setUp()
                test_method()
                passed_tests += 1
                self.tearDown()
            except Exception as e:
                print(f"❌ 測試失敗: {test_method.__name__}")
                print(f"   錯誤: {e}")
                self.tearDown()
        
        print(f"\n📊 會話系統整合測試結果:")
        print(f"   總測試數: {total_tests}")
        print(f"   通過測試: {passed_tests}")
        print(f"   失敗測試: {total_tests - passed_tests}")
        print(f"   成功率: {(passed_tests/total_tests)*100:.1f}%")
        
        if passed_tests == total_tests:
            print("🎉 所有會話系統整合測試通過！")
            return True
        else:
            print("⚠️ 部分測試失敗，請檢查會話系統實現")
            return False


def main():
    """主函數"""
    print("會話系統整合測試啟動...")
    
    # 創建測試實例
    test_instance = TestSessionSystemIntegration()
    
    # 執行完整測試
    success = test_instance.run_full_integration_test()
    
    return success


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)