# Echo Memory System - MVP

> 受 OpenSoul 啟發的自主記憶子系統原型
>
> 目標：將記憶系統從被動 RAG 工具轉變為主動、動態的認知子系統

---

## 設計理念

### Form-Function-Dynamic 模型

| 維度 | 設計決策 | 實作技術 |
|------|---------|---------|
| **Form（形式）** | 三層記憶架構 | NumPy + NetworkX 混合存儲 |
| **Function（功能）** | Working/Episodic/Semantic | 短期緩存、對話事件、抽象知識 |
| **Dynamic（動力）** | 動態邊緣權重 + 離線鞏固 | 時間衰減、頻率強化、顯著性調控 |

### 核心創新點

1. **EcphoryRAG 聯想檢索**
   - 向量相似度定位種子節點（NumPy 餘弦相似度）
   - 圖譜多跳關聯展開
   - 神經化學狀態調控檢索參數

   > **注意**：向量檢索目前使用 **NumPy dot product（精確餘弦相似度）** 實作。
   > FAISS ANN 索引為規劃功能（Planned），尚未整合。

2. **動態邊緣權重**
   ```
   W(edge) = α·Recency + β·Frequency + γ·Salience
           = 0.3·Recency + 0.4·Frequency + 0.3·Salience

   Recency   = exp(-λ·hours_ago)        # 時間單位：小時
               λ = EDGE_RECENCY_LAMBDA  # 預設 0.01 /小時
   Frequency = log(1+n) / log(1+n_max)
   Salience  = dopamine × (1 - 0.3×serotonin)
   ```

   **衰減行為（λ=0.01 /小時）：**
   | 時間 | Recency |
   |------|---------|
   | 1 小時後 | ≈ 0.990 |
   | 24 小時後 | ≈ 0.787 |
   | 7 天後 | ≈ 0.186 |

   **時間語義說明：**
   - `time_delta`（建邊時）：兩個 episode 之間的事件間隔，反映記憶距離的遠近
   - `edge_age`（Dream 更新時）：邊從建立到現在的存活時間，反映連結本身有多老舊
   - 兩者均以**小時**為單位傳入衰減公式

3. **離線記憶鞏固（Dream Engine）**
   - **LiDER 重播**：高顯著性情節總結經驗
   - **知識蒸餾**：從具體對話抽取抽象規則
   - **圖譜修剪**：刪除低權重邊緣，建立潛意識捷徑

---

## 專案結構

```
TestSeperateMemorySystem/
├── README.md                    # 本文件
├── requirements.txt             # Python 依賴
├── pyproject.toml               # 專案配置（setuptools）
├── .env.example                 # 環境變數模板
│
├── echo_memory/                 # 核心記憶引擎
│   ├── __init__.py
│   ├── config.py                # 配置管理
│   ├── engine.py                # MemoryEngine 主類
│   │
│   ├── storage/                 # 存儲層
│   │   ├── episodic.py         # 情節記憶（NumPy + JSON）
│   │   ├── semantic.py         # 語義記憶（簡化版 dict）
│   │   ├── working.py          # 工作記憶（TTL 緩存）
│   │   └── graph.py            # 圖譜管理（NetworkX）
│   │
│   ├── retrieval/               # 檢索引擎
│   │   └── ecphory.py          # EcphoryRAG 聯想檢索
│   │
│   ├── consolidation/           # 離線鞏固
│   │   ├── dream_engine.py     # Dream Engine 協調器
│   │   └── pruning.py          # 圖譜修剪
│   │
│   └── schemas/                 # 資料結構
│       ├── episode.py          # Episode 節點
│       ├── concept.py          # Concept 節點
│       └── memory_context.py   # 檢索結果容器
│
├── llm_integration/             # LLM 整合層
│   ├── __init__.py
│   └── llm_client.py           # LLM 調用封裝（Gemini/Claude）
│
├── neurochem/                   # [Planned] 神經化學模擬（目錄尚未建立）
│   └── （Phase 2+ 規劃中）
│
├── tests/                       # 單元測試
│   ├── test_config.py
│   ├── test_engine.py
│   ├── test_episodic.py
│   ├── test_dream_engine.py
│   ├── test_dream_quick.py
│   └── test_hybrid_retrieval.py
│
└── examples/                    # 示例
    └── minimal_chat.py          # 最小對話示例
```

---

## 快速開始

### 環境準備

```bash
# 1. 創建虛擬環境
python -m venv venv
.\venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/macOS

# 2. 安裝套件（開發模式）
pip install -e ".[dev]"

# 3. 配置環境變數
cp .env.example .env
# 編輯 .env，設定 GOOGLE_API_KEY 或 GCP_PROJECT_ID

# 4. 驗證配置
python verify_config.py
```

