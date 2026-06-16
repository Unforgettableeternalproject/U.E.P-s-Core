# Echo Memory System

> 受 OpenSoul 啟發的自主記憶子系統 — 從 U.E.P Core `mem_module` 獨立遷出
>
> 將記憶系統從被動 RAG 工具轉變為主動、動態的認知子系統

---

## 專案狀態

| 里程碑 | 狀態 |
|--------|------|
| Phase 1：基礎記憶存儲 | ✅ 完成 |
| Phase 2：EcphoryRAG 檢索 | ✅ 完成 |
| Phase 3：LLM 整合 + 工具 | ✅ 完成 |
| Phase 4：Dream Engine | ✅ 完成 |
| OpenSoul 完整遷移 | ✅ 完成（2026-06-15） |
| 端到端驗證 | ✅ 25 輪 Probe + 12 Integration Tests |
| 測試覆蓋 | ✅ 608 單元測試 + 12 整合測試 |
| 下一步 | Discord Bot 多用戶驗證 |

---

## 核心特性

- **四層記憶架構**：Working / Episodic / Semantic / Procedural，各層職責明確分離
- **EcphoryRAG 五步聯想檢索**：向量種子 → 圖譜展開 → 語義概念 → 矛盾偵測 → 融合排序
- **神經化學雙軸調控**：DA/5-HT 驅動 5 種認知模式，動態影響檢索廣度與閾值
- **OpenSoul 完整情感管線**：SubconsciousAssessor（五維評估）→ SalienceEvaluator（三值輸出）→ ResponseVerifier（四維品質打分）→ SubconsciousInhibitor（PASS/REVISE/SUPPRESS）
- **概念管理三層分流**：ConceptExtractor（daemon Thread 即時提取）→ 同義詞歸一 / 多義詞分裂 / 新概念建立
- **StorageJudge**：Hybrid 規則 + LLM 存儲決策，取代硬編碼 keyword matching
- **Dream Engine**：Graph Pruning + LATENT_BRIDGE + LiDER Replay + Knowledge Distillation + Procedure Extraction，閒置/Cron 雙模式觸發
- **反思行動閉環**：ReflectionModule（定時反思）→ ActionQueue → ReflectionExecutor（含 rollback 機制）
- **LLM 自主記憶工具**：`force_remember` / `deep_recall` / `adjust_importance` / `trigger_dream`，由對話 LLM 主動調用
- **MCP Server**：7 個標準工具 + 多租戶 namespace 隔離，支援外部系統整合

---

## 技術棧

| 類別 | 技術 | 說明 |
|------|------|------|
| Python | 3.10+ | 型別標注 + Google-style docstring |
| 向量 | NumPy | 稠密向量 + 精確餘弦相似度 |
| 圖譜 | NetworkX 3.2+ | 輕量替代 FalkorDB |
| Embedding | Ollama bge-m3 | 1024 維多語言（正式基準） |
| LLM | Google Gemini | gemini-3.1-flash-lite（Chat）+ gemini-2.5-flash-lite（Eval） |
| 排程 | APScheduler | Dream 閒置/Cron 觸發 |
| CLI | Rich | 互動式終端 UI |
| MCP | FastMCP | 外部整合介面 |
| 序列化 | Pydantic | 資料驗證與模式定義 |
| 測試 | pytest | 單元測試 + 整合測試 |

---

## 快速開始

### 環境準備

本專案刻意不維護獨立虛擬環境，統一使用同層主專案 `U.E.P-s-Core\env`。
正式 embedding 基準為 Ollama `bge-m3`（1024 維多語言）。

```powershell
$UEP_PYTHON = "..\U.E.P-s-Core\env\Scripts\python.exe"

# 1. 配置環境變數
Copy-Item .env.example .env
# 編輯 .env，必填項目：
#   GOOGLE_API_KEY=<your_key>
#   EMBEDDING_BACKEND=ollama
#   EMBEDDING_MODEL=bge-m3

# 2. 確認 Ollama 服務與模型
ollama list   # 應看到 bge-m3

# 3. 執行測試套件
& $UEP_PYTHON -m pytest tests/ -x -q
```

### 互動式 CLI

```powershell
.\run.bat
```

進入後可使用以下指令：

| 指令 | 說明 |
|------|------|
| `chat` | 對話模式（自動檢索 + 存儲 + 神經化學調控） |
| `store` | 手動存儲對話片段 |
| `retrieve` | 手動檢索相關記憶 |
| `stats` | 查看記憶統計與神經化學狀態 |
| `list` | 列出所有情節記憶 |
| `dream` | 手動觸發 Dream Engine |
| `help` | 查看完整說明 |

### 程式化使用

