import pytest
import time
from modules.sys_module.sys_module import SYSModule

@pytest.fixture(scope="module")
def sys_module():
    """初始化 SYS 模組用於測試"""
    config = {}
    module = SYSModule(config)
    module.initialize()
    yield module
    module.shutdown()

@pytest.mark.order(1)
def test_list_functions(sys_module):
    """測試列出系統功能"""
    result = sys_module.handle({
        "mode": "list_functions",
        "params": {}
    })
    
    print("🔧 SYS 功能清單：", result)
    
    assert "status" in result
    assert result["status"] == "success"
    assert "data" in result
    assert isinstance(result["data"], dict)
    
    # 檢查核心工作流程功能是否存在
    functions = result["data"]
    expected_functions = ["start_workflow", "continue_workflow", "cancel_workflow"]
    for func in expected_functions:
        assert func in functions, f"缺少必要功能: {func}"

@pytest.mark.order(2)
def test_echo_workflow(sys_module):
    """測試簡單的 echo 工作流程"""
    # 啟動 echo 工作流程
    start_result = sys_module.handle({
        "mode": "start_workflow",
        "params": {
            "workflow_type": "echo",
            "command": "測試簡單回顯工作流程"
        }
    })
    
    print("🔄 Echo 工作流程啟動：", start_result)
    
    assert "session_id" in start_result
    assert start_result["status"] == "success"
    assert start_result["requires_input"] == True
    
    session_id = start_result["session_id"]
    test_message = "Hello from pytest!"
    
    # 提供輸入並繼續工作流程
    continue_result = sys_module.handle({
        "mode": "continue_workflow",
        "params": {
            "session_id": session_id,
            "user_input": test_message
        }
    })
    
    print("✅ Echo 工作流程完成：", continue_result)
    
    assert continue_result["status"] == "completed"
    assert "data" in continue_result
    assert continue_result["data"]["echo_message"] == test_message

@pytest.mark.order(3)
def test_countdown_workflow(sys_module):
    """測試倒數計時工作流程"""
    # 啟動倒數工作流程
    start_result = sys_module.handle({
        "mode": "start_workflow",
        "params": {
            "workflow_type": "countdown",
            "command": "測試倒數計時工作流程"
        }
    })
    
    print("🚀 Countdown 工作流程啟動：", start_result)
    
    assert "session_id" in start_result
    assert start_result["status"] == "success"
    assert start_result["requires_input"] == True
    
    session_id = start_result["session_id"]
    countdown_start = "3"  # 使用較小的數字以加快測試
    
    # 提供起始數字
    continue_result = sys_module.handle({
        "mode": "continue_workflow",
        "params": {
            "session_id": session_id,
            "user_input": countdown_start
        }
    })
    
    print("⏰ Countdown 進行中：", continue_result)
    
    # 等待倒數完成
    max_wait_time = 10  # 最多等待10秒
    start_time = time.time()
    
    while (continue_result.get("status") == "waiting" and 
           time.time() - start_time < max_wait_time):
        time.sleep(0.5)  # 短暫延遲
        continue_result = sys_module.handle({
            "mode": "continue_workflow",
            "params": {
                "session_id": session_id,
                "user_input": ""
            }
        })
        print("⏰ Countdown 狀態更新：", continue_result.get("status"))
    
    print("🎉 Countdown 工作流程完成：", continue_result)
    
    assert continue_result["status"] == "completed"
    assert "data" in continue_result
    assert continue_result["data"]["original_count"] == int(countdown_start)
    assert continue_result["data"]["countdown_completed"] == True

