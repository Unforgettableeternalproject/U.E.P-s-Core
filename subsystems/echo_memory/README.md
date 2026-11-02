# Echo Memory System

> An autonomous memory subsystem inspired by OpenSoul — extracted from U.E.P Core `mem_module`, transforming passive RAG into an active, dynamic cognitive subsystem.

## Project Overview

Echo Memory System (`echo_memory`) is a standalone cognitive memory subsystem spun out of the U.E.P-s-Core project. Rather than treating memory as a passive retrieval index, it models memory as a living cognitive process: storing, consolidating, and actively surfacing the right context at the right time.

The system implements a four-layer memory architecture (Working / Episodic / Semantic / Procedural) unified under a single `MemoryEngine` interface. Retrieval is handled by EcphoryRAG — a five-step associative pipeline that expands beyond surface similarity into concept-graph traversal, contradiction detection, and neurochemically-weighted fusion ranking.

All storage and retrieval decisions flow through the OpenSoul affective pipeline, replacing hard-coded keyword rules with LLM-driven salience evaluation and hybrid StorageJudge logic. The system is designed to integrate back into U.E.P-s-Core as `mem_module_v2` once multi-user Discord validation is complete.

## Core Features

✯ **Four-Layer Memory Architecture**
- 🔹 Working Memory — TTL-based short-term cache for active context
- 🔹 Episodic Memory — NumPy vector + JSON episode storage with cosine retrieval
- 🔹 Semantic Memory — Concept graph with NetworkX for relational knowledge
- 🔹 Procedural Memory — SOP extraction from recurring experience patterns

✯ **EcphoryRAG Associative Retrieval**
- 🔹 Five-step pipeline: Vector Seed → Graph Expansion → Semantic Concept → Contradiction Detection → Fusion Ranking
- 🔹 Dynamic retrieval breadth controlled by neurochemical state (DA/5-HT modulates `seed_k`, `breadth_k`, `threshold`)

✯ **Neurochemical Dual-Axis Regulation**
- 🔹 DA/5-HT driven cognitive mode switching across 5 modes
- 🔹 Dynamic threshold adjustment for retrieval breadth and salience weighting
- 🔹 Event-driven state transitions with APScheduler-backed persistence

✯ **OpenSoul Affective Pipeline**
- 🔹 SubconsciousAssessor — 5-dimension evaluation (emotion / novelty / relevance / urgency / conflict)
- 🔹 SalienceEvaluator — 3-value output (HIGH / MEDIUM / LOW)
- 🔹 ResponseVerifier — 4-dimension quality scoring
- 🔹 SubconsciousInhibitor — PASS / REVISE / SUPPRESS gate
- 🔹 StorageJudge — Hybrid rule + LLM storage decision (no keyword matching)

✯ **Concept Management**
- 🔹 ConceptExtractor — daemon thread real-time extraction via ThreadPoolExecutor
- 🔹 Synonym unification / polysemy splitting / new concept creation
- 🔹 ConceptVerifier + ConceptInhibitor gate for quality control

✯ **Dream Engine (Offline Consolidation)**
- 🔹 Graph Pruning + LATENT_BRIDGE for structural cleanup
- 🔹 LiDER Replay — memory replay for consolidation
- 🔹 Knowledge Distillation across episodes
- 🔹 Procedure Extraction from recurring dialogue patterns
- 🔹 Contradiction detection + ConflictLedger; idle/Cron dual-trigger modes

✯ **LLM Autonomous Tools**
- 🔹 `force_remember` / `deep_recall` / `adjust_importance` / `trigger_dream`
- 🔹 Called proactively by the conversation LLM via Function Calling

✯ **MCP Server**
- 🔹 7 standard tools exposed via FastMCP
- 🔹 Multi-tenant namespace isolation for external system integration

✯ **Reflection-Action Loop**
- 🔹 ReflectionModule — periodic reflection (every 10 turns or uncertainty > 0.7)
- 🔹 ActionQueue → ReflectionExecutor with rollback support
- 🔹 Soul Note — subconscious diary written each turn

## Project Structure

