# Echo Memory System - MVP

> 受 OpenSoul 啟發的自主記憶子系統原型
> 
> 目標：將記憶系統從被動 RAG 工具轉變為主動、動態的認知子系統

---

## 🎯 設計理念

### Form-Function-Dynamic 模型

| 維度 | 設計決策 | 實作技術 |
|------|---------|---------|
| **Form（形式）** | 三層記憶架構 | NumPy + NetworkX 混合存儲 |
| **Function（功能）** | Working/Episodic/Semantic | 短期緩存、對話事件、抽象知識 |
| **Dynamic（動力）** | 動態邊緣權重 + 離線鞏固 | 時間衰減、頻率強化、顯著性調控 |

### 核心創新點

1. **EcphoryRAG 聯想檢索**
   - 向量相似度定位種子節點
   - 圖譜多跳關聯展開
   - 神經化學狀態調控檢索參數

2. **動態邊緣權重**
   ```
   W(edge) = 0.3·Recency + 0.4·Frequency + 0.3·Salience
   
   Recency   = exp(-0.01·hours_ago)
   Frequency = log(1+n) / log(1+n_max)
   Salience  = dopamine × (1 - 0.3×serotonin)
   ```

3. **離線記憶鞏固（Dream Engine）**
   - **LiDER 重播**：高顯著性情節總結經驗
   - **知識蒸餾**：從具體對話抽取抽象規則
   - **圖譜修剪**：刪除低權重邊緣，建立潛意識捷徑

---

## 📁 專案結構

```
TestSeperateMemorySystem/
├── README.md                    # 本文件
├── requirements.txt             # Python 依賴
├── pyproject.toml              # 專案配置（Poetry）
├── .env.example                # 環境變數模板
│
├── echo_memory/                # 核心記憶引擎
│   ├── __init__.py
│   ├── engine.py               # MemoryEngine 主類
│   │
│   ├── storage/                # 存儲層
│   │   ├── episodic.py        # 情節記憶（對話事件）
│   │   ├── semantic.py        # 語義記憶（抽象知識）
│   │   ├── working.py         # 工作記憶（短期緩存）
│   │   ├── graph.py           # 圖譜管理（NetworkX）
│   │   └── vector.py          # 向量存儲（規劃中）
│   │
│   ├── retrieval/              # 檢索引擎
│   │   ├── ecphory.py         # EcphoryRAG 聯想檢索
│   │   ├── weighting.py       # 動態邊緣權重計算
│   │   └── context.py         # MemoryContext 構建
│   │
│   ├── consolidation/          # 離線鞏固
│   │   ├── dream_engine.py    # Dream Engine 主協調器
│   │   ├── replay.py          # LiDER 經驗重播
│   │   ├── distillation.py    # 知識蒸餾（情節→規則）
│   │   └── pruning.py         # 圖譜修剪
│   │
│   └── schemas/                # 資料結構
│       ├── episode.py         # Episode 節點
│       ├── concept.py         # Concept 節點
│       ├── memory_context.py  # 檢索結果容器
│       └── edge.py            # 圖譜邊緣定義
│
├── llm_integration/            # LLM 整合層
│   ├── __init__.py
│   ├── llm_client.py          # LLM 調用封裝（Gemini/Claude）
│   ├── prompt_builder.py      # 系統提示詞構建
│   ├── thought_stream.py      # 可見思緒（Chain-of-Thought）
│   └── embedding.py           # 文本嵌入服務
│
├── neurochem/                  # 神經化學模擬
│   ├── __init__.py
│   ├── state.py               # NeurochemState（多巴胺/血清素）
│   └── dynamics.py            # 狀態動力學更新
│
├── tests/                      # 單元測試
│   ├── test_episodic.py
│   ├── test_ecphory.py
│   ├── test_weighting.py
│   └── test_dream_engine.py
│
├── examples/                   # 示例與演示
│   ├── minimal_chat.py        # 最小對話示例
│   ├── dream_demo.py          # Dream Engine 演示
│   └── thought_stream_demo.py # 可見思緒演示
│
└── data/                       # 資料存儲（不提交到 Git）
    ├── graphs/                # NetworkX 圖譜存儲
    ├── vectors/               # 向量資料
    └── snapshots/             # 夢境快照
```

---

## 🚀 快速開始

### 環境準備

```bash
# 1. 創建虛擬環境
python -m venv venv
.\venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/macOS

# 2. 安裝依賴
pip install -r requirements.txt

# 3. 配置環境變數（使用 U.E.P Core 相同設定）
# 編輯 .env，設定 GCP_PROJECT_ID 和 GCP_LOCATION
# 或設定 GOOGLE_API_KEY（備用）

# 4. 驗證配置
python verify_config.py
```

### 互動式 CLI（推薦）

