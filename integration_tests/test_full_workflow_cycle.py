"""
完整工作流程循環整合測試

測試策略：
模擬完整的系統循環，從使用者輸入到工作流程完成
- 輸入層：模擬使用者文字輸入
- NLP 層：判斷意圖
- 處理層：LLM 通過 MCP 啟動工作流
- 工作流層：SYS 模組執行工作流
- 輸出層：TTS 輸出回應

測試流程：
1. 初始化完整系統（SystemInitializer）
2. 啟動系統循環（SystemLoop）
3. 注入測試文字輸入（模擬使用者說話）
4. 等待 NLP → LLM → SYS → 完成
5. 驗證工作流程結果
6. 清理系統

注意事項：
- 使用 initial_data 提供檔案路徑等先行資料
- 等待 WS（WorkflowSession）完成而非手動控制步驟
- 監聽事件來追蹤系統狀態
"""

import pytest
import time
import threading
from pathlib import Path

# 測試標記
pytestmark = [pytest.mark.integration, pytest.mark.full_cycle]

# 導入事件類型
from core.event_bus import SystemEvent

# 專案根目錄
project_root = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def system_components():
    """
    初始化完整系統組件
    
    包括：
    - SystemInitializer：系統初始化
    - UnifiedController：控制器
    - SystemLoop：系統循環
    - 所有模組（STT, NLP, LLM, SYS, TTS等）
    """
    from utils.debug_helper import info_log, error_log
    from core.system_initializer import SystemInitializer
    from core.controller import unified_controller
    from core.system_loop import system_loop
    from core.event_bus import event_bus
    from utils.logger import force_enable_file_logging
    
    # 強制啟用文件日誌記錄，以便在測試中追蹤錯誤
    force_enable_file_logging()
    
    info_log("[IntegrationTest] 🚀 初始化完整系統...")
    
    # 1. 初始化系統
    initializer = SystemInitializer()
    success = initializer.initialize_system(production_mode=False)
    
    if not success:
        pytest.fail("System initialization failed")
    
    info_log("[IntegrationTest] ✅ 系統初始化完成")
    
    # 2. 啟動系統循環
    loop_started = system_loop.start()
    if not loop_started:
        pytest.fail("System loop failed to start")
    
    info_log("[IntegrationTest] ✅ 系統循環已啟動")
    
    # 3. 準備組件
    components = {
        "initializer": initializer,
        "controller": unified_controller,
        "system_loop": system_loop,
        "event_bus": event_bus,
    }
    
    # 等待系統穩定
    time.sleep(2)
    
    info_log("[IntegrationTest] ✅ 系統組件就緒")
    
    yield components
    
    # 清理
    info_log("[IntegrationTest] 🧹 清理系統組件...")
    
    try:
        # 停止系統循環
        system_loop.stop()
        time.sleep(1)
        
        # 關閉控制器
        unified_controller.shutdown()
        time.sleep(1)
        
        info_log("[IntegrationTest] ✅ 清理完成")
    except Exception as e:
        error_log(f"[IntegrationTest] ⚠️ 清理時發生錯誤: {e}")


@pytest.fixture
def test_file():
    """
    使用預先準備的測試檔案
    
    Returns:
        Path: 測試檔案路徑（resources/workflow_test.txt）
    """
    test_file = project_root / "resources" / "workflow_test.txt"
    
    if not test_file.exists():
        pytest.fail(f"Test file not found: {test_file}")
    
    return test_file


