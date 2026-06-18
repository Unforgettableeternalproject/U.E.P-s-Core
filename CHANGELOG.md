# Changelog

All notable changes to Echo Thought Core will be documented in this file.

## [0.2.0] — 2026-06-18 (In Progress)

### Added
- `ContextEngine` ABC — pluggable compression engine abstraction
- `EchoCompressor` — four-layer compression engine (extracted from ContextManager)
- `MemoryBridge` Protocol — integration interface for Echo Memory system
- Anti-thrashing protection for compression loops
- `ToolSummarizer` — structured tool output summarization
- Compression summary instruction safety markers
- Preflight compression with multi-pass support
- Multimodal content management (image token estimation + cleanup)
- Dynamic session boundary interface (reserved for DC bot integration)
- `py.typed` marker (PEP 561)
- CacheManager integration tests

### Changed
- `ContextManager` refactored to thin wrapper delegating to `ContextEngine`
- `Entry.py` CLI moved to `echo_thought_core/cli.py`
- `TokenBudget.get_usage_report()` no longer returns misleading zero api_usage
- `TokenBudget.update_api_usage()` method added for explicit API usage tracking

### Removed
- Orphan schemas from U.E.P Core migration (`LLMInput`, `LLMOutput`,
  `LearningSignals`, `LearningData`, `StatusUpdate`, `ConversationEntry`,
  `LLMMode`)
- `local` backend stub (`NotImplementedError` placeholder)

## [0.1.0] — 2026-06-14

### Added
- Initial release: four-layer progressive compression architecture
- Layer 1: Micro-Pruning (rule-based cleanup + LLMLingua-2)
- Layer 2: Rolling Summary (dynamic budget + six-section template)
- Layer 3: Full Compaction (nine-section structured compression)
- Layer 4: Emergency (truncation + circuit breaker + sacred re-injection)
- `ContextManager` with sync/async compression pipeline
- `LLMClient` (Gemini/Vertex AI unified client)
- `TokenCounter` (tiktoken + CJK fallback)
- `TokenBudget` (threshold-based tier detection)
- `SacredManager` (protected content management)
- `CacheManager` (Gemini explicit/implicit caching)
- `PersonaContext` and YAML-based persona loading
- Rich CLI with dashboard, sawtooth visualization, chat mode
- Tool pair integrity protection
- Prompt caching (Anthropic 4-breakpoint strategy)
- 166 tests, 70% coverage
