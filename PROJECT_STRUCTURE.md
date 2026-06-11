# Echo Memory System - 專案結構總覽

最後更新：2026-06-11

---

## 完整目錄樹（實際存在）

```
TestSeperateMemorySystem/
│
├── README.md                        # 專案主文檔（設計理念、架構說明）
├── QUICKSTART.md                    # 5 分鐘快速開始指南
├── DEVLOG.md                        # 開發日誌（進度、決策、學習）
├── PROJECT_STRUCTURE.md             # 本文件
├── requirements.txt                 # Python 依賴清單（由 pyproject.toml 同步）
├── pyproject.toml                   # 專案配置（setuptools）
├── .env.example                     # 環境變數模板
├── .gitignore                       # Git 忽略規則
├── Entry.py                         # 互動式 CLI 入口
├── verify_config.py                 # 配置驗證腳本
│
├── echo_memory/                     # 核心記憶引擎
│   ├── __init__.py                  # 模組導出
│   ├── config.py                    # 配置管理（EchoMemoryConfig）
│   ├── engine.py                    # MemoryEngine 主類
│   │
│   ├── schemas/                     # 資料結構定義
│   │   ├── __init__.py
│   │   ├── episode.py               # Episode 節點
│   │   ├── concept.py               # Concept 節點
│   │   └── memory_context.py        # 檢索結果容器
│   │
│   ├── storage/                     # 存儲層
│   │   ├── __init__.py
│   │   ├── episodic.py              # 情節記憶（NumPy + JSON）
│   │   ├── semantic.py              # 語義記憶（dict 骨架）
│   │   ├── working.py               # 工作記憶（TTL 緩存）
│   │   └── graph.py                 # 圖譜管理（NetworkX）
│   │
│   ├── retrieval/                   # 檢索引擎
│   │   ├── __init__.py
│   │   └── ecphory.py               # EcphoryRAG 聯想檢索
│   │
│   ├── consolidation/               # 離線鞏固
│   │   ├── __init__.py
│   │   ├── dream_engine.py          # Dream Engine 協調器
│   │   └── pruning.py               # 圖譜修剪
│   │
│   └── dream/                       # Dream 子模組
│       ├── __init__.py
│       ├── engine.py                # Dream Engine 實作
│       └── pruning.py               # 修剪策略
│
├── llm_integration/                 # LLM 整合層
│   ├── __init__.py
│   └── llm_client.py               # LLM 調用封裝（Gemini/Claude）
│
├── neurochem/                       # [Planned] 神經化學模擬
│   └── （目錄尚未建立，Phase 2+ 規劃中）
│
├── tests/                           # 單元測試
│   ├── __init__.py
│   ├── test_config.py               # 配置管理測試
│   ├── test_engine.py               # MemoryEngine 整合測試
│   ├── test_episodic.py             # Episodic Memory 測試
│   ├── test_dream_engine.py         # Dream Engine 測試
│   ├── test_dream_quick.py          # Dream 快速測試
│   └── test_hybrid_retrieval.py     # 混合檢索測試
│
├── examples/                        # 示例
│   └── minimal_chat.py              # 最小對話示例（echo-chat 入口點）
│
└── test_data/                       # 測試用資料（已提交 Git）
    └── episodic/
        ├── episodes.json
        ├── embeddings.npy
        └── memory_graph.gpickle
```

---

## 尚未實作的模組（規劃中）

| 模組 | 目錄 | 計畫階段 |
|------|------|---------|
| **neurochem** | `neurochem/` | Phase 2+ |
| **動態邊緣權重** | `echo_memory/retrieval/weighting.py` | Phase 2 |
| **LiDER 重播** | `echo_memory/consolidation/replay.py` | Phase 4 |
| **知識蒸餾** | `echo_memory/consolidation/distillation.py` | Phase 4 |
| **FAISS 向量索引** | `echo_memory/storage/vector.py` | Planned |
| **Dream Demo CLI** | `examples/dream_demo.py` | Planned |

> **重要**：`echo-dream` console script 已從 `pyproject.toml` 移除，
> 待 `examples/dream_demo.py` 實際實作後才會重新加入。

---

## Phase 1 完成狀態

### 已實作模組

| 模組 | 文件 | 功能 | 狀態 |
|------|------|------|------|
| **MemoryEngine** | `echo_memory/engine.py` | 統一記憶管理 API | 完成 |
| **EchoMemoryConfig** | `echo_memory/config.py` | 配置管理 | 完成 |
| **Episode Schema** | `echo_memory/schemas/episode.py` | 情節記憶節點定義 | 完成 |
| **Concept Schema** | `echo_memory/schemas/concept.py` | 語義概念節點定義 | 完成 |
| **MemoryContext** | `echo_memory/schemas/memory_context.py` | 檢索結果容器 | 完成 |
| **EpisodicMemory** | `echo_memory/storage/episodic.py` | NumPy + JSON 存儲 | 完成 |
| **SemanticMemory** | `echo_memory/storage/semantic.py` | 簡化版（dict） | 骨架 |
| **WorkingMemory** | `echo_memory/storage/working.py` | TTL 短期緩存 | 完成 |
| **EcphoryRetrieval** | `echo_memory/retrieval/ecphory.py` | NumPy 向量檢索 | Phase 1 |
| **DreamEngine** | `echo_memory/consolidation/dream_engine.py` | 離線鞏固 | 骨架 |

---

## 關鍵文件說明

### `echo_memory/engine.py`
**職責**：記憶系統的統一入口
**主要 API**：
- `store_episode()` - 存儲對話
- `retrieve()` - 檢索記憶
- `dream()` - 離線鞏固
- `get_stats()` - 統計資訊

### `echo_memory/storage/episodic.py`
**職責**：情節記憶的 CRUD 與向量檢索
**技術棧**：
- NumPy: 精確餘弦相似度檢索（非 FAISS）
- Sentence Transformers: 嵌入生成

### `examples/minimal_chat.py`
**入口點**：`echo-chat` console script
**展示功能**：完整的存儲-檢索循環

---

## 文檔導航

| 文檔 | 用途 | 目標讀者 |
|------|------|---------|
| **README.md** | 完整設計說明 | 所有人 |
| **QUICKSTART.md** | 快速上手指南 | 新用戶 |
| **DEVLOG.md** | 開發進度與決策 | 開發者 |
| **本文件** | 專案結構總覽 | 開發者 |

---

**Phase 1 完成日期**：2026-04-06
**結構最後核對**：2026-06-11
**下一階段**：Phase 2 - EcphoryRAG 多跳檢索 + neurochem 神經化學模擬
