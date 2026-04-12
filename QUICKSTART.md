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
# - python-dotenv (環境變數管理)
# - 其他核心依賴
```

**預計時間**：2-3 分鐘（取決於網速）

---

## ⚙️ 步驟 3：配置環境變數

### 方式 1：使用現有的 .env（推薦）

如果你已經設定了 U.E.P Core 的環境變數，可以直接編輯 `.env`：

```bash
# Windows
notepad .env

# Linux/macOS
nano .env
```

### 方式 2：從範本創建

```bash
# 複製範本（如果 .env 不存在）
cp .env.example .env

# 編輯 .env，填入你的 Google Cloud 配置
```

### 必要配置項

```bash
# Google Cloud Platform 配置（與 U.E.P Core 相同）
GCP_PROJECT_ID=your_gcp_project_id_here
GCP_LOCATION=asia-east1

# 或使用 Gemini API Key（備用）
GOOGLE_API_KEY=your_api_key_here
```

### 驗證配置

```bash
# 運行配置驗證腳本
python verify_config.py
```

**預期輸出**：

```
================================================================================
Echo Memory System - 配置驗證
================================================================================

Echo Memory System - 配置摘要
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LLM Provider:
  - GCP Project: your-project-id
  - GCP Location: asia-east1
  - Gemini API: ✓
  
Memory System:
  - Storage Path: ./data
  - Max Episodes: 10,000
  - Max Concepts: 5,000
  
...

配置驗證結果:
--------------------------------------------------------------------------------
✅ 所有必要配置已設定

可以開始使用 Echo Memory System！
```

---

## 🧪 步驟 4：運行測試

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

## 💬 步驟 5：使用互動式 CLI（推薦）

```bash
# 啟動 CLI
python Entry.py
```

### CLI 功能展示

```
================================================================================
                        Echo Memory System - 互動式 CLI
================================================================================

初始化 Echo Memory System
--------------------------------------------------------------------------------
✓ 記憶引擎初始化成功
ℹ 存儲路徑: ./data
ℹ 嵌入模型: all-MiniLM-L6-v2
ℹ 已載入 0 個情節, 0 個概念
✓ LLM 客戶端初始化成功

✓ 系統就緒！輸入 'help' 查看可用命令

主選單
--------------------------------------------------------------------------------
  ✓ store      - 存儲對話
  ✓ retrieve   - 檢索記憶
  ✓ chat       - 對話模式（存儲+檢索+LLM）
  ✓ stats      - 查看統計
  ✓ list       - 列出所有情節
  ℹ config     - 顯示配置
  ⚠ dream      - 執行 Dream Engine
  ✗ clear      - 清空記憶
  ? help       - 顯示幫助
  ← exit       - 退出

echo>
```

### 常用命令示例

**1. 對話模式（最常用 - 整合 LLM）**
```
echo> chat

對話模式
--------------------------------------------------------------------------------
ℹ 輸入 'exit' 退出對話模式
ℹ 系統提示詞: 你是一個有記憶的智能助手。請根據提供的記憶脈絡，提供更個性化...

你: 今天天氣如何？

⏳ 生成回應中...

Agent: 我目前沒有即時天氣資訊，但我可以記住你對天氣的關注。請問你在哪個城市呢？

✓ 已存儲 (ID: ep_a8f3c...)

你: 我在台北，喜歡晴天

相關記憶
--------------------------------------------------------------------------------
【相關對話記憶】
  - （剛才）[重要程度:《一般記憶》] 今天天氣如何？

⏳ 生成回應中...

Agent: 了解！我記住了你在台北並且喜歡晴朗的天氣。下次你詢問天氣時，我會特別注意台北的天氣情況，並留意晴天資訊。

✓ 已存儲 (ID: ep_b9e2d...)

你: exit
ℹ 退出對話模式
```

**2. 檢索記憶**
```
echo> retrieve

檢索記憶
--------------------------------------------------------------------------------
查詢內容: 我喜歡什麼天氣？
多巴胺濃度 (0-1, 默認 0.5): 
血清素濃度 (0-1, 默認 0.5): 

檢索結果
--------------------------------------------------------------------------------
【相關對話記憶】
  - （剛才）[重要程度:《核心記憶》] 我喜歡晴天
  - （剛才）[重要程度:《一般記憶》] 今天天氣如何？

✓ 找到 2 個相關情節
```

**3. 查看統計**
```
echo> stats

記憶統計
--------------------------------------------------------------------------------
  總情節數:     5
  總概念數:     0
  工作記憶緩存: 0
  當前會話計數: 5
```

---

## 💬 步驟 6：運行示例腳本（可選）

```bash
# 運行對話示例
python examples/minimal_chat.py
```

### 預期輸出

```
2026-04-06 ... [INFO] example: === 配置摘要 ===

Echo Memory System - 配置摘要
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LLM Provider:
  - GCP Project: your-project-id
  - GCP Location: asia-east1
  ...

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
# └── epis配置驗證失敗` 或 `ModuleNotFoundError: No module named 'dotenv'`

**解決**：
```bash
# 安裝 python-dotenv
pip install python-dotenv

# 或重新安裝所有依賴
pip install -r requirements.txt
```

### 問題 2：`.env 文件不存在`

**解決**：
```bash
# 複製範本
cp .env5example .env

# 編輯並填入你的配置
# Windows: notepad .env
# Linux/macOS: nano .env
```

### 問題 6：`GCP_PROJECT_ID 或 GOOGLE_API_KEY 未設定`

**解決**：
1. 確保 `.env` 文件已正確創建
2. 確保至少設定了 `GCP_PROJECT_ID` 或 `GOOGLE_API_KEY` 其中之一
3. 運行 `python verify_config.py` 確認配置載入成功

### 問題 4：`odic/
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

現在你已經有一個可運行的 MVP，並且可以通過互動式 CLI 測試！接下來可以：

1. **使用 CLI 測試記憶功能** - `python Entry.py`
2. **閱讀 README.md** - 了解完整架構設計
3. **查看 `echo_memory/engine.py`** - 理解核心 API
4. **擴展功能** - 參考 Phase 2/3/4 目標實作更多功能

---

## 🎯 Phase 1 完成檢查清單

- [x] 專案基建與目錄結構
- [x] 配置管理（.env 與環境變數）
- [x] Episode 存儲（FAISS + JSON）
- [x] 向量檢索基礎功能
- [x] MemoryContext 文本序列化
- [x] 單元測試（基礎覆蓋）
- [x] 最小對話示例
- [x] 配置驗證腳本

**Phase 1 狀態**：✅ **完成**

準備好進入 **Phase 2: EcphoryRAG 多跳檢索** 了嗎？