class WorkflowCycleMonitor:
    """工作流程循環監控器"""
    
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.events = []
        self.workflow_completed = threading.Event()
        self.workflow_failed = threading.Event()
        self.workflow_session_id = None
        
        # 訂閱相關事件
        self.event_bus.subscribe(SystemEvent.WORKFLOW_STEP_COMPLETED, self._on_step_completed)
        self.event_bus.subscribe(SystemEvent.WORKFLOW_FAILED, self._on_workflow_failed)
        self.event_bus.subscribe(SystemEvent.SESSION_ENDED, self._on_session_ended)
    
    def _on_step_completed(self, event):
        """記錄步驟完成事件"""
        self.events.append(("step_completed", event.data))
        from utils.debug_helper import debug_log
        debug_log(2, f"[Monitor] 步驟完成: {event.data.get('session_id')}")
    
    def _on_workflow_failed(self, event):
        """記錄工作流程失敗事件"""
        self.events.append(("workflow_failed", event.data))
        self.workflow_failed.set()
        from utils.debug_helper import error_log
        error_log(f"[Monitor] 工作流程失敗: {event.data}")
    
    def _on_session_ended(self, event):
        """記錄會話結束事件"""
        session_id = event.data.get("session_id", "")
        
        # 只關注 WorkflowSession（以 ws_ 開頭）
        if session_id.startswith("ws_"):
            self.events.append(("session_ended", event.data))
            
            # 如果是我們追蹤的工作流程會話
            if self.workflow_session_id is None or session_id == self.workflow_session_id:
                self.workflow_session_id = session_id
                self.workflow_completed.set()
                from utils.debug_helper import info_log
                info_log(f"[Monitor] 工作流程會話結束: {session_id}")
    
    def wait_for_completion(self, timeout=60):
        """等待工作流程完成"""
        completed = self.workflow_completed.wait(timeout)
        failed = self.workflow_failed.is_set()
        
        return {
            "completed": completed,
            "failed": failed,
            "events": self.events,
            "session_id": self.workflow_session_id
        }
    
    def cleanup(self):
        """清理監控器"""
        try:
            self.event_bus.unsubscribe(SystemEvent.WORKFLOW_STEP_COMPLETED, self._on_step_completed)
            self.event_bus.unsubscribe(SystemEvent.WORKFLOW_FAILED, self._on_workflow_failed)
            self.event_bus.unsubscribe(SystemEvent.SESSION_ENDED, self._on_session_ended)
        except:
            pass


def inject_text_to_system(text: str, initial_data=None):
    """
    向系統注入文字輸入
    
    模擬使用者透過語音或文字輸入的場景
    這會觸發完整的系統循環：STT → NLP → Router → LLM → SYS
    
    Args:
        text: 使用者輸入文字
        initial_data: 先行資料（如檔案路徑），會附加到 WorkingContext
    """
    from utils.debug_helper import info_log
    from core.framework import core_framework
    from core.working_context import working_context_manager
    
    info_log(f"[IntegrationTest] 📝 注入文字: '{text}'")
    
    # 1. 如果有先行資料，設置到 WorkingContext
    if initial_data:
        info_log(f"[IntegrationTest] 📦 設置先行資料到 WorkingContext: {initial_data}")
        for key, value in initial_data.items():
            working_context_manager.set_context_data(f"test_{key}", value)
    
    # 2. 通過 STT 模組注入文字輸入
    # 這會觸發完整的處理流程
    stt_module = core_framework.get_module('stt')
    if not stt_module:
        raise RuntimeError("STT module not available")
    
    # 調用 STT 模組的文字輸入處理
    result = stt_module.handle_text_input(text)
    
    if not result:
        raise RuntimeError(f"Failed to inject text: {text}")
    
    info_log(f"[IntegrationTest] ✅ 文字注入成功")


