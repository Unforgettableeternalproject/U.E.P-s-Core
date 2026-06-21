# TestSeparateSessionControl

> LLM session control engine with four-layer progressive compression — extracted from U.E.P-s-Core for isolated context management experimentation.

## Project Overview

TestSeparateSessionControl is the context management subsystem extracted from [U.E.P-s-Core](https://github.com/Unforgettableeternalproject/U.E.P-s-Core). It tackles the fundamental challenge of long-running LLM conversations: keeping context relevant without losing critical information. The design philosophy is **progressive degradation, not cliff-edge truncation** — inspired by Anthropic Claude Code's approach.

The core module, `echo_thought_core`, implements a four-layer compression pipeline that activates at increasing token thresholds (60% → 80% → 92% → API error). Each layer is independently testable and configurable. Sacred content (persona, explicit instructions, task objectives) is guaranteed to survive all compression levels.

This subsystem complements [Echo Memory](https://github.com/Unforgettableeternalproject/TestSeperateMemorySystem): **Session Control** handles within-session context management while **Echo Memory** handles cross-session persistence. Together they form U.E.P's complete thought continuity system.

## Core Features

✯ **Four-Layer Progressive Compression**
- 🔹 Layer 1: Micro-Pruning @60% — rule-based cleanup with zero LLM calls
- 🔹 Layer 2: Rolling Summary @80% — incremental LLM summarization
- 🔹 Layer 3: Full Compaction @92% — nine+one structured compression
- 🔹 Layer 4: Emergency — API 413 triggered, circuit breaker protection

✯ **Sacred Content Protection**
- 🔹 Persona context (character name, traits, speaking style) — never pruned
- 🔹 User explicit instructions — preserved verbatim through all layers
- 🔹 Current task objectives — always retained regardless of compression level

✯ **Multi-Backend Support**
- 🔹 Google Gemini — primary backend with context caching integration
- 🔹 Ollama — local model fallback (Qwen2.5, etc.)
- 🔹 `BackendRegistry` — runtime backend switching without session loss
- 🔹 Cache provider abstraction — local file cache with extensible interface

✯ **Session Management**
- 🔹 Auto session rotation — configurable token/turn thresholds
- 🔹 `SessionStore` persistence — sessions survive process restarts
- 🔹 Pre-compression snapshot preservation for audit
- 🔹 Prompt interface abstraction for backend-agnostic formatting

✯ **Rich CLI**
- 🔹 Interactive shell with `chat` / `compress` / `status` / `backend` / `session` commands
- 🔹 Real-time token usage display with budget visualization
- 🔹 Compression layer activity monitoring

✯ **Integration Hooks**
- 🔹 Echo Memory bridge — compression events flow to memory persistence
- 🔹 LLMLingua optional integration — Microsoft's prompt compression for Layer 1 enhancement
- 🔹 Multimodal content handling (image/audio token accounting)
- 🔹 Tool call pair management + output summarization

## Project Structure

```
TestSeparateSessionControl/
├── Entry.py                    # CLI entry point (shell subcommand)
├── pyproject.toml              # Package configuration (pip install -e .)
├── requirements.txt
├── run.bat
├── test.bat
├── echo_thought_core/
│   ├── __init__.py
│   ├── accounting/             # Token counting + budget management
│   │   ├── budget.py
│   │   └── counter.py
│   ├── backends/               # LLM backend abstraction
│   │   ├── base.py             # Abstract backend contract
│   │   ├── factory.py          # Backend registry + factory
│   │   ├── gemini.py           # Google Gemini backend
│   │   └── ollama.py           # Ollama local backend
│   ├── cache/                  # Cache providers
│   │   └── local_cache.py
│   ├── compression/            # Four-layer compression engine
│   │   ├── emergency.py        # Layer 4: Emergency truncation
│   │   ├── full_compaction.py  # Layer 3: LLM summarization
│   │   ├── llmlingua.py        # LLMLingua integration (optional)
│   │   ├── micro_pruning.py    # Layer 1: Rule-based cleanup
│   │   ├── multimodal.py       # Multimodal content handling
│   │   ├── rolling_summary.py  # Layer 2: Incremental summary
│   │   ├── sacred.py           # Sacred content protection
│   │   ├── tool_pairs.py       # Tool call pair management
│   │   └── tool_summarizer.py  # Tool output summarization
│   ├── schemas/                # Pydantic data models
│   │   ├── config.py
│   │   ├── message.py
│   │   └── session.py
│   ├── cli.py                  # Rich interactive CLI
│   ├── context_manager.py      # Main ContextManager orchestrator
│   ├── defaults.py             # Default configurations
│   ├── echo_compressor.py      # Compression coordinator
│   ├── engine_base.py          # Engine abstraction
│   ├── llm_client.py           # LLM client wrapper
│   ├── memory_bridge.py        # Echo Memory integration bridge
│   ├── persona.py              # PersonaContext (sacred content)
│   └── session_store.py        # Session persistence
├── tests/                      # pytest suite (unit + E2E)
└── docs/                       # Design notes
```

## Quick Start

### Prerequisites

- Python 3.10+
- Google Gemini API key or local Ollama instance
- (Optional) U.E.P-s-Core shared environment

### Installation

```powershell
# Standalone installation
pip install -e ".[dev]"

# Or use U.E.P-s-Core shared environment
$UEP_PYTHON = "..\U.E.P-s-Core\env\Scripts\python.exe"
& $UEP_PYTHON -m pip install -e ".[dev]"
```

### Running

```powershell
# Interactive shell
python Entry.py shell

# Run tests
pytest -v

# Or via U.E.P-s-Core environment
& $UEP_PYTHON Entry.py shell
```

### Shell Commands

| Command | Description |
|---------|-------------|
| `chat` | Conversation mode with automatic compression |
| `compress` | Manually trigger compression at current level |
| `status` | Show token usage, budget, and compression state |
| `backend` | Switch between Gemini and Ollama backends |
| `session` | Session management (list / rotate / export) |

## Technical Stack

| Category | Technology | Notes |
|----------|------------|-------|
| Python | 3.10+ | Type annotations + Pydantic 2.5+ |
| Token counting | tiktoken | OpenAI-compatible tokenizer |
| LLM | Gemini + Ollama | Dual backend with runtime switching |
| Compression | LLMLingua (optional) | Microsoft's prompt compression research |
| CLI | Rich | Interactive terminal UI |
| Serialization | Pydantic | Schema validation + JSON persistence |
| Testing | pytest | Unit + E2E tests |

## Development Status

### ✅ Phase 1 — Core Compression (Completed)
- Token Accounting（精確計數 + 預算管理）
- Layer 1 Micro-Pruning @60%（規則式清理，零 LLM 呼叫）
- Layer 3 Full Compaction @92%（九+一段式結構化壓縮）
- Layer 4 Emergency（API 413 觸發 + 斷路器保護）

### ✅ Phase 2 — Engine Abstraction (Completed)
- ContextManager 統一介面
- Layer 2 Rolling Summary @80%（增量 LLM 摘要）
- LLMLingua 整合（可選依賴）
- 人設保護機制（Sacred Content Protection）
- Ollama backend + BackendRegistry + runtime switch

### ✅ Phase 3 — Session & Cache (Completed)
- Auto session rotation（可配置閾值）
- Cache provider abstraction
- Prompt interface abstraction
- SessionStore persistence
- Session management CLI

### 📋 Next — Discord Integration
- 整合進 U.E.P Discord Bot
- Echo Memory 壓縮事件持久化整合
- 多後端 Compaction Provider 抽象

## Related Projects

- [U.E.P-s-Core](https://github.com/Unforgettableeternalproject/U.E.P-s-Core) — Parent project
- [TestSeperateMemorySystem](https://github.com/Unforgettableeternalproject/TestSeperateMemorySystem) — Echo Memory (cross-session persistence, complementary)
- [TestSeparateTTSSystem](https://github.com/Unforgettableeternalproject/TestSeparateTTSSystem) — TTS speech synthesis
- [TestSeparateSTTSystem](https://github.com/Unforgettableeternalproject/TestSeparateSTTSystem) — STT speech recognition

## License

MIT License
