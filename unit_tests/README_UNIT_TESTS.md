# U.E.P 單元測試套件

## 📋 測試架構

本測試套件專門針對 U.E.P 系統的核心組件進行單元測試，確保在整合測試前各個部分都能正常運作。

**當前狀態**: ✅ 135/152 測試通過 (88.8%)  
**最後更新**: 2024年11月

## 🎯 測試範圍

### 1. 事件總線測試 (`test_event_bus.py`) - ✅ 100% 通過
**通過率**: 21/21 (100%)
- ✅ 事件發布和訂閱機制
- ✅ 同步/異步處理
- ✅ 事件歷史記錄
- ✅ 單一來源原則檢查
- ✅ 事件處理順序
- ✅ 線程安全性測試

### 2. 會話生命週期測試 (`test_session_lifecycle.py`) - ✅ 84% 通過
**通過率**: 21/25 (4 個跳過)
- ✅ UnifiedSessionManager 初始化和延遲加載
- ✅ General Session (GS) 創建和結束
- ✅ Chatting Session (CS) 創建和結束
- ✅ Workflow Session (WS) 創建和結束
- ✅ 會話記錄管理和持久化
- ✅ 會話層次結構 (GS→CS, GS→WS)
- ✅ 超時處理和清理
- ⚠️ 跳過：會話中斷 API (2個)、單會話超時 (1個)、私有方法 (1個)

### 3. 工作流測試 (`test_workflows.py`) - ✅ 84% 通過
**通過率**: 38/45 (7 個跳過)
- ✅ StepResult 工廠方法 (success, failure, cancel, complete, skip_to)
- ✅ WorkflowDefinition 創建、驗證、步驟管理
- ✅ WorkflowEngine 初始化和狀態管理
- ✅ StepTemplate 工廠方法 (create_input_step, create_processing_step)
- ✅ 步驟執行邏輯和過渡規則
- ✅ LLM 審核機制
- ✅ 工作流模式 (DIRECT, BACKGROUND)
- ⚠️ 跳過：advance_step 方法 (4個，使用 process_input 代替)、get_workflow_status (1個)、其他 (2個)

### 4. MCP 集成測試 (`test_mcp_integration.py`) - ✅ 84% 通過
**通過率**: 31/37 (6 個跳過)
- ✅ MCPServer 初始化和工具註冊
- ✅ 8 個核心 MCP 工具驗證 (start_workflow, review_step, approve_step, modify_step, cancel_workflow, get_workflow_status, provide_workflow_input, resolve_path)
- ✅ 工具元數據完整性
- ✅ 工具參數模式驗證
- ✅ MCPClient 初始化和集成
- ✅ Server-Client 雙向引用
- ⚠️ 跳過：call_tool 方法 (2個，使用 handle_request 代替)、需要完整環境的測試 (4個)

### 5. LLM-MCP 集成測試 (`test_llm_mcp_integration.py`) - ✅ 100% 通過 🆕
**通過率**: 24/24 (100%)
- ✅ LLM 發現和獲取 MCP 工具規範
- ✅ LLM 調用 MCP 工具 (start_workflow, get_workflow_status 等)
- ✅ Gemini 回應解析 (JSON schema 和 function call)
- ✅ LLM 工作流決策機制
- ✅ 工具上下文注入和參數驗證
- ✅ 錯誤處理和降級策略
- ✅ MCP Client 與 LLM 的雙向集成
- ✅ 回應模式測試 (CHAT/WORK/function calling)

## 🔧 執行測試

### 執行所有測試
```powershell
# 激活虛擬環境
.\env\Scripts\Activate.ps1

# 執行所有單元測試（推薦）
pytest unit_tests/test_event_bus.py unit_tests/test_session_lifecycle.py unit_tests/test_workflows.py unit_tests/test_mcp_integration.py unit_tests/test_llm_mcp_integration.py -v

# 執行所有測試並顯示覆蓋率
pytest unit_tests/test_event_bus.py unit_tests/test_session_lifecycle.py unit_tests/test_workflows.py unit_tests/test_mcp_integration.py unit_tests/test_llm_mcp_integration.py -v --cov=modules --cov=core --cov-report=term-missing
```

### 執行特定測試文件
```powershell
# 測試事件總線
pytest unit_tests/test_event_bus.py -v

# 測試會話生命週期
pytest unit_tests/test_session_lifecycle.py -v

# 測試工作流
pytest unit_tests/test_workflows.py -v

# 測試 MCP 集成
pytest unit_tests/test_mcp_integration.py -v

# 測試 LLM-MCP 集成（新增）
pytest unit_tests/test_llm_mcp_integration.py -v
```

### 執行特定測試用例
```powershell
# 測試特定場景
pytest unit_tests/test_workflows.py::test_interactive_step_execution -v

# 使用標記過濾
pytest unit_tests/ -m "critical" -v

# 只執行關鍵測試
pytest unit_tests/ -m "critical" -v --tb=short
```

### 調試模式
```powershell
# 顯示 print 輸出
pytest unit_tests/test_workflows.py -v -s

# 失敗時進入調試器
pytest unit_tests/test_workflows.py -v --pdb

# 詳細輸出
pytest unit_tests/test_workflows.py -vv
```

## 📊 測試覆蓋率

查看測試覆蓋率報告：
```powershell
# 生成 HTML 報告
pytest unit_tests/ --cov=modules --cov=core --cov-report=html

# 查看報告（自動在瀏覽器中打開）
start htmlcov/index.html
```

