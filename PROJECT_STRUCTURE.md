# Echo Memory System - 專案結構總覽

最後更新：2026-04-06

---

## 📂 完整目錄樹

```
TestSeperateMemorySystem/
│
├── 📄 README.md                    # 專案主文檔（設計理念、架構說明）
├── 📄 QUICKSTART.md                # 5 分鐘快速開始指南
├── 📄 DEVLOG.md                    # 開發日誌（進度、決策、學習）
├── 📄 requirements.txt             # Python 依賴清單
├── 📄 pyproject.toml               # 專案配置（Poetry/setuptools）
├── 📄 .env.example                 # 環境變數模板
├── 📄 .gitignore                   # Git 忽略規則
│
├── 📁 echo_memory/                 # 核心記憶引擎
│   ├── __init__.py                # 模組導出
│   ├── engine.py                  # MemoryEngine 主類
│   │
│   ├── 📁 schemas/                # 資料結構定義
│   │   ├── __init__.py
│   │   ├── episode.py            # Episode 節點
│   │   ├── concept.py            # Concept 節點
│   │   └── memory_context.py     # 檢索結果容器
│   │
│   ├── 📁 storage/                # 存儲層
│   │   ├── __init__.py
│   │   ├── episodic.py           # 情節記憶（NumPy + JSON）
│   │   ├── semantic.py           # 語義記憶（佔位符）
│   │   └── working.py            # 工作記憶（TTL 緩存）
│   │
│   ├── 📁 retrieval/              # 檢索引擎
│   │   ├── __init__.py
│   │   └── ecphory.py            # EcphoryRAG 檢索
│   │
│   └── 📁 consolidation/          # 離線鞏固
│       ├── __init__.py
│       └── dream_engine.py       # Dream Engine（佔位符）
│
├── 📁 llm_integration/            # LLM 整合層（待實作）
│   └── （Phase 3）
│
├── 📁 neurochem/                  # 神經化學模擬（待實作）
│   └── （Phase 2+）
│
├── 📁 tests/                      # 單元測試
│   ├── __init__.py
│   ├── test_episodic.py          # Episodic Memory 測試
│   └── test_engine.py            # MemoryEngine 整合測試
│
├── 📁 examples/                   # 示例與演示
│   └── minimal_chat.py           # 最小對話示例
│
└── 📁 data/                       # 資料存儲（運行時生成，不提交 Git）
    └── episodic/
        ├── episodes.json         # Episode 元數據
        └── embeddings.npy        # 向量索引
```

---

## 🎯 Phase 1 完成狀態

### ✅ 已實作模組

| 模組 | 文件 | 功能 | 狀態 |
|------|------|------|------|
| **MemoryEngine** | `engine.py` | 統一記憶管理 API | ✅ 完成 |
| **Episode Schema** | `schemas/episode.py` | 情節記憶節點定義 | ✅ 完成 |
| **Concept Schema** | `schemas/concept.py` | 語義概念節點定義 | ✅ 完成 |
| **MemoryContext** | `schemas/memory_context.py` | 檢索結果容器 | ✅ 完成 |
| **EpisodicMemory** | `storage/episodic.py` | NumPy + JSON 存儲 | ✅ 完成 |
| **SemanticMemory** | `storage/semantic.py` | 簡化版（dict） | ✅ 骨架 |
| **WorkingMemory** | `storage/working.py` | TTL 短期緩存 | ✅ 完成 |
| **EcphoryRetrieval** | `retrieval/ecphory.py` | 向量檢索 | ✅ Phase 1 |
| **DreamEngine** | `consolidation/dream_engine.py` | 離線鞏固 | ⏳ 骨架 |

### ⏳ 待實作模組（Phase 2+）