```bash
# 啟動互動式 CLI
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
from llm_integration import LLMClient
from neurochem import NeurochemState

# 初始化記憶引擎
memory = MemoryEngine(
    storage_path="./data",
    embedding_model="all-MiniLM-L6-v2"
)

# 初始化神經化學狀態
neurochem = NeurochemState(dopamine=0.5, serotonin=0.5)

# 初始化 LLM 客戶端
llm = LLMClient(provider="google", model="gemini-2.0-flash-exp")

# 對話循環
user_input = "今天天氣如何？"

# 1. 檢索相關記憶
context = memory.retrieve(
    query=user_input,
    dopamine=neurochem.dopamine,
    serotonin=neurochem.serotonin
)

# 2. 構建提示詞（含記憶上下文）
prompt = llm.build_prompt(
    user_input=user_input,
    memory_context=context.to_text()
)

# 3. LLM 生成回應
response = llm.generate(prompt)

# 4. 存儲本次對話為情節記憶
memory.store_episode(
    user_input=user_input,
    agent_response=response,
    session_id="demo_001",
    salience=neurochem.compute_salience()
)

print(f"Agent: {response}")
```

---

## 🧪 MVP 驗證目標

### Phase 1: 基礎記憶存儲（1 週）
- [x] 專案基建與目錄結構
- [ ] Episodic Memory 存儲（NumPy 向量 + JSON 元數據）
- [ ] Semantic Memory 存儲（NetworkX 圖譜）
- [ ] Working Memory 短期緩存（dict + TTL）
- [ ] 單元測試覆蓋率 > 80%

### Phase 2: EcphoryRAG 檢索（2 週）
- [ ] 向量相似度種子節點定位
- [ ] 圖譜多跳關聯展開（BFS/DFS）
- [ ] 神經化學參數調控（seed_k, breadth_k, threshold）
- [ ] MemoryContext 文本序列化
- [ ] 檢索性能測試（< 100ms）

### Phase 3: LLM 整合（1 週）
- [ ] Gemini API 調用封裝
- [ ] 系統提示詞構建（含記憶注入）
- [ ] 可見思緒（Chain-of-Thought）生成
- [ ] 完整對話循環示例

### Phase 4: Dream Engine（2 週）
- [ ] LiDER 重播基礎框架
- [ ] 知識蒸餾（情節 → 規則）
- [ ] 圖譜修剪（低權重邊緣刪除）
- [ ] 定時觸發器（APScheduler）
- [ ] 夢境報告生成

---

## 📊 評估指標

### 功能性指標
- **記憶存儲**：支援 10,000+ 情節節點
- **檢索速度**：平均檢索時間 < 100ms
- **圖譜遍歷**：3 跳展開耗時 < 50ms
- **Dream 週期**：單次完整鞏固 < 5 分鐘

### 質量指標
- **檢索準確率**：相關記憶召回率 > 85%
- **權重衰減**：30 天前記憶權重 < 0.2
- **去重效果**：重複對話合併率 > 95%

---

## 🔧 技術棧

| 類別 | 技術選型 | 版本 |
|------|---------|------|
| **Python** | CPython | 3.10+ |
| **向量檢索** | NumPy 精確餘弦相似度 | 1.24+ |
| **圖譜存儲** | NetworkX | 3.2+ |
| **嵌入模型** | Sentence Transformers | 2.2+ |
| **LLM** | Google Gemini / Anthropic Claude | - |
| **排程** | APScheduler | 3.10+ |
| **序列化** | Pydantic | 2.5+ |
| **測試** | pytest | 7.4+ |

---

## 🔄 與 U.E.P Core 整合計畫

### 遷移策略（Phase 5）

1. **模組包裝**
   ```python
   # modules/mem_module_v2/__init__.py
   from echo_memory import MemoryEngine
   
   def register(event_bus, debug_api):
       return MemModuleV2(MemoryEngine(...))
   ```

2. **協作通道替換**
   ```python
   # core/module_coordinator.py
   # 舊：mem_module.retrieve()
   # 新：mem_engine_v2.retrieve()
   memory_context = mem_engine_v2.retrieve(user_text)
   working_context.set_channel_data("CHAT_MEM", {
       "memory_text": memory_context.to_text()
   })
   ```

3. **SLEEP 整合**
   ```python
   # core/states/state_manager.py
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
     embedding_model: "all-MiniLM-L6-v2"
   ```

---

## 📚 參考資料

### 理論基礎
- **Ecphory**: Tulving, E., & Thomson, D. M. (1973). *Encoding specificity and retrieval processes in episodic memory.*
- **Memory Consolidation**: Stickgold, R. (2005). *Sleep-dependent memory consolidation.*
- **Graph-based Memory**: Saxe, A. M., et al. (2019). *A mathematical theory of semantic development.*

### 技術參考
- **OpenSoul**: [架構文檔](../OpenSoul/docs/architecture.md)
- **U.E.P SDD**: [系統設計文檔](../U.E.P-s-Core/docs/SDD/)
- **FalkorDB GraphRAG**: 圖譜 RAG 最佳實踐

---

## 📝 開發日誌

### 2026-04-06
- ✅ 專案初始化
- ✅ README 編寫
- ⏳ 基礎目錄結構建立
- ⏳ 依賴配置

---

## 📄 授權

本專案為 U.E.P 重構實驗性模組，採用與主專案相同的授權協議。

參考 OpenSoul 開源實作（MIT License），並根據 U.E.P 需求進行適配。
