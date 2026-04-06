# Echo Memory System - 快速開始指南

本指南將帶你在 5 分鐘內完成 Echo Memory System MVP 的安裝與測試。

---

## 🚀 步驟 1：環境準備

### Windows (PowerShell)

```powershell
# 進入專案目錄
cd C:\Users\Bernie\source\repos\Unforgettableeternalproject\TestSeperateMemorySystem

# 創建虛擬環境
python -m venv venv

# 啟動虛擬環境
.\venv\Scripts\activate

# 升級 pip
python -m pip install --upgrade pip
```

### Linux/macOS

```bash
cd /path/to/TestSeperateMemorySystem

python -m venv venv
source venv/bin/activate

pip install --upgrade pip
```

---

## 📦 步驟 2：安裝依賴

```bash
# 安裝核心依賴
pip install -r requirements.txt

# 這將安裝：
# - sentence-transformers (嵌入模型)
# - faiss-cpu (向量檢索)
# - pydantic (資料驗證)
# - 其他核心依賴
```

**預計時間**：2-3 分鐘（取決於網速）

---

## 🧪 步驟 3：運行測試

```bash
# 運行所有測試
pytest tests/ -v

# 預期輸出：
# tests/test_episodic.py::test_episode_creation PASSED
# tests/test_episodic.py::test_episodic_write_and_get PASSED
# tests/test_episodic.py::test_episodic_search PASSED
# tests/test_engine.py::test_store_and_retrieve PASSED
# ...
# ========== X passed in Y.YYs ==========
```

---

## 💬 步驟 4：運行最小示例

```bash
# 運行對話示例
python examples/minimal_chat.py
```

### 預期輸出

```
2026-04-06 ... [INFO] example: === 初始化 Echo Memory System ===
2026-04-06 ... [INFO] echo_memory.episodic: [EpisodicMemory] 初始化完成 - 已載入 0 個情節
2026-04-06 ... [INFO] echo_memory.engine: [MemoryEngine] 初始化完成 - 存儲路徑: data

2026-04-06 ... [INFO] example: === 模擬對話 ===
2026-04-06 ... [INFO] example: 存儲對話 1: ep_xxxxxxxxxxxxx
2026-04-06 ... [INFO] example: 存儲對話 2: ep_xxxxxxxxxxxxx
2026-04-06 ... [INFO] example: 存儲對話 3: ep_xxxxxxxxxxxxx

2026-04-06 ... [INFO] example: === 檢索記憶 ===
2026-04-06 ... [INFO] example: 查詢: 我喜歡什麼天氣？

2026-04-06 ... [INFO] example:
檢索結果:
【相關對話記憶（以下為過去對話，不是當前訊息）】
  - （剛才）[重要程度:《核心記憶》] 我喜歡晴天
  - （剛才）[重要程度:《重要記憶》] 推薦我一部電影
  - （剛才）[重要程度:《一般記憶》] 今天天氣如何？

2026-04-06 ... [INFO] example: === 記憶統計 ===
2026-04-06 ... [INFO] example: 總情節數: 3
2026-04-06 ... [INFO] example: 總概念數: 0
2026-04-06 ... [INFO] example: 工作記憶緩存: 0
```

---

## ✅ 驗證成功

如果你看到上述輸出，恭喜！Echo Memory System MVP 已成功運行。

### 驗證檢查點

- [x] 虛擬環境啟動成功
- [x] 依賴安裝無錯誤
- [x] pytest 測試全部通過
- [x] 示例運行並正確檢索記憶
- [x] `data/` 目錄已生成（包含 `episodic/` 子目錄）

---

## 🔍 檢查生成的資料

```bash
# 查看生成的資料文件
ls -R data/

# Windows PowerShell:
dir data\ -Recurse

# 預期結構：
# data/
# └── episodic/
#     ├── episodes.json      # 元數據
#     └── embeddings.npy     # 向量索引
```

---

## 🐛 常見問題

### 問題 1：`ModuleNotFoundError: No module named 'sentence_transformers'`

**解決**：
```bash
pip install sentence-transformers
```

### 問題 2：`torch` 安裝失敗

**解決**（使用 CPU 版本）：
```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

### 問題 3：測試顯示 FAILED

**解決**：
```bash
# 清空舊資料
rm -rf data/
# Windows: rmdir /s data

# 重新運行測試
pytest tests/ -v
```

---

## 📚 下一步

現在你已經有一個可運行的 MVP！接下來可以：

1. **閱讀 README.md** - 了解完整架構設計
2. **查看 `echo_memory/engine.py`** - 理解核心 API
3. **擴展功能** - 參考 Phase 2/3/4 目標實作更多功能

---

## 🎯 Phase 1 完成檢查清單

- [x] 專案基建與目錄結構
- [x] Episode 存儲（FAISS + JSON）
- [x] 向量檢索基礎功能
- [x] MemoryContext 文本序列化
- [x] 單元測試（基礎覆蓋）
- [x] 最小對話示例

**Phase 1 狀態**：✅ **完成**

準備好進入 **Phase 2: EcphoryRAG 多跳檢索** 了嗎？
