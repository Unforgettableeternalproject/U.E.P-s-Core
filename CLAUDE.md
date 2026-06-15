# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## 重要工作流

每次會話優先載入 pm 技能，在進行任何操作前，先使用 `/pm query` 或直接使用 `mcp__open-notebook__search` 工具來查詢專案記憶，確保對當前專案狀態有足夠的了解。

在觀察到關鍵知識點（如架構決策、踩坑經驗、隱性依賴）時，使用 `/pm note <標題>` 或 `mcp__open-notebook__create_note` 工具將發現寫入專案記憶，注意盡量不要寫入重複內容。

**請不要隨便啟動程式或開發伺服器，除非明確被指示。**

## 語言規範

一律使用繁體中文回覆。程式碼註解也使用繁體中文。絕不使用簡體中文。
使用者面向的字串使用繁體中文（zh-TW），內部程式碼/變數名稱使用英文。

## 專案概覽

Echo Memory System 是受 **OpenSoul** 啟發並改良的自主記憶子系統，從 U.E.P Core 遷出的獨立實驗專案。目標是將記憶系統從被動 RAG 工具轉變為主動、動態的認知子系統。

- **來源**：U.E.P's Core `mem_module`（獨立遷出）
- **參考架構**：OpenSoul（`C:\Users\Bernie\source\repos\Other\OpenSoul`）
- **Python**: 3.10+
- **核心依賴**：NumPy + NetworkX + Sentence Transformers

## ⚠️ 架構禁令（必讀）

### 1. 禁止硬編碼 Keyword Matching 做記憶決策

**不得使用 keyword matching、正則比對、或任何基於規則的字串匹配來決定記憶的存取行為。**

這包括但不限於：
- 用關鍵字清單判斷「這段對話是否值得記住」
- 用關鍵字清單判斷 salience（顯著性）分數
- 用關鍵字清單判斷是否應該提取實體

硬編碼 keyword matching 是對記憶形成過程的廉價模仿。人類不會因為聽到「記住」這個詞才決定記住一件事——記憶的形成取決於情境、情感、新穎性、個人相關性等多維度因素，這些判斷只有 LLM 能做。

**如果你發現 codebase 中存在這類邏輯，應該標記並提議移除。**

### 2. Salience 是權重，不是門閥

Salience（顯著性）影響的是記憶在後續檢索、圖譜遍歷、Dream Engine 修剪時的**重要程度**，而不是決定「要不要存儲」。

- **每一輪對話都應該被存儲為 episode**——沒有例外
- Salience 分數作為 episode 的屬性寫入，影響：
  - 檢索排序（高 salience → 更容易被召回）
  - 邊緣權重計算（`W = α·Recency + β·Frequency + γ·Salience`）
  - Dream Engine 修剪決策（低 salience → 更容易被遺忘）
- 低 salience 的記憶不是被丟棄，而是隨時間自然衰減——這才是正確的遺忘機制

**不得引入任何 salience 門檻來過濾存儲。**

### 3. LLM 自主決策記憶重要性

記憶的重要性判斷應該交給**對話中的 LLM 自己**，而不是額外的輕量 LLM 或系統規則：

- 對話 LLM 透過 `force_remember` 工具主動標記高重要性記憶
- 對話 LLM 透過 `adjust_importance` 工具調整已有記憶的 salience
- 對話 LLM 透過 `deep_recall` 工具主動搜尋更多記憶
- System prompt（`tool_guidance`）應明確指導 LLM 何時使用這些工具

系統層面的啟發式評估（如 `_estimate_salience`）只是在 LLM 不主動介入時提供一個基礎權重，**不應作為主要判斷依據**。

### 4. 架構參考來源

本專案的記憶架構參考自 **OpenSoul**（`C:\Users\Bernie\source\repos\Other\OpenSoul`），並在此基礎上改良。

關鍵對應關係：
| OpenSoul | Echo Memory | 說明 |
|----------|-------------|------|
| `soul/memory/episodic.py` | `echo_memory/storage/episodic.py` | 情節記憶存儲 |
| `soul/memory/semantic.py` | `echo_memory/storage/semantic.py` | 語義記憶 |
| `soul/memory/retrieval.py` | `echo_memory/retrieval/ecphory.py` | EcphoryRAG 檢索 |
| `soul/affect/salience.py` | （待改良） | 顯著性評估 |
| `soul/affect/subconscious.py` | （待改良） | 潛意識評估 |
| `soul/affect/neurochem.py` | `echo_memory/neurochem/state.py` | 神經化學狀態 |
| `soul/dream/` | `echo_memory/dream/` | Dream Engine |
| FalkorDB | NumPy + NetworkX | 圖譜存儲（輕量化替代） |

**做任何架構決策前，先去看 OpenSoul 是怎麼做的，理解原始設計意圖再決定是否改良。**

## 架構總覽

