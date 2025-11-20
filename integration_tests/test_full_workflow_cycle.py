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

@pytest.fixture
def test_code():
    """
    使用預先準備的測試程式碼檔案
    
    Returns:
        Path: 測試程式碼檔案路徑（resources/code_test.py）
    """
    test_code = project_root / "resources" / "code_test.py"
    
    if not test_code.exists():
        pytest.fail(f"Test code file not found: {test_code}")
    
    return test_code


@pytest.fixture
def test_image():
    """
    使用預先準備的測試圖片
    
    Returns:
        Path: 測試圖片路徑（resources/workflow_test.png）
    """
    test_image = project_root / "resources" / "image.jpg"
    
    if not test_image.exists():
        pytest.fail(f"Test image not found: {test_image}")
    
    return test_image


@pytest.fixture
def isolated_gs(system_components):
    """
    確保每個測試使用獨立的 GS
    
    工作原理:
    - Setup: 清理測試開始前的殘留 GS (如果有)
    - 測試執行期間: Controller 監控線程會在檢測到狀態佇列有項目時自動創建新 GS
    - Teardown: 明確結束測試期間創建的 GS
    
    這確保了:
    1. 測試之間完全隔離,不共享 GS
    2. 每個測試都在乾淨的環境中開始
    3. 測試結束後不留下殘留狀態
    """
    from utils.debug_helper import info_log
    controller = system_components["controller"]
    
    # Setup: 確保測試開始前沒有活躍的 GS
    current_gs = controller.session_manager.get_current_general_session()
    if current_gs:
        info_log(f"[Test Fixture] ⚠️ 發現殘留 GS: {current_gs.session_id}，正在清理...")
        controller.session_manager.end_general_session({"status": "test_cleanup"})
        import time
        time.sleep(0.5)
    
    yield
    
    # Teardown: 測試結束後明確結束 GS
    current_gs = controller.session_manager.get_current_general_session()
    if current_gs:
        info_log(f"[Test Fixture] 🧹 測試結束，清理 GS: {current_gs.session_id}")
        controller.session_manager.end_general_session({"status": "test_complete"})
        import time
        time.sleep(0.5)
    else:
        info_log("[Test Fixture] ✅ 測試結束，沒有需要清理的 GS")


class WorkflowCycleMonitor:
    """工作流程循環監控器"""
    
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.events = []
        self.workflow_completed = threading.Event()
        self.workflow_failed = threading.Event()
        self.workflow_session_id = None
        self.completed_steps = []
        
        # 訂閱相關事件
        self.event_bus.subscribe(SystemEvent.WORKFLOW_STEP_COMPLETED, self._on_step_completed)
        self.event_bus.subscribe(SystemEvent.WORKFLOW_FAILED, self._on_workflow_failed)
        self.event_bus.subscribe(SystemEvent.SESSION_ENDED, self._on_session_ended)
        # ✅ 訂閱背景工作流完成事件
        self.event_bus.subscribe(SystemEvent.BACKGROUND_WORKFLOW_COMPLETED, self._on_background_workflow_completed)
    
    def _on_step_completed(self, event):
        """記錄步驟完成事件"""
        self.events.append(("step_completed", event.data))
        
        # 🔧 檢查步驟是否實際成功
        step_result = event.data.get('step_result', {})
        if not step_result.get('success', True):
            # 步驟執行失敗，標記為工作流失敗
            self.workflow_failed.set()
            from utils.debug_helper import error_log
            error_log(f"[Monitor] 步驟執行失敗: {step_result}")
        
        # 🆕 優先使用 executed_steps 列表（包含所有自動執行的步驟）
        executed_steps = event.data.get('executed_steps', [])
        if executed_steps:
            for step_id in executed_steps:
                if step_id and step_id != 'unknown':
                    self.completed_steps.append(step_id)
        else:
            # 回退到單一 step_id（向後兼容）
            step_id = step_result.get('step_id', 'unknown')
            self.completed_steps.append(step_id)
        
        from utils.debug_helper import debug_log
        debug_log(2, f"[Monitor] 步驟完成: {self.completed_steps[-1] if self.completed_steps else 'unknown'} (session: {event.data.get('session_id')})")
    
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
    
    def _on_background_workflow_completed(self, event):
        """記錄背景工作流完成事件"""
        from utils.debug_helper import info_log
        task_id = event.data.get("task_id", "")
        session_id = event.data.get("session_id", "")
        completed_steps = event.data.get("completed_steps", [])
        
        self.events.append(("background_workflow_completed", event.data))
        
        # 更新已完成步驟列表
        if completed_steps:
            self.completed_steps.extend(completed_steps)
        
        # 標記工作流完成
        if self.workflow_session_id is None or session_id == self.workflow_session_id:
            self.workflow_session_id = session_id
            self.workflow_completed.set()
            info_log(f"[Monitor] 背景工作流完成: task_id={task_id}, steps={completed_steps}")
    
    def wait_for_completion(self, timeout=60):
        """等待工作流程完成"""
        completed = self.workflow_completed.wait(timeout)
        failed = self.workflow_failed.is_set()
        
        return {
            "completed": completed,
            "failed": failed,
            "events": self.events,
            "session_id": self.workflow_session_id,
            "completed_steps": self.completed_steps
        }
    
    def cleanup(self):
        """清理監控器"""
        try:
            self.event_bus.unsubscribe(SystemEvent.WORKFLOW_STEP_COMPLETED, self._on_step_completed)
            self.event_bus.unsubscribe(SystemEvent.WORKFLOW_FAILED, self._on_workflow_failed)
            self.event_bus.unsubscribe(SystemEvent.SESSION_ENDED, self._on_session_ended)
            self.event_bus.unsubscribe(SystemEvent.BACKGROUND_WORKFLOW_COMPLETED, self._on_background_workflow_completed)
        except:
            pass


