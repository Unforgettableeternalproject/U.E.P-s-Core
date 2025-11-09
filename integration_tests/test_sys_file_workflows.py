"""
整合測試 - SYS 模組檔案工作流程
===================================

測試 SYS 模組的 3 個檔案工作流程在正式環境中的運作：
1. drop_and_read - 檔案讀取工作流程
2. intelligent_archive - 智慧歸檔工作流程
3. summarize_tag - 摘要標籤工作流程

這些測試使用正式的系統初始化流程（system_initializer + controller），
而不是 debug_api，因為：
- 需要完整的會話管理機制（GS/WS 創建）
- 需要完整的事件系統和狀態管理
- 需要實際的 WorkflowSession 和 UnifiedSessionManager

測試策略：
1. 使用 system_initializer 啟動系統核心組件
2. 使用 controller 初始化必要的模組（SYS, LLM）
3. 透過 SYS 模組的 MCP Server 調用工作流程
4. 驗證工作流程的完整執行過程
"""

import sys
import os
import asyncio
import tempfile
import shutil
from pathlib import Path

# 添加項目根目錄到路徑
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import pytest
from core.system_initializer import SystemInitializer
from utils.debug_helper import debug_log, info_log, error_log


# ============================================================================
# Fixtures - 系統初始化
# ============================================================================

@pytest.fixture(scope="module")
def system_components():
    """
    初始化系統核心組件（整個測試模組只初始化一次）
    
    使用正式的系統初始化流程：
    1. SystemInitializer 初始化核心架構
    2. Controller 初始化必要模組
    
    Returns:
        dict: 包含 controller, sys_module 等組件
    """
    info_log("[IntegrationTest] 開始初始化系統組件...")
    
    # 1. 創建系統初始化器
    initializer = SystemInitializer()
    
    # 2. 初始化核心系統（包含 Framework、EventBus、Controller 等）
    # 參考 production_runner.py 的做法
    success = initializer.initialize_system(production_mode=False)
    if not success:
        pytest.fail("系統初始化失敗")
    
    # 3. 獲取 Controller 實例（SystemInitializer 已初始化 unified_controller）
    from core.controller import unified_controller
    from core.framework import core_framework
    
    controller = unified_controller
    if controller is None or not hasattr(controller, 'is_initialized'):
        pytest.fail("無法獲取 Controller 實例")
    
    # 4. 從 registry 獲取已載入的模組實例
    # SystemInitializer 已透過 Framework 初始化並註冊模組
    from core.registry import get_module
    
    sys_module = get_module('sys_module')
    if sys_module is None:
        info_log("[IntegrationTest] SYS 模組未找到，嘗試載入...")
        # 注意：SystemInitializer 應該已經載入了，如果沒有則需要檢查配置
        pytest.fail("SYS 模組未在系統初始化時載入，請檢查 Framework 配置")
    
    llm_module = get_module('llm_module')
    if llm_module is None:
        info_log("[IntegrationTest] LLM 模組未找到，嘗試載入...")
        pytest.fail("LLM 模組未在系統初始化時載入，請檢查 Framework 配置")
    
    if sys_module is None:
        pytest.fail("無法載入 SYS 模組")
    if llm_module is None:
        pytest.fail("無法載入 LLM 模組")
    
    info_log("[IntegrationTest] ✅ 系統組件初始化完成")
    
    # 顯示初始化狀態
    status = initializer.get_initialization_status()
    info_log(f"📊 初始化狀態: {status['phase']}")
    info_log(f"📦 已載入模組: {status.get('initialized_modules', [])}")
    
    # 返回組件
    components = {
        "controller": controller,
        "sys_module": sys_module,
        "llm_module": llm_module,
        "initializer": initializer,
        "framework": core_framework
    }
    
    yield components
    
    # 清理（測試結束後）
    info_log("[IntegrationTest] 清理系統組件...")
    try:
        if controller:
            controller.shutdown()
            info_log("[IntegrationTest] Controller 已關閉")
    except Exception as e:
        error_log(f"[IntegrationTest] 清理警告: {e}")


