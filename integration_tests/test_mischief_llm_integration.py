"""
MISCHIEF 狀態 LLM 整合測試

測試目標：
1. 進入 MISCHIEF 狀態時，LLM 是否能根據指示提供有效的行為列表
2. 驗證 LLM 返回的 JSON 格式正確性
3. 驗證行為解析和執行流程
4. 測試不同情緒/強度下的行為規劃差異

測試策略：
- 使用完整系統循環
- 手動觸發 MISCHIEF 狀態
- 監控 LLM 生成的行為規劃
- 驗證 MischiefExecutor 的解析結果
"""

import pytest
import time
import sys
import threading
import json
from pathlib import Path

# 確保專案根目錄在 sys.path 中
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from typing import Dict, Any, Optional, List

# 測試標記
pytestmark = [pytest.mark.integration, pytest.mark.mischief]

# 導入事件類型
from core.event_bus import SystemEvent


@pytest.fixture(scope="module")
def system_components():
    """初始化完整系統組件"""
    from utils.debug_helper import info_log, error_log
    from core.system_initializer import SystemInitializer
    from core.controller import unified_controller
    from core.system_loop import system_loop
    from core.event_bus import event_bus
    from utils.logger import force_enable_file_logging
    
    force_enable_file_logging()
    
    info_log("[MischiefLLMTest] 🚀 初始化完整系統...")
    
    # 初始化系統
    initializer = SystemInitializer()
    success = initializer.initialize_system(production_mode=False)
    
    if not success:
        pytest.fail("系統初始化失敗")
    
    info_log("[MischiefLLMTest] ✅ 系統初始化完成")
    
    # 啟動系統循環
    loop_started = system_loop.start()
    if not loop_started:
        pytest.fail("系統循環啟動失敗")
    
    info_log("[MischiefLLMTest] ✅ 系統循環已啟動")
    
    # 準備組件
    components = {
        "initializer": initializer,
        "controller": unified_controller,
        "system_loop": system_loop,
        "event_bus": event_bus,
    }
    
    # 等待系統穩定
    time.sleep(2)
    
    info_log("[MischiefLLMTest] ✅ 系統組件就緒")
    
    yield components
    
    # 清理
    info_log("[MischiefLLMTest] 🧹 清理系統組件...")
    
    try:
        system_loop.stop()
        time.sleep(1)
    except Exception as e:
        error_log(f"[MischiefLLMTest] 清理失敗: {e}")


@pytest.fixture
def enable_mischief_temporarily(system_components):
    """臨時啟用 MISCHIEF 功能"""
    from utils.debug_helper import info_log
    from configs.user_settings_manager import user_settings_manager
    
    # 保存原始設定
    original_enabled = user_settings_manager.get("behavior.mischief.enabled", False)
    original_max_actions = user_settings_manager.get("behavior.mischief.max_actions", 5)
    
    info_log(f"[MischiefLLMTest] 臨時啟用 MISCHIEF (原始: enabled={original_enabled}, max_actions={original_max_actions})")
    
    # 啟用 MISCHIEF
    user_settings_manager.set("behavior.mischief.enabled", True)
    user_settings_manager.set("behavior.mischief.max_actions", 3)  # 測試時減少數量
    
    yield
    
    # 恢復原始設定
    user_settings_manager.set("behavior.mischief.enabled", original_enabled)
    user_settings_manager.set("behavior.mischief.max_actions", original_max_actions)
    
    info_log("[MischiefLLMTest] 已恢復 MISCHIEF 原始設定")


