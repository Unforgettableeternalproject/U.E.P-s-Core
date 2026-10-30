"""FakeMemoryEngine：照 echo_memory ``MemoryEngine`` 真實簽名寫的替身（S2 只用它，不 import echo_memory）。

- ``call_log`` 為多個 engine 共用的有序呼叫紀錄（``(namespace_id, 方法名, 參數)``），測試用來固定啟動順序。
- 檢索：以子字串比對 ``episodes``（私人 engine）；``retrieve_shared`` 回 ``shared_summaries``。
- ``dream_result`` 可設成 ``{"skipped_reason": "租約由 pid=... 持有"}`` 或 ``{"error": "..."}``。
- ``retrieve_delay_s`` 模擬慢檢索（逾時案例）；``close`` 預設不存在（真 engine 沒有），``with_close=True`` 才有。
- ``emit(draft)`` 透過已注入的 sink 發草稿，模擬記憶端 emitter。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from uep.memory.namespaces import resolve_namespace_path


@dataclass(frozen=True)
class FakeDraft:
    """與 echo_memory ``EventDraft`` 同形（event_type／payload／idempotency_key／scope_ref）。"""

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    scope_ref: str = ""


class FakeMemoryContext:
    def __init__(self, texts: Sequence[str], degraded: bool = False, degraded_reason: str = "") -> None:
        self.texts = list(texts)
        self.degraded = degraded
        self.degraded_reason = degraded_reason

    def is_empty(self) -> bool:
        return not self.texts

    def to_text(self) -> str:
        return "\n".join(self.texts)


class FakeSharedContext:
    def __init__(self, texts: Sequence[str], ids: Sequence[str] = ()) -> None:
        self._texts = list(texts)
        self.ids = list(ids)
        self.degraded = False
        self.degraded_reason = ""

    def to_text(self, display_name: Callable[[str], str] | None = None) -> str:
        return "\n".join(self._texts)


class FakeEventEmitter:
    def __init__(self) -> None:
        self.drained: list[float] = []

    def drain(self, timeout: float = 2.0) -> bool:
        self.drained.append(timeout)
        return True


class FakeMemoryEngine:
    def __init__(
        self,
        namespace_id: str,
        storage_path: Path,
        call_log: list[tuple[str, str, Any]] | None = None,
        *,
        with_close: bool = False,
    ) -> None:
        self.namespace_id = namespace_id
        self.storage_path = Path(storage_path)
        self.call_log = call_log if call_log is not None else []
        self.episodes: list[str] = []
        self.shared_summaries: list[str] = []
        self.stored: dict[str, str] = {}  # idempotency_key -> episode_id
        self.store_calls = 0
        self.retrieve_calls: list[dict[str, Any]] = []
        self.shared_calls: list[dict[str, Any]] = []
        self.scheduler_result: dict[str, Any] = {"started": True, "reason": "started"}
        self.dream_result: dict[str, Any] = {"triggered_by": "manual", "error": None, "skipped_reason": None}
        self.retrieve_delay_s = 0.0
        self.degraded = False
        self.sink: Any = None
        self.inhibitor: Any = "unset"
        self.event_emitter = FakeEventEmitter()
        self.profile_text = ""
        self.closed = False
        if with_close:
            self.close = self._close  # type: ignore[method-assign]

    def _log(self, name: str, arg: Any = None) -> None:
        self.call_log.append((self.namespace_id, name, arg))

    # ---- 注入 ----

    def set_llm_fn(self, llm_fn: Callable[[str], str] | None) -> None:
        self._log("set_llm_fn", llm_fn)

    def set_concept_inhibitor(self, inhibitor: Any) -> None:
        self.inhibitor = inhibitor
        self._log("set_concept_inhibitor", inhibitor)

    def set_candidate_sink(self, sink: Any) -> None:
        self.sink = sink
        self._log("set_candidate_sink", sink)

    # ---- 做夢 ----

    def start_dream_scheduler(self) -> dict[str, Any]:
        self._log("start_dream_scheduler")
        return dict(self.scheduler_result)

    def stop_dream_scheduler(self) -> None:
        self._log("stop_dream_scheduler")

    def dream_readiness(self) -> dict[str, Any]:
        return {"pending": 0, "min_pending": 5, "daydream_pending": 20, "suggested_trigger": None}

    def dream(self, triggered_by: str = "manual") -> dict[str, Any]:
        self._log("dream", triggered_by)
        return {**self.dream_result, "triggered_by": triggered_by}

    # ---- 讀寫 ----

    def retrieve(
        self, query: str, dopamine: float | None = None, serotonin: float | None = None, top_k: int = 10,
        min_similarity: float | None = None, exclude_session_id: str | None = None,
        exclude_episode_ids: set[str] | None = None, include_history: bool = False,
        include_hypotheses: bool = False, shared_turn: bool = False,
    ) -> FakeMemoryContext:
        self._log("retrieve", query)
        self.retrieve_calls.append({
            "query": query, "top_k": top_k, "min_similarity": min_similarity,
            "exclude_session_id": exclude_session_id, "shared_turn": shared_turn,
        })
        if self.retrieve_delay_s:
            time.sleep(self.retrieve_delay_s)
        hits = [e for e in self.episodes if query in e or e in query][:top_k]
        reason = "embedding_unavailable" if self.degraded else ""
        return FakeMemoryContext(hits, degraded=self.degraded, degraded_reason=reason)

    def retrieve_shared(
        self, query: str, *, scope_id: str, viewer_uid: str, present_uids: Sequence[Any] = (),
        top_k: int | None = None, min_similarity: float | None = None,
    ) -> FakeSharedContext:
        self._log("retrieve_shared", scope_id)
        self.shared_calls.append({"query": query, "scope_id": scope_id, "viewer_uid": viewer_uid})
        ids = [f"proj_{i}" for i, _ in enumerate(self.shared_summaries)]
        return FakeSharedContext(self.shared_summaries, ids=ids)

    def store_episode(
        self, user_input: str, agent_response: str, session_id: str, content_summary: str | None = None,
        salience: float = 0.5, da_weight: float | None = None, ht_weight: float | None = None,
        *, evidence: Sequence[Any] | None = None, episode_kind: str = "turn",
        idempotency_key: str | None = None, shared_context_ids: Sequence[str] | None = None,
        shared_scope_id: str | None = None, **metadata: Any,
    ) -> str:
        self._log("store_episode", idempotency_key)
        self.store_calls += 1
        if idempotency_key is not None and idempotency_key in self.stored:
            return self.stored[idempotency_key]  # 同 key 回原 episode_id，不重寫
        episode_id = f"ep{len(self.episodes) + 1:04d}"
        self.episodes.append(user_input)
        if idempotency_key is not None:
            self.stored[idempotency_key] = episode_id
        return episode_id

    def get_profile(self, max_chars: int = 800, limit: int = 30) -> str:
        return self.profile_text[:max_chars]

    def _close(self) -> None:
        self.closed = True
        self._log("close")

    # ---- 模擬記憶端 emitter ----

    def emit(self, draft: FakeDraft) -> None:
        assert self.sink is not None, "尚未注入 sink"
        self.sink.emit(draft)


class FakeEngineFactory:
    """``MemoryHost(engine_factory=...)`` 用：記錄建立過的 engine 與共用 call_log。"""

    def __init__(self, *, with_close: bool = False) -> None:
        self.call_log: list[tuple[str, str, Any]] = []
        self.engines: dict[str, FakeMemoryEngine] = {}
        self._with_close = with_close

    def __call__(self, namespace_id: str, data_root: Path) -> FakeMemoryEngine:
        engine = FakeMemoryEngine(
            namespace_id, resolve_namespace_path(data_root, namespace_id), self.call_log,
            with_close=self._with_close,
        )
        self.engines[namespace_id] = engine
        return engine

    def calls(self, namespace_id: str) -> list[str]:
        return [name for ns, name, _ in self.call_log if ns == namespace_id]
