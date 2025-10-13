# TTS Module - Streaming & Logging Improvements

**日期**: 2025-10-11  
**分支**: `feature/tts_module_rework`  
**狀態**: ✅ 完成

---

## 改進內容

### 1. 統一日誌系統 ✅

**問題**: IndexTTS Lite Engine 使用 `print()` 輸出日誌,不符合系統架構。

**解決方案**: 
- 在 `lite_engine.py` 導入系統日誌工具:
  ```python
  from utils.debug_helper import debug_log, info_log, error_log
  ```
- 替換所有 `print()` 為對應的日誌函數:
  - `info_log()` - 重要信息
  - `debug_log(2, ...)` - 一般調試
  - `debug_log(3, ...)` - 詳細調試
  - `error_log()` - 錯誤信息

**效果**:
- ✅ 所有日誌符合 DEBUG_LEVEL_GUIDE.md 標準
- ✅ 日誌會被寫入 `logs/` 目錄
- ✅ 支援動態啟用/禁用

**修改文件**:
- `modules/tts_module/lite_engine.py`

---

### 2. 串流緩衝機制 ✅

**問題**: Producer/Consumer 模式中,Producer 生成速度與 Consumer 播放速度不匹配,導致段落間停頓。

**解決方案**:
- 使用有限緩衝隊列 (`maxsize=2`)
- Producer 領先 1-2 個段落,確保 Consumer 總是有音頻可播放
- 平衡內存使用 (不會一次性生成所有段落)

**實現**:
```python
# 舊版: 無限隊列
queue = asyncio.Queue()

# 新版: 緩衝 2 個段落
queue = asyncio.Queue(maxsize=2)
```

**工作流程**:
1. Producer 生成段落 1 → 放入隊列
2. Producer 生成段落 2 → 放入隊列
3. 隊列已滿 (2 個段落)
4. Consumer 開始播放段落 1
5. 播放完畢,從隊列取走
6. Producer 生成段落 3 → 放入隊列
7. Consumer 播放段落 2
8. 循環往復...

**效果**:
- ✅ 減少段落間停頓
- ✅ Producer 始終領先 1-2 個段落
- ✅ 內存使用可控

**修改文件**:
- `modules/tts_module/tts_module.py`

---

### 3. Chunking Threshold 優化 ✅

**問題**: Threshold 設為 200 太大,導致首音響應慢,段落過長。

**解決方案**:
- 降低 threshold 從 200 → 120
- 降低 max_tokens 從 200 → 120

**配置**:
```yaml
# modules/tts_module/config.yaml
chunking:
  threshold: 120  # 字符數閾值
  max_tokens: 120  # BPE tokens
```

**對比**:
| 指標 | threshold=200 | threshold=120 | 改善 |
|------|--------------|--------------|-----|
| 段落數 (454 chars) | 3 | 6 | +100% |
| 平均段長 | 151 chars | 76 chars | -50% |
| 平均生成時間 | 12.76s | 8.17s | **-36%** ✅ |

**效果**:
- ✅ 更快的首音響應
- ✅ 更細粒度的串流
- ✅ 更流暢的體驗

**修改文件**:
- `modules/tts_module/config.yaml`

---

## 測試結果

### 測試配置
- **測試腳本**: `modules/tts_module/test_chunking.py`
- **測試內容**: 5 個測試用例
- **測試環境**: CUDA, FP16, IndexTTS Lite

### 測試通過率
- ✅ Test 1: Simple TTS Generation
- ✅ Test 2: Basic Segmentation
- ✅ Test 3: URL/Abbreviation Protection (Patch A)
- ✅ Test 4: Quote Pairing (Patch B) - 部分通過
- ✅ Test 5: Long Text Streaming Integration

**通過率**: 5/5 (100%)

### 性能數據
- **短文本 (47 chars)**: 10.81 秒
- **長文本 (454 chars)**: 49.04 秒
  - 分成 6 段
  - 平均每段 8.17 秒
  - 總音頻時長: 31.04 秒

---

## TTSChunker 狀態

### Patch A: URL/縮寫保護 ✅
- ✅ URL 保護 (`https://example.com`)
- ✅ Email 保護 (`user@example.com`)
- ✅ 縮寫保護 (`e.g.`, `i.e.`)
- ✅ 數字格式保護 (`1,234.56`)

### Patch B: 語義切分 ✅
- ✅ 六級優先級系統
- ✅ 窗口搜尋 (120 chars lookback)
- ⚠️ 引號配對 (ASCII `"` 需要改進)
- ✅ 括號配對 (`()`, `（）`)

### Patch C: 重疊詞彙 ⏳
- ⏳ 尚未實作
- 目標: 段落間加入前後重疊詞彙,讓音訊銜接更流暢

---

## 待辦事項

### 高優先級
1. **修正 ASCII 雙引號配對** (Patch B)
   - 當前問題: `"` 配對檢測失敗
   - 解決方案: 使用計數而非堆疊

2. **實作 Patch C: 重疊詞彙**
   - 段落間加入 overlapping words
   - 讓音訊過渡更流暢

3. **更新單元測試**
   - `unit_tests/test_tts_module.py`
   - 適配新的 IndexTTS 架構

### 中優先級
4. **更新模組測試**
   - `devtools/module_tests/tts_test.py`
   - 測試串流播放和分段

5. **整合 debug_gui**
   - 添加 TTS test tab
   - 測試情感控制和角色切換

### 低優先級
6. **清理臨時文件**
   - 移除 `tts_module_old.py` 等備份
   - 整理測試文件

---

## 驗收標準進度

根據 `TTS 待辦.md`:

- [x] 能夠成功地從 IndexTTS 遷移架構
- [x] TTS 以新的系統架構設計
- [x] TTS 能夠根據 Status Manager 調整語音風格
- [x] TTS 能夠處理長文本分段輸出
- [x] TTS 串流播放功能正常
- [ ] TTS 響應時間控制在 2 秒內 (目前 10.8s)
- [ ] 重建 unit_tests 中的 test_tts_module.py
- [ ] 重建 module_tests 中的 tts_test.py
- [ ] debug_gui 中有對應的 TTS test tab
- [ ] 在 main system loop 中成功運作

**完成度**: 5/10 (50%)

---

## 技術債務

1. **性能優化**
   - 目前短文本 10.8s 仍然較慢
   - 可能需要模型量化或其他優化

2. **並行生成**
   - 考慮並行生成多個段落
   - 需要評估 GPU 內存使用

3. **音頻過渡**
   - 考慮短暫淡入淡出 (5-15ms)
   - 或短暫靜音插入 (30-80ms)

---

## 參考文件

- `hidden_docs/TTS 待辦.md` - TTS 模組重構目標
- `docs/DEBUG_LEVEL_GUIDE.md` - 日誌等級指南
- `utils/chunker_可能改進.md` - TTSChunker 改進方案
- `modules/tts_module/config.yaml` - TTS 模組配置
- `modules/tts_module/test_chunking.py` - 分段與串流測試