```
TestSeperateMemorySystem/
├── Entry.py                     # 互動式 CLI（對話、記憶管理、除錯）
├── echo_memory/                 # 核心記憶引擎
│   ├── engine.py                # MemoryEngine 主類
│   ├── config.py                # 配置管理
│   ├── namespace.py             # 命名空間隔離
│   ├── schemas/                 # 資料結構定義
│   │   ├── episode.py           # Episode（情節記憶）
│   │   ├── concept.py           # Concept（語義概念）
│   │   ├── entity.py            # Entity（實體）
│   │   ├── procedure.py         # Procedure（程序記憶）
│   │   └── memory_context.py    # MemoryContext（檢索結果容器）
│   ├── storage/                 # 存儲層
│   │   ├── episodic.py          # 情節記憶（NumPy 向量 + JSON）
│   │   ├── semantic.py          # 語義記憶
│   │   ├── working.py           # 工作記憶（TTL 短期快取）
│   │   ├── procedural.py        # 程序記憶
│   │   ├── graph.py             # NetworkX 記憶圖譜
│   │   ├── entity.py            # 實體存儲
│   │   └── locking.py           # 檔案鎖
│   ├── retrieval/               # 檢索層
│   │   └── ecphory.py           # EcphoryRAG 聯想檢索
│   ├── neurochem/               # 神經化學調控
│   │   └── state.py             # NeurochemState（DA/5-HT）
│   └── dream/                   # Dream Engine（離線鞏固）
│       ├── engine.py            # DreamEngine 主類
│       ├── replay.py            # LiDER 記憶重播
│       ├── distillation.py      # 知識蒸餾
│       ├── pruning.py           # 圖譜修剪
│       └── entity_extractor.py  # 實體提取
├── llm_integration/             # LLM 整合層
│   ├── llm_client.py            # Gemini LLM 客戶端
│   ├── config.yaml              # LLM 配置（角色、prompt）
│   └── tools/                   # Function Calling 工具
│       ├── definitions.py       # 工具定義 Schema
│       └── executor.py          # 工具執行器
├── mcp_server/                  # MCP Server（外部整合）
│   ├── server.py                # FastMCP 伺服器
│   ├── handlers.py              # 工具處理器
│   └── resources.py             # 資源暴露
├── tests/                       # 測試套件
└── examples/                    # 使用範例
```

## 三層記憶架構

```
Working Memory（工作記憶）    — dict + TTL，短期快取（5 分鐘）
Episodic Memory（情節記憶）   — NumPy 向量 + JSON，對話事件
Semantic Memory（語義記憶）   — 抽象知識與概念
Procedural Memory（程序記憶） — 技能與 SOP
```

## 記憶流程

```
用戶輸入
  → EcphoryRAG 檢索相關記憶（向量 + 圖譜多跳）
  → LLM 生成回覆（含 tool-use loop：force_remember / deep_recall）
  → 存儲 Episode（每輪都存，salience 作為權重）
  → Entity 提取（salience >= 門檻時）
  → 神經化學狀態更新
```

## 常用指令

```bash
# 啟動虛擬環境
source .venv/Scripts/activate  # bash on Windows

# 執行互動式 CLI
python Entry.py

# 執行測試
python -m pytest tests/ -x -q          # 快速測試
python -m pytest tests/ -v             # 詳細測試
python -m pytest tests/ --cov          # 含覆蓋率

# Lint
python -m flake8 echo_memory/ llm_integration/ --max-line-length=120
```

## 分支策略

```
main (production) ← PR from release/* or hotfix/*
develop (日常開發)
feature/* (功能開發分支)
fix/* (修復分支)
```

Commit 使用 Conventional Commits：`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, `test:` 等。

## 程式碼風格

- Python 3.10，使用 type hints 和 docstrings（Google style）
- 不要硬編碼密鑰、絕對路徑
- 使用 `logging` 模組，不要用 `print()`（Entry.py 的 Rich console 輸出除外）
- 變更盡量最小化——只修改需要的部分
- 不要隨意重命名類別/函數/變數

## 環境變數

- API 金鑰設定在 `.env` 中（已被 gitignore）
- 記憶系統配置透過 `echo_memory/config.py` 管理
- LLM 配置透過 `llm_integration/config.yaml` 管理

## 專案記憶協定

本專案使用 Project Memory (PM) 協定管理跨 session 知識。使用 `/pm` 指令操作。

**工具分工：**
- 檔案查詢 → FFF (`mcp__fff__grep` / `mcp__fff__find_files`)
- 專案記憶（讀取 + 寫入）→ **Open Notebook**（Notebook: `[PM] TestSeperateMemorySystem`）
- 跨 Agent 協調（僅限 cowork push）→ MEMPAL (`mcp__mempal__mempal_cowork_push`)

**⚠️ MEMPAL 使用限制：**
- **禁止** 使用 `mempal_search`、`mempal_ingest`、`mempal_status` 等工具來讀取或儲存本專案記憶
- MEMPAL 在本專案中 **唯一** 的用途是 `mempal_cowork_push`（通知另一個 agent）
- 所有記憶查詢一律使用 `mcp__open-notebook__search`
- 所有記憶寫入一律使用 `mcp__open-notebook__create_note` / `mcp__open-notebook__update_note`

## 注意事項

- 不要主動啟動程式或 dev server
- Windows 環境下注意 EPERM 和編碼問題
- 寫入檔案時注意中文字元不要被轉成 Unicode escape sequences
- 修改核心架構前，先查閱 OpenSoul 原始設計
- `_estimate_salience` 是臨時的啟發式方案，未來應被 LLM-based 評估取代