## 🎨 測試標記

- `@pytest.mark.critical` - 關鍵功能測試，必須通過
- `@pytest.mark.workflow` - 工作流相關測試
- `@pytest.mark.mcp` - MCP 服務器相關測試
- `@pytest.mark.llm` - LLM 模組相關測試 🆕
- `@pytest.mark.integration` - 集成測試 🆕
- `@pytest.mark.session` - 會話管理相關測試
- `@pytest.mark.event` - 事件總線相關測試
- `@pytest.mark.slow` - 執行時間較長的測試
- `@pytest.mark.asyncio` - 異步測試

## 📝 測試原則

1. **隔離性**: 每個測試用例獨立運行，不依賴其他測試
2. **可重複性**: 測試結果應該穩定可重複
3. **快速性**: 單元測試應該在幾秒內完成
4. **清晰性**: 測試名稱和斷言應該清楚表達意圖
5. **真實性**: 盡可能模擬真實場景，但不依賴外部資源

## 🐛 常見問題

### 測試失敗時的調試

1. 使用 `-v` 查看詳細輸出
2. 使用 `-s` 查看 print 語句
3. 使用 `--pdb` 在失敗時進入調試器
4. 使用 `--tb=short` 簡化錯誤回溯

```powershell
pytest unit_tests/test_workflows.py::test_specific_case -v -s --pdb
```

### Mock 和 Fixture

所有測試都使用 pytest fixtures 來初始化組件，確保測試環境的一致性：

**核心 Fixtures:**
- `event_bus` - 乾淨的事件總線實例
- `unified_session_manager` - UnifiedSessionManager 實例 (更新：不再使用 session_manager)
- `mock_sys_module` - Mock SYS 模組實例
- `mcp_server` - MCPServer(sys_module) 實例
- `mcp_client` - MCPClient(mcp_server, llm_module) 實例
- `workflow_definition` - WorkflowDefinition 實例（使用 StepTemplate）
- `mock_workflow_session` - Mock WorkflowSession 實例
- `workflow_engine` - WorkflowEngine(definition, session) 實例
- `llm_module_with_mcp` - LLM 模組實例（連接 MCP Server）🆕
- `mock_gemini_wrapper` - Mock Gemini API wrapper 🆕

所有 fixtures 定義在 `conftest.py` 中。

## 🔄 持續改進

隨著系統演進，請及時更新測試用例以反映最新的架構變化。每次修改核心邏輯後，應該：

1. 運行所有相關的單元測試
2. 更新或添加新的測試用例
3. 確保測試覆蓋率不降低
4. 記錄測試結果和發現的問題

## 📚 測試執行順序建議

建議按以下順序執行測試，確保基礎穩固：

1. **基礎層** - 先測試基礎組件
   ```powershell
   pytest unit_tests/test_event_bus.py -v
   pytest unit_tests/test_session_lifecycle.py -v
   ```

2. **組件層** - 再測試獨立組件
   ```powershell
   pytest unit_tests/test_workflows.py -v
   pytest unit_tests/test_mcp_integration.py -v
   ```

3. **集成層** - 最後測試組件協作 🆕
   ```powershell
   pytest unit_tests/test_llm_mcp_integration.py -v
   ```

4. **完整測試** - 執行所有測試
   ```powershell
   pytest unit_tests/test_event_bus.py unit_tests/test_session_lifecycle.py unit_tests/test_workflows.py unit_tests/test_mcp_integration.py unit_tests/test_llm_mcp_integration.py -v
   ```

## 🎯 測試目標

本測試套件旨在確保：

1. **事件匯流排的事件處理順序正常** ✅ 100% - 發布者與訂閱者符合設計
2. **會話、狀態的觸發與結束上沒有問題** ✅ 84% - UnifiedSessionManager 三層架構
3. **工作流本身的運作沒有問題** ✅ 84% - StepResult、WorkflowDefinition、WorkflowEngine
4. **MCP 伺服器的處理沒有問題** ✅ 84% - 8 個核心工具完整驗證
5. **LLM 能正確發現和調用 MCP 工具** ✅ 100% 🆕 - LLM-MCP 集成完整驗證
6. **Gemini 回應解析機制正常** ✅ 100% 🆕 - JSON schema 和 function call 格式

### 關鍵驗證點 🎯

- ✅ **LLM 知道有哪些系統功能可用** - 通過 MCP 工具發現機制
- ✅ **LLM 能夠正確執行系統操作** - 通過 handle_mcp_tool_call 驗證
- ✅ **AI 回應格式正確解析** - 支持 CHAT/WORK/function calling 模式
- ✅ **工具參數驗證機制有效** - 防止無效的工具調用
- ✅ **錯誤處理完善** - 系統在異常情況下保持穩定

### 測試覆蓋摘要

| 類別 | 通過率 | 狀態 |
|------|--------|------|
| 事件總線 | 100% | ✅ 完美 |
| LLM-MCP 集成 | 100% | ✅ 完美 |
| 會話管理 | 84% | ✅ 良好 |
| 工作流引擎 | 84% | ✅ 良好 |
| MCP 工具 | 84% | ✅ 良好 |
| **總體** | **88.8%** | ✅ **優秀** |

所有測試通過後，即可進行整合測試。詳細報告請參考 `TEST_RESULTS.md`。