```
TestSeperateMemorySystem/
├── Entry.py                     # Interactive CLI (chat, memory management, debug)
├── run.bat                      # Launch script
├── requirements.txt             # Python dependencies
├── pyproject.toml               # Project config (setuptools)
├── .env.example                 # Environment variable template
│
├── echo_memory/                 # Core memory engine
│   ├── engine.py                # MemoryEngine main class
│   ├── config.py                # Configuration management
│   ├── namespace.py             # Namespace isolation
│   ├── schemas/                 # Data structure definitions
│   ├── storage/                 # Storage layer (episodic/semantic/working/procedural/graph/entity)
│   ├── retrieval/               # EcphoryRAG associative retrieval
│   ├── neurochem/               # Neurochemical regulation (DA/5-HT)
│   ├── affect/                  # Affective & subconscious pipeline (OpenSoul)
│   ├── utils/                   # Shared utilities
│   └── dream/                   # Dream Engine (offline consolidation)
│
├── llm_integration/             # LLM integration layer
│   ├── llm_client.py            # Gemini LLM client
│   ├── config.yaml              # LLM config (roles, prompts)
│   └── tools/                   # Function Calling tools
│       ├── definitions.py       # Tool definition schemas
│       └── executor.py          # Tool executor
│
├── mcp_server/                  # MCP Server (external integration)
│   ├── server.py                # FastMCP server
│   ├── handlers.py              # Tool handlers
│   └── resources.py             # Resource exposure
│
├── scripts/                     # End-to-end validation scripts
│   ├── probe_pipeline.py        # 25-round auto-script + Rich reasoning map
│   └── dialogue_script.py       # Dialogue scenario scripts
│
├── tests/                       # Test suite
│   ├── *.py                     # 608 unit tests
│   └── integration/             # 12 integration tests
│
└── examples/                    # Usage examples
    └── minimal_chat.py          # Minimal chat example
```

## Quick Start

### Prerequisites

This project intentionally uses the shared U.E.P environment `~/.uep/venv-inference-312` (managed by the `U.E.P-s-Core` uv.lock, with echo-memory installed editable) rather than maintaining its own. The canonical embedding backend is Ollama `bge-m3` (1024-dim multilingual).

- Python 3.12 (via `~/.uep/venv-inference-312`)
- Ollama running locally with `bge-m3` pulled
- Google API key for Gemini

```powershell
$UEP_PYTHON = "$env:USERPROFILE\.uep\venv-inference-312\Scripts\python.exe"

# 1. Configure environment variables
Copy-Item .env.example .env
# Edit .env — required fields:
#   GOOGLE_API_KEY=<your_key>
#   EMBEDDING_BACKEND=ollama
#   EMBEDDING_MODEL=bge-m3

# 2. Verify Ollama service and model
ollama list   # should show bge-m3

# 3. Run the test suite
& $UEP_PYTHON -m pytest tests/ -x -q
```

### Interactive CLI

```powershell
.\run.bat
```

| Command | Description |
|---------|-------------|
| `chat` | Dialogue mode (auto retrieval + storage + neurochemical control) |
| `store` | Manually store a conversation fragment |
| `retrieve` | Manually retrieve relevant memories |
| `stats` | View memory statistics and neurochemical state |
| `list` | List all episodic memories |
| `dream` | Manually trigger Dream Engine consolidation |
| `help` | Show full command reference |

### Programmatic Usage

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

# Store a conversation turn (every turn is stored; salience is an attribute, not a gate)
memory.store_episode(
    user_input="How is the weather today?",
    agent_response="Sunny in Taipei, 25 degrees.",
    session_id="demo_001",
)

# Retrieve memories (neurochemical parameters dynamically affect retrieval breadth)
context = memory.retrieve(
    query="What was the weather like?",
    dopamine=0.5,
    serotonin=0.5,
)

