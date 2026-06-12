# Memory Safety And Namespace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent cross-namespace disclosure, concurrent lost updates, corrupt embedding migrations, and unsafe memory prompt injection while retaining backward compatibility.

**Architecture:** Add a namespace resolver above `MemoryEngine`, isolate each namespace into its own storage location, and make file snapshots cross-process serialized and atomic. Persist an embedding manifest and rebuild the complete index on model changes. Treat retrieved memory as quoted untrusted data and restrict Dream decay to temporal edges.

**Tech Stack:** Python 3.10+, pathlib, JSON, NumPy, NetworkX, platform file locking, pytest, MCP Python SDK.

---

### Task 1: Namespace Resolution And Engine Routing

**Files:**
- Create: `echo_memory/namespace.py`
- Modify: `echo_memory/engine.py`
- Modify: `mcp_server/server.py`
- Modify: `mcp_server/handlers.py`
- Test: `tests/test_namespace_isolation.py`

- [ ] Write tests proving invalid namespace IDs are rejected, two namespaces have different storage paths, and records cannot cross retrieval/list boundaries.
- [ ] Run `pytest tests/test_namespace_isolation.py -v` and confirm failures are caused by missing namespace support.
- [ ] Implement `normalize_namespace_id()`, `resolve_namespace_path()`, `MemoryEngine.namespace_id`, and per-namespace MCP engine caching.
- [ ] Add optional `namespace_id` schemas and handler routing to all MCP memory operations.
- [ ] Re-run the namespace tests and confirm they pass.

### Task 2: Cross-Process Safe Atomic Snapshots

**Files:**
- Create: `echo_memory/storage/locking.py`
- Modify: `echo_memory/storage/episodic.py`
- Modify: `echo_memory/storage/graph.py`
- Test: `tests/test_storage_concurrency.py`

- [ ] Write a regression test using two independently loaded `EpisodicMemory` instances that each write one episode and expect both to survive reload.
- [ ] Run the test and confirm it reproduces the one-record lost update.
- [ ] Implement a Windows/Unix cross-process lock context manager and mutation-time reload.
- [ ] Write JSON, NumPy, graph, and manifest snapshots to unique temporary files and atomically replace destinations while holding the lock.
- [ ] Re-run concurrency, episodic, graph, deletion, and entity tests.

### Task 3: Embedding Manifest And Full Reindex

**Files:**
- Modify: `echo_memory/storage/episodic.py`
- Test: `tests/test_embedding_migration.py`

- [ ] Write a test that stores old two-dimensional vectors, opens with a 512-dimensional model, adds a record, and verifies all three IDs map to their own regenerated vectors.
- [ ] Run the test and confirm the old first record is incorrectly returned for the new vector.
- [ ] Persist `embedding_index.json` with model, dimension, ordered IDs, and format version.
- [ ] Re-embed every existing episode under lock whenever model, dimension, row count, or ordered IDs disagree.
- [ ] Re-run migration, episodic search, persistence, and benchmark tests.

### Task 4: Untrusted Memory Envelope

**Files:**
- Modify: `echo_memory/schemas/memory_context.py`
- Modify: `llm_integration/llm_client.py`
- Test: `tests/test_memory_prompt_safety.py`

- [ ] Write tests with historical text containing fake system instructions and closing memory delimiters.
- [ ] Run tests and confirm raw text can currently escape or impersonate instructions.
- [ ] Serialize records as JSON-compatible quoted data inside fixed begin/end delimiters.
- [ ] Add a system-level rule that memory records are untrusted evidence and never instructions.
- [ ] Re-run memory context, engine, LLM prompt, and tool tests.

### Task 5: Dream Edge Semantics And CLI Compatibility

**Files:**
- Modify: `echo_memory/dream/pruning.py`
- Modify: `Entry.py`
- Test: `tests/test_dream_edge_semantics.py`
- Test: `tests/test_cli_search.py`

- [ ] Write tests proving Dream changes `PRECEDES` weight but preserves `CONTRADICTS`, `RELATES_TO`, and `MENTIONS` weights.
- [ ] Write a CLI helper regression test that exercises semantic result similarity lookup.
- [ ] Run both tests and confirm the incorrect behavior.
- [ ] Restrict time decay by `edge_type` and replace `episode_similarities` with ID-based `similarities` lookup.
- [ ] Re-run Dream, graph, CLI, and retrieval tests.

### Task 6: Full Verification And Documentation

**Files:**
- Modify: `README.md`
- Modify: `mcp_server/README.md`
- Modify: `.env.example`

- [ ] Document `MEMORY_NAMESPACE_ID`, default namespace migration, MCP `namespace_id`, locking limitations, and model reindex behavior.
- [ ] Run targeted tests for all new regression modules.
- [ ] Run `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider` outside the restricted sandbox if Windows temp access requires it.
- [ ] Run `.venv\\Scripts\\python.exe -m compileall -q echo_memory mcp_server llm_integration Entry.py`.
- [ ] Run `git diff --check` and review the final diff without reverting pre-existing user changes.

