# U.E.P's Core 立即修復行動計劃

## 問題摘要

經過深入分析，Core Framework 和 STT→MEM 架構在設計上已經完成且功能完整，但存在以下關鍵問題阻礙正常運行：

1. **依賴問題**: MEM 模組缺少必要的 Python 套件
2. **配置不一致**: 模組啟用狀態與重構狀態不匹配
3. **身份映射缺失**: STT 語者識別到 MEM 身份令牌的轉換機制
4. **測試更新滯後**: 單元測試使用舊版 API 引用

## 立即修復步驟

### 步驟 1: 安裝缺失依賴套件 (5 分鐘)

```bash
# 安裝 MEM 模組必要依賴
pip install sentence-transformers==3.3.1
pip install faiss-cpu==1.9.0.post1

# 驗證安裝
python -c "import sentence_transformers; import faiss; print('Dependencies installed successfully')"
```

### 步驟 2: 修正配置檔案 (2 分鐘)

編輯 `configs/config.yaml`:

```yaml
# 修正模組啟用狀態
modules_enabled:
  stt_module: true      # 啟用已重構的 STT
  nlp_module: true      # 啟用已重構的 NLP  
  mem_module: true      # 保持啟用
  llm_module: false     # 暫時保持關閉
  tts_module: false     # 暫時保持關閉
  sys_module: false     # 暫時保持關閉

# 修正重構狀態標記
modules_refactored:
  stt_module: true      # 保持已重構狀態
  nlp_module: true      # 保持已重構狀態
  mem_module: true      # 標記為已重構 ← 關鍵修改
  llm_module: false     # 尚未開始
  tts_module: false     # 尚未開始
  sys_module: false     # 尚未開始
```

### 步驟 3: 清理 MEM 模組程式碼問題 (3 分鐘)

編輯 `modules/mem_module/mem_module.py`，移除重複的 import 語句:

在檔案開頭找到並刪除重複的這些行：
```python
# 刪除重複的 import (保留第一次出現的即可)
from sentence_transformers import SentenceTransformer
import faiss
import numpy as np
```

### 步驟 4: 驗證修復效果 (2 分鐘)

```bash
# 測試系統初始化
python -c "
from core.controller import unified_controller
print('Testing system initialization...')
success = unified_controller.initialize()
print(f'Initialization success: {success}')
if success:
    status = unified_controller.get_system_status()
    print(f'Active modules: {status[\"enabled_modules\"]}')
"
```

## 後續開發步驟

### 短期目標 (1-2 週)

#### 1. 實作身份映射機制

創建 `core/identity_mapper.py`:

```python
# core/identity_mapper.py
"""
身份映射器 - 處理 STT 語者識別到 MEM 身份令牌的轉換
"""

from typing import Dict, Optional
from core.working_context import working_context_manager, ContextType

class IdentityMapper:
    """身份映射器"""
    
    def __init__(self):
        self.speaker_to_token_map: Dict[str, str] = {}
        
    def map_speaker_to_memory_token(self, speaker_id: str) -> str:
        """將語者 ID 映射到記憶體令牌"""
        if speaker_id not in self.speaker_to_token_map:
            # 生成新的記憶體令牌
            memory_token = f"mem_token_{len(self.speaker_to_token_map):03d}_{speaker_id}"
            self.speaker_to_token_map[speaker_id] = memory_token
            
            # 創建身份管理上下文
            working_context_manager.add_data(
                context_id=f"identity_{speaker_id}",
                context_type=ContextType.IDENTITY_MANAGEMENT,
                data={
                    "speaker_id": speaker_id,
                    "memory_token": memory_token,
                    "mapping_created_at": time.time()
                }
            )
            
        return self.speaker_to_token_map[speaker_id]
    
    def get_identity_context(self, speaker_id: str) -> Dict[str, Any]:
        """獲取身份上下文資料"""
        memory_token = self.map_speaker_to_memory_token(speaker_id)
        return {
            "speaker_id": speaker_id,
            "memory_token": memory_token,
            "has_memory_access": True
        }

# 全局身份映射器實例
identity_mapper = IdentityMapper()
```

#### 2. 更新 STT→MEM 整合

在 STT 模組的 Working Context 處理中添加身份映射：