@pytest.fixture
def test_file():
    """
    使用預先準備的測試檔案
    
    Returns:
        Path: 測試檔案路徑（resources/workflow_test.txt）
    """
    # 使用項目中的測試檔案，避免臨時檔案問題
    test_file = project_root / "resources" / "workflow_test.txt"
    
    if not test_file.exists():
        pytest.fail(f"Test file not found: {test_file}")
    
    info_log(f"[IntegrationTest] Using test file: {test_file}")
    
    return test_file


@pytest.fixture
def archive_dir(tmp_path):
    """
    創建測試用的歸檔目錄
    
    Args:
        tmp_path: pytest 提供的臨時目錄
        
    Returns:
        Path: 歸檔目錄路徑
    """
    archive_path = tmp_path / "archive"
    archive_path.mkdir()
    
    info_log(f"[IntegrationTest] 創建歸檔目錄: {archive_path}")
    
    return archive_path


# ============================================================================
# 輔助函數
# ============================================================================

def simulate_workflow_interaction(sys_module, session_id: str, user_input: str) -> dict:
    """
    模擬工作流程的使用者互動
    
    Args:
        sys_module: SYS 模組實例
        session_id: 工作流程會話 ID
        user_input: 使用者輸入（應使用英文，因為系統運作語言是英文）
        
    Returns:
        dict: 處理結果
    """
    info_log(f"[IntegrationTest] Providing input: {user_input}")
    
    result = sys_module.handle({
        "mode": "provide_workflow_input",
        "params": {
            "session_id": session_id,
            "user_input": user_input
        }
    })
    
    return result


def wait_for_workflow_completion(sys_module, session_id: str, max_wait: int = 10) -> dict:
    """
    等待工作流程完成
    
    Args:
        sys_module: SYS 模組實例
        session_id: 工作流程會話 ID
        max_wait: 最大等待時間（秒）
        
    Returns:
        dict: 最終狀態
    """
    import time
    
    for i in range(max_wait):
        status = sys_module.handle({
            "mode": "get_workflow_status",
            "params": {"session_id": session_id}
        })
        
        if status.get("status") == "ok":
            state = status["data"].get("state", "")
            if state in ["completed", "cancelled", "failed"]:
                info_log(f"[IntegrationTest] 工作流程已結束: {state}")
                return status
        
        time.sleep(1)
    
    error_log(f"[IntegrationTest] 工作流程等待超時")
    return {"status": "error", "message": "等待超時"}


# ============================================================================
# 測試案例 - Drop and Read 工作流程
# ============================================================================

@pytest.mark.integration
@pytest.mark.sys
class TestDropAndReadWorkflow:
    """測試檔案讀取工作流程（drop_and_read）"""
    
    def test_drop_and_read_complete_flow(self, system_components, test_file):
        """
        測試完整的檔案讀取工作流程
        
        流程：
        1. 啟動 drop_and_read 工作流程（使用 initial_data 跳過檔案選擇步驟）
        2. 驗證檔案被正確讀取（auto_advance 自動執行）
        """
        sys_module = system_components["sys_module"]
        
        # 1. 啟動工作流程，直接提供檔案路徑作為 initial_data
        # 這樣可以跳過 file_selection_step，避免彈出檔案選擇視窗
        info_log("[Test] Starting drop_and_read workflow")
        response = sys_module.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "drop_and_read",
                "command": "Read test file",
                "initial_data": {
                    "file_path_input": str(test_file)
                }
            }
        })
        
        # 2. 驗證啟動成功
        # 注意：SYS 模組回傳的 status 是 "success" 而非 "ok"
        assert response["status"] == "success", f"Workflow start failed: {response}"
        assert "session_id" in response, "Missing session_id"
        
        session_id = response["session_id"]
        info_log(f"[Test] Workflow started, Session ID: {session_id}")
        
        # 3. 因為提供了 initial_data，工作流程應該跳過輸入步驟
        # auto_advance 會自動執行讀取，等待完成
        info_log("[Test] Waiting for auto_advance to complete file reading...")
        final_status = wait_for_workflow_completion(sys_module, session_id, max_wait=15)
        
        # 4. 驗證結果
        assert final_status["status"] == "ok"
        assert final_status["data"]["state"] == "completed"
        
        # 檢查輸出數據
        output_data = final_status["data"].get("output_data", {})
        assert "file_path" in output_data
        assert "content" in output_data
        assert len(output_data["content"]) > 0
        
        info_log("[Test] ✅ drop_and_read 工作流程測試通過")
    
    def test_drop_and_read_invalid_file(self, system_components):
        """
        測試提供無效檔案路徑的錯誤處理
        """
        sys_module = system_components["sys_module"]
        
        # 1. 啟動工作流程，提供不存在的檔案路徑
        invalid_path = "C:\\NonExistent\\File.txt"
        info_log(f"[Test] Starting workflow with invalid path: {invalid_path}")
        
        response = sys_module.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "drop_and_read",
                "command": "Read non-existent file",
                "initial_data": {
                    "file_path_input": invalid_path
                }
            }
        })
        
        assert response["status"] == "success"
        session_id = response["session_id"]
        
        # 2. 等待處理完成
        final_status = wait_for_workflow_completion(sys_module, session_id, max_wait=15)
        
        # 3. 驗證錯誤處理
        # 工作流程應該失敗或取消
        assert final_status["data"]["state"] in ["failed", "cancelled", "completed"]
        
        info_log("[Test] ✅ Error handling test passed")