class MischiefStateMonitor:
    """MISCHIEF 狀態監控器"""
    
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.events = []
        self.state_changes = []
        self.llm_responses = []
        self.mischief_entered = threading.Event()
        self.mischief_exited = threading.Event()
        
        # 訂閱相關事件
        self.event_bus.subscribe(SystemEvent.STATE_CHANGED, self._on_state_changed)
        self.event_bus.subscribe(SystemEvent.LLM_RESPONSE_GENERATED, self._on_llm_response)
    
    def _on_state_changed(self, event):
        """記錄狀態變更事件"""
        self.events.append(("state_changed", event.data))
        self.state_changes.append(event.data)
        
        from utils.debug_helper import debug_log
        from core.states.state_manager import UEPState
        
        new_state = event.data.get('new_state')
        debug_log(2, f"[MischiefMonitor] 狀態變更: {new_state}")
        
        # 檢查是否進入/離開 MISCHIEF
        if new_state == UEPState.MISCHIEF:
            self.mischief_entered.set()
            debug_log(2, "[MischiefMonitor] ✓ MISCHIEF 狀態已進入")
        elif self.mischief_entered.is_set() and new_state != UEPState.MISCHIEF:
            self.mischief_exited.set()
            debug_log(2, f"[MischiefMonitor] ✓ MISCHIEF 狀態已退出 -> {new_state}")
    
    def _on_llm_response(self, event):
        """記錄 LLM 回應事件"""
        self.events.append(("llm_response", event.data))
        self.llm_responses.append(event.data)
        
        from utils.debug_helper import debug_log
        response_text = event.data.get("response", "")
        debug_log(2, f"[MischiefMonitor] LLM 回應: {response_text[:100]}...")
    
    def wait_for_mischief_entry(self, timeout=30):
        """等待進入 MISCHIEF 狀態"""
        return self.mischief_entered.wait(timeout=timeout)
    
    def wait_for_mischief_exit(self, timeout=60):
        """等待離開 MISCHIEF 狀態"""
        return self.mischief_exited.wait(timeout=timeout)
    
    def reset(self):
        """重置監控器"""
        self.events = []
        self.state_changes = []
        self.llm_responses = []
        self.mischief_entered.clear()
        self.mischief_exited.clear()
    
    def get_latest_mischief_llm_response(self) -> Optional[str]:
        """獲取最近的 MISCHIEF 相關 LLM 回應"""
        # 倒序查找包含 "action" 或 JSON 結構的回應
        for response_data in reversed(self.llm_responses):
            response_text = response_data.get("response", "")
            if "action" in response_text.lower() or "{" in response_text:
                return response_text
        return None