```python
from echo_memory import MemoryEngine

memory = MemoryEngine(
    storage_path="./data",
    embedding_model="bge-m3",
    embedding_backend="ollama",
    ollama_base_url="http://localhost:11434",
    query_prefix="",
    namespace_id="local-default",
)

# 存儲對話（每一輪均存儲，salience 作為屬性而非門閥）
memory.store_episode(
    user_input="今天天氣如何？",
    agent_response="台北晴朗，25°C。",
    session_id="demo_001",
)

# 檢索記憶（神經化學參數動態影響檢索廣度）
context = memory.retrieve(
    query="天氣怎麼樣？",
    dopamine=0.5,
    serotonin=0.5,
)

print(context.to_text())
```

### Namespace 與資料安全

- `local-default` 沿用既有 `storage_path`，其他 namespace 存放於 `<storage_path>/namespaces/<namespace_id>`
- 同一 namespace 的寫入與 Dream 操作透過跨程序檔案鎖序列化，防止多實例資料衝突
- `embedding_index.json` 記錄模型、維度與 episode 順序；模型或索引不一致時自動完整重建
- 檢索內容以 `UNTRUSTED_MEMORY_DATA` JSON envelope 注入 prompt，防範 prompt injection

---

## 專案結構

```
TestSeperateMemorySystem/
├── Entry.py                     # 互動式 CLI（對話、記憶管理、除錯）
├── run.bat                      # 啟動腳本
├── requirements.txt             # Python 依賴
├── pyproject.toml               # 專案配置（setuptools）
├── .env.example                 # 環境變數模板
│
├── echo_memory/                 # 核心記憶引擎
│   ├── engine.py                # MemoryEngine 主類
│   ├── config.py                # 配置管理
│   ├── namespace.py             # 命名空間隔離
│   │
│   ├── schemas/                 # 資料結構定義
│   │   ├── episode.py           # Episode（情節記憶）
│   │   ├── concept.py           # Concept（語義概念）
│   │   ├── entity.py            # Entity（實體）
│   │   ├── procedure.py         # Procedure（程序記憶）
│   │   └── memory_context.py    # MemoryContext（檢索結果容器）
│   │
│   ├── storage/                 # 存儲層
│   │   ├── episodic.py          # 情節記憶（NumPy 向量 + JSON）
│   │   ├── semantic.py          # 語義記憶
│   │   ├── working.py           # 工作記憶（TTL 短期快取）
│   │   ├── procedural.py        # 程序記憶
│   │   ├── graph.py             # NetworkX 記憶圖譜
│   │   ├── entity.py            # 實體存儲
│   │   └── locking.py           # 檔案鎖
│   │
│   ├── retrieval/               # 檢索層
│   │   └── ecphory.py           # EcphoryRAG 聯想檢索
│   │
│   ├── neurochem/               # 神經化學調控
│   │   └── state.py             # NeurochemState（DA/5-HT + 事件驅動 + 持久化）
│   │
│   ├── affect/                  # 情感與潛意識評估（OpenSoul 遷移）
│   │   ├── signals.py           # SalienceSignals + SubconsciousAssessment
│   │   ├── salience.py          # SalienceEvaluator（三值輸出）
│   │   ├── subconscious.py      # SubconsciousAssessor（LLM 五維度）
│   │   ├── concept_verifier.py  # ConceptVerifier（概念前驗證）
│   │   ├── concept_inhibitor.py # ConceptInhibitor（概念 Patch 閘門）
│   │   ├── verifier.py          # ResponseVerifier（四維品質打分）
│   │   ├── inhibitor.py         # SubconsciousInhibitor（PASS/REVISE/SUPPRESS）
│   │   ├── judge.py             # StorageJudge（Hybrid 工具預過濾 + 存儲決策）
│   │   ├── concept_extractor.py # 即時概念提取（ThreadPoolExecutor）
│   │   ├── soul_note.py         # Soul Note 潛意識日記
│   │   ├── reflection.py        # ReflectionModule（定時反思）
│   │   ├── reflection_executor.py # ReflectionExecutor（反思行動執行器）
│   │   └── action_queue.py      # ReflectionActionQueue（行動排隊 + audit）
│   │
│   ├── utils/                   # 通用工具
│   │   └── sanitizer.py         # 內容消殺（偽造工具標記清理）
│   │
│   └── dream/                   # Dream Engine（離線鞏固）
│       ├── engine.py            # DreamEngine 主協調器
│       ├── replay.py            # LiDER 記憶重播
│       ├── distillation.py      # 知識蒸餾
│       ├── pruning.py           # 圖譜修剪 + LATENT_BRIDGE + 歸檔
│       ├── entity_extractor.py  # 實體提取
│       ├── procedure_extractor.py # 程序 SOP 提取
│       └── contradiction.py     # 矛盾偵測 + ConflictLedger
│
├── llm_integration/             # LLM 整合層
│   ├── llm_client.py            # Gemini LLM 客戶端
│   ├── config.yaml              # LLM 配置（角色、prompt）
│   └── tools/                   # Function Calling 工具
│       ├── definitions.py       # 工具定義 Schema
│       └── executor.py          # 工具執行器
│
├── mcp_server/                  # MCP Server（外部整合）
│   ├── server.py                # FastMCP 伺服器
│   ├── handlers.py              # 工具處理器
│   └── resources.py             # 資源暴露
│
├── scripts/                     # 端到端驗證腳本
│   ├── probe_pipeline.py        # 25 輪自動劇本 + Rich 推理地圖
│   └── dialogue_script.py       # 對話場景腳本
│
├── tests/                       # 測試套件
│   ├── *.py                     # 608 個單元測試
│   └── integration/             # 12 個整合測試
│
└── examples/                    # 使用範例
    └── minimal_chat.py          # 最小對話範例
```