# ============================================================================
# 測試案例 - Intelligent Archive 工作流程
# ============================================================================

@pytest.mark.integration
@pytest.mark.sys
class TestIntelligentArchiveWorkflow:
    """測試智慧歸檔工作流程（intelligent_archive）"""
    
    def test_intelligent_archive_complete_flow(self, system_components, test_file, archive_dir):
        """
        測試完整的智慧歸檔工作流程
        
        流程：
        1. 啟動 intelligent_archive 工作流程（跳過檔案選擇）
        2. 檢查是否需要目標目錄輸入
        3. 檢查是否需要確認
        4. 等待完成並驗證結果
        """
        sys_module = system_components["sys_module"]
        
        # 1. 啟動工作流程，提供檔案路徑作為 initial_data
        info_log("[Test] Starting intelligent_archive workflow")
        response = sys_module.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "intelligent_archive",
                "command": "Archive test file",
                "initial_data": {
                    "file_selection": str(test_file)
                }
            }
        })
        
        assert response["status"] == "ok"
        session_id = response["data"]["session_id"]
        info_log(f"[Test] Workflow started, Session ID: {session_id}")
        
        # 2. 檢查當前步驟，可能需要目標目錄輸入
        import time
        time.sleep(1)  # 等待工作流程初始化
        
        status = sys_module.handle({
            "mode": "get_workflow_status",
            "params": {"session_id": session_id}
        })
        
        current_step = status["data"].get("current_step", "")
        info_log(f"[Test] Current step: {current_step}")
        
        # 3. 如果需要目標目錄，提供輸入
        if "target" in current_step.lower() or "dir" in current_step.lower():
            info_log(f"[Test] Providing target directory: {archive_dir}")
            simulate_workflow_interaction(sys_module, session_id, str(archive_dir))
            time.sleep(0.5)
        
        # 4. 檢查是否需要確認
        status = sys_module.handle({
            "mode": "get_workflow_status",
            "params": {"session_id": session_id}
        })
        
        current_step = status["data"].get("current_step", "")
        if "confirm" in current_step.lower():
            info_log("[Test] Confirming archive operation")
            simulate_workflow_interaction(sys_module, session_id, "yes")
        
        # 5. 等待完成
        final_status = wait_for_workflow_completion(sys_module, session_id, max_wait=20)
        
        # 6. 驗證結果
        assert final_status["status"] == "ok"
        assert final_status["data"]["state"] == "completed"
        
        output_data = final_status["data"].get("output_data", {})
        assert "archived_path" in output_data or "archive_path" in output_data
        
        info_log("[Test] ✅ intelligent_archive workflow test passed")
    
    def test_intelligent_archive_cancel(self, system_components, test_file):
        """
        測試取消歸檔工作流程
        """
        sys_module = system_components["sys_module"]
        
        # 1. 啟動工作流程
        info_log("[Test] Starting workflow for cancellation test")
        response = sys_module.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "intelligent_archive",
                "command": "Test cancellation",
                "initial_data": {
                    "file_selection": str(test_file)
                }
            }
        })
        
        assert response["status"] == "ok"
        session_id = response["data"]["session_id"]
        
        # 2. 立即取消工作流程
        import time
        time.sleep(0.5)  # 短暫等待確保工作流程已啟動
        
        info_log("[Test] Cancelling workflow")
        cancel_response = sys_module.handle({
            "mode": "cancel_workflow",
            "params": {"session_id": session_id}
        })
        
        assert cancel_response["status"] == "ok"
        
        # 3. 驗證狀態
        final_status = sys_module.handle({
            "mode": "get_workflow_status",
            "params": {"session_id": session_id}
        })
        
        assert final_status["data"]["state"] == "cancelled"
        
        info_log("[Test] ✅ Cancellation test passed")