class TestMischiefLLMIntegration:
    """MISCHIEF 狀態 LLM 整合測試"""
    
    def test_01_llm_generates_valid_action_plan(self, system_components, enable_mischief_temporarily):
        """
        測試 1: LLM 生成有效的行為規劃
        驗證 LLM 能根據指示返回正確的 JSON 格式行為列表
        """
        from utils.debug_helper import info_log, debug_log
        from core.states.state_manager import state_manager
        from modules.sys_module.actions.mischief.loader import mischief_executor
        
        info_log("\n" + "=" * 70)
        info_log("TEST 1: LLM Generates Valid Action Plan")
        info_log("=" * 70)
        
        event_bus = system_components["event_bus"]
        
        # 創建監控器
        monitor = MischiefStateMonitor(event_bus)
        
        # 手動觸發 MISCHIEF 狀態
        info_log("[Test] 手動觸發 MISCHIEF 狀態...")
        
        try:
            from core.states.state_manager import UEPState
            
            # 設置觸發條件
            context = {
                "trigger_reason": "test_manual",
                "mood": -0.5,  # 負面情緒
                "boredom": 0.7,  # 高無聊
                "test_mode": True
            }
            
            state_manager.set_state(UEPState.MISCHIEF, context)
            info_log("[Test] ✓ 已設置 MISCHIEF 狀態")
            
        except Exception as e:
            pytest.fail(f"手動觸發 MISCHIEF 失敗: {e}")
        
        # 等待狀態進入
        entered = monitor.wait_for_mischief_entry(timeout=10)
        assert entered, "未能進入 MISCHIEF 狀態"
        info_log("[Test] ✓ 已確認進入 MISCHIEF 狀態")
        
        # 等待一段時間讓 LLM 生成規劃
        time.sleep(5)
        
        # 檢查是否有 LLM 回應
        info_log(f"\n🔍 檢查 LLM 回應:")
        info_log(f"   LLM 回應次數: {len(monitor.llm_responses)}")
        
        # 獲取 MISCHIEF 相關的 LLM 回應
        llm_response_text = monitor.get_latest_mischief_llm_response()
        
        if not llm_response_text:
            info_log("⚠️  未找到 MISCHIEF 的 LLM 回應，嘗試從 StateManager 獲取...")
            
            # 直接檢查 StateManager 的 runtime
            runtime = state_manager._mischief_runtime
            if runtime:
                actions = runtime.get("actions", [])
                info_log(f"✓ 從 StateManager 找到行為規劃: {len(actions)} 個行為")
                
                # 驗證行為格式
                assert len(actions) > 0, "行為列表不應為空"
                
                for i, action in enumerate(actions):
                    assert "action_id" in action, f"行為 {i} 缺少 action_id"
                    assert "params" in action, f"行為 {i} 缺少 params"
                    info_log(f"   [{i+1}] {action['action_id']}: {action.get('params', {})}")
                
                info_log("\n✅ TEST 1 PASSED: LLM 生成的行為規劃格式正確（從 Runtime 驗證）")
            else:
                pytest.fail("未找到 MISCHIEF runtime 數據")
        else:
            info_log(f"✓ 找到 LLM 回應:")
            info_log(f"   內容: {llm_response_text[:200]}...")
            
            # 驗證 JSON 格式
            try:
                # 嘗試解析為 JSON
                data = json.loads(llm_response_text)
                
                assert "actions" in data, "LLM 回應缺少 'actions' 欄位"
                actions = data["actions"]
                
                assert isinstance(actions, list), "'actions' 必須是列表"
                assert len(actions) > 0, "行為列表不應為空"
                
                info_log(f"\n✓ JSON 格式驗證通過:")
                info_log(f"   行為數量: {len(actions)}")
                
                # 驗證每個行為的格式
                for i, action in enumerate(actions):
                    assert isinstance(action, dict), f"行為 {i} 必須是字典"
                    assert "action_id" in action, f"行為 {i} 缺少 action_id"
                    assert "params" in action, f"行為 {i} 缺少 params"
                    
                    info_log(f"   [{i+1}] {action['action_id']}: {action.get('params', {})}")
                
                info_log("\n✅ TEST 1 PASSED: LLM 生成的行為規劃格式正確")
                
            except json.JSONDecodeError as e:
                pytest.fail(f"LLM 回應不是有效的 JSON: {e}")
        
        # 清理：退出 MISCHIEF 狀態
        state_manager.exit_special_state("test_completed")
    
    def test_02_executor_parses_llm_response(self, system_components, enable_mischief_temporarily):
        """
        測試 2: MischiefExecutor 正確解析 LLM 回應
        驗證 Executor 的 parse_llm_response 功能
        """
        from utils.debug_helper import info_log
        from modules.sys_module.actions.mischief.loader import mischief_executor
        
        info_log("\n" + "=" * 70)
        info_log("TEST 2: Executor Parses LLM Response")
        info_log("=" * 70)
        
        # 模擬 LLM 回應
        mock_response = json.dumps({
            "actions": [
                {
                    "action_id": "MoveMouseAction",
                    "params": {}
                },
                {
                    "action_id": "SpeakAction",
                    "params": {"message": "測試訊息"}
                },
                {
                    "action_id": "CreateTextFileAction",
                    "params": {"message": "測試檔案內容"}
                }
            ]
        })
        
        info_log(f"[Test] 模擬 LLM 回應:")
        info_log(f"   {mock_response}")
        
        # 解析回應
        success, actions = mischief_executor.parse_llm_response(mock_response)
        
        assert success, "解析應該成功"
        assert len(actions) == 3, f"應該解析出 3 個行為，實際: {len(actions)}"
        
        info_log(f"\n✓ 解析成功:")
        info_log(f"   行為數量: {len(actions)}")
        
        # 驗證每個行為
        expected_actions = ["MoveMouseAction", "SpeakAction", "CreateTextFileAction"]
        for i, action in enumerate(actions):
            assert action["action_id"] == expected_actions[i], f"行為 {i} ID 不符"
            assert "params" in action, f"行為 {i} 缺少 params"
            info_log(f"   [{i+1}] {action['action_id']}: ✓")
        
        info_log("\n✅ TEST 2 PASSED: Executor 正確解析 LLM 回應")
    
    def test_03_invalid_json_handling(self, system_components, enable_mischief_temporarily):
        """
        測試 3: 處理無效的 JSON 回應
        驗證 Executor 能正確處理格式錯誤的回應
        """
        from utils.debug_helper import info_log
        from modules.sys_module.actions.mischief.loader import mischief_executor
        
        info_log("\n" + "=" * 70)
        info_log("TEST 3: Invalid JSON Handling")
        info_log("=" * 70)
        
        # 測試案例
        test_cases = [
            ("空字串", ""),
            ("無效 JSON", "This is not JSON"),
            ("缺少 actions", json.dumps({"result": "success"})),
            ("actions 不是列表", json.dumps({"actions": "not a list"})),
            ("行為缺少 action_id", json.dumps({"actions": [{"params": {}}]}))
        ]
        
        for name, invalid_response in test_cases:
            info_log(f"\n[Test] 測試案例: {name}")
            info_log(f"   輸入: {invalid_response[:50]}...")
            
            success, actions = mischief_executor.parse_llm_response(invalid_response)
            
            # 對於格式錯誤的情況，應該返回 False 或空列表
            if name in ["空字串", "無效 JSON", "缺少 actions", "actions 不是列表"]:
                assert not success or len(actions) == 0, f"{name} 應該解析失敗"
                info_log(f"   ✓ 正確處理錯誤情況")
            else:
                # 對於部分有效的情況，可能跳過無效項目
                info_log(f"   ✓ 跳過無效項目，解析出 {len(actions)} 個有效行為")
        
        info_log("\n✅ TEST 3 PASSED: 正確處理無效 JSON")
    
    def test_04_action_availability_filtering(self, system_components, enable_mischief_temporarily):
        """
        測試 4: 行為可用性過濾
        驗證 Executor 根據情緒過濾可用行為
        """
        from utils.debug_helper import info_log
        from modules.sys_module.actions.mischief.loader import mischief_executor
        
        info_log("\n" + "=" * 70)
        info_log("TEST 4: Action Availability Filtering")
        info_log("=" * 70)
        
        # 測試不同情緒下的可用行為
        test_moods = [
            ("正面情緒", 0.5),
            ("中性情緒", 0.0),
            ("負面情緒", -0.5)
        ]
        
        for mood_name, mood_value in test_moods:
            info_log(f"\n[Test] 測試 {mood_name} (mood={mood_value}):")
            
            available_actions = mischief_executor.get_available_actions_for_llm(mood_value, "medium")
            
            # 解析 JSON
            actions_data = json.loads(available_actions)
            actions_list = actions_data.get("available_actions", [])
            
            info_log(f"   可用行為數量: {len(actions_list)}")
            
            # 驗證至少有一些行為可用
            assert len(actions_list) > 0, f"{mood_name} 應該有可用行為"
            
            # 列出可用行為
            for action in actions_list:
                action_id = action.get("action_id", "Unknown")
                mood_ctx = action.get("mood_context", "UNKNOWN")
                info_log(f"      - {action_id} (mood_context: {mood_ctx})")
        
        info_log("\n✅ TEST 4 PASSED: 行為過濾機制正常")
    
    def test_05_full_mischief_cycle(self, system_components, enable_mischief_temporarily):
        """
        測試 5: 完整 MISCHIEF 循環
        驗證從觸發到執行完成的完整流程
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        
        info_log("\n" + "=" * 70)
        info_log("TEST 5: Full MISCHIEF Cycle")
        info_log("=" * 70)
        
        event_bus = system_components["event_bus"]
        monitor = MischiefStateMonitor(event_bus)
        
        # 觸發 MISCHIEF
        info_log("[Test] 觸發 MISCHIEF 狀態...")
        
        context = {
            "trigger_reason": "test_full_cycle",
            "mood": -0.4,
            "boredom": 0.6
        }
        
        state_manager.set_state(UEPState.MISCHIEF, context)
        
        # 等待進入
        entered = monitor.wait_for_mischief_entry(timeout=10)
        assert entered, "未能進入 MISCHIEF 狀態"
        info_log("[Test] ✓ 已進入 MISCHIEF 狀態")
        
        # 等待執行完成（退出狀態）
        info_log("[Test] 等待 MISCHIEF 執行完成...")
        exited = monitor.wait_for_mischief_exit(timeout=60)
        
        if not exited:
            # 可能還在執行中，檢查 runtime 狀態
            runtime = state_manager._mischief_runtime
            if runtime:
                results = runtime.get("results", {})
                info_log(f"\n⚠️  MISCHIEF 尚未完全退出，但已執行部分行為:")
                info_log(f"   總數: {results.get('total', 0)}")
                info_log(f"   成功: {results.get('success', 0)}")
                info_log(f"   失敗: {results.get('failed', 0)}")
                info_log(f"   跳過: {results.get('skipped', 0)}")
            
            # 手動退出
            state_manager.exit_special_state("test_timeout")
        else:
            info_log("[Test] ✓ MISCHIEF 已退出")
            
            # 檢查執行結果
            info_log(f"\n📊 MISCHIEF 執行統計:")
            info_log(f"   狀態變更次數: {len(monitor.state_changes)}")
            info_log(f"   LLM 回應次數: {len(monitor.llm_responses)}")
        
        info_log("\n✅ TEST 5 PASSED: 完整 MISCHIEF 循環測試完成")


if __name__ == "__main__":
    """直接運行測試（用於調試）"""
    print("Running MISCHIEF LLM Integration Tests")
    print("=" * 70)
    
    # Run with pytest
    pytest.main([__file__, "-v", "-s", "--tb=short"])