| 模組 | 計畫階段 | 優先級 |
|------|---------|--------|
| **動態邊緣權重** | Phase 2 | ⭐⭐⭐ |
| **圖譜多跳展開** | Phase 2 | ⭐⭐⭐ |
| **神經化學狀態** | Phase 2 | ⭐⭐ |
| **LLM Client** | Phase 3 | ⭐⭐⭐ |
| **Thought Stream** | Phase 3 | ⭐⭐ |
| **LiDER Replay** | Phase 4 | ⭐⭐ |
| **Knowledge Distillation** | Phase 4 | ⭐⭐ |
| **Graph Pruning** | Phase 4 | ⭐ |

---

## 📊 代碼統計

### 文件數量
- Python 源碼：15 個
- 測試文件：2 個
- 文檔文件：4 個
- **總計**：21 個文件

### 代碼行數（估算）
- 核心代碼：~800 行
- 測試代碼：~200 行
- 文檔：~1500 行
- **總計**：~2500 行

---

## 🔍 關鍵文件說明

### 核心實作

#### `echo_memory/engine.py`
**職責**：記憶系統的統一入口  
**主要 API**：
- `store_episode()` - 存儲對話
- `retrieve()` - 檢索記憶
- `dream()` - 離線鞏固
- `get_stats()` - 統計資訊

#### `echo_memory/storage/episodic.py`
**職責**：情節記憶的 CRUD 與向量檢索  
**技術棧**：
- NumPy: 精確餘弦相似度檢索與向量操作
- Sentence Transformers: 嵌入生成

#### `echo_memory/schemas/memory_context.py`
**職責**：檢索結果的結構化容器  
**核心方法**：
- `to_text()` - 序列化為 LLM Prompt 格式
- `_relative_time()` - 相對時間標記

### 測試與示例

#### `tests/test_episodic.py`
**覆蓋範圍**：
- Episode 創建與序列化
- 寫入與讀取
- 向量檢索
- 持久化

#### `examples/minimal_chat.py`
**展示功能**：
- 完整的存儲-檢索循環
- MemoryContext 文本格式
- 模擬 LLM Prompt 構建

---

## 🚀 運行流程

### 1. 初始化
```python
from echo_memory import MemoryEngine

engine = MemoryEngine(storage_path="./data")
```

### 2. 存儲對話
```python
ep_id = engine.store_episode(
    user_input="今天天氣如何？",
    agent_response="台北晴朗，25°C。",
    session_id="s1",
    salience=0.7,
)
```

### 3. 檢索記憶
```python
context = engine.retrieve(
    query="天氣怎麼樣？",
    dopamine=0.5,
    serotonin=0.5,
)
```

### 4. 使用記憶
```python
print(context.to_text())
# 輸出：
# 【相關對話記憶】
#   - （剛才）[重要程度:《核心記憶》] 今天天氣如何？
```

---

## 📚 文檔導航

| 文檔 | 用途 | 目標讀者 |
|------|------|---------|
| **README.md** | 完整設計說明 | 所有人 |
| **QUICKSTART.md** | 快速上手指南 | 新用戶 |
| **DEVLOG.md** | 開發進度與決策 | 開發者 |
| **本文件** | 專案結構總覽 | 開發者 |

---

## 🔗 與 U.E.P Core 整合路徑

### Phase 5: 遷移到 Core

1. **模組包裝**
   ```
   modules/mem_module_v2/
   └── __init__.py  # 包裝 MemoryEngine
   ```

2. **事件整合**
   ```python
   # 訂閱 CS_ENDED 事件
   event_bus.subscribe(
       SystemEvent.CS_ENDED,
       lambda data: mem_engine_v2.store_episode(...)
   )
   ```

3. **協作通道替換**
   ```python
   # 替換 CHAT_MEM
   context = mem_engine_v2.retrieve(user_text)
   working_context.set_channel_data("CHAT_MEM", context.to_text())
   ```

4. **SLEEP 整合**
   ```python
   # 在 SLEEP 狀態中觸發 Dream
   async def _handle_sleep_started():
       report = await mem_engine_v2.dream()
   ```

---

**Phase 1 完成日期**：2026-04-06  
**下一階段**：Phase 2 - EcphoryRAG 多跳檢索