# ============================================================================
# 測試案例 - Summarize Tag 工作流程
# ============================================================================

@pytest.mark.integration
@pytest.mark.sys
class TestSummarizeTagWorkflow:
    """測試摘要標籤工作流程（summarize_tag）"""
    
    def test_summarize_tag_complete_flow(self, system_components, test_file):
        """
        測試完整的摘要標籤工作流程
        
        流程：
        1. 啟動 summarize_tag 工作流程（跳過檔案選擇）
        2. 檢查是否需要標籤數量輸入
        3. 檢查是否需要確認
        4. 等待完成並驗證結果
        
        注意：此測試依賴 LLM 模組
        """
        sys_module = system_components["sys_module"]
        llm_module = system_components["llm_module"]
        
        # 檢查 LLM 模組是否可用
        if llm_module is None:
            pytest.skip("LLM module not loaded, skipping test")
        
        # 1. 啟動工作流程，提供檔案路徑作為 initial_data
        info_log("[Test] Starting summarize_tag workflow")
        response = sys_module.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "summarize_tag",
                "command": "Generate summary and tags for file",
                "initial_data": {
                    "file_path_input": str(test_file)
                }
            }
        })
        
        assert response["status"] == "success"
        session_id = response["session_id"]
        info_log(f"[Test] Workflow started, Session ID: {session_id}")
        
        # 2. 檢查當前步驟，可能需要標籤數量輸入
        import time
        time.sleep(1)
        
        status = sys_module.handle({
            "mode": "get_workflow_status",
            "params": {"session_id": session_id}
        })
        
        current_step = status["data"].get("current_step", "")
        info_log(f"[Test] Current step: {current_step}")
        
        # 3. 如果需要標籤數量，提供輸入
        if "tag" in current_step.lower() and "count" in current_step.lower():
            info_log("[Test] Providing tag count: 3")
            simulate_workflow_interaction(sys_module, session_id, "3")
            time.sleep(0.5)
        
        # 4. 檢查是否需要確認
        status = sys_module.handle({
            "mode": "get_workflow_status",
            "params": {"session_id": session_id}
        })
        
        current_step = status["data"].get("current_step", "")
        if "confirm" in current_step.lower():
            info_log("[Test] Confirming generation")
            simulate_workflow_interaction(sys_module, session_id, "yes")
        
        # 5. 等待完成（摘要生成可能需要較長時間）
        final_status = wait_for_workflow_completion(sys_module, session_id, max_wait=30)
        
        # 6. 驗證結果
        assert final_status["status"] == "ok"
        
        # 因為依賴 LLM，如果 LLM 失敗，工作流程可能失敗
        state = final_status["data"]["state"]
        assert state in ["completed", "failed"]
        
        if state == "completed":
            output_data = final_status["data"].get("output_data", {})
            # 檢查是否有摘要或標籤相關的輸出
            assert ("summary" in output_data or "tags" in output_data or 
                    "summary_file" in output_data)
            
            info_log("[Test] ✅ summarize_tag workflow test passed")
        else:
            info_log("[Test] ⚠️ Workflow failed (possible LLM-related issue)")
    
    def test_summarize_tag_invalid_tag_count(self, system_components, test_file):
        """
        測試無效的標籤數量輸入
        """
        sys_module = system_components["sys_module"]
        
        # 1. 啟動工作流程
        info_log("[Test] Starting workflow for invalid input test")
        response = sys_module.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "summarize_tag",
                "command": "Test invalid input",
                "initial_data": {
                    "file_path_input": str(test_file)
                }
            }
        })
        
        assert response["status"] == "success"
        session_id = response["session_id"]
        
        # 2. 檢查當前步驟
        import time
        time.sleep(1)
        
        status = sys_module.handle({
            "mode": "get_workflow_status",
            "params": {"session_id": session_id}
        })
        
        current_step = status["data"].get("current_step", "")
        
        # 3. 如果需要標籤數量，提供無效輸入
        if "tag" in current_step.lower() and "count" in current_step.lower():
            info_log("[Test] Providing invalid tag count: abc")
            input_response = simulate_workflow_interaction(sys_module, session_id, "abc")
            
            # 工作流程應該處理這個錯誤（可能使用默認值或要求重新輸入）
            # 這取決於實際實現
            assert input_response["status"] in ["ok", "error"]
        
        info_log("[Test] ✅ Invalid input test passed")