print(context.to_text())
```

### Namespace Safety

- `local-default` uses the existing `storage_path` directly; other namespaces reside under `<storage_path>/namespaces/<namespace_id>`
- Write and Dream operations within the same namespace are serialized via cross-process file locks to prevent multi-instance data conflicts
- `embedding_index.json` records model, dimension, and episode order; mismatches trigger automatic full rebuild
- Retrieved content is injected into prompts inside an `UNTRUSTED_MEMORY_DATA` JSON envelope to prevent prompt injection

## Technical Stack

| Category | Technology | Notes |
|----------|------------|-------|
| Language | Python 3.10+ | Type annotations + Google-style docstrings |
| Vectors | NumPy | Dense vectors + exact cosine similarity |
| Graph | NetworkX 3.2+ | Lightweight replacement for FalkorDB |
| Embedding | Ollama bge-m3 | 1024-dim multilingual (canonical baseline) |
| LLM (Chat) | Google Gemini | gemini-3.1-flash-lite |
| LLM (Eval) | Google Gemini | gemini-2.5-flash-lite |
| Scheduling | APScheduler | Dream idle/Cron dual-trigger |
| CLI | Rich | Interactive terminal UI |
| MCP | FastMCP | External integration interface |
| Serialization | Pydantic | Data validation and schema definition |
| Testing | pytest | 608 unit tests + 12 integration tests |

## Development Status

### ✅ Phase 1 — Basic Memory Storage (Completed)
- Working / Episodic / Semantic 三層存儲
- 基礎 embedding + cosine retrieval

### ✅ Phase 2 — EcphoryRAG + Neurochem (Completed)
- 五步聯想檢索管線
- 神經化學雙軸調控（DA/5-HT）
- Dream Engine 離線鞏固

### ✅ Phase 3 — LLM Integration + Tools (Completed)
- Function Calling 工具定義
- 全模組結構化 LLM 輸出遷移（Gemini `response_schema`）
- LLMRouter Ollama fallback

### ✅ Phase 4 — OpenSoul Full Migration (Completed)
- 情感管線完整遷移（SubconsciousAssessor / SalienceEvaluator / ResponseVerifier / SubconsciousInhibitor）
- StorageJudge hybrid 決策取代硬編碼 keyword matching
- Reflection-Action 閉環（ActionQueue + ReflectionExecutor + rollback）
- Soul Note 潛意識日記

### ✅ Phase 5 — E2E Verification (Completed)
- 25 輪 Probe Pipeline + Rich 推理地圖視覺化
- 12 整合測試（覆蓋完整 pipeline 端到端行為）
- 608 單元測試
- Robustness cleanup（Discord Bot 前置準備）

### 📋 Next — Discord Integration
- 多用戶驗證（namespace 隔離 per user）
- 記憶跨 session 持久化驗證

## Complete Conversation Processing Flow

```
User Input
  │
  ├─ [Step 1]   EcphoryRAG Five-Step Associative Retrieval
  │              Vector Seed → Graph Expansion → Semantic Concept
  │              → Contradiction Detection → Fusion Ranking
  │              (DA/5-HT dynamically controls seed_k / breadth_k / threshold)
  │
  ├─ [Step 1.5] SubconsciousAssessor 5-dimension evaluation
  │              + JudgeAgent tool pre-filtering
  │
  ├─ [Step 2]   LLM generates response
  │              → Content sanitization (clears forged tool markers)
  │              → ResponseVerifier 4-dimension quality scoring
  │              → SubconsciousInhibitor gate (PASS / REVISE / SUPPRESS)
  │
  ├─ [Step 3]   SalienceEvaluator 3-value output (HIGH / MEDIUM / LOW)
  │              → Store Episode (salience as attribute, not gate)
  │
  ├─ [Step 4]   Background concept extraction (daemon thread)
  │              Synonym unification / polysemy splitting / new concept creation
  │
  ├─ [Step 5]   Soul Note subconscious diary write
  │
  ├─ [Step 6]   ReflectionModule periodic reflection
  │              → ActionQueue → ReflectionExecutor (with rollback)
  │
  ├─ [Step 7]   Entity extraction (when salience >= threshold)
  │
  └─ [Step 8]   NeurochemState tick() + persistence
```

### Dynamic Edge Weight Formula

```
W(edge) = 0.3 * Recency + 0.4 * Frequency + 0.3 * Salience

Recency   = exp(-0.01 * hours_ago)
Frequency = log(1 + n) / log(1 + n_max)
Salience  = dopamine * (1 - 0.3 * serotonin)
```

## Architecture Design Notes

**Salience is a weight, not a gate.** Every conversation turn is stored as an Episode without exception. Salience scores influence retrieval ranking, edge weight calculation, and Dream Engine pruning decisions — low-salience memories decay naturally rather than being discarded outright.

**No keyword matching.** Memory importance is determined by the conversation LLM via autonomous tool calls and by `SalienceEvaluator` + `SubconsciousAssessor` providing semantic weights. No keyword lists or regex rules exist anywhere in the memory decision path.

## U.E.P Core Integration Plan

The planned migration path back into `U.E.P-s-Core` as `mem_module_v2`:

```python
# modules/mem_module_v2/__init__.py
from echo_memory import MemoryEngine

def register(event_bus, debug_api):
    return MemModuleV2(MemoryEngine(...))
```

```python
# Collaboration channel replacement
memory_context = mem_engine_v2.retrieve(user_text)
working_context.set_channel_data("CHAT_MEM", {
    "memory_text": memory_context.to_text()
})
```

```python
# SLEEP event integration
async def _handle_sleep_started(self):
    dream_report = await mem_engine_v2.dream_now("sleep")
    logger.info(dream_report.summary())
```

## Related Projects

- [U.E.P-s-Core](https://github.com/Unforgettableeternalproject/U.E.P-s-Core) — Parent project; `echo_memory` will be reintegrated as `mem_module_v2`
- [TestSeparateTTSSystem](https://github.com/Unforgettableeternalproject/TestSeparateTTSSystem) — TTS speech synthesis
- [TestSeparateSTTSystem](https://github.com/Unforgettableeternalproject/TestSeparateSTTSystem) — STT speech recognition
- [TestSeparateSessionControl](https://github.com/Unforgettableeternalproject/TestSeparateSessionControl) — Session Control (complementary: within-session context)

## References

- Tulving, E., & Thomson, D. M. (1973). *Encoding specificity and retrieval processes in episodic memory.* — Ecphory basis
- Stickgold, R. (2005). *Sleep-dependent memory consolidation.* — Dream Engine design basis
- [OpenSoul](https://github.com/opensouls/soul-engine) — Affective pipeline architecture reference

## License

MIT License