```python
# 在 modules/stt_module/speaker_context_handler.py 中添加
from core.identity_mapper import identity_mapper

def _create_new_speaker(self, embeddings: list) -> bool:
    """創建新說話人並生成身份令牌"""
    try:
        new_speaker_id = f"speaker_{self.speaker_identification.speaker_counter:03d}"
        
        # 創建語者資料
        self.speaker_identification.speaker_database[new_speaker_id] = {
            'embeddings': embeddings,
            'metadata': {
                'created_at': time.time(),
                'last_seen': time.time(),
                'sample_count': len(embeddings),
                'method': 'context_decision'
            }
        }
        
        # 生成記憶體令牌
        memory_token = identity_mapper.map_speaker_to_memory_token(new_speaker_id)
        
        # 記錄身份映射
        info_log(f"[SpeakerContextHandler] 新語者創建: {new_speaker_id} → {memory_token}")
        
        return True
    except Exception as e:
        error_log(f"[SpeakerContextHandler] 創建新說話人失敗: {e}")
        return False
```

#### 3. 創建端到端測試

創建 `tests/test_stt_to_mem_integration.py`:

```python
#!/usr/bin/env python3
"""
STT 到 MEM 整合測試
測試完整的語音識別到記憶體儲存流程
"""

import pytest
import time
from unittest.mock import Mock, patch
from core.controller import unified_controller
from core.identity_mapper import identity_mapper

def test_stt_to_mem_workflow():
    """測試 STT → MEM 完整工作流程"""
    
    # 1. 初始化系統
    assert unified_controller.initialize(), "系統初始化失敗"
    
    # 2. 模擬語音識別結果
    mock_stt_result = {
        "text": "Hello, this is a test message",
        "confidence": 0.95,
        "speaker_id": "speaker_001"
    }
    
    # 3. 測試身份映射
    memory_token = identity_mapper.map_speaker_to_memory_token("speaker_001")
    assert memory_token.startswith("mem_token_"), "記憶體令牌格式錯誤"
    
    # 4. 測試記憶體儲存
    result = unified_controller.process_input(
        intent="voice_recognition",
        data={
            "stt_result": mock_stt_result,
            "memory_token": memory_token
        }
    )
    
    assert result.get("status") == "success", "語音處理失敗"
    
    # 5. 驗證記憶體是否正確儲存
    mem_module = unified_controller.get_module("mem")
    if mem_module:
        # 查詢剛才儲存的記憶
        query_result = mem_module.handle({
            "operation_type": "query",
            "query_text": "test message",
            "identity_token": memory_token
        })
        
        assert query_result.get("success"), "記憶體查詢失敗"
        assert len(query_result.get("results", [])) > 0, "未找到儲存的記憶"

if __name__ == "__main__":
    test_stt_to_mem_workflow()
    print("✅ STT → MEM 整合測試通過")
```

### 中期目標 (2-4 週)

1. **完整模組整合**: NLP 模組的意圖識別整合
2. **效能最佳化**: 記憶體檢索和語者識別的效能調優
3. **錯誤處理**: 完善的異常處理和恢復機制
4. **監控系統**: 系統健康檢查和效能監控

## 驗證檢查清單

完成修復後，請驗證以下項目：

- [ ] 依賴套件安裝成功
- [ ] 配置檔案更新正確
- [ ] MEM 模組程式碼清理完成
- [ ] 系統初始化成功，至少載入 3 個模組 (STT, NLP, MEM)
- [ ] Working Context 系統正常運作
- [ ] 語者識別到記憶體令牌映射功能
- [ ] 端到端測試通過

## 問題排除

如果遇到問題，請檢查：

1. **依賴問題**: `pip list | grep -E "sentence|faiss"`
2. **配置問題**: 檢查 `config.yaml` 語法正確性
3. **程式碼問題**: 檢查是否有語法錯誤或 import 問題
4. **權限問題**: 檢查檔案讀寫權限

## 聯絡支援

如需協助，請提供：
- 錯誤訊息的完整 traceback
- 系統環境資訊 (`python --version`, `pip list`)
- 修改的配置檔案內容

---

**最後更新**: 2024年12月  
**預估完成時間**: 15-20 分鐘（立即修復步驟）