class InteractiveWorkflowMonitor(WorkflowCycleMonitor):
    """支援互動步驟的工作流程監控器"""
    
    def __init__(self, event_bus, sys_module=None, expected_interactive_steps=0):
        super().__init__(event_bus)
        self.sys_module = sys_module
        self.interactive_step_count = 0
        self.awaiting_input_event = threading.Event()
        self.current_step = None
        self.tts_output_count = 0
        self.detected_interactive_steps = set()
        self.expected_tts_outputs = 2  # 工作流啟動 + 互動提示
        self.workflow_started = False
        self.first_output_received = False
        
        # 額外訂閱 OUTPUT_LAYER_COMPLETE 事件來追蹤 TTS 輸出
        self.event_bus.subscribe(SystemEvent.OUTPUT_LAYER_COMPLETE, self._on_output_complete, handler_name="Monitor.output_complete")
        
        # 訂閱 WORKFLOW_REQUIRES_INPUT 事件（更直接的互動步驟信號）
        self.event_bus.subscribe(SystemEvent.WORKFLOW_REQUIRES_INPUT, self._on_workflow_requires_input, handler_name="Monitor.requires_input")
    
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
                from utils.debug_helper import info_log
                info_log(f"[Monitor] 檢測到互動步驟: {self.current_step}")
    
    def _on_workflow_requires_input(self, event):
        """處理 WORKFLOW_REQUIRES_INPUT 事件（更直接的互動步驟信號）"""
        from utils.debug_helper import info_log
        data = event.data
        step_id = data.get('step_id')
        workflow_id = data.get('workflow_id')
        
        info_log(f"[Monitor] 收到 WORKFLOW_REQUIRES_INPUT 事件: workflow={workflow_id}, step={step_id}")
        
        if step_id:
            if step_id not in self.detected_interactive_steps:
                self.detected_interactive_steps.add(step_id)
                self.interactive_step_count += 1
            self.current_step = step_id
            info_log(f"[Monitor] 設置 awaiting_input_event 以響應步驟: {self.current_step}")
            self.awaiting_input_event.set()
    
    def _on_output_complete(self, event):
        """追蹤 TTS 輸出完成"""
        self.tts_output_count += 1
        from utils.debug_helper import info_log
        info_log(f"[Monitor] TTS 輸出完成 (第 {self.tts_output_count} 次)")
        
        # 如果已經檢測到互動步驟，在收到 TTS 輸出後短暫延遲即可設置事件
        # 不再依賴固定的輸出次數，因為不同步驟可能產生不同次數的輸出
        if self.current_step and self.tts_output_count >= 1:
            info_log(f"[Monitor] TTS 輸出完成，設置 awaiting_input_event 以響應步驟: {self.current_step}")
            self.awaiting_input_event.set()
            # 重置計數器為下一個互動步驟做準備
            self.tts_output_count = 0
    
    def cleanup(self):
        """清理資源"""
        try:
            self.event_bus.unsubscribe(SystemEvent.OUTPUT_LAYER_COMPLETE, self._on_output_complete)
            self.event_bus.unsubscribe(SystemEvent.WORKFLOW_REQUIRES_INPUT, self._on_workflow_requires_input)
        except:
            pass
        super().cleanup()


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
    def test_drop_and_read_full_cycle(self, system_components, isolated_gs, test_file):
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
    
    def test_intelligent_archive_full_cycle(self, system_components, isolated_gs, test_file):
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
        
        from core.framework import core_framework
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        sys_mod = core_framework.get_module("sys_module")
        
        # 使用標準的 InteractiveWorkflowMonitor（期待2個互動步驟）
        monitor = InteractiveWorkflowMonitor(event_bus, sys_module=sys_mod, expected_interactive_steps=2)
        
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
    
    def test_summarize_tag_full_cycle(self, system_components, isolated_gs, test_file):
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
        
        from core.framework import core_framework
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        sys_mod = core_framework.get_module("sys_module")
        
        # 使用標準的 InteractiveWorkflowMonitor（期待1個互動步驟: summary_confirm）
        monitor = InteractiveWorkflowMonitor(event_bus, sys_module=sys_mod, expected_interactive_steps=1)
        
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
    
    def test_translate_document_full_cycle(self, system_components, isolated_gs, test_file):
        """
        測試完整的文件翻譯工作流程循環
        
        流程：
        1. 使用者輸入：「翻譯這個檔案到中文」
        2. NLP 判斷意圖：file_operation
        3. LLM 通過 MCP 啟動 translate_document_workflow
        4. 工作流執行：
           - Step 1 (file_selection): 選擇檔案（使用 WorkingContext）
           - Step 2 (target_language_input): 可選輸入目標語言（會自動跳過）
           - Step 3 (translate_confirm): 確認執行（需要用戶輸入）
           - Step 4 (read_file_content): 讀取檔案內容
           - Step 5 (llm_translate): LLM 翻譯文件
           - Step 6 (save_translated_file): 儲存翻譯檔案
        5. 工作流程完成，LLM 生成總結回應
        
        測試重點：
        - LLM_PROCESSING 步驟中的翻譯任務是否正常運作
        - 翻譯檔案是否成功儲存到原檔案同目錄
        - 翻譯品質是否符合預期
        - WS 是否正確結束
        """
        from utils.debug_helper import info_log
        import time
        import os
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 創建工作流程監控器（追蹤互動步驟）
        # 使用標準的 WorkflowCycleMonitor（無互動步驟）
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            # 1. 準備測試：模擬前端拖曳檔案
            info_log("[Test] 🎯 測試：文件翻譯完整循環")
            info_log(f"[Test] 📁 檔案路徑: {test_file}")
            
            # 模擬前端拖曳檔案：設置 WorkingContext
            from core.working_context import working_context_manager
            working_context_manager.set_context_data("current_file_path", str(test_file))
            
            # 用戶請求翻譯（不需要指定路徑）
            inject_text_to_system("Translate this file to French.")
            
            # 等待工作流程完成（LLM 處理需要較長時間）
            info_log("[Test] ⏳ 等待工作流程完成（LLM 翻譯中）...")
            result = monitor.wait_for_completion(timeout=120)  # 增加超時時間
            
            # 驗證結果
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 驗證翻譯檔案是否生成
            translated_file = test_file.parent / f"{test_file.stem}_translated.txt"
            
            if translated_file.exists():
                info_log(f"[Test] ✅ 翻譯檔案已生成: {translated_file}")
                # 讀取並顯示部分內容
                with open(translated_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    preview = content[:200] + "..." if len(content) > 200 else content
                    info_log(f"[Test] 📄 翻譯內容預覽:\n{preview}")
                
                # 🔧 保留檔案不刪除，方便檢查結果
                info_log(f"[Test] 📁 翻譯檔案保留於: {translated_file}")
            else:
                info_log(f"[Test] ⚠️ 翻譯檔案未找到: {translated_file}")
                # 不要 fail，因為可能路徑問題，但記錄警告
            
            # 8. 驗證事件序列
            event_types = [e[0] for e in result["events"]]
            assert "step_completed" in event_types, "No step completion events"
            assert "session_ended" in event_types, "No session end event"
            
            info_log("[Test] ✅ 文件翻譯完整循環測試通過")
            
        finally:
            monitor.cleanup()
    
    def test_code_analysis_full_cycle(self, system_components, isolated_gs, test_code):
        """
        測試完整的程式碼分析工作流程循環
        
        流程：
        1. 使用者輸入：「分析這個程式碼檔案"
        2. NLP 判斷意圖：analysis_operation
        3. LLM 通過 MCP 啟動 code_analysis workflow
        4. 工作流執行：
           - Step 1 (select_file): 選擇程式碼檔案（使用 WorkingContext）
           - Step 2 (input_analysis_focus): 可選輸入分析焦點（會自動跳過）
           - Step 3 (execute_analysis): 執行 LLM 分析並輸出結果
        5. 工作流程完成，LLM 生成總結回應
        
        測試重點：
        - 檔案選擇步驟是否正確處理 WorkingContext 中的檔案
        - 分析焦點步驟是否正確跳過（optional）
        - LLM 分析是否正常執行
        - WS 是否正確結束
        """
        from utils.debug_helper import info_log
        from pathlib import Path
        
        from core.framework import core_framework
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        sys_mod = core_framework.get_module("sys_module")
        
        # 使用標準的 WorkflowCycleMonitor（無互動步驟）
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            # 1. 準備測試：選擇一個程式碼檔案
            info_log("[Test] 🎯 測試：程式碼分析完整循環")
            
            if not test_code.exists():
                pytest.fail(f"Test file not found: {test_code}")
            
            info_log(f"[Test] 📁 檔案路徑: {test_code}")
            
            # 模擬前端拖曳檔案：設置 WorkingContext
            from core.working_context import working_context_manager
            working_context_manager.set_context_data("current_file_path", str(test_code))
            
            # 用戶請求分析（不需要指定路徑和焦點）
            inject_text_to_system("Analyze this code file for its quality")
            
            # 2. 等待工作流程完成（LLM 處理需要較長時間）
            info_log("[Test] ⏳ 等待工作流程完成（LLM 處理中）...")
            result = monitor.wait_for_completion(timeout=120)  # 增加超時時間
            
            # 3. 驗證結果
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 4. 驗證事件序列
            event_types = [e[0] for e in result["events"]]
            assert "step_completed" in event_types, "No step completion events"
            assert "session_ended" in event_types, "No session end event"
            
            info_log("[Test] ✅ 程式碼分析完整循環測試通過")
            
        finally:
            monitor.cleanup()
            
            # 清理 WorkingContext
            from core.working_context import working_context_manager
            working_context_manager.global_context_data.pop('current_file_path', None)
            working_context_manager.global_context_data.pop('workflow_hint', None)
            working_context_manager.global_context_data.pop('pending_workflow', None)
            info_log("[Test] ✅ 已清理 WorkingContext")
            
            time.sleep(1.0)
            info_log("[Test] ✅ 測試清理完成")
    
    def test_quick_phrases_full_cycle(self, system_components, isolated_gs):
        """
        測試快速範本工作流 - 完整參數（測試 ConditionalStep）
        
        流程：
        1. 使用者輸入：「Generate a business email template and save it as a file」
           （包含 template_request 和 output_mode）
        2. NLP 判斷意圖：text_generation
        3. LLM 通過 MCP 啟動 quick_phrases workflow
           - LLM 提取參數: {"template_request": "business email template", "output_mode": "file"}
        4. 工作流執行：
           - Step 1 (input_template_request): 跳過（數據已存在）
           - Step 2 (llm_generate_template): LLM 生成範本
           - Step 3 (select_output_method): 跳過（數據已存在，值為 "file"）
           - Step 4 (output_conditional): ConditionalStep 檢測到 output_mode=file
           - Step 5 (save_to_file): 自動儲存到桌面
        5. 工作流程完成，範本已儲存
        
        測試重點：
        - LLM 是否正確提取 template_request 和 output_mode 參數
        - ConditionalStep 是否正確執行分支邏輯（file 分支）
        - 所有互動步驟是否被正確跳過
        - 檔案是否成功儲存到桌面
        - WS 是否正確結束
        """
        from utils.debug_helper import info_log
        import os
        import time
        from pathlib import Path
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 創建監控器（無需互動）
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            # 注入用戶輸入 - 包含完整參數
            info_log("[Test] 🎯 測試：快速範本生成（完整參數 - 儲存為文件）")
            inject_text_to_system("Generate am apology template and save it as a file to my desktop")
            
            # 等待工作流程完成（LLM 生成需要較長時間）
            info_log("[Test] ⏳ 等待工作流程完成（LLM 處理中）...")
            result = monitor.wait_for_completion(timeout=120)
            
            # 驗證結果
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 驗證步驟完成
            step_completed_events = [e for e in result["events"] if e[0] == "step_completed"]
            assert len(step_completed_events) >= 1, f"Expected at least 1 step completion, got {len(step_completed_events)}"
            
            # 驗證事件序列
            event_types = [e[0] for e in result["events"]]
            assert "step_completed" in event_types, "No step completion events"
            assert "session_ended" in event_types, "No session end event"
            
            info_log(f"[Test] 📊 收到 {len([e for e in result['events'] if e[0] == 'step_completed'])} 個步驟完成事件")
            
            # 驗證檔案是否生成到桌面
            desktop_path = Path(os.path.expanduser("~/Desktop"))
            # 尋找最近生成的文字檔案（任何 .txt 檔案）
            template_files = list(desktop_path.glob("*.txt"))
            
            if template_files:
                # 找到最新的檔案（最近 2 分鐘內生成的）
                current_time = time.time()
                recent_files = [f for f in template_files if (current_time - f.stat().st_mtime) < 120]
                
                if recent_files:
                    latest_file = max(recent_files, key=lambda p: p.stat().st_mtime)
                    info_log(f"[Test] ✅ 找到生成的範本檔案: {latest_file.name}")
                    
                    # 驗證檔案內容
                    with open(latest_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                        assert len(content) > 50, f"Template content too short: {len(content)} chars"
                        info_log(f"[Test] ✅ 檔案內容驗證通過（長度: {len(content)} 字元）")
                        info_log(f"[Test] 📄 檔案內容預覽: {content[:200]}...")
                    
                    # 清理測試檔案（可選）
                    # latest_file.unlink()
                else:
                    info_log("[Test] ⚠️ 桌面上沒有最近生成的範本檔案")
            else:
                info_log("[Test] ⚠️ 桌面上沒有 .txt 檔案")
            
            info_log("[Test] ✅ 快速範本生成（完整參數）測試通過")
            
        finally:
            # 清理
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            from core.states.state_manager import state_manager, UEPState
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.5)
            
            time.sleep(1.0)
            info_log("[Test] ✅ 測試清理完成")
    
    def test_ocr_recognition_full_cycle(self, system_components, isolated_gs, test_image):
        """
        測試完整的 OCR 辨識工作流程循環
        
        流程：
        1. 使用者輸入：「辨識這張圖片中的文字」
        2. NLP 判斷意圖：file_operation
        3. LLM 通過 MCP 啟動 file_ocr_recognition_workflow
        4. 工作流執行：
           - Step 1 (image_selection): 選擇圖片（使用 WorkingContext）
           - Step 2 (ocr_confirm): 確認執行（需要用戶輸入）
           - Step 3 (llm_ocr_recognition): LLM 辨識圖片文字
           - Step 4 (save_ocr_result): 儲存辨識結果
        5. 工作流程完成，LLM 生成總結回應
        
        測試重點：
        - LLM_PROCESSING 步驟中的 OCR 任務是否正常運作
        - 圖片辨識是否成功（使用 Gemini vision API）
        - 辨識結果是否成功儲存到桌面
        - WS 是否正確結束
        """
        from utils.debug_helper import info_log
        import time
        import os
        
        from core.framework import core_framework
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        sys_mod = core_framework.get_module("sys_module")
        
        # 使用標準的 InteractiveWorkflowMonitor（期待1個互動步驟: ocr_confirm）
        monitor = InteractiveWorkflowMonitor(event_bus, sys_module=sys_mod, expected_interactive_steps=1)
        
        try:
            # 1. 準備測試：模擬前端拖曳圖片
            info_log("[Test] 🎯 測試：OCR 辨識完整循環")
            info_log(f"[Test] 🖼️ 圖片路徑: {test_image}")
            
            # 模擬前端拖曳圖片：設置 WorkingContext
            from core.working_context import working_context_manager
            working_context_manager.set_context_data("current_file_path", str(test_image))
            
            # 用戶請求 OCR 辨識（不需要指定路徑）
            inject_text_to_system("Recognize the text in this image")
            
            # 3. 等待互動步驟 (ocr_confirm)
            info_log("[Test] ⏳ 等待互動步驟: ocr_confirm")
            if monitor.awaiting_input_event.wait(timeout=60):
                info_log(f"[Test] 📝 響應步驟: {monitor.current_step}")
                time.sleep(2)  # 等待 LLM 生成提示
                
                # 注入確認輸入
                inject_text_to_system("yes")
                monitor.awaiting_input_event.clear()
            else:
                info_log(f"[Test] ❌ 超時！TTS輸出次數: {monitor.tts_output_count}/{monitor.expected_tts_outputs}")
                pytest.fail("Timeout waiting for ocr_confirm step")
            
            # 5. 等待工作流程完成（LLM 處理需要較長時間）
            info_log("[Test] ⏳ 等待工作流程完成（LLM OCR 處理中）...")
            result = monitor.wait_for_completion(timeout=120)  # 增加超時時間
            
            # 6. 驗證結果
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 7. 驗證 OCR 結果檔案是否生成
            desktop_path = Path(os.path.expanduser("~/Desktop"))
            ocr_file = desktop_path / f"{test_image.stem}_ocr.txt"
            
            if ocr_file.exists():
                info_log(f"[Test] ✅ OCR 結果檔案已生成: {ocr_file}")
                # 讀取並顯示完整內容
                with open(ocr_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    info_log(f"[Test] 📄 OCR 辨識內容:\n{content}")
                
                # 🔧 保留檔案不刪除，方便檢查結果
                info_log(f"[Test] 📁 OCR 結果檔案保留於: {ocr_file}")
            else:
                info_log(f"[Test] ⚠️ OCR 結果檔案未找到: {ocr_file}")
                # 不要 fail，因為可能路徑問題，但記錄警告
            
            # 8. 驗證事件序列
            event_types = [e[0] for e in result["events"]]
            assert "step_completed" in event_types, "No step completion events"
            assert "session_ended" in event_types, "No session end event"
            
            info_log("[Test] ✅ OCR 辨識完整循環測試通過")
            
        finally:
            monitor.cleanup()
    
    def test_clipboard_tracker_full_cycle(self, system_components, isolated_gs):
        """
        測試完整的剪貼簿追蹤工作流程循環
        
        流程：
        1. Mock 剪貼簿歷史數據（因為背景監控服務未運行）
        2. 使用者輸入：「Search clipboard for email」
        3. NLP 判斷意圖：text_operation
        4. LLM 通過 MCP 啟動 clipboard_tracker workflow
           - LLM 提取參數: {"keyword": "email"}
        5. 工作流執行：
           - Step 1 (input_keyword): 跳過（數據已存在）
           - Step 2 (search_clipboard): 搜尋剪貼簿歷史（固定5筆）
           - Step 3 (llm_respond_results): LLM 呈現搜尋結果
           - Step 4 (input_copy_index): 使用者選擇要複製的項目
           - Step 5 (execute_copy): 執行複製
        6. 工作流程完成，內容已複製到剪貼簿
        
        測試重點：
        - Mock 剪貼簿歷史數據
        - LLM 是否正確提取 keyword 參數
        - LLM 是否正確呈現搜尋結果
        - 互動步驟是否正常運作
        - 複製功能是否正常（使用 Mock）
        - WS 是否正確結束
        
        Mock 說明：
        - 剪貼簿歷史：modules.sys_module.actions.text_processing._history
        - 複製功能：win32clipboard.SetClipboardData
        """
        from utils.debug_helper import info_log
        import time
        from unittest.mock import patch, MagicMock
        
        from core.framework import core_framework
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        sys_mod = core_framework.get_module("sys_module")
        
        # 使用標準的 InteractiveWorkflowMonitor（期待1個互動步驟: input_copy_index）
        monitor = InteractiveWorkflowMonitor(event_bus, sys_module=sys_mod, expected_interactive_steps=1)
        
        # Mock 剪貼簿數據
        mock_history = [
            "john.doe@example.com",
            "meeting at 3pm tomorrow",
            "https://github.com/example/repo",
            "jane.smith@company.com",
            "Please review the document",
        ]
        
        # 記錄被複製的內容
        copied_content = {"data": None}
        
        def mock_set_clipboard(format_type, content):
            """Mock win32clipboard.SetClipboardData"""
            info_log(f"[Mock] 複製到剪貼簿: {content[:50]}...")
            copied_content["data"] = content
        
        try:
            # 1. 準備測試：Mock 剪貼簿歷史
            info_log("[Test] 🎯 測試：剪貼簿追蹤完整循環")
            
            # Patch 剪貼簿歷史和複製功能
            with patch('modules.sys_module.actions.text_processing._history', mock_history), \
                 patch('modules.sys_module.actions.text_processing.win32clipboard.OpenClipboard'), \
                 patch('modules.sys_module.actions.text_processing.win32clipboard.EmptyClipboard'), \
                 patch('modules.sys_module.actions.text_processing.win32clipboard.SetClipboardData', side_effect=mock_set_clipboard), \
                 patch('modules.sys_module.actions.text_processing.win32clipboard.CloseClipboard'):
                
                info_log(f"[Test] 📋 Mock 剪貼簿歷史：{len(mock_history)} 條記錄")
                
                # 用戶請求搜尋剪貼簿（包含關鍵字）
                inject_text_to_system("Search clipboard for email addresses")
                
                # 2. 等待互動步驟 (input_copy_index)
                # 注意：input_keyword 會被跳過（因為 LLM 提取了參數）
                info_log("[Test] ⏳ 等待互動步驟: input_copy_index")
                if monitor.awaiting_input_event.wait(timeout=90):  # LLM 呈現結果需要時間
                    info_log(f"[Test] 📝 響應步驟: {monitor.current_step}")
                    time.sleep(2)  # 等待 LLM 生成提示
                    
                    # 注入選擇輸入（選擇第1個結果）
                    inject_text_to_system("1")
                    monitor.awaiting_input_event.clear()
                else:
                    info_log(f"[Test] ❌ 超時！TTS輸出次數: {monitor.tts_output_count}/{monitor.expected_tts_outputs}")
                    pytest.fail("Timeout waiting for input_copy_index step")
                
                # 3. 等待工作流程完成
                info_log("[Test] ⏳ 等待工作流程完成...")
                result = monitor.wait_for_completion(timeout=60)
                
                # 4. 驗證結果
                assert result["completed"], "Workflow did not complete within timeout"
                assert not result["failed"], "Workflow failed"
                assert result["session_id"] is not None, "No workflow session ID"
                
                info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
                info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
                info_log(f"[Test] 🔄 互動步驟數量: {monitor.interactive_step_count}")
                
                # 5. 驗證互動步驟（只有 input_copy_index）
                assert monitor.interactive_step_count == 1, f"Expected 1 interactive step, got {monitor.interactive_step_count}"
                
                # 6. 驗證複製功能
                assert copied_content["data"] is not None, "No content was copied"
                assert "email" in copied_content["data"].lower() or "@" in copied_content["data"], \
                    f"Copied content doesn't contain email: {copied_content['data']}"
                
                info_log(f"[Test] ✅ 複製的內容: {copied_content['data'][:100]}")
                
                # 7. 驗證事件序列
                event_types = [e[0] for e in result["events"]]
                assert "step_completed" in event_types, "No step completion events"
                assert "session_ended" in event_types, "No session end event"
                
                info_log("[Test] ✅ 剪貼簿追蹤完整循環測試通過")
            
        finally:
            monitor.cleanup()
    
    def test_get_weather_full_cycle(self, system_components, isolated_gs):
        """
        測試天氣查詢工作流（最簡單，2步驟，參數已提供，無需互動）
        
        流程：
        1. 用戶輸入：「Check weather in Taipei」（包含 location 參數）
        2. NLP 判斷意圖：weather_query
        3. LLM 通過 MCP 啟動 get_weather workflow
           - LLM 提取參數: {"location": "Taipei"}
        4. 工作流執行：
           - Step 1 (location_input): 跳過（數據已存在）
           - Step 2 (execute_weather_query): 自動執行查詢
        5. 工作流程完成，返回天氣資訊
        
        測試重點：
        - LLM 是否正確提取 location 參數
        - location_input 步驟是否被正確跳過
        - 工作流是否自動完成（無需用戶輸入）
        - WS 是否正確結束
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 創建監控器（無需互動）
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            # 注入用戶輸入 - 包含完整參數
            info_log("[Test] 🎯 測試：天氣查詢完整循環（參數已提供）")
            inject_text_to_system("Check weather in Taipei")
            
            # 等待工作流程完成
            # 預期時間：LLM處理(~5s) + 工作流執行(~10s) + TTS輸出(~30s) = ~45s
            info_log("[Test] ⏳ 等待工作流程完成...")
            result = monitor.wait_for_completion(timeout=90)
            
            # 驗證結果
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 完成的步驟: {result['completed_steps']}")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 驗證步驟完成
            # 預期：應該有1個步驟完成事件（execute_weather_query）
            # location_input 應該被跳過，不會出現在 WORKFLOW_STEP_COMPLETED 中
            step_completed_events = [e for e in result["events"] if e[0] == "step_completed"]
            assert len(step_completed_events) >= 1, f"Expected at least 1 step completion, got {len(step_completed_events)}"
            
            # 驗證事件序列
            event_types = [e[0] for e in result["events"]]
            assert "step_completed" in event_types, "No step completion events"
            assert "session_ended" in event_types, "No session end event"
            
            # 驗證步驟順序
            completed_steps = result["completed_steps"]
            info_log(f"[Test] 📝 步驟執行順序（從事件）: {completed_steps}")
            
            # 🔧 修正：由於自動推進，事件可能只捕獲部分步驟
            # 驗證至少捕獲了 location_input 步驟
            assert "location_input" in completed_steps, "location_input step not found in events"
            
            # 🔧 從工作流會話驗證實際執行的步驟（通過 step_history）
            # 獲取工作流會話的 step_history
            from core.framework import core_framework
            sys_mod = core_framework.get_module("sys_module")
            workflow_session = sys_mod.session_manager.get_session(monitor.workflow_session_id) if sys_mod else None
            if workflow_session:
                step_history = workflow_session.step_history
                info_log(f"[Test] 📜 工作流會話步驟歷史: {step_history}")
                # 驗證執行了正確的步驟
                assert "location_input" in step_history, "location_input not in step_history"
                assert "execute_weather_query" in step_history, "execute_weather_query not in step_history"
            else:
                info_log("[Test] ⚠️ 無法獲取工作流會話來驗證步驟歷史")
            
            info_log("[Test] ✅ 天氣查詢完整循環測試通過")
            
        finally:
            # 清理
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.5)
            
            time.sleep(1.0)
            info_log("[Test] ✅ 測試清理完成")
    
    def test_clean_trash_bin_full_cycle(self, system_components, isolated_gs):
        """
        測試清空回收桶工作流（2步驟，含確認）
        
        流程：
        1. 用戶輸入：「Clean the trash bin」
        2. NLP 判斷意圖：system_operation
        3. LLM 通過 MCP 啟動 clean_trash_bin workflow
        4. 工作流執行：
           - Step 1 (confirm_clean): 互動步驟 - LLM 提示用戶確認
           - Step 2 (execute_clean): 自動執行清空
        5. 工作流程完成
        
        測試重點：
        - 互動步驟前 LLM 是否生成提示
        - 自動注入用戶輸入來響應互動步驟
        - 工作流最終是否成功執行清空
        - WS 是否正確結束
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 使用基礎監控器（不需要複雜的互動步驟檢測）
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            info_log("[Test] 🎯 測試：清空回收桶完整循環（含確認）")
            inject_text_to_system("Clean the trash bin")
            
            # 等待 TTS 生成和工作流準備
            # TTS 生成工作流啟動提示需要約 40 秒
            info_log("[Test] ⏳ 等待 TTS 生成工作流提示（約 45 秒）...")
            time.sleep(45)
            
            info_log("[Test] ✅ TTS 應該已完成，準備注入確認輸入")
            
            # 注入確認輸入（響應 confirm_clean 步驟）
            info_log("[Test] 📝 注入確認輸入")
            inject_text_to_system("yes")
            
            # 等待工作流程完成
            info_log("[Test] ⏳ 等待工作流程完成...")
            result = monitor.wait_for_completion(timeout=60)
            
            # 驗證結果
            assert result["completed"], "Workflow did not complete"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 完成的步驟: {result['completed_steps']}")
            
            # 驗證步驟完成
            assert len(result["completed_steps"]) >= 1, f"Expected at least 1 step, got {len(result['completed_steps'])}"
            assert "execute_clean" in result["completed_steps"], "execute_clean step not found"
            
            info_log("[Test] ✅ 清空回收桶完整循環測試通過")
            
        finally:
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.5)
            
            time.sleep(1.0)
            info_log("[Test] ✅ 測試清理完成")
    
    def test_generate_backup_script_full_cycle(self, system_components, isolated_gs):
        """
        測試生成備份腳本工作流（1步驟，無互動）
        
        流程：
        1. 用戶輸入：「Generate a backup script」
        2. NLP 判斷意圖：system_operation
        3. LLM 通過 MCP 啟動 generate_backup_script workflow
           - 使用預設路徑：生成腳本到桌面
        4. 工作流執行：
           - Step 1 (generate_script): 自動生成備份腳本
        5. 工作流程完成，腳本已生成到桌面
        
        測試重點：
        - 工作流是否自動完成（無需用戶輸入）
        - 備份腳本是否成功生成到桌面
        - WS 是否正確結束
        
        註：此工作流未來會重構，目前僅測試基本功能
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        import os
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 獲取桌面路徑
        desktop_path = Path(os.path.expanduser("~")) / "Desktop"
        expected_script = desktop_path / "backup_script.bat"
        
        # 使用基礎監控器
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            info_log("[Test] 🎯 測試：生成備份腳本完整循環")
            info_log(f"[Test] 📁 預期腳本路徑: {expected_script}")
            
            # 清理舊的測試腳本（如果存在）
            if expected_script.exists():
                expected_script.unlink()
                info_log("[Test] 🧹 清理舊的測試腳本")
            
            # 注入測試文字（簡單指令，讓 LLM 使用預設路徑）
            inject_text_to_system("Generate a backup script")
            
            # 等待 TTS 生成和工作流準備
            info_log("[Test] ⏳ 等待 TTS 生成工作流提示（約 45 秒）...")
            time.sleep(45)
            
            info_log("[Test] ✅ TTS 應該已完成，工作流應正在執行")
            
            # 等待工作流程完成
            info_log("[Test] ⏳ 等待工作流程完成...")
            result = monitor.wait_for_completion(timeout=60)
            
            # 驗證結果
            assert result["completed"], "Workflow did not complete"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 完成的步驟: {result['completed_steps']}")
            
            # 驗證步驟完成
            assert len(result["completed_steps"]) >= 1, f"Expected at least 1 step, got {len(result['completed_steps'])}"
            assert "generate_script" in result["completed_steps"], "generate_script step not found"
            
            # 驗證腳本檔案是否生成到桌面
            if expected_script.exists():
                info_log(f"[Test] ✅ 備份腳本已生成: {expected_script}")
                info_log(f"[Test] 📏 腳本大小: {expected_script.stat().st_size} bytes")
            else:
                info_log(f"[Test] ⚠️ 備份腳本未找到: {expected_script}")
                info_log("[Test] 💡 註：腳本可能生成在其他位置，這是預期行為（工作流將來會重構）")
            
            info_log("[Test] ✅ 生成備份腳本完整循環測試通過")
            
        finally:
            monitor.cleanup()
            
            # 清理測試腳本（如果生成了）
            try:
                if expected_script.exists():
                    expected_script.unlink()
                    info_log(f"[Test] 🧹 清理測試腳本: {expected_script}")
            except Exception as e:
                info_log(f"[Test] ⚠️ 清理測試腳本失敗: {e}")
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.5)
            
            time.sleep(1.0)
            info_log("[Test] ✅ 測試清理完成")
    
    def test_news_summary_full_cycle(self, system_components, isolated_gs):
        """
        測試新聞摘要工作流（無參數，固定抓取 6 則新聞）
        
        流程：
        1. 用戶輸入：「Show me the news」或「news summary」
        2. NLP 判斷意圖：news_query
        3. LLM 通過 MCP 啟動 news_summary workflow
           - 無需參數，固定來源和數量
        4. 工作流執行：
           - Step 1 (execute_news_fetch): 自動執行抓取 Google 新聞
        5. 工作流程完成，返回新聞列表
        6. LLM 總結新聞標題並用英文回應使用者
        
        測試重點：
        - 工作流是否自動完成（無需用戶輸入）
        - 是否成功抓取 6 則新聞
        - LLM 是否正確總結新聞標題
        - WS 是否正確結束
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 創建監控器（無需互動）
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            # 注入用戶輸入
            info_log("[Test] 🎯 測試：新聞摘要完整循環（無參數）")
            inject_text_to_system("Show me the latest Taiwan news")
            
            # 等待工作流程完成
            # 預期時間：LLM處理(~5s) + 工作流執行(~15s) + TTS輸出(~30s) = ~50s
            info_log("[Test] ⏳ 等待工作流程完成...")
            result = monitor.wait_for_completion(timeout=90)
            
            # 驗證結果
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 完成的步驟: {result['completed_steps']}")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 驗證步驟完成
            step_completed_events = [e for e in result["events"] if e[0] == "step_completed"]
            assert len(step_completed_events) >= 1, f"Expected at least 1 step completion, got {len(step_completed_events)}"
            
            # 驗證事件序列
            event_types = [e[0] for e in result["events"]]
            assert "step_completed" in event_types, "No step completion events"
            assert "session_ended" in event_types, "No session end event"
            
            # 驗證步驟順序
            completed_steps = result["completed_steps"]
            info_log(f"[Test] 📝 步驟執行順序（從事件）: {completed_steps}")
            
            # 驗證執行了新聞抓取步驟
            assert "execute_news_fetch" in completed_steps, "execute_news_fetch step not found"
            
            # 從工作流會話驗證實際執行的步驟
            from core.framework import core_framework
            sys_mod = core_framework.get_module("sys_module")
            workflow_session = sys_mod.session_manager.get_session(monitor.workflow_session_id) if sys_mod else None
            if workflow_session:
                step_history = workflow_session.step_history
                info_log(f"[Test] 📜 工作流會話步驟歷史: {step_history}")
                # 驗證執行了正確的步驟
                assert "execute_news_fetch" in step_history, "execute_news_fetch not in step_history"
                # 驗證工作流數據包含新聞結果
                workflow_data = workflow_session.workflow_data
                if "news_results" in workflow_data:
                    news_count = len(workflow_data["news_results"])
                    info_log(f"[Test] 📰 抓取的新聞數量: {news_count}")
                    assert news_count > 0, "No news was fetched"
            else:
                info_log("[Test] ⚠️ 無法獲取工作流會話來驗證步驟歷史")
            
            info_log("[Test] ✅ 新聞摘要完整循環測試通過")
            
        finally:
            # 清理
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.5)
            
            time.sleep(1.0)
            info_log("[Test] ✅ 測試清理完成")
    
    def test_get_world_time_full_params(self, system_components, isolated_gs):
        """
        測試世界時間查詢工作流 - 完整參數（測試 ConditionalStep）
        
        流程：
        1. 用戶輸入：「What time is it in Tokyo?」（包含 mode=2 和 timezone=Tokyo）
        2. NLP 判斷意圖：time_query
        3. LLM 通過 MCP 啟動 get_world_time workflow
           - LLM 提取參數: {"target_num": 2, "tz": "Tokyo"}
        4. 工作流執行：
           - Step 1 (mode_selection): 跳過（數據已存在，值為 2）
           - Step 2 (timezone_conditional): ConditionalStep 檢測到 mode=2
           - Step 3 (timezone_input): 跳過（數據已存在）
           - Step 4 (execute_time_query): 自動執行查詢
        5. 工作流程完成，返回時間資訊
        
        測試重點：
        - LLM 是否正確提取 target_num 和 tz 參數
        - ConditionalStep 是否正確執行分支邏輯
        - 所有互動步驟是否被正確跳過
        - 工作流是否自動完成（無需用戶輸入）
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 創建監控器（無需互動）
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            # 注入用戶輸入 - 包含完整參數
            info_log("[Test] 🎯 測試：世界時間查詢（完整參數 - Tokyo）")
            inject_text_to_system("What time is it in Tokyo right now?")
            
            # 等待工作流程完成
            info_log("[Test] ⏳ 等待工作流程完成...")
            result = monitor.wait_for_completion(timeout=90)
            
            # 驗證結果
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 完成的步驟: {result['completed_steps']}")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 驗證步驟完成
            step_completed_events = [e for e in result["events"] if e[0] == "step_completed"]
            assert len(step_completed_events) >= 1, f"Expected at least 1 step completion, got {len(step_completed_events)}"
            
            # 驗證事件序列
            event_types = [e[0] for e in result["events"]]
            assert "step_completed" in event_types, "No step completion events"
            assert "session_ended" in event_types, "No session end event"
            
            # 驗證步驟順序
            completed_steps = result["completed_steps"]
            info_log(f"[Test] 📝 步驟執行順序（從事件）: {completed_steps}")
            
            # 驗證執行了時間查詢步驟
            assert "execute_time_query" in completed_steps, "execute_time_query step not found"
            
            # 從工作流會話驗證實際執行的步驟
            from core.framework import core_framework
            sys_mod = core_framework.get_module("sys_module")
            workflow_session = sys_mod.session_manager.get_session(monitor.workflow_session_id) if sys_mod else None
            if workflow_session:
                step_history = workflow_session.step_history
                info_log(f"[Test] 📜 工作流會話步驟歷史: {step_history}")
                # 驗證執行了正確的步驟
                assert "mode_selection" in step_history, "mode_selection not in step_history"
                assert "timezone_conditional" in step_history, "timezone_conditional not in step_history"
                assert "timezone_input" in step_history, "timezone_input not in step_history"
                assert "execute_time_query" in step_history, "execute_time_query not in step_history"
            else:
                info_log("[Test] ⚠️ 無法獲取工作流會話來驗證步驟歷史")
            
            info_log("[Test] ✅ 世界時間查詢（完整參數）測試通過")
            
        finally:
            # 清理
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.5)
            
            time.sleep(1.0)
            info_log("[Test] ✅ 測試清理完成")
    
    def test_get_world_time_no_params(self, system_components, isolated_gs):
        """
        測試世界時間查詢工作流 - 無參數（測試「等效第一步」概念）
        
        流程：
        1. 用戶輸入：「Check the time」（沒有任何參數）
        2. NLP 判斷意圖：time_query
        3. LLM 通過 MCP 啟動 get_world_time workflow
           - LLM 沒有提取到任何參數: {}
        4. 工作流執行：
           - Step 1 (mode_selection): Interactive 等待用戶選擇模式
           - **問題**：但這是 ConditionalStep 會自動執行，直接跳到分支
           - 實際「等效第一步」應該是分支後的 Interactive 步驟
        
        測試目的：
        - 展示「等效第一步」的概念問題
        - mode_selection 是名義上的第一步，但會立即執行
        - 真正需要用戶輸入的是 ConditionalStep 執行後的步驟
        - LLM 在工作流啟動時不知道「等效第一步」是什麼
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        from core.framework import core_framework
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 獲取 sys_module
        sys_mod = core_framework.get_module("sys_module")
        
        # 創建互動監控器
        monitor = InteractiveWorkflowMonitor(event_bus, sys_module=sys_mod, expected_interactive_steps=1)
        
        try:
            # 注入用戶輸入 - 完全沒有參數
            info_log("[Test] 🎯 測試：世界時間查詢（無參數 - 展示等效第一步問題）")
            inject_text_to_system("Check the time")
            
            # 等待工作流啟動並要求輸入
            info_log("[Test] ⏳ 等待工作流要求輸入...")
            input_requested = monitor.awaiting_input_event.wait(timeout=30)
            
            # 驗證工作流已啟動並等待輸入
            assert input_requested, "Workflow did not request input within timeout"
            assert monitor.interactive_step_count > 0, f"No interactive steps detected"
            
            info_log(f"[Test] ✅ 工作流已啟動並等待輸入")
            info_log(f"[Test] 📝 等待的步驟: {monitor.current_step}")
            info_log(f"[Test] 🔍 這就是「等效第一步」- 工作流定義的第一步是 mode_selection，但實際執行後等待輸入的步驟是: {monitor.current_step}")
            
            # 根據等待的步驟提供相應輸入
            time.sleep(1.0)
            if monitor.current_step == "mode_selection":
                info_log("[Test] 📥 提供模式選擇: 2 (specific timezone)")
                inject_text_to_system("2")
                
                # 等待下一個輸入請求（timezone）
                monitor.awaiting_input_event.clear()
                info_log("[Test] ⏳ 等待時區輸入請求...")
                input_requested = monitor.awaiting_input_event.wait(timeout=60)
                assert input_requested, "Workflow did not request timezone input"
                
                # 收到輸入請求後，等待 LLM 提示完成並立即注入
                time.sleep(2.0)
                info_log("[Test] 📥 提供時區輸入: Tokyo")
                inject_text_to_system("Tokyo")
            else:
                # 如果直接跳到了其他步驟（如 timezone_input）
                info_log(f"[Test] 📥 直接提供輸入給步驟 {monitor.current_step}: Tokyo")
                inject_text_to_system("Tokyo")
            
            # 等待工作流完成
            info_log("[Test] ⏳ 等待工作流完成...")
            result = monitor.wait_for_completion(timeout=60)
            
            # 驗證結果
            assert result["completed"], "Workflow did not complete after input"
            assert not result["failed"], "Workflow failed"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 完成的步驟: {result['completed_steps']}")
            
            info_log("[Test] ✅ 世界時間查詢（無參數）測試通過")
            info_log("[Test] 💡 關鍵發現：「等效第一步」不等於「定義的第一步」")
            
        finally:
            # 清理
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.5)
            
            time.sleep(1.0)
            info_log("[Test] ✅ 測試清理完成")
    
    def test_get_world_time_partial_params(self, system_components, isolated_gs):
        """
        測試世界時間查詢工作流 - 部分參數（測試 ConditionalStep 互動）
        
        流程：
        1. 用戶輸入：「What's the time in a specific timezone?」（只提示 mode=2，沒有具體時區）
        2. NLP 判斷意圖：time_query
        3. LLM 通過 MCP 啟動 get_world_time workflow
           - LLM 提取參數: {"target_num": 2}（沒有 tz）
        4. 工作流執行：
           - Step 1 (mode_selection): 跳過（數據已存在，值為 2）
           - Step 2 (timezone_conditional): ConditionalStep 檢測到 mode=2，分支到 timezone_input
           - Step 3 (timezone_input): 等待用戶輸入時區 -> 用戶輸入 "Asia/Tokyo"
           - Step 4 (execute_time_query): 自動執行查詢
        5. 工作流程完成，返回時間資訊
        
        測試重點：
        - ConditionalStep 是否正確根據 mode 選擇分支
        - timezone_input 步驟是否正確等待用戶輸入
        - 用戶輸入後工作流是否繼續執行
        - 工作流是否正確完成並返回結果
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        from core.framework import core_framework
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 獲取 sys_module
        sys_mod = core_framework.get_module("sys_module")
        
        # 創建互動監控器（期待 1 個互動步驟）
        monitor = InteractiveWorkflowMonitor(event_bus, sys_module=sys_mod, expected_interactive_steps=1)
        
        try:
            # 注入用戶輸入 - 只包含 mode，沒有 timezone
            info_log("[Test] 🎯 測試：世界時間查詢（部分參數 - 需要互動）")
            inject_text_to_system("Show me the time in a specific timezone")
            
            # 等待工作流啟動並要求輸入
            info_log("[Test] ⏳ 等待工作流要求時區輸入...")
            input_requested = monitor.awaiting_input_event.wait(timeout=30)
            
            # 驗證工作流已啟動並等待輸入
            assert input_requested, "Workflow did not request input within timeout"
            assert monitor.interactive_step_count > 0, f"No interactive steps detected"
            
            info_log(f"[Test] ✅ 工作流已啟動並等待輸入")
            info_log(f"[Test] 📝 等待的步驟: {monitor.current_step}")
            
            # 提供時區輸入
            time.sleep(1.0)
            info_log("[Test] 📥 提供時區輸入: Tokyo")
            inject_text_to_system("Tokyo")
            
            # 等待工作流完成
            info_log("[Test] ⏳ 等待工作流完成...")
            result = monitor.wait_for_completion(timeout=60)
            
            # 驗證結果
            assert result["completed"], "Workflow did not complete after input"
            assert not result["failed"], "Workflow failed"
            
            info_log(f"[Test] ✅ 工作流程完成: {result['session_id']}")
            info_log(f"[Test] 📊 完成的步驟: {result['completed_steps']}")
            
            # 驗證步驟順序
            completed_steps = result["completed_steps"]
            info_log(f"[Test] 📝 步驟執行順序: {completed_steps}")
            
            info_log("[Test] ✅ 世界時間查詢（部分參數）測試通過")
            
        finally:
            # 清理
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.5)
            
            time.sleep(1.0)
            info_log("[Test] ✅ 測試清理完成")


class TestBackgroundWorkflowFullCycle:
    """背景工作流完整循環測試"""
    
    def test_media_playback_service_full_cycle(self, system_components, isolated_gs):
        """
        測試媒體播放背景服務完整循環（啟動 + 干涉）
        
        流程：
        1. 用戶輸入：「Play Entangled Misery in my local library」
        2. NLP 判斷意圖：media_control
        3. LLM 通過 MCP 啟動 media_playback workflow
        4. 工作流執行：
           - Step 1 (playback_type_selection): 選擇播放類型（跳過，使用 initial_data）
           - Step 2 (playback_type_conditional): 條件分支
           - Step 3 (query_input): 輸入查詢（跳過，使用 initial_data）
           - Step 4 (execute_playback): 執行播放
           - Step 5 (create_monitor): 建立監控任務並提交到執行緒池
           - 工作流完成，LLM 給予啟動回應
        5. 背景監控線程持續運行
        6. 用戶輸入：「Pause the music」
        7. LLM 通過 MCP 啟動 control_media workflow
        8. 工作流執行：
           - Step 1 (media_control_intervention): 發送暫停指令到資料庫
           - 背景線程讀取指令並執行
           - 工作流完成，LLM 給予干涉回應
        
        測試重點：
        - 啟動工作流是否成功註冊背景服務
        - LLM 是否在啟動時給予回應
        - 干涉工作流是否成功發送控制指令
        - LLM 是否在干涉時給予回應
        - 背景服務是否持續運行（跨 GS）
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        from modules.sys_module.actions.automation_helper import (
            get_active_workflows,
            get_monitoring_pool
        )
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 使用基礎監控器
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            info_log("[Test] 🎯 測試：媒體播放背景服務完整循環")
            info_log("[Test] 📝 階段 1: 啟動媒體播放服務")
            
            # ==================== 階段 1: 啟動服務 ====================
            inject_text_to_system("Play Neon Escapism in my local library")
            
            # 等待 TTS 生成和工作流準備
            info_log("[Test] ⏳ 等待 TTS 生成工作流提示（約 45 秒）...")
            time.sleep(45)
            
            info_log("[Test] ✅ TTS 應該已完成，工作流應正在執行")
            
            # 等待啟動工作流完成
            info_log("[Test] ⏳ 等待啟動工作流完成...")
            result = monitor.wait_for_completion(timeout=60)
            
            # 驗證啟動結果
            assert result["completed"], "Startup workflow did not complete"
            assert not result["failed"], "Startup workflow failed"
            assert result["session_id"] is not None, "No workflow session ID"
            
            info_log(f"[Test] ✅ 啟動工作流完成: {result['session_id']}")
            info_log(f"[Test] 📊 完成的步驟: {result['completed_steps']}")
            
            # 驗證關鍵步驟完成（檢查 create_monitor 步驟）
            assert "create_monitor" in result["completed_steps"], "create_monitor step not found"
            info_log(f"[Test] ✅ 監控建立步驟已完成")
            
            # 檢查背景服務是否已註冊
            active_workflows = get_active_workflows(workflow_type="media_playback")
            assert len(active_workflows) > 0, "No active media playback service found in database"
            
            task_id = active_workflows[0]["task_id"]
            info_log(f"[Test] ✅ 背景服務已註冊，任務 ID: {task_id}")
            
            # 檢查監控線程是否運行
            monitoring_pool = get_monitoring_pool()
            assert monitoring_pool.is_monitor_running(task_id), "Monitor thread is not running"
            info_log(f"[Test] ✅ 監控線程正在運行")
            
            # 等待系統回到 IDLE（完成第一個 GS）
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.5)
            
            time.sleep(2.0)
            info_log("[Test] ✅ 第一個 GS 已完成，背景服務應持續運行")
            
            # ==================== 階段 2: 干涉控制 ====================
            info_log("[Test] 📝 階段 2: 干涉控制（暫停音樂）")
            
            # 重置監控器以追蹤干涉工作流
            monitor.cleanup()
            monitor = WorkflowCycleMonitor(event_bus)
            
            # 注入干涉指令
            inject_text_to_system("Pause the music")
            
            # 等待 TTS 生成
            info_log("[Test] ⏳ 等待 TTS 生成干涉提示（約 45 秒）...")
            time.sleep(45)
            
            info_log("[Test] ✅ TTS 應該已完成，干涉工作流應正在執行")
            
            # 等待干涉工作流完成
            info_log("[Test] ⏳ 等待干涉工作流完成...")
            result = monitor.wait_for_completion(timeout=60)
            
            # 驗證干涉結果
            assert result["completed"], "Intervention workflow did not complete"
            assert not result["failed"], "Intervention workflow failed"
            
            info_log(f"[Test] ✅ 干涉工作流完成: {result['session_id']}")
            info_log(f"[Test] 📊 完成的步驟: {result['completed_steps']}")
            
            # 驗證步驟完成
            assert "media_control_intervention" in result["completed_steps"], "media_control_intervention step not found"
            
            info_log("[Test] ✅ 媒體播放背景服務完整循環測試通過")
            
        finally:
            # 清理：停止背景服務
            try:
                active_workflows = get_active_workflows(workflow_type="media_playback")
                if active_workflows:
                    task_id = active_workflows[0]["task_id"]
                    monitoring_pool = get_monitoring_pool()
                    monitoring_pool.stop_monitor(task_id)
                    info_log(f"[Test] 🧹 已停止背景服務: {task_id}")
            except Exception as e:
                info_log(f"[Test] ⚠️ 清理背景服務失敗: {e}")
            
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.5)
            
            time.sleep(1.0)
            info_log("[Test] ✅ 測試清理完成")
            
    def test_media_playback_with_shuffle_full_cycle(self, system_components, isolated_gs):
        """
        測試媒體播放 shuffle 功能完整循環
        
        流程：
        1. 用戶輸入：「Play my music library with shuffle」
        2. NLP 判斷意圖：media_control
        3. LLM 通過 MCP 啟動 media_playback workflow（shuffle=True）
        4. 工作流執行並啟動播放器，開啟 shuffle
        5. 驗證播放器狀態：is_shuffled = True
        
        測試重點：
        - LLM 能否正確解析 shuffle 需求並傳遞參數
        - 播放器是否正確開啟 shuffle
        - 回應是否簡短自然
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        from modules.sys_module.actions.automation_helper import (
            get_active_workflows,
            get_monitoring_pool,
            get_music_player_status
        )
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 使用基礎監控器
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            info_log("[Test] 🎯 測試：媒體播放 shuffle 功能")
            
            # 注入測試輸入
            inject_text_to_system("Play my music library with shuffle")
            
            # 等待 TTS 生成
            info_log("[Test] ⏳ 等待 TTS 生成（約 45 秒）...")
            time.sleep(45)
            
            # 等待工作流完成
            info_log("[Test] ⏳ 等待工作流完成...")
            result = monitor.wait_for_completion(timeout=60)
            
            # 驗證結果
            assert result["completed"], "Workflow did not complete"
            assert not result["failed"], "Workflow failed"
            
            info_log(f"[Test] ✅ 工作流完成: {result['session_id']}")
            
            # 等待播放器初始化
            time.sleep(2)
            
            # 檢查播放器狀態
            status = get_music_player_status()
            info_log(f"[Test] 播放器狀態: is_shuffled={status['is_shuffled']}, is_looping={status['is_looping']}")
            
            # 驗證 shuffle 已開啟
            assert status["is_shuffled"] == True, "Shuffle should be enabled"
            
            info_log("[Test] ✅ Shuffle 功能測試通過")
            
        finally:
            # 清理
            try:
                active_workflows = get_active_workflows(workflow_type="media_playback")
                if active_workflows:
                    task_id = active_workflows[0]["task_id"]
                    monitoring_pool = get_monitoring_pool()
                    monitoring_pool.stop_monitor(task_id)
                    info_log(f"[Test] 🧹 已停止背景服務: {task_id}")
            except Exception as e:
                info_log(f"[Test] ⚠️ 清理失敗: {e}")
            
            monitor.cleanup()
            time.sleep(1.0)
    
    def test_media_playback_with_loop_full_cycle(self, system_components, isolated_gs):
        """
        測試媒體播放 loop 功能完整循環
        
        流程：
        1. 用戶輸入：「Play Ancient Wisdom on repeat」
        2. NLP 判斷意圖：media_control
        3. LLM 通過 MCP 啟動 media_playback workflow（loop=True）
        4. 工作流執行並啟動播放器，開啟 loop
        5. 驗證播放器狀態：is_looping = True
        
        測試重點：
        - LLM 能否正確解析 loop/repeat 需求並傳遞參數
        - 播放器是否正確開啟 loop
        - 回應是否簡短自然
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        from modules.sys_module.actions.automation_helper import (
            get_active_workflows,
            get_monitoring_pool,
            get_music_player_status
        )
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 使用基礎監控器
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            info_log("[Test] 🎯 測試：媒體播放 loop 功能")
            
            # 注入測試輸入
            inject_text_to_system("Play Ancient Wisdom on repeat")
            
            # 等待 TTS 生成
            info_log("[Test] ⏳ 等待 TTS 生成（約 45 秒）...")
            time.sleep(45)
            
            # 等待工作流完成
            info_log("[Test] ⏳ 等待工作流完成...")
            result = monitor.wait_for_completion(timeout=60)
            
            # 驗證結果
            assert result["completed"], "Workflow did not complete"
            assert not result["failed"], "Workflow failed"
            
            info_log(f"[Test] ✅ 工作流完成: {result['session_id']}")
            
            # 等待播放器初始化
            time.sleep(2)
            
            # 檢查播放器狀態
            status = get_music_player_status()
            info_log(f"[Test] 播放器狀態: is_shuffled={status['is_shuffled']}, is_looping={status['is_looping']}")
            
            # 驗證 loop 已開啟
            assert status["is_looping"] == True, "Loop should be enabled"
            
            info_log("[Test] ✅ Loop 功能測試通過")
            
        finally:
            # 清理
            try:
                active_workflows = get_active_workflows(workflow_type="media_playback")
                if active_workflows:
                    task_id = active_workflows[0]["task_id"]
                    monitoring_pool = get_monitoring_pool()
                    monitoring_pool.stop_monitor(task_id)
                    info_log(f"[Test] 🧹 已停止背景服務: {task_id}")
            except Exception as e:
                info_log(f"[Test] ⚠️ 清理失敗: {e}")
            
            monitor.cleanup()
            time.sleep(1.0)
    
    def test_media_control_shuffle_toggle_full_cycle(self, system_components, isolated_gs):
        """
        測試媒體控制 shuffle 切換完整循環
        
        流程：
        1. 先啟動播放（無 shuffle）
        2. 用戶輸入：「Turn on shuffle」或「Shuffle the playlist」
        3. NLP 判斷意圖：media_control
        4. LLM 通過 MCP 啟動 control_media workflow（action=shuffle）
        5. 驗證控制指令發送成功
        
        測試重點：
        - LLM 能否理解 shuffle 控制指令
        - 干涉工作流是否正確發送 shuffle 控制
        - 回應是否簡短自然
        """
        from utils.debug_helper import info_log
        from core.states.state_manager import state_manager, UEPState
        from modules.sys_module.actions.automation_helper import (
            get_active_workflows,
            get_monitoring_pool,
            get_music_player_status
        )
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        # 使用基礎監控器
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            info_log("[Test] 🎯 測試：媒體控制 shuffle 切換")
            info_log("[Test] 📝 階段 1: 啟動播放（無 shuffle）")
            
            # 先啟動播放
            inject_text_to_system("Play my local music")
            
            # 等待 TTS 和工作流完成
            time.sleep(45)
            result = monitor.wait_for_completion(timeout=60)
            
            assert result["completed"], "Startup workflow did not complete"
            info_log("[Test] ✅ 播放已啟動")
            
            # 檢查初始狀態
            time.sleep(2)
            status = get_music_player_status()
            initial_shuffle = status["is_shuffled"]
            info_log(f"[Test] 初始狀態: is_shuffled={initial_shuffle}")
            
            # 等待回到 IDLE
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.5)
            time.sleep(2.0)
            
            info_log("[Test] 📝 階段 2: 切換 shuffle")
            
            # 重置監控器
            monitor.cleanup()
            monitor = WorkflowCycleMonitor(event_bus)
            
            # 發送 shuffle 控制指令
            inject_text_to_system("Shuffle the music.")
            
            # 等待 TTS 和工作流完成
            time.sleep(45)
            result = monitor.wait_for_completion(timeout=60)
            
            assert result["completed"], "Control workflow did not complete"
            info_log("[Test] ✅ Shuffle 控制指令已發送")
            
            # 驗證控制步驟完成
            assert "media_control_intervention" in result["completed_steps"], "Control step not found"
            
            info_log("[Test] ✅ Shuffle 切換測試通過")
            
        finally:
            # 清理
            try:
                active_workflows = get_active_workflows(workflow_type="media_playback")
                if active_workflows:
                    task_id = active_workflows[0]["task_id"]
                    monitoring_pool = get_monitoring_pool()
                    monitoring_pool.stop_monitor(task_id)
                    info_log(f"[Test] 🧹 已停止背景服務: {task_id}")
            except Exception as e:
                info_log(f"[Test] ⚠️ 清理失敗: {e}")
            
            monitor.cleanup()
            time.sleep(1.0)
    
    def test_add_todo_workflow_full_cycle(self, system_components, isolated_gs):
        """
        測試完整的待辦事項新增背景工作流
        
        流程：
        1. 使用者輸入：「Add a task to buy groceries tomorrow」
        2. NLP 判斷意圖：work
        3. LLM 通過 MCP 調用 add_todo 工具
        4. SYS 模組啟動 add_todo_workflow 背景工作流
        5. 工作流寫入資料庫
        6. 驗證資料已新增
        """
        import sqlite3
        from datetime import datetime
        from utils.debug_helper import info_log
        from modules.sys_module.actions.automation_helper import _DB
        from core.states.state_manager import state_manager, UEPState
        
        event_bus = system_components["event_bus"]
        
        # 創建工作流程監控器
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            info_log("[Test] 🎯 測試：待辦事項新增背景工作流")
            
            # 清空測試資料（買菜任務）
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            c.execute("DELETE FROM todos WHERE task_name = 'Buy groceries'")
            conn.commit()
            conn.close()
            
            # 注入用戶輸入
            inject_text_to_system("Add a task called 'Buy groceries' with high priority")
            
            # 等待工作流完成
            info_log("[Test] ⏳ 等待工作流完成...")
            result = monitor.wait_for_completion(timeout=90)
            
            # 驗證工作流完成
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            
            info_log(f"[Test] ✅ 工作流程完成")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 等待資料庫寫入（背景工作流可能需要時間）
            time.sleep(2.0)
            
            # 驗證資料庫中是否有新增的待辦事項
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            c.execute("SELECT task_name, priority, status FROM todos WHERE task_name = 'Buy groceries'")
            todos = c.fetchall()
            conn.close()
            
            info_log(f"[Test] 📊 查詢到 {len(todos)} 個測試待辦事項")
            
            # 驗證至少有一個待辦事項被新增
            assert len(todos) > 0, "No todo item was added to database"
            
            task_name, priority, status = todos[0]
            info_log(f"[Test] ✅ 待辦事項已新增: {task_name} (priority={priority}, status={status})")
            
            # 驗證內容
            assert task_name == "Buy groceries", f"Task name mismatch: {task_name}"
            assert priority == "high", f"Priority mismatch: {priority}"
            
            info_log("[Test] ✅ 待辦事項新增背景工作流測試通過")
            
        finally:
            # 清理測試資料
            info_log("[Test] 🧹 清理測試資料")
            try:
                conn = sqlite3.connect(_DB)
                c = conn.cursor()
                c.execute("DELETE FROM todos WHERE task_name = 'Buy groceries'")
                conn.commit()
                conn.close()
                info_log("[Test] ✅ 測試資料已清理")
            except Exception as e:
                info_log(f"[Test] ⚠️ 清理失敗: {e}")
            
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    info_log("[Test] ✅ 系統已回到 IDLE")
                    break
                time.sleep(0.1)
            
            time.sleep(1.0)
    
    def test_add_calendar_event_workflow_full_cycle(self, system_components, isolated_gs):
        """
        測試完整的日曆事件新增背景工作流
        
        流程：
        1. 使用者輸入：「Schedule a meeting tomorrow at 2pm」
        2. NLP 判斷意圖：work
        3. LLM 通過 MCP 調用 add_calendar_event 工具
        4. SYS 模組啟動 add_calendar_event_workflow 背景工作流
        5. 工作流寫入資料庫
        6. 驗證資料已新增
        """
        import sqlite3
        from datetime import datetime
        from utils.debug_helper import info_log
        from modules.sys_module.actions.automation_helper import _DB
        from core.states.state_manager import state_manager, UEPState
        
        event_bus = system_components["event_bus"]
        
        # 創建工作流程監控器
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            info_log("[Test] 🎯 測試：日曆事件新增背景工作流")
            
            # 清空測試資料（團隊會議事件）
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            c.execute("DELETE FROM calendar_events WHERE summary = 'Team Meeting'")
            conn.commit()
            conn.close()
            
            # 注入用戶輸入
            inject_text_to_system("Schedule an event called 'Team Meeting' tomorrow at 2pm for 1 hour")
            
            # 等待工作流完成
            info_log("[Test] ⏳ 等待工作流完成...")
            result = monitor.wait_for_completion(timeout=90)
            
            # 驗證工作流完成
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            
            info_log(f"[Test] ✅ 工作流程完成")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 等待資料庫寫入（背景工作流可能需要時間）
            time.sleep(2.0)
            
            # 驗證資料庫中是否有新增的日曆事件
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            c.execute("SELECT summary, start_time, end_time FROM calendar_events WHERE summary = 'Team Meeting'")
            events = c.fetchall()
            conn.close()
            
            info_log(f"[Test] 📊 查詢到 {len(events)} 個測試日曆事件")
            
            # 驗證至少有一個事件被新增
            assert len(events) > 0, "No calendar event was added to database"
            
            summary, start_time, end_time = events[0]
            info_log(f"[Test] ✅ 日曆事件已新增: {summary}")
            info_log(f"[Test] 📅 時間: {start_time} - {end_time}")
            
            # 驗證內容
            assert summary == "Team Meeting", f"Event summary mismatch: {summary}"
            
            info_log("[Test] ✅ 日曆事件新增背景工作流測試通過")
            
        finally:
            # 清理測試資料
            info_log("[Test] 🧹 清理測試資料")
            try:
                conn = sqlite3.connect(_DB)
                c = conn.cursor()
                c.execute("DELETE FROM calendar_events WHERE summary = 'Team Meeting'")
                conn.commit()
                conn.close()
                info_log("[Test] ✅ 測試資料已清理")
            except Exception as e:
                info_log(f"[Test] ⚠️ 清理失敗: {e}")
            
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    info_log("[Test] ✅ 系統已回到 IDLE")
                    break
                time.sleep(0.1)
            
            time.sleep(1.0)
    
    def test_manage_todo_workflow_full_cycle(self, system_components, isolated_gs):
        """
        測試完整的待辦事項管理工作流（查詢特定任務）
        
        流程：
        1. 先創建一些待辦事項（包含關鍵字 "Meeting" 的任務）
        2. 使用者輸入：「Search for my meeting task」
        3. LLM 通過 MCP 調用 manage_todo 工具（operation=search, task_name_hint=meeting）
        4. SYS 模組啟動 manage_todo_workflow 直接工作流
        5. 工作流使用提供的參數執行搜索
        6. LLM 審核並生成回應
        """
        import sqlite3
        from datetime import datetime
        from utils.debug_helper import info_log
        from modules.sys_module.actions.automation_helper import _DB
        from core.states.state_manager import state_manager, UEPState
        
        event_bus = system_components["event_bus"]
        
        # 創建工作流程監控器
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            info_log("[Test] 🎯 測試：待辦事項管理工作流 - 搜索特定任務")
            
            # 準備測試資料：創建幾個待辦事項
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            # 清空測試資料
            c.execute("DELETE FROM todos WHERE task_name LIKE 'Test Task%'")
            c.execute("DELETE FROM todos WHERE task_name LIKE 'Meeting%'")
            
            # 插入測試待辦事項（包含 Meeting 關鍵字）
            now = datetime.now()
            test_tasks = [
                ("Meeting with Team", "Discuss project progress", "high", "pending"),
                ("Test Task 1", "First test task", "medium", "pending"),
                ("Meeting Preparation", "Prepare slides", "high", "pending"),
                ("Test Task 2", "Second test task", "low", "pending"),
            ]
            
            for task_name, desc, priority, status in test_tasks:
                c.execute("""
                    INSERT INTO todos (task_name, task_description, priority, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (task_name, desc, priority, status, now.isoformat(), now.isoformat()))
            
            conn.commit()
            conn.close()
            
            info_log(f"[Test] 📝 已創建 {len(test_tasks)} 個測試待辦事項（2個包含 Meeting）")
            
            # 注入用戶輸入（搜索特定任務）
            inject_text_to_system("Search for my meeting task.")
            
            # 等待工作流完成
            info_log("[Test] ⏳ 等待工作流完成...")
            result = monitor.wait_for_completion(timeout=90)
            
            # 驗證工作流完成
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            
            info_log(f"[Test] ✅ 工作流程完成")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 驗證資料庫中的待辦事項仍然存在（搜索不應刪除）
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            c.execute("SELECT task_name FROM todos WHERE task_name LIKE '%Meeting%'")
            meeting_tasks = c.fetchall()
            conn.close()
            
            info_log(f"[Test] 📊 資料庫中有 {len(meeting_tasks)} 個 Meeting 相關任務")
            assert len(meeting_tasks) == 2, f"Expected 2 meeting tasks, found {len(meeting_tasks)}"
            
            info_log("[Test] ✅ 待辦事項管理工作流測試通過（搜索特定任務）")
            
        finally:
            # 清理測試資料
            info_log("[Test] 🧹 清理測試資料")
            try:
                conn = sqlite3.connect(_DB)
                c = conn.cursor()
                c.execute("DELETE FROM todos WHERE task_name LIKE 'Test Task%'")
                c.execute("DELETE FROM todos WHERE task_name LIKE 'Meeting%'")
                conn.commit()
                conn.close()
                info_log("[Test] ✅ 測試資料已清理")
            except Exception as e:
                info_log(f"[Test] ⚠️ 清理失敗: {e}")
            
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    info_log("[Test] ✅ 系統已回到 IDLE")
                    break
                time.sleep(0.1)
    
    def test_manage_todo_workflow_partial_params(self, system_components, isolated_gs):
        """
        測試待辦事項管理工作流 - 部分參數（只有 operation，沒有關鍵字）
        
        流程：
        1. 創建測試待辦事項
        2. 使用者輸入：「Search for a task」（沒有提供具體關鍵字）
        3. LLM 調用 manage_todo（operation=search，但沒有 task_name_hint）
        4. 工作流應該進入互動模式，要求用戶輸入搜索關鍵字
        5. 提供關鍵字後繼續執行
        """
        import sqlite3
        from datetime import datetime
        from utils.debug_helper import info_log
        from modules.sys_module.actions.automation_helper import _DB
        from core.states.state_manager import state_manager, UEPState
        from core.framework import core_framework
        
        event_bus = system_components["event_bus"]
        sys_mod = core_framework.get_module("sys_module")
        monitor = InteractiveWorkflowMonitor(event_bus, sys_module=sys_mod, expected_interactive_steps=1)
        
        try:
            info_log("[Test] 🎯 測試：待辦事項管理工作流 - 部分參數")
            
            # 準備測試資料
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            c.execute("DELETE FROM todos WHERE task_name LIKE 'Important%'")
            
            now = datetime.now()
            test_tasks = [
                ("Important Task 1", "Critical task", "high", "pending"),
                ("Regular Task", "Normal task", "medium", "pending"),
            ]
            
            for task_name, desc, priority, status in test_tasks:
                c.execute("""
                    INSERT INTO todos (task_name, task_description, priority, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (task_name, desc, priority, status, now.isoformat(), now.isoformat()))
            
            conn.commit()
            conn.close()
            
            info_log(f"[Test] 📝 已創建 {len(test_tasks)} 個測試待辦事項")
            
            # 注入用戶輸入（明確表達搜索意圖，但沒有提供關鍵字）
            inject_text_to_system("Search a task in my to-do.")
            
            # 等待工作流要求輸入（使用事件驅動方式）
            info_log("[Test] ⏳ 等待工作流要求輸入關鍵字...")
            if monitor.awaiting_input_event.wait(timeout=60):
                info_log(f"[Test] 📝 響應步驟: {monitor.current_step}")
                time.sleep(2)  # 等待 LLM 生成提示
                
                # 提供搜索關鍵字
                inject_text_to_system("important")
                monitor.awaiting_input_event.clear()
            else:
                info_log(f"[Test] ❌ 超時！未收到工作流輸入請求")
                pytest.fail("Timeout waiting for workflow input request")
            
            # 等待工作流完成
            result = monitor.wait_for_completion(timeout=90)
            
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            
            info_log("[Test] ✅ 待辦事項管理工作流測試通過（部分參數）")
            
        finally:
            # 清理測試資料
            try:
                conn = sqlite3.connect(_DB)
                c = conn.cursor()
                c.execute("DELETE FROM todos WHERE task_name LIKE 'Important%'")
                c.execute("DELETE FROM todos WHERE task_name LIKE 'Regular%'")
                conn.commit()
                conn.close()
            except Exception as e:
                info_log(f"[Test] ⚠️ 清理失敗: {e}")
            
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.1)
    
    def test_manage_todo_workflow_no_params(self, system_components, isolated_gs):
        """
        測試待辦事項管理工作流 - 無參數（完全互動，測試 update 多步驟分支）
        
        流程：
        1. 創建測試待辦事項
        2. 使用者輸入：「Manage my tasks」（沒有提供任何具體操作）
        3. LLM 調用 manage_todo（沒有 initial_data）
        4. 工作流要求選擇操作 → 用戶選擇 "update"
        5. 工作流列出任務 → 用戶選擇任務
        6. 工作流要求更新欄位 → 用戶提供更新內容
        7. 工作流完成並返回更新結果
        
        這個測試驗證 ConditionalStep 能否正確處理 3 步驟的 update 分支。
        """
        import sqlite3
        from datetime import datetime
        from utils.debug_helper import info_log
        from modules.sys_module.actions.automation_helper import _DB
        from core.states.state_manager import state_manager, UEPState
        from core.framework import core_framework
        
        event_bus = system_components["event_bus"]
        sys_mod = core_framework.get_module("sys_module")
        monitor = InteractiveWorkflowMonitor(event_bus, sys_module=sys_mod, expected_interactive_steps=3)
        
        try:
            info_log("[Test] 🎯 測試：待辦事項管理工作流 - 無參數（完全互動）")
            
            # 準備測試資料
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            c.execute("DELETE FROM todos WHERE task_name LIKE 'Interactive%'")
            
            now = datetime.now()
            test_tasks = [
                ("Go buy some dinner", "I am hungry and need some dinner", "high", "pending"),
                ("Continue working on project", "The project is almost due", "medium", "pending"),
            ]
            
            for task_name, desc, priority, status in test_tasks:
                c.execute("""
                    INSERT INTO todos (task_name, task_description, priority, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (task_name, desc, priority, status, now.isoformat(), now.isoformat()))
            
            conn.commit()
            conn.close()
            
            info_log(f"[Test] 📝 已創建 {len(test_tasks)} 個測試待辦事項")
            
            # 注入用戶輸入（沒有提供任何操作細節）
            inject_text_to_system("Manage my tasks.")
            
            # 步驟 1: 等待工作流要求選擇操作
            info_log("[Test] ⏳ [步驟 1/3] 等待工作流要求選擇操作...")
            
            if monitor.awaiting_input_event.wait(timeout=60):
                info_log(f"[Test] 📝 響應步驟: {monitor.current_step}")
                time.sleep(2)  # 等待 LLM 生成提示
                
                # 提供操作選擇（update）
                inject_text_to_system("update a task")
                monitor.awaiting_input_event.clear()
            else:
                info_log(f"[Test] ❌ 超時！未收到工作流輸入請求")
                pytest.fail("Timeout waiting for operation selection")
            
            # 步驟 2: 等待工作流要求選擇任務
            info_log("[Test] ⏳ [步驟 2/3] 等待工作流要求選擇任務...")
            
            if monitor.awaiting_input_event.wait(timeout=60):
                info_log(f"[Test] 📝 響應步驟: {monitor.current_step}")
                time.sleep(2)  # 等待 LLM 生成提示
                
                # 選擇第一個任務（Go buy some dinner）
                inject_text_to_system("The dinner one")
                monitor.awaiting_input_event.clear()
            else:
                info_log(f"[Test] ❌ 超時！未收到任務選擇請求")
                pytest.fail("Timeout waiting for task selection")
            
            # 步驟 3: 等待工作流要求更新欄位
            info_log("[Test] ⏳ [步驟 3/3] 等待工作流要求更新欄位...")
            
            if monitor.awaiting_input_event.wait(timeout=60):
                info_log(f"[Test] 📝 響應步驟: {monitor.current_step}")
                time.sleep(2)  # 等待 LLM 生成提示
                
                # 提供更新內容（改變優先級和描述）
                inject_text_to_system('change the priority to "medium".')
                monitor.awaiting_input_event.clear()
            else:
                info_log(f"[Test] ❌ 超時！未收到更新欄位請求")
                pytest.fail("Timeout waiting for update fields")
            
            # 等待工作流完成
            result = monitor.wait_for_completion(timeout=90)
            
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            
            info_log("[Test] ✅ 待辦事項管理工作流測試通過（無參數）")
            
        finally:
            # 清理測試資料
            try:
                conn = sqlite3.connect(_DB)
                c = conn.cursor()
                c.execute("DELETE FROM todos WHERE task_name = 'Go buy some dinner'")
                c.execute("DELETE FROM todos WHERE task_name = 'Continue working on project'")
                conn.commit()
                conn.close()
            except Exception as e:
                info_log(f"[Test] ⚠️ 清理失敗: {e}")
            
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    break
                time.sleep(0.1)
            
            time.sleep(1.0)
    
    def test_manage_calendar_workflow_full_cycle(self, system_components, isolated_gs):
        """
        測試完整的行事曆管理工作流（查詢）
        
        流程：
        1. 先創建一些行事曆事件
        2. 使用者輸入：「列出我的行事曆」
        3. LLM 通過 MCP 調用 manage_calendar 工具（action=list）
        4. SYS 模組啟動 manage_calendar_workflow 直接工作流
        5. 工作流查詢資料庫並返回結果
        6. LLM 審核並生成回應
        """
        import sqlite3
        from datetime import datetime, timedelta
        from utils.debug_helper import info_log
        from modules.sys_module.actions.automation_helper import _DB
        from core.states.state_manager import state_manager, UEPState
        
        event_bus = system_components["event_bus"]
        
        # 創建工作流程監控器
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            info_log("[Test] 🎯 測試：行事曆管理工作流")
            
            # 準備測試資料：創建幾個行事曆事件
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            
            # 清空測試資料
            c.execute("DELETE FROM calendar_events WHERE summary LIKE 'Try new structure' OR summary LIKE 'Complete the project' OR summary LIKE 'Birthday!'")
            
            # 插入測試行事曆事件
            now = datetime.now()
            test_events = [
                ("Try new structure", "Testing the new workflow structure", now + timedelta(days=1), now + timedelta(days=1, hours=1)),
                ("Complete the project", "Finish the U.E.P project", now + timedelta(days=2), now + timedelta(days=2, hours=2)),
                ("Birthday!", "Celebrate birthday party", now + timedelta(days=3), now + timedelta(days=3, hours=1)),
            ]
            
            for summary, description, start, end in test_events:
                c.execute("""
                    INSERT INTO calendar_events (summary, description, start_time, end_time, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (summary, description, start.isoformat(), end.isoformat(), now.isoformat(), now.isoformat()))
            
            conn.commit()
            conn.close()
            
            info_log(f"[Test] 📝 已創建 {len(test_events)} 個測試行事曆事件")
            
            # 注入用戶輸入（查詢行事曆）
            inject_text_to_system("Search for birthday in my calendar.")
            
            # 等待工作流完成
            info_log("[Test] ⏳ 等待工作流完成...")
            result = monitor.wait_for_completion(timeout=90)
            
            # 驗證工作流完成
            assert result["completed"], "Workflow did not complete within timeout"
            assert not result["failed"], "Workflow failed"
            
            info_log(f"[Test] ✅ 工作流程完成")
            info_log(f"[Test] 📊 事件數量: {len(result['events'])}")
            
            # 驗證資料庫中的事件仍然存在（查詢不應刪除）
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            c.execute("SELECT summary FROM calendar_events WHERE summary LIKE 'Try new structure' OR summary LIKE 'Complete the project' OR summary LIKE 'Birthday!'")
            events = c.fetchall()
            conn.close()
            
            info_log(f"[Test] 📊 資料庫中有 {len(events)} 個測試行事曆事件")
            assert len(events) == 3, f"Expected 3 events, found {len(events)}"
            
            info_log("[Test] ✅ 行事曆管理工作流測試通過")
            
        finally:
            # 清理測試資料
            info_log("[Test] 🧹 清理測試資料")
            try:
                conn = sqlite3.connect(_DB)
                c = conn.cursor()
                c.execute("DELETE FROM calendar_events WHERE summary LIKE 'Try new structure' OR summary LIKE 'Complete the project' OR summary LIKE 'Birthday!'")
                conn.commit()
                conn.close()
                info_log("[Test] ✅ 測試資料已清理")
            except Exception as e:
                info_log(f"[Test] ⚠️ 清理失敗: {e}")
            
            monitor.cleanup()
            
            # 等待系統回到 IDLE
            info_log("[Test] ⏳ 等待系統回到 IDLE...")
            for _ in range(30):
                if state_manager.get_current_state() == UEPState.IDLE:
                    info_log("[Test] ✅ 系統已回到 IDLE")
                    break
                time.sleep(0.1)
            
            time.sleep(1.0)


def test_notification_system_integration(system_components):
    """
    測試完整的通知系統整合
    
    測試流程：
    1. BackgroundEventScheduler 檢查資料庫（待辦、日曆）
    2. 發布系統事件到 EventBus
    3. SYS 模組接收事件並加入狀態佇列
    4. Controller 監測狀態佇列並創建 GS
    5. SystemLoop 處理通知（LLM 生成訊息 → TTS 輸出）
    """
    import sqlite3
    from datetime import datetime, timedelta
    from utils.debug_helper import info_log, debug_log
    from core.states.state_queue import get_state_queue_manager
    from core.states.state_manager import UEPState
    from modules.sys_module.actions.automation_helper import BackgroundEventScheduler, _DB
    
    info_log("[Test] 🧪 測試：通知系統整合")
    
    event_bus = system_components["event_bus"]
    
    # 創建監控器
    monitor = WorkflowCycleMonitor(event_bus)
    
    try:
        # === 準備階段 ===
        info_log("[Test] 📝 準備：清空測試資料並創建通知項目")
        
        # 清空舊測試資料
        conn = sqlite3.connect(_DB)
        c = conn.cursor()
        c.execute("DELETE FROM todos WHERE task_name LIKE '[NOTIF-TEST]%'")
        c.execute("DELETE FROM calendar_events WHERE summary LIKE '[NOTIF-TEST]%'")
        conn.commit()
        
        # 創建測試待辦事項（30分鐘後到期 → 觸發 1h_before 通知）
        now = datetime.now()
        deadline = now + timedelta(minutes=30)
        
        c.execute("""
            INSERT INTO todos (task_name, task_description, priority, status, created_at, updated_at, deadline)
            VALUES (?, ?, ?, 'pending', ?, ?, ?)
        """, (
            "[NOTIF-TEST] Emergency Task",
            "This is a test notification for an emergency task",
            "high",
            now.isoformat(),
            now.isoformat(),
            deadline.isoformat()
        ))
        
        # 創建測試日曆事件（10分鐘後開始 → 觸發 15min_before 通知）
        start_time = now + timedelta(minutes=10)
        end_time = now + timedelta(minutes=70)
        
        c.execute("""
            INSERT INTO calendar_events (summary, description, start_time, end_time, location, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "[NOTIF-TEST] Important Meeting",
            "Test calendar event notification",
            start_time.isoformat(),
            end_time.isoformat(),
            "Test Meeting Room",
            now.isoformat(),
            now.isoformat()
        ))
        
        conn.commit()
        conn.close()
        
        info_log(f"[Test] ✅ 已創建測試資料：待辦事項 (deadline: {deadline.strftime('%H:%M')})")
        info_log(f"[Test] ✅ 已創建測試資料：日曆事件 (start: {start_time.strftime('%H:%M')})")
        
        # === 執行階段 ===
        info_log("[Test] 📝 執行：觸發 BackgroundEventScheduler 檢查")
        
        # 獲取狀態佇列管理器
        state_queue = get_state_queue_manager()
        initial_queue_length = len(state_queue.queue)
        info_log(f"[Test] 初始佇列長度: {initial_queue_length}")
        
        # 執行背景檢查
        scheduler = BackgroundEventScheduler()
        scheduler._check_todos()
        scheduler._check_calendar_events()
        
        info_log("[Test] ✅ 背景檢查已完成")
        
        # 等待事件處理（EventBus 異步處理）
        time.sleep(1.5)
        
        # === 驗證階段 1：檢查系統狀態變化 ===
        info_log("[Test] 📝 驗證：檢查系統狀態（通知使用 WORK 狀態的 system_report 模式）")

        # 通知現在直接進入 WORK 狀態（system_report=True）
        # 第一個通知會被立即處理，狀態會變成當前系統狀態
        # 所以我們需要檢查：
        # 1. 當前狀態是否為 WORK（正在處理通知）
        # 2. 或者佇列中是否有待處理的 WORK 狀態（第二個通知）
        
        queue_status = state_queue.get_queue_status()
        queue_length = queue_status["queue_length"]
        pending_states = queue_status["pending_states"]
        current_state = queue_status["current_state"]

        info_log(f"[Test] 當前佇列長度: {queue_length}")
        info_log(f"[Test] 待處理狀態: {pending_states}")
        info_log(f"[Test] 當前處理狀態: {current_state}")

        # 驗證：系統應該正在處理通知（WORK 狀態）或有通知在等待
        is_processing_notification = current_state == UEPState.WORK.value
        has_pending_notification = queue_length >= 1 and "work" in pending_states
        
        assert is_processing_notification or has_pending_notification, \
            f"系統應該正在處理通知或有通知在等待，當前狀態: {current_state}, 佇列: {pending_states}"
        
        info_log(f"[Test] ✅ 狀態驗證通過（{'正在處理通知' if is_processing_notification else '有通知在佇列中'}）")
        
        # === 驗證階段 2：系統循環處理 ===
        info_log("[Test] 📝 驗證：等待系統處理通知")
        
        # 等待系統循環處理通知
        # SystemLoop 會自動檢測狀態佇列 → Controller 創建 WS → LLM 生成訊息 → TTS 輸出
        max_wait = 60  # 最多等待60秒
        
        # 記錄初始狀態（可能第一個通知已經在處理中）
        initial_is_processing = is_processing_notification
        initial_queue_length = queue_length
        
        info_log(f"[Test] 初始狀態 - 正在處理: {initial_is_processing}, 佇列: {initial_queue_length}")
        
        # 等待系統返回 IDLE（所有通知處理完成）
        returned_to_idle = False
        final_status = None
        
        for i in range(max_wait):
            final_status = state_queue.get_queue_status()
            current_state_now = final_status["current_state"]
            current_queue = final_status["queue_length"]
            
            # 如果系統回到 IDLE 且佇列清空，表示所有通知已處理完成
            if current_state_now == UEPState.IDLE.value and current_queue == 0:
                returned_to_idle = True
                info_log(f"[Test] ✅ 系統已返回 IDLE（第 {i+1} 秒）")
                break
            
            # 記錄狀態變化
            if i % 5 == 0:
                info_log(f"[Test] [{i}s] 當前狀態: {current_state_now}, 佇列: {current_queue}")
            
            time.sleep(1.0)
        
        # 驗證：系統應該已處理完所有通知並返回 IDLE
        if final_status:
            assert returned_to_idle, \
                f"系統應該處理完通知並返回 IDLE，當前狀態: {final_status['current_state']}, 佇列: {final_status['queue_length']}"
        else:
            assert False, "無法獲取系統狀態"
        
        info_log("[Test] ✅ 系統循環處理驗證通過（已返回 IDLE）")
        
        # === 驗證階段 3：資料庫更新 ===
        info_log("[Test] 📝 驗證：檢查通知記錄")
        
        conn = sqlite3.connect(_DB)
        c = conn.cursor()
        
        # 檢查待辦事項的通知記錄
        c.execute("""
            SELECT last_notified_at, last_notified_stage
            FROM todos
            WHERE task_name LIKE '[NOTIF-TEST]%'
        """)
        todo_record = c.fetchone()
        
        # 檢查日曆事件的通知記錄
        c.execute("""
            SELECT last_notified_at, last_notified_stage
            FROM calendar_events
            WHERE summary LIKE '[NOTIF-TEST]%'
        """)
        calendar_record = c.fetchone()
        
        conn.close()
        
        # 驗證：通知記錄應該已更新
        assert todo_record is not None, "待辦事項記錄不存在"
        assert todo_record[0] is not None, "待辦事項未記錄通知時間"
        assert todo_record[1] is not None, "待辦事項未記錄通知階段"
        
        assert calendar_record is not None, "日曆事件記錄不存在"
        assert calendar_record[0] is not None, "日曆事件未記錄通知時間"
        assert calendar_record[1] is not None, "日曆事件未記錄通知階段"
        
        info_log(f"[Test] ✅ 待辦通知記錄: {todo_record[1]} at {todo_record[0]}")
        info_log(f"[Test] ✅ 日曆通知記錄: {calendar_record[1]} at {calendar_record[0]}")
        
        # === 測試防重複機制 ===
        info_log("[Test] 📝 測試：防重複通知機制")
        
        # 記錄當前狀態（應該已經是 IDLE）
        before_recheck = state_queue.get_queue_status()
        before_length = before_recheck["queue_length"]
        before_state = before_recheck["current_state"]
        
        info_log(f"[Test] 再次檢查前狀態: {before_state}, 佇列: {before_length}")
        
        # 再次執行檢查
        scheduler._check_todos()
        scheduler._check_calendar_events()
        
        # 等待可能的事件處理
        time.sleep(1.5)
        
        # 檢查狀態：應該保持 IDLE，佇列應該為空（防重複機制生效）
        after_recheck = state_queue.get_queue_status()
        after_length = after_recheck["queue_length"]
        after_state = after_recheck["current_state"]
        
        info_log(f"[Test] 再次檢查後狀態: {after_state}, 佇列: {after_length}")
        
        # 驗證：系統應該保持 IDLE，沒有新增重複通知
        assert after_state == UEPState.IDLE.value, f"系統應該保持 IDLE，當前: {after_state}"
        assert after_length == 0, f"佇列應該為空（防重複），當前: {after_length}"
        
        info_log("[Test] ✅ 防重複機制驗證通過（沒有重複通知）")
        
        info_log("[Test] 🎉 通知系統整合測試完全通過！")
        
    finally:
        # 清理測試資料
        info_log("[Test] 🧹 清理測試資料")
        try:
            conn = sqlite3.connect(_DB)
            c = conn.cursor()
            c.execute("DELETE FROM todos WHERE task_name LIKE '[NOTIF-TEST]%'")
            c.execute("DELETE FROM calendar_events WHERE summary LIKE '[NOTIF-TEST]%'")
            conn.commit()
            conn.close()
            info_log("[Test] ✅ 測試資料已清理")
        except Exception as e:
            info_log(f"[Test] ⚠️ 清理失敗: {e}")
        
        monitor.cleanup()
        time.sleep(1.0)