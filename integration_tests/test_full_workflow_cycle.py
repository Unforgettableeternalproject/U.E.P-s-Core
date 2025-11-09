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
            # 2. 設置檔案路徑到 WorkingContext（不使用 test_ 前綴）
            from core.working_context import working_context_manager
            working_context_manager.set_context_data("current_file_path", str(test_file))
            info_log(f"[Test] 📁 設置檔案路徑: {test_file}")
            
            # 3. 注入使用者輸入
            info_log("[Test] 🎯 測試：檔案讀取完整循環")
            inject_text_to_system("Read the content of the test file")
            
            # 3. 等待工作流程完成（最多 60 秒）
            info_log("[Test] ⏳ 等待工作流程完成...")
            result = monitor.wait_for_completion(timeout=60)
            
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
            
            info_log("[Test] ✅ 檔案讀取完整循環測試通過")
            
        finally:
            # 清理監控器
            monitor.cleanup()
    
    @pytest.mark.skip(reason="Need to fix intelligent_archive workflow entry point logic")
    def test_intelligent_archive_full_cycle(self, system_components, test_file):
        """
        測試完整的智慧歸檔工作流程循環
        
        流程：
        1. 使用者輸入：「歸檔這個檔案到 D:\\」
        2. NLP 判斷意圖：file_operation
        3. LLM 通過 MCP 啟動 file_intelligent_archive_workflow
        4. SYS 模組執行工作流程
        5. 工作流程完成，檔案被歸檔
        """
        from utils.debug_helper import info_log
        
        system_loop = system_components["system_loop"]
        event_bus = system_components["event_bus"]
        
        monitor = WorkflowCycleMonitor(event_bus)
        
        try:
            info_log("[Test] 🎯 測試：智慧歸檔完整循環")
            inject_text_to_system(
                "Archive this file to D drive",
                initial_data={
                    "file_path": str(test_file),
                    "target_dir": "D:\\",
                    "workflow_type": "intelligent_archive"
                }
            )
            
            result = monitor.wait_for_completion(timeout=60)
            
            assert result["completed"], "Workflow did not complete"
            assert not result["failed"], "Workflow failed"
            
            info_log("[Test] ✅ 智慧歸檔完整循環測試通過")
            
        finally:
            monitor.cleanup()