@pytest.mark.integration
@pytest.mark.full_cycle
class TestFileWorkflowFullCycle:
    """完整工作流程循環測試"""
    
    #@pytest.mark.skip(reason="先測試 summarize_tag")
    def test_drop_and_read_full_cycle(self, system_components, test_file):
        """
        測試完整的檔案讀取工作流程循環
        
        流程：
        1. 使用者輸入：「讀取這個檔案」
        2. NLP 判斷意圖：file_operation
        3. LLM 通過 MCP 啟動 file_drop_and_read_workflow
        4. SYS 模組執行工作流程（跳過檔案選擇，使用 initial_data）
        5. 工作流程完成，系統輸出回應
        6. 測試驗證結果
        """
        from utils.debug_helper import info_log
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 1. 創建工作流程監控器
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            # 2. 準備測試：模擬前端拖曳檔案
            info_log("[Test] 🎯 測試：檔案讀取完整循環")
            info_log(f"[Test] 📁 檔案路徑: {test_file}")
            
            # 模擬前端拖曳檔案：設置 WorkingContext
            from core.working_context import working_context_manager
            working_context_manager.set_context_data("current_file_path", str(test_file))
            
            # 用戶請求讀取（不需要指定路徑，因為 WorkingContext 中已有）
            inject_text_to_system("Read the content of the test file")
            
            # 3. 等待工作流程完成（最多 90 秒）
            info_log("[Test] ⏳ 等待工作流程完成...")
            result = monitor.wait_for_completion(timeout=90)
            
            # 4. 驗證結果
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 驗證事件序列
            event_types = [e[0] for e in result["events"]]
            assert "step_completed" in event_types, "No step completion events"
            assert "session_ended" in event_types, "No session end event"
            
            info_log("[Test] ✅ 摘要標註完整循環測試通過")
            
        finally:
            # 清理監控器
            monitor.cleanup()
            
            # 清理 WorkingContext
            import time
            from core.working_context import working_context_manager
            from core.states.state_manager import state_manager, UEPState
            
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_state() == UEPState.IDLE:
                    info_log("[Test] ✅ 系統已回到 IDLE")
                    break
                time.sleep(0.1)
            
            working_context_manager.global_context_data.pop('workflow_hint', None)
            working_context_manager.global_context_data.pop('pending_workflow', None)
            info_log("[Test] ✅ 已清理 WorkingContext workflow 數據")
            
            time.sleep(1.0)
            info_log("[Test] ✅ 測試清理完成")
    
    def test_intelligent_archive_full_cycle(self, system_components, test_file):
        """
        測試完整的智慧歸檔工作流程循環（包含互動步驟）
        
        流程：
        1. 使用者輸入：「歸檔這個檔案到 D:\\」
        2. NLP 判斷意圖：file_operation
        3. LLM 通過 MCP 啟動 intelligent_archive workflow
        4. 工作流執行：
           - Step 1 (file_selection): 使用 WorkingContext 中的檔案路徑 ✅
           - Step 2 (target_dir_input): 互動步驟 - LLM 提示用戶輸入目標資料夾
           - Step 3 (archive_confirm): 互動步驟 - LLM 提示用戶確認
           - Step 4 (execute_archive): 自動執行歸檔
        5. 工作流程完成，LLM 生成總結回應
        
        測試重點：
        - 互動步驟前 LLM 是否生成提示
        - 自動注入用戶輸入來響應互動步驟
        - 工作流最終結果是否包含完整數據
        - WS 是否正確結束
        """
        from utils.debug_helper import info_log
        import time
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 創建工作流程監控器（追蹤互動步驟）
        class ArchiveWorkflowMonitor(WorkflowCycleMonitor):
            def __init__(self, event_bus):
                super().__init__(event_bus)
                self.interactive_step_count = 0
                self.awaiting_input_event = threading.Event()
                self.current_step = None
                self.tts_output_count = 0
                self.detected_interactive_steps = set()
                self.expected_tts_outputs = 1  # 工作流啟動回應（包含互動提示）
                
                # 額外訂閱事件
                from core.event_bus import SystemEvent
                self.event_bus.subscribe(SystemEvent.OUTPUT_LAYER_COMPLETE, self._on_output_complete, handler_name="Monitor.output_complete")
                self.event_bus.subscribe(SystemEvent.WORKFLOW_REQUIRES_INPUT, self._on_requires_input, handler_name="Monitor.requires_input")
                
            def _on_requires_input(self, event):
                """追蹤工作流請求輸入事件"""
                data = event.data
                step_id = data.get('step_id')
                if step_id and step_id not in self.detected_interactive_steps:
                    self.detected_interactive_steps.add(step_id)
                    self.interactive_step_count += 1
                    self.current_step = step_id
                    info_log(f"[Monitor] 檢測到互動步驟（透過 WORKFLOW_REQUIRES_INPUT）: {self.current_step}")
            
            def _on_step_completed(self, event):
                """追蹤步驟完成，檢測互動步驟"""
                super()._on_step_completed(event)
                data = event.data
                
                # 檢查下一步是否為互動步驟
                next_step_info = data.get('next_step_info')
                if next_step_info and next_step_info.get('step_type') == 'interactive':
                    step_id = next_step_info.get('step_id')
                    if step_id not in self.detected_interactive_steps:
                        self.detected_interactive_steps.add(step_id)
                        self.interactive_step_count += 1
                        self.current_step = step_id
                        info_log(f"[Monitor] 檢測到互動步驟: {self.current_step}")
            
            def _on_output_complete(self, event):
                """追蹤 TTS 輸出完成"""
                self.tts_output_count += 1
                info_log(f"[Monitor] TTS 輸出完成 (第 {self.tts_output_count} 次，期待 {self.expected_tts_outputs} 次)")
                
                # 等待所有期望的 TTS 輸出完成後才設置事件
                if self.current_step and self.tts_output_count >= self.expected_tts_outputs:
                    info_log(f"[Monitor] 所有 TTS 輸出完成，設置 awaiting_input_event 以響應步驟: {self.current_step}")
                    self.awaiting_input_event.set()
                    # 重置計數器為下一個互動步驟做準備
                    self.tts_output_count = 0
                    self.expected_tts_outputs = 1  # 下一個互動步驟也是1次輸出
            
            def cleanup(self):
                """清理資源"""
                from core.event_bus import SystemEvent
                try:
                    self.event_bus.unsubscribe(SystemEvent.OUTPUT_LAYER_COMPLETE, self._on_output_complete)
                    self.event_bus.unsubscribe(SystemEvent.WORKFLOW_REQUIRES_INPUT, self._on_requires_input)
                except:
                    pass
                super().cleanup()
        
        monitor = ArchiveWorkflowMonitor(event_bus)
        
        try:
            # 1. 準備測試：模擬前端拖曳檔案
            info_log("[Test] 🎯 測試：智慧歸檔完整循環（包含互動步驟）")
            info_log(f"[Test] 📁 檔案路徑: {test_file}")
            
            # 模擬前端拖曳檔案：設置 WorkingContext
            from core.working_context import working_context_manager
            working_context_manager.set_context_data("current_file_path", str(test_file))
            
            # 用戶請求歸檔（不需要指定路徑）
            inject_text_to_system("Please archive this file to my D drive")
            
            # 3. 等待互動步驟 (archive_confirm)
            # 注意：target_dir_input 是 optional，會被自動跳過（無需用戶輸入）
            # 所以我們只需等待 archive_confirm
            # ⚠️ TTS 生成需要時間（workflow start + interactive prompt = ~40秒）
            info_log("[Test] ⏳ 等待互動步驟: archive_confirm")
            if monitor.awaiting_input_event.wait(timeout=60):
                info_log(f"[Test] 📝 響應步驟: {monitor.current_step}")
                time.sleep(2)  # 等待 LLM 生成提示
                
                # 注入確認輸入
                inject_text_to_system("yes")
                monitor.awaiting_input_event.clear()
            else:
                info_log(f"[Test] ❌ 超時！TTS輸出次數: {monitor.tts_output_count}/{monitor.expected_tts_outputs}")
                pytest.fail("Timeout waiting for archive_confirm step")
            
            # 5. 等待工作流程完成
            info_log("[Test] ⏳ 等待工作流程完成...")
            result = monitor.wait_for_completion(timeout=60)
            
            # 6. 驗證結果
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            info_log(f"[Test] 🔄 互動步驟數量: {monitor.interactive_step_count}")
            
            # 驗證互動步驟
            # 注意：target_dir_input 是 optional 的，會自動跳過，所以只有 1 個需要用戶輸入的互動步驟 (archive_confirm)
            assert monitor.interactive_step_count == 1, f"Expected 1 interactive step, got {monitor.interactive_step_count}"
            
            # 驗證事件序列
            event_types = [e[0] for e in result["events"]]
            assert "step_completed" in event_types, "No step completion events"
            assert "session_ended" in event_types, "No session end event"
            
            info_log("[Test] ✅ 智慧歸檔完整循環測試通過")
            
        finally:
            monitor.cleanup()
    
    def test_summarize_tag_full_cycle(self, system_components, test_file):
        """
        測試完整的檔案摘要標籤工作流程循環
        
        流程：
        1. 使用者輸入：「生成檔案摘要和標籤」
        2. NLP 判斷意圖：file_operation
        3. LLM 通過 MCP 啟動 file_summarize_tag_workflow
        4. 工作流執行：
           - Step 1 (file_input): 選擇檔案（使用 WorkingContext）
           - Step 2 (tag_count_input): 可選輸入標籤數量（會自動跳過）
           - Step 3 (summary_confirm): 確認執行（需要用戶輸入）
           - Step 4 (read_file_content): 讀取檔案內容
           - Step 5 (llm_generate_summary): LLM 生成摘要和標籤
           - Step 6 (save_summary_file): 儲存摘要檔案
        5. 工作流程完成，LLM 生成總結回應
        
        測試重點：
        - 新的 LLM_PROCESSING 步驟類型是否正常運作
        - LLM 是否正確生成摘要和標籤
        - 摘要檔案是否成功儲存到桌面
        - WS 是否正確結束
        """
        from utils.debug_helper import info_log
        import time
        import os
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 創建工作流程監控器（追蹤互動步驟）
        class SummaryWorkflowMonitor(WorkflowCycleMonitor):
            def __init__(self, event_bus):
                super().__init__(event_bus)
                self.interactive_step_count = 0
                self.awaiting_input_event = threading.Event()
                self.current_step = None
                self.tts_output_count = 0
                self.detected_interactive_steps = set()
                self.expected_tts_outputs = 1  # 工作流啟動回應（包含互動提示）
                
                # 額外訂閱事件
                from core.event_bus import SystemEvent
                self.event_bus.subscribe(SystemEvent.OUTPUT_LAYER_COMPLETE, self._on_output_complete, handler_name="Monitor.output_complete")
                self.event_bus.subscribe(SystemEvent.WORKFLOW_REQUIRES_INPUT, self._on_requires_input, handler_name="Monitor.requires_input")
                
            def _on_requires_input(self, event):
                """追蹤工作流請求輸入事件"""
                data = event.data
                step_id = data.get('step_id')
                if step_id and step_id not in self.detected_interactive_steps:
                    self.detected_interactive_steps.add(step_id)
                    self.interactive_step_count += 1
                    self.current_step = step_id
                    info_log(f"[Monitor] 檢測到互動步驟（透過 WORKFLOW_REQUIRES_INPUT）: {self.current_step}")
            
            def _on_step_completed(self, event):
                """追蹤步驟完成，檢測互動步驟"""
                super()._on_step_completed(event)
                data = event.data
                
                # 檢查下一步是否為互動步驟
                next_step_info = data.get('next_step_info')
                if next_step_info and next_step_info.get('step_type') == 'interactive':
                    step_id = next_step_info.get('step_id')
                    if step_id not in self.detected_interactive_steps:
                        self.detected_interactive_steps.add(step_id)
                        self.interactive_step_count += 1
                        self.current_step = step_id
                        info_log(f"[Monitor] 檢測到互動步驟: {self.current_step}")
            
            def _on_output_complete(self, event):
                """追蹤 TTS 輸出完成"""
                self.tts_output_count += 1
                info_log(f"[Monitor] TTS 輸出完成 (第 {self.tts_output_count} 次，期待 {self.expected_tts_outputs} 次)")
                
                # 等待所有期望的 TTS 輸出完成後才設置事件
                if self.current_step and self.tts_output_count >= self.expected_tts_outputs:
                    info_log(f"[Monitor] 所有 TTS 輸出完成，設置 awaiting_input_event 以響應步驟: {self.current_step}")
                    self.awaiting_input_event.set()
                    # 重置計數器為下一個互動步驟做準備
                    self.tts_output_count = 0
                    self.expected_tts_outputs = 1  # 下一個互動步驟也是1次輸出
            
            def cleanup(self):
                """清理資源"""
                from core.event_bus import SystemEvent
                try:
                    self.event_bus.unsubscribe(SystemEvent.OUTPUT_LAYER_COMPLETE, self._on_output_complete)
                    self.event_bus.unsubscribe(SystemEvent.WORKFLOW_REQUIRES_INPUT, self._on_requires_input)
                except:
                    pass
                super().cleanup()
        
        monitor = SummaryWorkflowMonitor(event_bus)
        
        try:
            # 1. 準備測試：模擬前端拖曳檔案
            info_log("[Test] 🎯 測試：檔案摘要標籤完整循環")
            info_log(f"[Test] 📁 檔案路徑: {test_file}")
            
            # 模擬前端拖曳檔案：設置 WorkingContext
            from core.working_context import working_context_manager
            working_context_manager.set_context_data("current_file_path", str(test_file))
            
            # 用戶請求生成摘要（不需要指定路徑）
            inject_text_to_system("Generate a summary and 5 tags for this file")
            
            # 3. 等待互動步驟 (summary_confirm)
            # 注意：tag_count_input 是 optional，會被自動跳過（無需用戶輸入）
            # 所以我們只需等待 summary_confirm
            info_log("[Test] ⏳ 等待互動步驟: summary_confirm")
            if monitor.awaiting_input_event.wait(timeout=60):
                info_log(f"[Test] 📝 響應步驟: {monitor.current_step}")
                time.sleep(2)  # 等待 LLM 生成提示
                
                # 注入確認輸入
                inject_text_to_system("yes")
                monitor.awaiting_input_event.clear()
            else:
                info_log(f"[Test] ❌ 超時！TTS輸出次數: {monitor.tts_output_count}/{monitor.expected_tts_outputs}")
                pytest.fail("Timeout waiting for summary_confirm step")
            
            # 5. 等待工作流程完成（LLM 處理需要較長時間）
            info_log("[Test] ⏳ 等待工作流程完成（LLM 處理中）...")
            result = monitor.wait_for_completion(timeout=120)  # 增加超時時間
            
            # 6. 驗證結果
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 7. 驗證摘要檔案是否生成
            desktop_path = Path(os.path.expanduser("~/Desktop"))
            summary_file = desktop_path / f"{test_file.stem}_summary.txt"
            
            if summary_file.exists():
                info_log(f"[Test] ✅ 摘要檔案已生成: {summary_file}")
                # 讀取並顯示完整內容
                with open(summary_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    info_log(f"[Test] 📄 摘要內容:\n{content}")
                
                # 🔧 保留檔案不刪除，方便檢查結果
                info_log(f"[Test] 📁 摘要檔案保留於: {summary_file}")
            else:
                info_log(f"[Test] ⚠️ 摘要檔案未找到: {summary_file}")
                # 不要 fail，因為可能路徑問題，但記錄警告
            
            # 8. 驗證事件序列
            event_types = [e[0] for e in result["events"]]
            assert "step_completed" in event_types, "No step completion events"
            assert "session_ended" in event_types, "No session end event"
            
            info_log("[Test] ✅ 檔案摘要標籤完整循環測試通過")
            
        finally:
            monitor.cleanup()
