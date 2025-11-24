# 整合測試 - SYS 模組檔案工作流程

## 概述

本目錄包含 SYS 模組檔案工作流程的整合測試，使用正式的系統初始化流程（`system_initializer` + `controller`）來驗證工作流程在真實環境中的運作。

## 測試檔案

### `test_sys_file_workflows.py`
自動化整合測試，測試 3 個檔案工作流程：
- `drop_and_read` - 檔案讀取工作流程
- `intelligent_archive` - 智慧歸檔工作流程
- `summarize_tag` - 摘要標籤工作流程

## 執行測試

### 執行所有整合測試
```powershell
# 激活虛擬環境
.\env\Scripts\Activate.ps1

# 執行所有整合測試
pytest integration_tests/ -v -s

# 執行並顯示詳細輸出
pytest integration_tests/test_sys_file_workflows.py -v -s --tb=short
```

### 執行特定測試類別
```powershell
# 只測試 drop_and_read 工作流程
pytest integration_tests/test_sys_file_workflows.py::TestDropAndReadWorkflow -v -s

# 只測試 intelligent_archive 工作流程
pytest integration_tests/test_sys_file_workflows.py::TestIntelligentArchiveWorkflow -v -s

# 只測試 summarize_tag 工作流程
pytest integration_tests/test_sys_file_workflows.py::TestSummarizeTagWorkflow -v -s
```

### 執行特定測試案例
```powershell
# 測試完整的 drop_and_read 流程
pytest integration_tests/test_sys_file_workflows.py::TestDropAndReadWorkflow::test_drop_and_read_complete_flow -v -s

# 測試錯誤處理
pytest integration_tests/test_sys_file_workflows.py::TestDropAndReadWorkflow::test_drop_and_read_invalid_file -v -s
```

## 測試策略

### 為什麼使用正式初始化流程？

之前使用 `debug_api` 的方法發現太多機制依賴於正式系統：
- 無法自行創建 General Session (GS)
- 無法模擬完整的系統循環
- WorkflowSession 需要 UnifiedSessionManager
- 事件系統需要完整的 EventBus 和狀態管理

因此改用正式的初始化流程：
1. `SystemInitializer` 初始化核心架構
2. `Controller` 初始化必要模組（SYS、LLM）
3. 透過 SYS 模組的 MCP Server 調用工作流程
4. 驗證工作流程的完整執行過程

### 測試覆蓋範圍

每個工作流程測試包括：
- ✅ 工作流程啟動（start_workflow）
- ✅ 多步驟互動（provide_workflow_input）
- ✅ 狀態查詢（get_workflow_status）
- ✅ 工作流程完成（complete_workflow）
- ✅ 錯誤處理（invalid input）
- ✅ 工作流程取消（cancel_workflow）
- ✅ 會話生命週期（session creation → active → completion）

## 測試標記

- `@pytest.mark.integration` - 整合測試
- `@pytest.mark.sys` - SYS 模組相關測試

可以使用標記過濾測試：
```powershell
# 只執行整合測試
pytest -m integration -v -s

# 只執行 SYS 相關測試
pytest -m sys -v -s
```

## 注意事項

1. **測試環境**
   - 測試使用 `pytest` 的 `tmp_path` fixture 創建臨時檔案
   - 測試結束後自動清理
   - 不會影響實際的檔案系統

2. **模組依賴**
   - SYS 模組：必需（測試主體）
   - LLM 模組：必需（summarize_tag 需要）
   - 其他模組：可選

3. **執行時間**
   - Drop and Read: ~5-10 秒
   - Intelligent Archive: ~10-15 秒
   - Summarize Tag: ~20-30 秒（依賴 LLM 生成）

4. **已知限制**
   - `summarize_tag` 測試依賴 LLM 模組，如果 LLM 失敗，測試會標記為失敗
   - 測試使用 `terminal_mode=True` 載入模組，排除 UI 相關功能
   - 測試環境是單次執行，不模擬長期運行的系統狀態

## 除錯

如果測試失敗，檢查以下項目：

1. **系統初始化失敗**
   ```
   錯誤：系統初始化失敗
   解決：檢查 SystemInitializer 和 Controller 的日誌
   ```

2. **模組載入失敗**
   ```
   錯誤：無法載入 SYS 模組
   解決：檢查模組配置和依賴項
   ```

3. **工作流程超時**
   ```
   錯誤：等待超時
   解決：檢查工作流程執行日誌，確認是否有阻塞
   ```

4. **會話創建失敗**
   ```
   錯誤：缺少 session_id
   解決：檢查 UnifiedSessionManager 的初始化狀態
   ```

## 後續計劃

自動化測試通過後，可以進行手動測試：
1. 使用真實的檔案和路徑
2. 測試不同的檔案類型和大小
3. 測試與其他模組的整合（TTS、UI 等）
4. 測試長時間運行的場景
5. 測試錯誤恢復和異常處理

## 相關文檔

- [單元測試文檔](../unit_tests/README_UNIT_TESTS.md)
- [測試結果](../unit_tests/TEST_RESULTS.md)
- [系統架構文檔](../docs/三層架構實現深度分析.md)
- [工作流程設計](../docs/模組開發指南.md)
- [舊的整合測試參考](../devtools/module_tests/integration_tests_old.py.disabled)
