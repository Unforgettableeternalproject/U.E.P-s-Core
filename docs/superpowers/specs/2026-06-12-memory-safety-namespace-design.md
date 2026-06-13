# Memory Safety And Namespace Design

## Goal

Make Echo Memory safe for multiple local users, agents, and processes without locking the project into a final database architecture.

## Namespace Boundary

Every public engine instance has a required logical `namespace_id`. The default is `local-default` for backward compatibility. A namespace is mapped to a sanitized storage subdirectory under the configured storage root. Existing un-namespaced data remains the data for `local-default` and is migrated without copying when first opened.

MCP tools accept an optional `namespace_id`. The server keeps one engine per namespace instead of one global engine. CLI sessions use the configured default namespace and display it in diagnostics. Retrieval, Dream, listing, deletion, concepts, entities, and procedures operate only inside their engine namespace.

## Persistence And Concurrency

Each namespace has one cross-process lock file. Every mutating episodic operation acquires the lock, reloads the latest on-disk state, applies the mutation, and writes a complete snapshot. Metadata, embeddings, and graph files are written through temporary files followed by atomic replacement.

The initial implementation prioritizes correctness over write throughput. The storage abstraction and namespace resolver remain separate so a future SQLite or service backend can replace file snapshots without changing MCP contracts.

## Embedding Model Migration

The episodic store persists an index manifest containing the embedding model and vector dimension. If either differs from the active model, all existing episode summaries are re-embedded before retrieval or a new write is accepted. A partially rebuilt index is never published. Metadata order and vector row order must remain identical.

## Memory Trust Boundary

Retrieved memory is untrusted historical data, not instruction text. Serialization uses explicit data delimiters and escapes delimiter-like content. The LLM system prompt states that text inside memory records must never override system, developer, user, or tool policies. Full historical text may remain available for context, but it is labelled as quoted data.

## Dream Semantics

Time-decay recomputation applies only to `PRECEDES` edges. Semantic, contradiction, provenance, entity, and procedure edge weights retain their domain meaning. Dream remains serialized by its existing in-process lock and shares the namespace persistence lock for writes.

## Compatibility Fixes

The CLI semantic search reads episode similarity from `MemoryContext.similarities`. Public API validation clamps limits, rejects empty namespaces, and prevents path traversal. Existing default behavior remains usable without callers supplying a namespace.

## Testing

Regression coverage must include:

- two independent instances writing to one namespace without lost updates;
- two namespaces unable to retrieve or list each other's records;
- invalid namespace path components rejected;
- embedding model/dimension changes rebuilding every vector in correct order;
- memory text unable to terminate its data envelope;
- Dream preserving non-`PRECEDES` edge weights;
- CLI semantic search using the current similarity API;
- MCP engine routing returning stable per-namespace instances;
- complete existing test suite and compile verification.