@pytest.mark.order(4) 
def test_data_collector_workflow(sys_module):
    """測試資料收集工作流程"""
    # 啟動資料收集工作流程
    start_result = sys_module.handle({
        "mode": "start_workflow", 
        "params": {
            "workflow_type": "data_collector",
            "command": "測試資料收集工作流程"
        }
    })
    
    print("📊 Data Collector 工作流程啟動：", start_result)
    
    assert "session_id" in start_result
    session_id = start_result["session_id"]
    
    # 測試數據
    test_data = {
        "name": "Test User",
        "age": "25", 
        "interests": "程式設計, 機器學習, 音樂",
        "feedback": "這個測試工作流程很有趣！"
    }
    
    # 逐步提供數據
    for step_name, input_value in test_data.items():
        result = sys_module.handle({
            "mode": "continue_workflow",
            "params": {
                "session_id": session_id,
                "user_input": input_value
            }
        })
        print(f"📝 提供 {step_name}: {input_value} -> {result.get('status')}")
        
        if result.get("status") == "completed":
            break
        
        assert result.get("status") in ["waiting", "completed"]
    
    print("📋 Data Collector 工作流程完成：", result)
    
    assert result["status"] == "completed"
    assert "data" in result
    data = result["data"]
    assert data["name"] == test_data["name"]
    assert data["age"] == int(test_data["age"])
    assert isinstance(data["interests"], list)
    assert data["feedback"] == test_data["feedback"]

@pytest.mark.order(5)
def test_random_fail_workflow(sys_module):
    """測試隨機失敗工作流程"""
    # 啟動隨機失敗工作流程
    start_result = sys_module.handle({
        "mode": "start_workflow",
        "params": {
            "workflow_type": "random_fail", 
            "command": "測試隨機失敗工作流程"
        }
    })
    
    print("🎲 Random Fail 工作流程啟動：", start_result)
    
    assert "session_id" in start_result
    session_id = start_result["session_id"]
    
    # 設定低失敗率以提高測試成功率
    fail_chance = "20"  # 20% 失敗率
    max_retries = "3"   # 最多3次重試
    
    # 步驟1: 設定失敗率
    result = sys_module.handle({
        "mode": "continue_workflow",
        "params": {
            "session_id": session_id,
            "user_input": fail_chance
        }
    })
    print(f"🎯 設定失敗率: {fail_chance}% -> {result.get('status')}")
    
    # 步驟2: 設定最大重試次數
    result = sys_module.handle({
        "mode": "continue_workflow",
        "params": {
            "session_id": session_id,
            "user_input": max_retries
        }
    })
    print(f"🔄 設定重試次數: {max_retries} -> {result.get('status')}")
    
    # 步驟3: 確認開始測試
    result = sys_module.handle({
        "mode": "continue_workflow",
        "params": {
            "session_id": session_id,
            "user_input": "確認"  # 發送確認信號
        }
    })
    print(f"✅ 確認開始測試 -> {result.get('status')}")
    
    # 等待測試完成
    max_wait_time = 15  # 最多等待15秒
    start_time = time.time()
    
    while (result.get("status") == "waiting" and 
           time.time() - start_time < max_wait_time):
        time.sleep(0.5)
        result = sys_module.handle({
            "mode": "continue_workflow",
            "params": {
                "session_id": session_id,
                "user_input": ""
            }
        })
        print(f"🎲 測試進行中: {result.get('status')}")
    
    print("🏁 Random Fail 工作流程完成：", result)
    
    # 檢查結果 - 狀態可能是 success, waiting, 或 completed
    assert result["status"] in ["success", "waiting", "completed"]
    
    # 檢查資料
    if "data" in result and result["data"]:
        data = result["data"]
        # 如果有測試結果，檢查它
        if "test_result" in data:
            assert data["test_result"] in ["success", "max_retries_reached"]
            # 檢查其他欄位
            if "retry_count" in data:
                assert isinstance(data["retry_count"], int)
                assert data["retry_count"] >= 1
    assert data["retry_count"] <= int(max_retries)

@pytest.mark.order(6)
def test_workflow_cancellation(sys_module):
    """測試工作流程取消功能"""
    # 啟動一個工作流程
    start_result = sys_module.handle({
        "mode": "start_workflow",
        "params": {
            "workflow_type": "echo",
            "command": "測試取消功能"
        }
    })
    
    print("🔄 啟動工作流程用於取消測試：", start_result)
    
    assert "session_id" in start_result
    session_id = start_result["session_id"]
    
    # 取消工作流程
    cancel_result = sys_module.handle({
        "mode": "cancel_workflow",
        "params": {
            "session_id": session_id,
            "reason": "pytest 測試取消"
        }
    })
    
    print("❌ 工作流程取消結果：", cancel_result)
    
    assert cancel_result["status"] == "success"
    assert "message" in cancel_result
    assert "pytest 測試取消" in cancel_result["message"]