---

## 完整對話處理流程

```
用戶輸入
  │
  ├─ [Step 1]   EcphoryRAG 五步聯想檢索
  │              向量種子 → 圖譜展開 → 語義概念 → 矛盾偵測 → 融合排序
  │              （DA/5-HT 動態調控 seed_k / breadth_k / threshold）
  │
  ├─ [Step 1.5] SubconsciousAssessor 五維評估（情緒/新穎/相關/緊迫/衝突）
  │              + JudgeAgent 工具預過濾
  │
  ├─ [Step 2]   LLM 生成回覆
  │              → 內容消殺（sanitizer 清除偽造工具標記）
  │              → ResponseVerifier 四維品質打分
  │              → SubconsciousInhibitor 閘門（PASS / REVISE / SUPPRESS）
  │
  ├─ [Step 3]   SalienceEvaluator 三值計算（HIGH / MEDIUM / LOW）
  │              → 存儲 Episode（salience / da / ht 作為屬性，不作為門閥）
  │
  ├─ [Step 4]   背景概念提取（daemon Thread）
  │              同義詞歸一 / 多義詞分裂 / 新概念建立
  │
  ├─ [Step 5]   Soul Note 潛意識日記寫入
  │
  ├─ [Step 6]   ReflectionModule 定時反思
  │              每 10 輪或 uncertainty > 0.7 觸發
  │              → ActionQueue → ReflectionExecutor（含 rollback）
  │
  ├─ [Step 7]   Entity 提取（salience >= 門檻時）
  │
  └─ [Step 8]   NeurochemState tick() + 持久化
```

### 動態邊緣權重公式

```
W(edge) = α·Recency + β·Frequency + γ·Salience
        = 0.3·Recency + 0.4·Frequency + 0.3·Salience

Recency   = exp(-λ·hours_ago)       # λ = 0.01 /小時
Frequency = log(1+n) / log(1+n_max)
Salience  = dopamine × (1 - 0.3×serotonin)
```

---

## 端到端驗證

### 驗證工具

| 工具 | 說明 |
|------|------|
| `scripts/probe_pipeline.py` | 25 輪自動劇本，Rich 推理地圖視覺化，覆蓋記憶跨輪喚起與矛盾偵測 |
| `scripts/dialogue_script.py` | 多場景對話腳本，涵蓋情感轉折、概念演化、神經化學切換 |
| `tests/integration/` | 12 個整合測試，覆蓋完整 pipeline 端到端行為 |

### 驗證結果

- 2 個 Procedure 在 Dream Engine 蒸餾後被後續對話成功重用
- EcphoryRAG 跨輪喚起率符合預期閾值
- 神經化學狀態在連續高壓對話後正確觸發 CALM 模式切換

---

## 架構設計原則

### Salience 是權重，不是門閥

每一輪對話都存儲為 Episode，沒有例外。Salience 分數影響：
- 檢索排序（高 salience → 更容易被召回）
- 邊緣權重計算
- Dream Engine 修剪決策（低 salience → 更容易隨時間衰減）

低 salience 的記憶不被丟棄，而是自然衰減——這才是正確的遺忘機制。

### 禁止 Keyword Matching

記憶的重要性判斷由對話 LLM 透過工具主動介入，以及 `SalienceEvaluator` 和 `SubconsciousAssessor` 提供基礎權重。系統中不存在任何 keyword 清單或正則規則用於記憶決策。

---

## 與 U.E.P Core 整合計畫

### 遷移策略

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
     embedding_model: "bge-m3"
     embedding_backend: "ollama"
     ollama_base_url: "http://localhost:11434"
   ```

---

## 參考資料

### 理論基礎

- **Ecphory**：Tulving, E., & Thomson, D. M. (1973). *Encoding specificity and retrieval processes in episodic memory.*
- **Memory Consolidation**：Stickgold, R. (2005). *Sleep-dependent memory consolidation.*

### 架構參考

- **OpenSoul**：`C:\Users\Bernie\source\repos\Other\OpenSoul`（情感管線、記憶架構設計來源）
- **U.E.P SDD**：系統設計文檔

---

## 授權

本專案為 U.E.P 重構實驗性模組，採用 MIT License。
