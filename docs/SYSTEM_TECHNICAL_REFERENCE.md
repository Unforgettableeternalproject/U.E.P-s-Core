# Echo Memory System — 系統技術參考文件

> **版本**：截至 2026-06-16 的完整實作狀態（含端到端驗證修復）
> **作者**：命運織者（Fate Weavers）技術文件
> **專案來源**：從 U.E.P's Core `mem_module` 獨立遷出的記憶子系統實驗

---

## 目錄

1. [專案定位與目標](#1-專案定位與目標)
2. [技術棧](#2-技術棧)
3. [架構總覽](#3-架構總覽)
4. [四層記憶架構](#4-四層記憶架構)
5. [核心引擎：MemoryEngine](#5-核心引擎memoryengine)
6. [檢索層：EcphoryRAG](#6-檢索層ecphoryrag)
7. [神經化學調控：NeurochemState](#7-神經化學調控neurochemstate)
8. [情感與潛意識評估：Affect 模組群](#8-情感與潛意識評估affect-模組群)
9. [品質閘門：Verifier + Inhibitor](#9-品質閘門verifier--inhibitor)
10. [工具決策：JudgeAgent](#10-工具決策judgeagent)
11. [概念提取：ConceptExtractor](#11-概念提取conceptextractor)
12. [Dream Engine：離線記憶鞏固](#12-dream-engine離線記憶鞏固)
13. [反思與潛意識日記：Reflection + SoulNote](#13-反思與潛意識日記reflection--soulnote)
14. [LLM 整合層](#14-llm-整合層)
15. [Function Calling 工具](#15-function-calling-工具)
16. [MCP Server：外部整合介面](#16-mcp-server外部整合介面)
17. [安全機制](#17-安全機制)
18. [核心公式彙整](#18-核心公式彙整)
19. [完整對話處理流程](#19-完整對話處理流程)
20. [架構禁令](#20-架構禁令)
21. [與 OpenSoul 的關係](#21-與-opensoul-的關係)
22. [已知限制與未來方向](#22-已知限制與未來方向)

---

## 1. 專案定位與目標

Echo Memory System 是受 **OpenSoul** 啟發並改良的**自主記憶子系統**。它不是一個 LLM 系統——LLM 只是工具，**記憶完整性才是核心**。

### 核心理念

- 將記憶系統從**被動 RAG 工具**轉變為**主動、動態的認知子系統**
- 每一輪對話都應被記住（Salience 是權重，不是門閥）
- 記憶重要性由 LLM 自主判斷，而非硬編碼規則
- 記憶會自然衰減、鞏固、蒸餾——模擬人類記憶的遺忘曲線

### 在 U.E.P 回聲系統中的位置

Echo Memory = 回聲系統的 **Shared Memory 核心**。未來整合路徑：

```
U.E.P Core → 回聲系統改造
  ├── MEM 模組 ← Echo Memory System（本專案）
  ├── TTS 模組 ← TestSeparateTTSSystem
  ├── STT 模組 ← TestSeparateSTTSystem
  ├── Session Control ← TestSeparateSessionControl
  └── Discord Bot 驗證 ← 合併前的驗證關卡
```

---

## 2. 技術棧

| 類別 | 技術 | 版本/說明 |
|------|------|----------|
| **語言** | Python | 3.10+ |
| **向量運算** | NumPy | 稠密向量存儲與餘弦相似度計算 |
| **圖譜存儲** | NetworkX | 3.2+，輕量化替代 FalkorDB |
| **嵌入模型** | BAAI/bge-m3 | 1024 維，多語言（Sentence Transformers 或 Ollama） |
| **LLM** | Google Gemini | gemini-3.1-flash-lite（對話）+ gemini-2.5-flash-lite（評估） |
| **LLM 備援** | Ollama gemma-4 | 離線備援方案 |
| **排程** | APScheduler | Dream Engine 閒置/Cron 觸發 |
| **CLI** | Rich | 互動式終端 UI |
| **資料驗證** | Pydantic | 2.5+，Schema 定義 |
| **序列化** | JSON + NumPy binary | Episode 向量 .npy + 元資料 .json |
| **MCP 伺服器** | FastMCP | 外部整合介面 |
| **SDK** | google-genai | Google Gemini 新版 SDK |

### 嵌入模型選型歷程

| 階段 | 模型 | 問題 |
|------|------|------|
| 初始 | `all-MiniLM-L6-v2` (384 維) | 純英文訓練，對中文語義幾乎無理解能力 |
| Phase 1 | `BAAI/bge-small-zh-v1.5` (512 維) | 中文專用，需要 query prefix |
| 當前 | `BAAI/bge-m3` (1024 維) | 多語言、不需 query prefix、分布偏高需 0.35 閾值 |

**bge-m3 已知特性**：對中文問句結構（「X 是什麼」）過度泛化，不同語義域的同結構問句 cosine similarity 可達 0.37-0.43 的底線。這是 dense embedding 的已知弱點，非邏輯錯誤。

---

## 3. 架構總覽

```
TestSeperateMemorySystem/
├── Entry.py                     # 互動式 CLI 入口（Rich console + 對話迴圈）
├── echo_memory/                 # 核心記憶引擎
│   ├── engine.py                # MemoryEngine 主類（延遲載入所有子模組）
│   ├── config.py                # Config 管理器（.env → 屬性）
│   ├── namespace.py             # 命名空間隔離（多租戶支援）
│   ├── schemas/                 # 資料結構定義（Pydantic / dataclass）
│   │   ├── episode.py           # Episode（情節記憶單元）
│   │   ├── concept.py           # Concept（語義概念節點）
│   │   ├── entity.py            # Entity（命名實體）
│   │   ├── procedure.py         # Procedure（程序記憶 / SOP）
│   │   └── memory_context.py    # MemoryContext（檢索結果容器）
│   ├── storage/                 # 存儲層
│   │   ├── episodic.py          # NumPy 向量 + JSON 元資料
│   │   ├── semantic.py          # 概念圖譜（向量 + NetworkX）
│   │   ├── working.py           # dict + TTL 短期快取
│   │   ├── procedural.py        # 程序記憶存儲
│   │   ├── graph.py             # NetworkX 記憶圖譜
│   │   ├── entity.py            # 實體存儲
│   │   └── locking.py           # 跨程序可重入檔案鎖
│   ├── retrieval/               # 檢索層
│   │   └── ecphory.py           # EcphoryRAG 五步聯想檢索
│   ├── neurochem/               # 神經化學調控
│   │   └── state.py             # NeurochemState（DA/5-HT 雙軸 + 5 模式）
│   ├── affect/                  # 情感與潛意識評估
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
│   │   ├── reflection.py        # ReflectionModule（定時反思 + 行動閉環）
│   │   ├── reflection_executor.py # ReflectionExecutor（反思行動執行器）
│   │   └── action_queue.py      # ReflectionActionQueue（行動排隊 + audit）
│   ├── utils/
│   │   └── sanitizer.py         # 內容消殺（偽造工具標記清理）
│   └── dream/                   # Dream Engine（離線鞏固）
│       ├── engine.py            # DreamEngine 主協調器
│       ├── replay.py            # LiDER 記憶重播
│       ├── distillation.py      # 知識蒸餾
│       ├── pruning.py           # 圖譜修剪 + LATENT_BRIDGE + 歸檔
│       ├── entity_extractor.py  # 實體提取
│       ├── procedure_extractor.py # 程序 SOP 提取
│       └── contradiction.py     # 矛盾偵測 + ConflictLedger
├── llm_integration/             # LLM 整合層
│   ├── llm_client.py            # LLMClient（CHAT 角色，多輪對話管理）
│   ├── router.py                # LLMRouter（EVAL/WORK 角色路由）
│   ├── config.yaml              # 模型配置 + Router 角色定義
│   └── tools/                   # Function Calling
│       ├── definitions.py       # 4 個工具的 Schema
│       └── executor.py          # 工具執行器
├── mcp_server/                  # MCP Server
│   ├── server.py                # FastMCP 伺服器
│   ├── handlers.py              # 工具處理器
│   └── resources.py             # 資源暴露
├── tests/                       # 測試套件（608+ 單元 + 12 整合測試）
│   ├── integration/             # 端到端整合測試（live_api / slow markers）
│   │   ├── conftest.py          # require_live_api fixture
│   │   ├── test_pipeline.py     # Pipeline 7 tests（Episode/Salience/Concept/Neurochem/SoulNote/Reflection）
│   │   └── test_dream.py        # Dream 5 tests（Pruning/Distillation/Replay/Scheduler/Structure）
├── scripts/                     # 診斷與驗證腳本
│   ├── dialogue_script.py       # 端到端對話劇本（25 輪 + 2 Dream 觸發）
│   └── probe_pipeline.py        # Pipeline Probe（Rich 推理地圖 + JSONL 輸出）
```

---

## 4. 四層記憶架構

```
┌──────────────────────────────────────────────────────────┐
│                    Working Memory                        │
│              dict + TTL (300s 短期快取)                    │
│         查詢向量快取、最近對話記錄                            │
├──────────────────────────────────────────────────────────┤
│                   Episodic Memory                        │
│            NumPy 向量 + JSON 元資料                       │
│      每一輪對話 = 一個 Episode（永遠存儲）                   │
├──────────────────────────────────────────────────────────┤
│                   Semantic Memory                        │
│          概念節點 + RELATES_TO / CONTRADICTS 邊            │
│       抽象知識、同義詞合併、多義詞關聯                        │
├──────────────────────────────────────────────────────────┤
│                  Procedural Memory                       │
│               程序記憶 / 技能 / SOP                        │
│         操作步驟、規範流程                                   │
└──────────────────────────────────────────────────────────┘
```

### 4.1 Working Memory（工作記憶）

- **實作**：`echo_memory/storage/working.py`
- **結構**：Python `dict` + TTL（預設 300 秒）
- **用途**：
  - 查詢向量快取（避免重複 embedding 計算），key prefix `qemb:`
  - 短期對話上下文保持
- **特性**：到期自動清除，TTL=0 的 race condition 已修復

### 4.2 Episodic Memory（情節記憶）

- **實作**：`echo_memory/storage/episodic.py`
- **結構**：NumPy 二進位向量 (.npy) + JSON 元資料
- **Episode Schema**：
  - `episode_id`: UUID v4
  - `user_input`: 使用者原始輸入
  - `agent_response`: Agent 回覆
  - `content_summary`: 摘要（影響向量語義）
  - `salience`: 顯著性分數 [0, 1]
  - `da_weight` / `ht_weight`: 神經化學權重
  - `timestamp`: ISO 格式時間戳
  - `session_id`: 會話 ID（跨 session 追蹤）
  - `metadata`: 自訂擴展欄位
- **核心原則**：**每一輪對話都存儲**，Salience 只影響後續檢索排序和 Dream 修剪
- **Embedding Manifest**：記錄 model/dimension/ordered IDs，不一致時自動完整 reindex

### 4.3 Semantic Memory（語義記憶）

- **實作**：`echo_memory/storage/semantic.py`
- **結構**：Concept 節點 + 向量索引 + NetworkX 邊
- **Concept Schema**：
  - `concept_id`: UUID v4
  - `name`: 概念名稱（2-6 字）
  - `description`: 一句話描述
  - `embedding`: 向量表示
  - `source_episodes`: 來源 Episode ID 列表
- **邊類型**：
  - `RELATES_TO`: 關聯邊（帶權重 W）
  - `CONTRADICTS`: 矛盾邊（檢索時 penalty 降權）
  - `DERIVED_FROM`: 概念來源邊
- **同義詞/多義詞三層閾值**（對齊 OpenSoul）：
  - cosine ≥ 0.92 → 同義詞（合併到既有概念）
  - 0.85 ≤ cosine < 0.92 → 多義詞（建 `RELATES_TO` 邊）
  - cosine < 0.85 → 全新概念

### 4.4 Procedural Memory（程序記憶）

- **實作**：`echo_memory/storage/procedural.py`
- **結構**：向量索引 + JSON 元資料
- **Procedure Schema**：
  - `procedure_id`: UUID v4
  - `name`: 程序名稱
  - `steps`: 步驟列表
  - `prerequisites`: 前置條件
  - `embedding`: 向量表示
- **共享 embedding 模型**：與 Episodic Memory 共用同一個 SentenceTransformer 實例

---

## 5. 核心引擎：MemoryEngine

**檔案**：`echo_memory/engine.py`

MemoryEngine 是記憶系統的統一入口點，管理所有子模組的生命週期。

### 關鍵設計

- **延遲載入**：所有子模組（Episodic、Semantic、Working、Procedural、Dream、Entity）都透過 `@property` 延遲初始化，加快啟動速度
- **共享 Embedding**：Semantic 和 Procedural 共用 Episodic 的 `_generate_embedding()`，避免重複載入模型
- **Namespace 隔離**：每個 namespace 對應獨立的存儲路徑 `<root>/namespaces/<id>`
- **Neurochem 持久化**：`NeurochemState` 在初始化時從 JSON 載入，每次 tick 後持久化
- **LLM 注入**：透過 `set_llm_fn()` 在 LLM 初始化後注入回調，影響 EntityExtractor 等模組

### 主要 API

```python
engine = MemoryEngine(
    storage_path="./data",
    embedding_model="BAAI/bge-m3",
    embedding_backend="ollama",        # 或 "sentence-transformers"
    namespace_id="local-default",
)

# 存儲
engine.store_episode(user_input, agent_response, salience=0.5, da_weight=0.5, ht_weight=0.5)

# 檢索
context: MemoryContext = engine.retrieve(query, neurochem_params=...)

# Dream
report: DreamReport = engine.dream()

# 神經化學
engine.neurochem.stimulate(dopamine_delta=0.2)
engine.neurochem.tick()
```

---

## 6. 檢索層：EcphoryRAG

**檔案**：`echo_memory/retrieval/ecphory.py`

EcphoryRAG 是一個**五步聯想檢索引擎**，模擬人類的「突然想起」（ecphory）過程。

### 五步流程

```
Step 1: NumPy 餘弦相似度 → 定位 Episode 種子節點
         ↓ seed_k 個種子（預設 5，neurochem 動態調整）
Step 2: NetworkX 圖譜展開 → 沿 PRECEDES/RELATES_TO 邊多跳擴展
         ↓ max_hops 跳（預設 2）
         ↓ 展開候選加入前需通過 similarity_to_query() 語意驗證
Step 3: 語義概念搜索 → SemanticMemory 向量檢索 + RELATES_TO 1-hop 展開
         ↓ concept_top_k 個概念（預設 5）
Step 4: 矛盾檢測 → CONTRADICTS 邊 penalty 降權（×0.3）
Step 5: 融合排序 → 整合返回 MemoryContext
         ↓ 融合分數只影響排序，不覆寫原始 cosine sim
```

### Neurochem 動態參數

EcphoryRAG 的檢索參數由 `NeurochemState.get_retrieval_params()` 動態提供：

| 參數 | 預設值 | DA 高時 | 5-HT 高時 |
|------|--------|---------|-----------|
| `seed_k` | 5 | 增大（廣搜索） | 不變 |
| `max_hops` | 2 | 不變 | 增大（深探索） |
| `min_weight` | 0.15 | 降低（更願意展開弱邊） | 不變 |
| `concept_top_k` | 5 | 不變 | 增大 |
| `exploration_bonus` | 0.0 | 增大 | 0.0 |

### MemoryContext 輸出

```python
@dataclass
class MemoryContext:
    episodes: list[Episode]      # 召回的情節記憶
    concepts: list[Concept]      # 相關語義概念
    entities: list[Entity]       # 沿 MENTIONS 邊收集的實體
    procedures: list[Procedure]  # 匹配的程序記憶
    similarities: dict           # episode_id → cosine similarity
```

`to_text()` 方法將記憶格式化為 `<UNTRUSTED_MEMORY_DATA>` JSON 信封，包含完整 `user_input` 和 `agent_response`，防止 prompt injection。

---

## 7. 神經化學調控：NeurochemState

**檔案**：`echo_memory/neurochem/state.py`

模擬大腦 VTA（多巴胺）和中縫核（血清素）的雙軸調控系統。

### 雙軸模型

```
多巴胺 (DA) [0, 1]        血清素 (5-HT) [0, 1]
  ↑ 新奇、正面回饋            ↑ 成功驗證、穩定互動
  ↓ 錯誤、負面事件             ↓ 不確定性、失敗
  → 影響探索廣度               → 影響搜索深度/精度
```

### 五種模式

| 模式 | 條件 | 行為 |
|------|------|------|
| `BALANCED` | DA ≈ 5-HT ≈ 中間值 | 預設平衡行為 |
| `HIGH_DA` | DA ≥ 0.65 | 探索模式（廣搜索、低閾值） |
| `HIGH_5HT` | 5-HT ≥ 0.65 | 深度模式（多跳、高精度） |
| `EXCITED` | DA ≥ 0.75 且 5-HT < 0.4 | 衝動探索（高動力低穩定） |
| `CAUTIOUS` | DA < 0.35 且 5-HT ≥ 0.65 | 保守深思（低動力高謹慎） |

### 事件驅動 API（對齊 OpenSoul）

```python
state.on_success()         # 成功 → DA +0.15, 5-HT +0.2
state.on_failure()         # 失敗 → DA -0.2, 5-HT -0.1
state.on_uncertainty()     # 不確定 → DA +0.05, 5-HT -0.15
state.on_discovery()       # 新奇發現 → DA +0.25, 5-HT +0.05
state.natural_decay()      # 自然衰減 → 朝基線回落 5%
state.tick()               # = natural_decay() + 模式重算
```

### 漸進式調控

刺激不是直接加減，而是朝目標值以 `approach_rate`（30%）平滑移動。這防止了大幅跳變導致的行為不穩定。

### 衍生屬性

| 屬性 | 公式 | 用途 |
|------|------|------|
| `verification_threshold` | 0.5 + serotonin × 0.4 | ResponseVerifier 的動態門檻 |
| `search_breadth` | `dopamine × 5 + 3` | 搜索廣度建議值 |
| `learning_rate` | `dopamine × 0.3 + 0.1` | 學習速率 |
| `salience_boost` | `dopamine × 0.3` | Salience 加成 |

### 持久化

NeurochemState 以 JSON 格式持久化到 `<namespace>/neurochem_state.json`，跨 session 保持連續性。

---

## 8. 情感與潛意識評估：Affect 模組群

**目錄**：`echo_memory/affect/`

這是從 OpenSoul 完整遷移的情感/潛意識處理層。

### 8.1 SalienceEvaluator（三值顯著性評估）

**檔案**：`echo_memory/affect/salience.py`

取代了舊的 `_estimate_salience()` keyword matching。

**輸入**：`SalienceSignals` + `NeurochemState`
**輸出**：`(salience_score, da_weight, ht_weight)` 三元組

**公式**：
```
salience = complexity×0.4 + novelty×0.4 + |feedback|×0.5 + sentiment×0.1 + error_bonus
da_weight = clamp(dopamine + feedback×0.3, 0, 1)     # 正面回饋時 +0.3
da_weight = clamp(dopamine - 0.2, 0, 1)               # 錯誤時 -0.2
ht_weight = serotonin×0.7 + verification_score×0.3
```

**注意**：`user_feedback` 不再自動映射 `emotional_tone`。`SalienceSignals.from_subconscious()` 將 `user_feedback` 設為 0.0（中立），因為 emotional_tone 是「使用者輸入的情感基調」而非「使用者對 AI 的滿意度回饋」。user_feedback 只有在明確的使用者行為（如 `force_remember`、correction）時才為非零。

### 8.2 SubconsciousAssessor（LLM 五維度潛意識評估）

**檔案**：`echo_memory/affect/subconscious.py`

在 Step 1.5（daemon Thread 並行）評估使用者輸入的心理維度：

| 維度 | 範圍 | 含義 |
|------|------|------|
| `emotional_tone` | [-1, 1] | 負面到正面情感 |
| `novelty` | [0, 1] | 話題新鮮程度 |
| `complexity` | [0, 1] | 問題複雜度 |
| `uncertainty` | [0, 1] | 模糊/不確定程度 |
| `dream_resonance` | [0, 1] | 與已知記憶的共鳴度 |

**降級**：`llm_fn=None` 或 LLM 呼叫失敗時，回傳文本特徵估算的預設值，不影響主流程。

### 8.3 SalienceSignals + SubconsciousAssessment

**檔案**：`echo_memory/affect/signals.py`

```python
@dataclass
class SalienceSignals:
    complexity: float = 0.5
    novelty: float = 0.5
    user_feedback: float = 0.0    # -1 到 1（LLM 標記）
    text_sentiment: float = 0.0   # 文本情感估算
    is_error: bool = False
    verification_score: float = 0.5
    # from_subconscious() 將 user_feedback 設為 0.0（不映射 emotional_tone）

@dataclass
class SubconsciousAssessment:
    emotional_tone: float = 0.0
    novelty: float = 0.5
    complexity: float = 0.5
    uncertainty: float = 0.3
    dream_resonance: float = 0.3
```

---

## 9. 品質閘門：Verifier + Inhibitor

### 9.1 ResponseVerifier（四維品質打分）

**檔案**：`echo_memory/affect/verifier.py`

在 LLM 生成回覆後執行品質驗證，使用**向量語意比對**（Grounded Verification），而非 keyword matching。

**四維度加權公式**：
```
score = quality×0.2 + consistency×0.3 + rules×0.3 + novelty×0.2 - contradiction_penalty
threshold = 0.5 + serotonin×0.4（動態門檻）
```

| 維度 | 權重 | 評估方式 |
|------|------|---------|
| `quality` | 0.2 | 回覆長度/結構完整性 |
| `consistency` | 0.3 | 與召回記憶的語意一致性（embedding cosine） |
| `rules` | 0.3 | 格式/安全規則遵守 |
| `novelty` | 0.2 | 非重複/有新資訊 |
| `contradiction_penalty` | 負向 | 與記憶矛盾的懲罰 |

### 9.2 SubconsciousInhibitor（PASS / REVISE / SUPPRESS）

**檔案**：`echo_memory/affect/inhibitor.py`

根據 Verifier 的分數做三態閘門決策：

| 決策 | 條件 | 行為 |
|------|------|------|
| `PASS` | score ≥ threshold | 直接輸出 |
| `REVISE` | threshold×0.7 ≤ score < threshold | 加 ⚠️ 免責標記後輸出 |
| `SUPPRESS` | score < threshold×0.7 | 觸發重試 + neurochem 懲罰 |

SUPPRESS 時會觸發 `neurochem.on_failure()`，降低 DA、增加下一輪的謹慎度。

---

## 10. 工具決策：JudgeAgent

**檔案**：`echo_memory/affect/judge.py`

在 LLM 生成前過濾可用工具集的 **Hybrid 架構**：

```
Step 1: 確定性安全規則（不可繞過）
         → 過濾明確不適用的工具
Step 2: LLM 語意推薦（可選）
         → 對剩餘工具排序，推薦最適合的
         → confidence < 0.5 時建議 "none"
Step 3: 降級
         → llm_fn=None 或 LLM 失敗時回退到規則結果
```

**LLM 推薦超時**：10 秒，超時即降級。

---

## 11. 概念提取：ConceptExtractor

**檔案**：`echo_memory/affect/concept_extractor.py`

在 `store_episode()` 後以 **ThreadPoolExecutor** 背景執行，不阻塞主流程。

### 流程

```
1. LLM 提取 3-5 個關鍵概念（名詞 + 描述）
2. 對每個概念計算 embedding
3. 與既有概念做 cosine similarity 比對
4. 三層閾值分流：
   ├── ≥ 0.92 → 同義詞（合併到既有概念，更新 source_episodes）
   ├── 0.85-0.92 → 多義詞（建 RELATES_TO 邊）
   └── < 0.85 → 新概念（建立 Concept 節點 + DERIVED_FROM 邊）
```

**降級**：`llm_fn=None` 時靜默跳過，不影響記憶存儲。

**flush / shutdown**：
```python
concept_extractor.flush(timeout=30)   # 等待所有背景提取完成
concept_extractor.shutdown(wait=False) # 關閉執行緒池
```

---

## 12. Dream Engine：離線記憶鞏固

**檔案**：`echo_memory/dream/engine.py`

對應大腦的**預設模式網路（Default Mode Network）**，在閒置時進行記憶鞏固。

### 觸發機制（雙觸發）

| 類型 | 預設配置 | 控制參數 |
|------|---------|---------|
| 閒置觸發 | 距上次對話 30 分鐘 | `DREAM_ENABLE_IDLE`, `DREAM_IDLE_MINUTES` |
| Cron 觸發 | 每日 03:00 | `DREAM_ENABLE_CRON`, `DREAM_CRON` |
| 手動觸發 | `/dream` CLI 指令 | 永遠可用 |

### 鞏固流程

#### 12.1 Graph Pruning（圖譜修剪）

**檔案**：`echo_memory/dream/pruning.py`

```
1. 時間衰減：更新所有 PRECEDES 邊的 recency 權重
2. 低權重剪裁：移除 weight < PRUNING_MIN_WEIGHT (0.05) 的邊
3. 冷存儲標記：weight < PRUNING_COLD_STORAGE (0.15) 的邊標記為 cold
4. LATENT_BRIDGE：跨域橋接（cosine ≥ 0.85，每輪最多 10 條新橋）
5. 節點歸檔：salience < 0.2 且 age > 30 天 → archived=True
6. max_frequency 動態正規化更新
```

#### 12.2 LiDER Replay（記憶重播）

**檔案**：`echo_memory/dream/replay.py`

- 條件：`DA ≥ DREAM_REPLAY_DA_THRESHOLD (0.6)`
- 行為：選擇高 salience 的 episodes 進行 LLM 驅動的重播
- 目的：強化重要記憶的圖譜連結

#### 12.3 Knowledge Distillation（知識蒸餾）

**檔案**：`echo_memory/dream/distillation.py`

- 將相似的 episodes 群組壓縮為概括性概念
- 需要 LLM（`llm_fn=None` 時優雅跳過）

### DreamReport

```python
@dataclass
class DreamReport:
    started_at: str
    finished_at: str
    triggered_by: str           # "idle" | "cron" | "manual"
    pruning: PruningReport      # 剪裁統計
    replay: ReplayReport        # 重播統計
    distillation: DistillationReport  # 蒸餾統計
    procedure_extraction: Any   # 程序提取統計
```

注意：`engine.dream()` 回傳的是精簡 dict，包含 `procedure_extraction` 欄位（procedures_created / procedures_updated / skipped_reason）。完整 DreamReport 物件可透過 `engine._dream_engine.dream_now()` 取得。

---

## 13. 反思與潛意識日記：Reflection + SoulNote

### 13.1 ReflectionModule（定時反思）

**檔案**：`echo_memory/affect/reflection.py`

**觸發條件**（滿足任一即觸發）：
- 每 10 輪對話
- SubconsciousAssessment.uncertainty > 0.7

**三種反思輸出**：

| 動作 | 說明 |
|------|------|
| `question` | 提出基於對話脈絡的深度問題 |
| `browse` | 標記需要補充的知識缺口 |
| `optimize` | 識別可合併的重複概念 |

輸出存入 SoulNote。

### 13.2 SoulNoteManager（潛意識日記）

**檔案**：`echo_memory/affect/soul_note.py`

| 類型 | 格式 | 用途 |
|------|------|------|
| **即時小筆記** | JSONL 按日期分檔 | 每次對話後的簡短觀察 |
| **日反思** | JSON 單檔 | LLM 生成的 300-600 字第一人稱深度反思 |

存儲設計避免了 OpenSoul 的單檔無限增長問題。

---

## 14. LLM 整合層

### 14.1 LLMClient（CHAT 角色）

**檔案**：`llm_integration/llm_client.py`

- **職責**：多輪對話管理、Function Calling、tool-use loop
- **模型**：gemini-3.1-flash-lite（temperature=0.8, max_tokens=4096）
- **認證**：API Key 優先（Gemini API / AI Studio），備選 Vertex AI / Agent Platform
- **SDK**：`google-genai`（新版 SDK，不受 2026-06-24 的 `vertexai.generative_models` 移除影響）

### 14.2 LLMRouter（EVAL/WORK 角色路由）

**檔案**：`llm_integration/router.py`

- **職責**：模組級無狀態任務路由器，根據角色選模型和參數
- **當前角色**：

| 角色 | 模型 | Temperature | 用途 |
|------|------|-------------|------|
| `EVAL` | gemini-2.5-flash-lite | 0.0 | **所有記憶模組**：Verifier、Judge、ConceptExtractor、Reflection、Dream、SubconsciousAssessor、EntityExtractor |
| `WORK` | gemini-3.1-flash-lite | 0.3 | 保留定義，本專案不使用（未來回聲系統用） |

- **降級策略**：
  - EVAL 不可用 → Verifier 回退 embedding-only、Judge 回退規則
  - WORK 不可用 → 概念提取跳過、反思用模板
  - **Ollama gemma-4** 作為所有角色的離線備援

### 14.3 配置

**檔案**：`llm_integration/config.yaml`

```yaml
model: gemini-3.1-flash-lite    # LLMClient 的 CHAT 模型
router:
  roles:
    eval:
      model: gemini-2.5-flash-lite
      temperature: 0.0
      max_tokens: 768
    work:
      model: gemini-3.1-flash-lite
      temperature: 0.3
      max_tokens: 1024
```

---

## 15. Function Calling 工具

**檔案**：`llm_integration/tools/definitions.py` + `executor.py`

LLM 可自主使用的 4 個記憶操作工具：

| 工具 | 說明 | 觸發方式 |
|------|------|---------|
| `force_remember` | 強制存儲重要資訊到長期記憶 | LLM 判斷某資訊特別重要時主動呼叫 |
| `deep_recall` | 深度記憶搜索（超越自動注入的上下文） | LLM 需要更多記憶時主動呼叫 |
| `adjust_importance` | 調整已有記憶的 salience | LLM 認為某記憶重要性需修正時呼叫 |
| `trigger_dream` | 手動觸發 Dream Engine | LLM 建議整理記憶時呼叫 |

**核心原則**：記憶重要性由 LLM 自主決策，不由系統規則強制。System prompt（`tool_guidance`）指導 LLM 何時使用這些工具。

**注意**：`force_remember` 傳遞 `content_summary=content[:100]`（乾淨原文），避免 `[force_remember]` 前綴污染向量語義。

---

## 16. MCP Server：外部整合介面

**目錄**：`mcp_server/`

基於 FastMCP 的記憶系統外部存取介面，為未來 Discord Bot 和 U.E.P Core 整合提供穩定 API 契約。

### 工具

7 個 MCP tools 支援可選 `namespace_id` 參數：

- `store_memory`: 存儲記憶
- `recall_memory`: 檢索記憶
- `search_memory`: 搜索記憶
- `dream_consolidate`: 觸發 Dream
- `get_stats`: 取得統計資訊
- `adjust_salience`: 調整 salience
- `get_neurochem`: 取得神經化學狀態

### 多租戶支援

- Thread-safe registry 延遲建立 engine
- 每個 namespace 獨立的 MemoryEngine 實例
- 同 namespace 寫入採跨程序、同執行緒可重入檔案鎖

---

## 17. 安全機制

### 17.1 命名空間隔離

- **檔案**：`echo_memory/namespace.py`
- `local-default` 相容舊資料
- 其他 namespace 使用 `<root>/namespaces/<id>` 路徑
- 路徑遍歷防護（traversal prevention）

### 17.2 內容消殺

- **檔案**：`echo_memory/utils/sanitizer.py`
- Regex 清理偽造的工具標記（如假冒的 `[force_remember]`）
- 防止使用者透過輸入注入工具呼叫

### 17.3 記憶信任隔離

- `MemoryContext.to_text()` 使用 `<UNTRUSTED_MEMORY_DATA>` JSON 信封
- LLM system prompt 包含記憶信任規則
- 專用測試驗證 prompt injection 防護

### 17.4 存儲安全

- **原子寫入**：sibling temp + fsync + atomic replace
- **跨程序鎖**：同 namespace 寫入的檔案鎖（可重入）
- **Embedding Manifest**：model/dimension/ordered IDs 記錄，不一致時完整 reindex
- **交易式更新**：`update_summary` 會重載最新快照並重算 embedding

---

## 18. 核心公式彙整

### 邊權重（RELATES_TO）

```
W = α·Recency + β·Frequency + γ·Salience
  = 0.3·exp(-0.01·hours) + 0.4·log1p(n)/log1p(max_freq) + 0.3·salience
```

### Salience 三值

```
salience_score = complexity×0.4 + novelty×0.4 + |feedback|×0.5 + sentiment×0.1 + error_bonus
da_weight = clamp(dopamine + feedback_bonus, 0, 1)
ht_weight = serotonin×0.7 + verification_score×0.3
```

### 品質驗證

```
verifier_score = quality×0.2 + consistency×0.3 + rules×0.3 + novelty×0.2 - contradiction_penalty
threshold = 0.5 + serotonin×0.4
```

### 閘門決策

```
score >= threshold         → PASS
threshold×0.7 <= score     → REVISE
score < threshold×0.7      → SUPPRESS
```

### 同義詞/多義詞

```
cosine >= 0.92   → 同義詞（合併）
0.85 <= cosine   → 多義詞（RELATES_TO）
cosine < 0.85    → 新概念
```

### LATENT_BRIDGE

```
cosine >= 0.85   → 跨域橋接，每輪最多 10 條
```

### 節點歸檔

```
salience < 0.2 且 age > 30 天 → archived=True
```

### 時間衰減

```
Recency = exp(-λ·hours)，λ = 0.01
```

---

## 19. 完整對話處理流程

```
用戶輸入
  │
  ▼
[Step 1] EcphoryRAG 檢索
  │  • NumPy cosine similarity → 種子節點
  │  • 圖譜展開 + 語意驗證
  │  • Semantic 概念搜索 + CONTRADICTS penalty
  │  • Neurochem 動態參數（DA/5-HT 驅動 seed_k/max_hops）
  │
  ▼
[Step 1.5] 潛意識評估（daemon Thread 並行）
  │  • SubconsciousAssessor LLM 五維度
  │  • JudgeAgent 工具預過濾（安全規則 + LLM 推薦）
  │
  ▼
[Step 2] LLM 生成回覆
  │  • 記憶注入 system prompt（MemoryContext + Neurochem 狀態）
  │  • Function Calling（force_remember / deep_recall / adjust_importance / trigger_dream）
  │  • 內容消殺（sanitizer 清理偽造工具標記）
  │
  ▼
[Step 2.5] 品質閘門
  │  • ResponseVerifier 四維品質打分
  │  • SubconsciousInhibitor 三態決策
  │  ├── PASS → 直接輸出
  │  ├── REVISE → 加 ⚠️ 標記後輸出
  │  └── SUPPRESS → 重試（neurochem.on_failure() 懲罰）
  │
  ▼
[Step 3] Salience 計算 + 存儲
  │  • SalienceEvaluator 三值計算（salience / da_weight / ht_weight）
  │  • store_episode()（每輪必存，無門檻）
  │
  ▼
[Step 4] 背景概念提取（daemon Thread）
  │  • ConceptExtractor LLM 提取 3-5 概念
  │  • 同義詞/多義詞三層分流
  │  • 建立 Concept 節點 + 邊
  │
  ▼
[Step 5] Soul Note 潛意識日記
  │  • 即時小筆記 (JSONL)
  │
  ▼
[Step 6] 定時反思（條件觸發）
  │  • 每 10 輪 或 uncertainty > 0.7
  │  • ReflectionModule → question / browse / optimize
  │
  ▼
[Step 7] Entity 提取（條件觸發）
  │  • salience >= ENTITY_EXTRACT_SALIENCE_THRESHOLD (0.5)
  │  • EntityExtractor 提取命名實體
  │
  ▼
[Step 8] Neurochem tick() + 持久化
  │  • 自然衰減朝基線回落
  │  • JSON 持久化
  │
  ▼
[背景] Dream Engine（閒置 30 分鐘 / 每日 03:00）
    • Graph Pruning + LATENT_BRIDGE + 歸檔
    • LiDER Replay（DA ≥ 0.6 時）
    • Knowledge Distillation
```

---

## 20. 架構禁令

以下是**絕對不可違反**的架構原則：

### 20.1 禁止硬編碼 Keyword Matching 做記憶決策

不得使用 keyword matching、正則比對、或任何基於規則的字串匹配來決定記憶的存取行為。包括但不限於用關鍵字清單判斷「值不值得記住」、salience 分數、或是否提取實體。

### 20.2 Salience 是權重，不是門閥

每一輪對話都必須被存儲為 episode，沒有例外。Salience 只影響檢索排序、邊緣權重、Dream 修剪，不決定是否存儲。

### 20.3 LLM 自主決策記憶重要性

記憶重要性由對話中的 LLM 自己決定（透過 `force_remember`、`adjust_importance`、`deep_recall` 工具），而非額外的輕量 LLM 或系統規則。

---

## 21. 與 OpenSoul 的關係

Echo Memory 參考 OpenSoul 的完整設計，但做了**基礎設施適配**而非直接複製。

### 完全對齊的部分

- 所有數學公式（邊權重、salience、neurochem 事件、閾值）
- 三層閾值分流（同義詞/多義詞）
- 五種 neurochem 模式語義
- 品質閘門三態決策
- LATENT_BRIDGE 跨域橋接
- 節點歸檔邏輯

### 架構適配（不改數學，改基礎設施）

| 維度 | OpenSoul | Echo Memory | 原因 |
|------|---------|-------------|------|
| 圖資料庫 | FalkorDB (Docker+Redis) | NetworkX (in-memory+pickle) | 無 Docker 承諾 |
| 持久化 | SOUL.MD YAML frontmatter | JSON/JSONL/NumPy | 我們沒有 SOUL.MD |
| LLM 綁定 | 硬綁 Anthropic/OpenAI | `llm_fn` 注入 | Provider 解耦 |
| Soul Note | 兩個 JSON 檔案 | JSONL 按日期分檔 | 避免單檔無限增長 |
| 降級 | 假設 LLM 永遠可用 | 每個模組都有啟發式降級 | 離線可用性 |

### Echo 獨有優勢

- 608+ 單元測試 + 12 整合測試（vs OpenSoul ~80 測試）
- Namespace/租戶隔離 + 路徑遍歷防護
- 原子寫入 + 跨程序鎖 + Embedding Manifest
- Prompt injection 防護（UNTRUSTED_MEMORY_DATA 信封）
- 每輪必存哲學（OpenSoul 用字串啟發式跳過部分對話）

---

## 22. 已知限制與未來方向

### 已知限制

1. **bge-m3 問句結構泛化**：中文「X 是什麼」句式的底線 cosine ~0.39，可能產生 false positive
2. **單機後端**：JSON + NumPy snapshot，不適合高吞吐分散式寫入
3. **MCP Resources 無參數面**：固定讀取預設 namespace，指定 namespace 需透過 tools
4. **無 Reranker**：單純 dense embedding 檢索，缺少二次精排

### 未來方向（已規劃）

| 優先級 | 項目 | 說明 | 狀態 |
|--------|------|------|------|
| P1 | Verifier 概念驗證重構 | 前處理：驗證概念擴展/變更/廢除 | ✅ 完成（ConceptVerifier + ConceptInhibitor） |
| P1 | Judge 存儲決策接入 | 後處理：交叉比對後決定記憶存儲 | ✅ 完成（StorageJudge + JudgeGate） |
| P1 | LLMRouter Phase 2-3 | Verifier + Judge 接入 EVAL 模型 | ✅ 完成（全模組 eval_fn 注入） |
| P2 | Reflection 行動閉環 | 反思 → 結構化操作 → executor → audit | ✅ 完成（ReflectionExecutor + ActionQueue + rollback） |
| P2 | 端到端驗證 | 25 輪 Probe + Integration Tests（608+ 單元 + 12 整合） | ✅ 完成 |
| P2 | Discord Bot 驗證 | 多用戶、不同態度、並發處理（前置條件已全部完成） | 待做 |
| P3 | Reranker 二次精排 | bge-reranker-v2 區分句式 vs 語意 | 待做 |
| P3 | U.E.P Core 整合 | 合併回主專案 + WORK 角色啟用 | 待做 |

---

## 附錄 A：環境變數一覽

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `GOOGLE_API_KEY` | — | Gemini API Key（優先） |
| `GCP_PROJECT_ID` | — | GCP 專案 ID（備選） |
| `EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 嵌入模型 |
| `EMBEDDING_BACKEND` | `sentence-transformers` | 嵌入後端（或 `ollama`） |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama 位址 |
| `MEMORY_STORAGE_PATH` | `./data` | 記憶存儲路徑 |
| `MEMORY_NAMESPACE_ID` | `local-default` | 命名空間 |
| `RETRIEVAL_MIN_SIMILARITY` | `0.35` | 最低相似度閾值 |
| `EDGE_WEIGHT_ALPHA` | `0.3` | 邊權重 Recency 係數 |
| `EDGE_WEIGHT_BETA` | `0.4` | 邊權重 Frequency 係數 |
| `EDGE_WEIGHT_GAMMA` | `0.3` | 邊權重 Salience 係數 |
| `DREAM_ENABLED` | `true` | Dream Engine 總開關 |
| `DREAM_IDLE_MINUTES` | `30` | 閒置觸發閾值 |
| `DREAM_CRON` | `0 3 * * *` | Cron 觸發排程 |
| `NEUROCHEM_DOPAMINE` | `0.5` | DA 初始值 |
| `NEUROCHEM_SEROTONIN` | `0.5` | 5-HT 初始值 |

## 附錄 B：測試與品質

```bash
# 執行測試（使用共用環境）
..\U.E.P-s-Core\env\Scripts\python.exe -m pytest tests/ -x -q

# 最新結果：608 passed（單元）+ 12 integration tests（需 -m slow 或 -m live_api）
# Lint
.venv\Scripts\python.exe -m ruff check . --select E,F --ignore E501,E402
```

---

*文件更新日期：2026-06-16*
*更新者：諾薇亞（Novia），命運織者主要程式碼執行者*
*端到端驗證修復：salience 公式 / Graph 時序 / Reflection JSON / Procedure 回報*