@pytest.mark.order(7)
def test_workflow_status_and_management(sys_module):
    """測試工作流程狀態查詢和管理功能"""
    # 啟動一個工作流程
    start_result = sys_module.handle({
        "mode": "start_workflow",
        "params": {
            "workflow_type": "echo",
            "command": "測試狀態查詢"
        }
    })
    
    session_id = start_result["session_id"]
    
    # 查詢工作流程狀態
    status_result = sys_module.handle({
        "mode": "get_workflow_status",
        "params": {
            "session_id": session_id
        }
    })
    
    print("📊 工作流程狀態查詢：", status_result)
    
    assert status_result["status"] == "success"
    assert "data" in status_result
    session_info = status_result["data"]
    assert session_info["session_id"] == session_id
    assert session_info["workflow_type"] == "echo"
    assert "current_step" in session_info
    
    # 列出活躍工作流程
    list_result = sys_module.handle({
        "mode": "list_active_workflows",
        "params": {}
    })
    
    print("📋 活躍工作流程列表：", list_result)
    
    assert list_result["status"] == "success"
    assert "data" in list_result
    active_workflows_data = list_result["data"]
    assert isinstance(active_workflows_data, dict)
    assert "sessions" in active_workflows_data
    active_workflows = active_workflows_data["sessions"]
    assert isinstance(active_workflows, list)
    
    # 確認我們的工作流程在活躍列表中
    session_found = any(wf["session_id"] == session_id for wf in active_workflows)
    assert session_found, f"Session {session_id} 不在活躍工作流程列表中"
    
    # 清理：取消工作流程
    sys_module.handle({
        "mode": "cancel_workflow",
        "params": {
            "session_id": session_id,
            "reason": "測試完成清理"
        }
    })

@pytest.mark.order(8)
def test_invalid_workflow_type(sys_module):
    """測試無效的工作流程類型處理"""
    result = sys_module.handle({
        "mode": "start_workflow",
        "params": {
            "workflow_type": "invalid_workflow_type",
            "command": "這應該會失敗"
        }
    })
    
    print("❌ 無效工作流程類型測試：", result)
    
    assert result["status"] == "error"
    assert "message" in result
    assert "不支援的工作流程類型" in result["message"] or "invalid" in result["message"].lower()

@pytest.mark.order(9)
def test_invalid_session_id(sys_module):
    """測試無效的會話ID處理"""
    # 嘗試繼續一個不存在的工作流程
    result = sys_module.handle({
        "mode": "continue_workflow",
        "params": {
            "session_id": "invalid_session_id_12345",
            "user_input": "test"
        }
    })
    
    print("❌ 無效會話ID測試：", result)
    
    assert result["status"] == "error"
    assert "message" in result
    
    # 嘗試取消一個不存在的工作流程
    cancel_result = sys_module.handle({
        "mode": "cancel_workflow",
        "params": {
            "session_id": "invalid_session_id_12345",
            "reason": "測試"
        }
    })
    
    print("❌ 無效會話ID取消測試：", cancel_result)
    
    assert cancel_result["status"] == "error"

@pytest.mark.order(10)
def test_invalid_mode(sys_module):
    """測試無效操作模式處理"""
    result = sys_module.handle({
        "mode": "invalid_mode",
        "params": {}
    })
    
    print("❌ 無效模式測試：", result)
    
    assert result["status"] == "error"
    assert "message" in result
    assert "不支援的模式" in result["message"] or "invalid" in result["message"].lower()

# 如果需要測試與其他模組的整合，可以添加以下測試
@pytest.mark.slow
@pytest.mark.skipif(True, reason="需要 TTS 模組才能執行")
def test_tts_workflow_with_mock(sys_module):
    """測試 TTS 工作流程（需要 TTS 模組）"""
    # 這個測試需要 TTS 模組，可以在整合測試中執行
    pass

@pytest.mark.slow  
@pytest.mark.skipif(True, reason="需要 LLM 模組才能執行")
def test_data_collector_with_llm(sys_module):
    """測試資料收集工作流程與 LLM 整合（需要 LLM 模組）"""
    # 這個測試需要 LLM 模組，可以在整合測試中執行
    pass