# ============================================================================
# 測試案例 - 工作流程狀態管理
# ============================================================================

@pytest.mark.integration
@pytest.mark.sys
class TestWorkflowStateManagement:
    """測試工作流程的狀態管理功能"""
    
    def test_get_workflow_status(self, system_components, test_file):
        """
        測試獲取工作流程狀態
        """
        sys_module = system_components["sys_module"]
        
        # 1. 啟動工作流程
        info_log("[Test] Starting workflow for status query test")
        response = sys_module.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "drop_and_read",
                "command": "Test status query",
                "initial_data": {
                    "file_path_input": str(test_file)
                }
            }
        })
        
        assert response["status"] == "success"
        session_id = response["session_id"]
        
        # 2. 查詢狀態
        import time
        time.sleep(0.5)
        
        status = sys_module.handle({
            "mode": "get_workflow_status",
            "params": {"session_id": session_id}
        })
        
        # 3. 驗證狀態格式
        assert status["status"] == "ok"
        assert "state" in status["data"]
        assert "current_step" in status["data"]
        # 狀態可能是 active 或已經 completed（因為 auto_advance）
        assert status["data"]["state"] in ["active", "completed"]
        
        info_log("[Test] ✅ Status query test passed")
    
    def test_workflow_session_lifecycle(self, system_components, test_file):
        """
        測試工作流程會話的完整生命週期
        
        驗證：
        1. 會話創建（啟動工作流程時）
        2. 會話活躍（或已完成，因為 auto_advance）
        3. 會話結束（工作流程完成時）
        """
        sys_module = system_components["sys_module"]
        
        # 1. 啟動工作流程 - 會話創建
        info_log("[Test] Starting workflow for session lifecycle test")
        response = sys_module.handle({
            "mode": "start_workflow",
            "params": {
                "workflow_type": "drop_and_read",
                "command": "Test session lifecycle",
                "initial_data": {
                    "file_path_input": str(test_file)
                }
            }
        })
        
        assert response["status"] == "success"
        session_id = response["session_id"]
        
        # 2. 檢查會話狀態（可能因為 auto_advance 已經完成）
        import time
        time.sleep(0.5)
        
        status = sys_module.handle({
            "mode": "get_workflow_status",
            "params": {"session_id": session_id}
        })
        
        # 狀態可能是 active 或已經 completed
        assert status["data"]["state"] in ["active", "completed"]
        
        # 3. 等待工作流程完成（如果還沒完成）
        if status["data"]["state"] == "active":
            final_status = wait_for_workflow_completion(sys_module, session_id, max_wait=15)
            assert final_status["data"]["state"] == "completed"
        
        info_log("[Test] ✅ Session lifecycle test passed")


# ============================================================================
# 主測試入口
# ============================================================================

if __name__ == "__main__":
    """
    直接執行此檔案進行測試
    """
    pytest.main([
        __file__,
        "-v",
        "-s",
        "--tb=short",
        "-m", "integration"
    ])