### 互動式 CLI（推薦）

```bash
python Entry.py
```

進入後可使用以下命令：
- `store` - 手動存儲對話
- `retrieve` - 檢索相關記憶
- `chat` - 對話模式（自動檢索+存儲）
- `stats` - 查看統計資訊
- `list` - 列出所有情節
- `help` - 查看完整說明

### 程式化使用

```python
from echo_memory import MemoryEngine

# 初始化記憶引擎（不需要 API key）
# 預設模型：BAAI/bge-small-zh-v1.5（中文優化，512 維）
# 切換模型時請同步刪除 data/embeddings.npy，舊向量與新模型不相容
memory = MemoryEngine(
    storage_path="./data",
    embedding_model="BAAI/bge-small-zh-v1.5"
)

# 存儲對話
memory.store_episode(
    user_input="今天天氣如何？",
    agent_response="台北晴朗，25°C。",
    session_id="demo_001",
    salience=0.7,
)

# 檢索記憶
context = memory.retrieve(
    query="天氣怎麼樣？",
    dopamine=0.5,
    serotonin=0.5,
)

print(context.to_text())
```

---

## MVP 驗證目標

### Phase 1: 基礎記憶存儲（已完成）
- [x] 專案基建與目錄結構
- [x] Episodic Memory 存儲（NumPy 向量 + JSON 元數據）
- [x] Semantic Memory 存儲（NetworkX 圖譜骨架）
- [x] Working Memory 短期緩存（dict + TTL）

### Phase 2: EcphoryRAG 檢索（進行中）
- [x] 向量相似度種子節點定位（NumPy 餘弦相似度）
- [ ] 圖譜多跳關聯展開（BFS/DFS）
- [ ] 神經化學參數調控（seed_k, breadth_k, threshold）
- [ ] MemoryContext 文本序列化

### Phase 3: LLM 整合（規劃中）
- [ ] Gemini API 調用封裝
- [ ] 系統提示詞構建（含記憶注入）

### Phase 4: Dream Engine（規劃中）
- [ ] LiDER 重播基礎框架
- [ ] 知識蒸餾（情節 -> 規則）
- [ ] 圖譜修剪（低權重邊緣刪除）
- [ ] 定時觸發器（APScheduler）

### Phase 5: 神經化學模擬（Planned）
- [ ] NeurochemState（多巴胺/血清素）
- [ ] 狀態動力學更新
- [ ] 與 EcphoryRAG 整合

---

## 技術棧

| 類別 | 技術選型 | 版本 | 狀態 |
|------|---------|------|------|
| **Python** | CPython | 3.10+ | 已用 |
| **向量檢索** | NumPy 精確餘弦相似度 | 1.24+ | 已用 |
| **向量索引加速** | FAISS | 1.9+ | [Planned] |
| **圖譜存儲** | NetworkX | 3.2+ | 已用 |
| **嵌入模型** | Sentence Transformers | 2.2+ | 已用 |
| **LLM** | Google Gemini / Anthropic Claude | - | 已用 |
| **排程** | APScheduler | 3.10+ | 已用 |
| **序列化** | Pydantic | 2.5+ | 已用 |
| **測試** | pytest | 7.4+ | 已用 |

---

## 與 U.E.P Core 整合計畫

### 遷移策略（Phase 6）

1. **模組包裝**
   ```python
   # modules/mem_module_v2/__init__.py
   from echo_memory import MemoryEngine

   def register(event_bus, debug_api):
       return MemModuleV2(MemoryEngine(...))
   ```

2. **協作通道替換**
   ```python
   memory_context = mem_engine_v2.retrieve(user_text)
   working_context.set_channel_data("CHAT_MEM", {
       "memory_text": memory_context.to_text()
   })
   ```

3. **SLEEP 整合**
   ```python
   async def _handle_sleep_started(self):
       dream_report = await mem_engine_v2.dream_now("sleep")
       logger.info(dream_report.summary())
   ```

4. **配置統一**
   ```yaml
   # configs/config.yaml
   modules_enabled:
     mem_module_v2: true
   
   memory_v2:
     storage_path: "memory/v2"
     embedding_model: "BAAI/bge-small-zh-v1.5"
   ```

---

## 參考資料

### 理論基礎
- **Ecphory**: Tulving, E., & Thomson, D. M. (1973). *Encoding specificity and retrieval processes in episodic memory.*
- **Memory Consolidation**: Stickgold, R. (2005). *Sleep-dependent memory consolidation.*

### 技術參考
- **OpenSoul**: 架構設計參考
- **U.E.P SDD**: 系統設計文檔

---

## 授權

本專案為 U.E.P 重構實驗性模組，採用 MIT License。
