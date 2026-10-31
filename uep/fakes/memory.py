"""FakeMemoryEngine：照 echo_memory ``MemoryEngine`` 真實簽名寫的替身（S2 只用它，不 import echo_memory）。

- ``call_log`` 為多個 engine 共用的有序呼叫紀錄（``(namespace_id, 方法名, 參數)``），測試用來固定啟動順序。
- 檢索：以子字串比對 ``episodes``（私人 engine）；``retrieve_shared`` 回 ``shared_summaries``。
- ``dream_result`` 可設成 ``{"skipped_reason": "租約由 pid=... 持有"}`` 或 ``{"error": "..."}``。
- ``retrieve_delay_s`` 模擬慢檢索（逾時案例）；``close(timeout)`` 永遠存在（B-4，冪等）；``with_close`` 僅為相容舊測試，已無作用。
- ``FakeMemoryAdapter`` 照 ES ``EchoMemoryAdapter`` 的形狀（async ``store``／``execute_tool``／``aclose``），寫入轉呼叫 engine；
  ``FakeAdapterFactory`` 記錄每個 engine 建了幾個 adapter（單一 adapter 約束）。
- ``emit(draft)`` 透過已注入的 sink 發草稿，模擬記憶端 emitter。
- 做夢：``readiness`` 為 ``dream_readiness()`` 的回傳（可改）；``dream()`` 與真 engine 一樣在返回前同步呼叫 listener
  （``notify_listener``）、以 ``dream_hook(triggered_by)`` 可注入阻塞／例外／自訂回傳，並記錄 ``max_dream_concurrency``。
"""
from __future__ import annotations

import asyncio
import threading
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


@dataclass(frozen=True)
class FakeSharedItem:
    """共享投影：``owner`` 為投影擁有者 uid，``about`` 為被提到的成員 uid（旁證）。"""

    owner: str
    text: str
    id: str
    about: str | None = None


class FakeSharedContext:
    def __init__(
        self, texts: Sequence[str], ids: Sequence[str] = (), items: Sequence[FakeSharedItem] = ()
    ) -> None:
        self._texts = list(texts)
        self._items = list(items)
        self.ids = list(ids) + [i.id for i in self._items]
        self.degraded = False
        self.degraded_reason = ""

    def to_text(self, display_name: Callable[[str], str] | None = None) -> str:
        """沒給 ``display_name`` 時刻意輸出原始 uid：呼叫端漏傳解析器的洩漏會被測試抓到。"""
        name = display_name or (lambda uid: uid)
        lines = list(self._texts)
        for item in self._items:
            who = name(item.owner)
            lines.append(f"- {who}：{item.text}" if item.about is None else f"- {who} 提到 {name(item.about)}：{item.text}")
        return chr(10).join(lines)


class FakeEventEmitter:
    def __init__(self, engine: Any = None) -> None:
        self.drained: list[float] = []
        self._engine = engine

    def drain(self, timeout: float = 2.0) -> bool:
        self.drained.append(timeout)
        return True if self._engine is None else self._engine.drain_ok


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
        self.shared_items: list[FakeSharedItem] = []
        self.stored: dict[str, str] = {}  # idempotency_key -> episode_id
        self.store_calls = 0
        self.retrieve_calls: list[dict[str, Any]] = []
        self.shared_calls: list[dict[str, Any]] = []
        self.scheduler_result: dict[str, Any] = {"started": True, "reason": "started"}
        self.dream_result: dict[str, Any] = {"triggered_by": "manual", "error": None, "skipped_reason": None}
        self.retrieve_delay_s = 0.0
        self.store_delay_s = 0.0
        self.fail_on: set[str] = set()  # 這些方法呼叫時拋 RuntimeError（注入啟動失敗用）
        self.drain_ok = True
        self.degraded = False
        self.sink: Any = None
        self.inhibitor: Any = "unset"
        self.event_emitter = FakeEventEmitter(self)
        self.profile_text = ""
        self.closed = False
        self.dream_overrides: dict[str, Any] = {}
        self.dream_listener: Callable[[dict[str, Any]], None] | None = None
        self.engine_resolver: Callable[[str], Any] | None = None
        self.close_timeouts: list[float] = []
        self.readiness: dict[str, Any] = {"pending": 0, "min_pending": 5, "daydream_pending": 20, "suggested_trigger": None}
        self.notify_listener = True
        self.dream_hook: Callable[[str], dict[str, Any] | None] | None = None
        self.dream_count = 0
        self.max_dream_concurrency = 0
        self._dream_active = 0
        self._dream_lock = threading.Lock()

    def _log(self, name: str, arg: Any = None) -> None:
        self.call_log.append((self.namespace_id, name, arg))
        if name in self.fail_on:
            raise RuntimeError(f"注入的失敗：{name}")

    # ---- 注入 ----

    def set_llm_fn(self, llm_fn: Callable[[str], str] | None) -> None:
        self._log("set_llm_fn", llm_fn)

    def set_concept_inhibitor(self, inhibitor: Any) -> None:
        self.inhibitor = inhibitor
        self._log("set_concept_inhibitor", inhibitor)

    def set_candidate_sink(self, sink: Any) -> None:
        self.sink = sink
        self._log("set_candidate_sink", sink)

    def set_engine_resolver(self, resolver: Callable[[str], Any] | None) -> None:
        self.engine_resolver = resolver
        self._log("set_engine_resolver", resolver)

    def set_dream_listener(self, fn: Callable[[dict[str, Any]], None] | None) -> None:
        self.dream_listener = fn
        self._log("set_dream_listener", fn)

    def configure_dream(self, **overrides: Any) -> dict[str, Any]:
        self.dream_overrides.update(overrides)
        self._log("configure_dream", dict(overrides))
        return {"applied": True, "overrides": dict(self.dream_overrides)}

    def report_dream(self, report: dict[str, Any]) -> None:
        """模擬一次 dream_now 結束：呼叫已註冊的 listener（含 skipped／error）。"""
        assert self.dream_listener is not None, "尚未註冊 dream listener"
        self.dream_listener(report)

    # ---- 做夢 ----

    def start_dream_scheduler(self) -> dict[str, Any]:
        self._log("start_dream_scheduler")
        return dict(self.scheduler_result)

    def stop_dream_scheduler(self) -> None:
        self._log("stop_dream_scheduler")

    def dream_readiness(self) -> dict[str, Any]:
        self._log("dream_readiness")
        return dict(self.readiness)

    def dream(self, triggered_by: str = "manual") -> dict[str, Any]:
        self._log("dream", triggered_by)
        with self._dream_lock:
            self.dream_count += 1
            self._dream_active += 1
            self.max_dream_concurrency = max(self.max_dream_concurrency, self._dream_active)
        try:
            override = self.dream_hook(triggered_by) if self.dream_hook is not None else None
        finally:
            with self._dream_lock:
                self._dream_active -= 1
        result = {**self.dream_result, **(override or {}), "triggered_by": triggered_by}
        if "status" not in result:
            result["status"] = "skipped" if result.get("skipped_reason") else "error" if result.get("error") else "completed"
        if self.notify_listener and self.dream_listener is not None:
            self.dream_listener(dict(result))
        return result

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
        top_k: int | None = None, min_similarity: float | None = None, owner_uid: str | None = None,
    ) -> FakeSharedContext:
        """照真 engine 簽名：``owner_uid`` 為指名成員查詢（本人段加旁證段）；viewer 等於 owner 回空；
        沒給 owner_uid 時回一般檢索（排除檢視者自己的投影）。"""
        self._log("retrieve_shared", scope_id)
        self.shared_calls.append({
            "query": query, "scope_id": scope_id, "viewer_uid": viewer_uid, "present_uids": tuple(present_uids),
            "owner_uid": owner_uid,
        })
        if owner_uid is not None:
            if viewer_uid == owner_uid:
                return FakeSharedContext([], [])
            return FakeSharedContext(
                [], [], [i for i in self.shared_items if i.owner == owner_uid or i.about == owner_uid]
            )
        ids = [f"proj_{i}" for i, _ in enumerate(self.shared_summaries)]
        return FakeSharedContext(self.shared_summaries, ids, [i for i in self.shared_items if i.owner != viewer_uid])

    def store_episode(
        self, user_input: str, agent_response: str, session_id: str, content_summary: str | None = None,
        salience: float = 0.5, da_weight: float | None = None, ht_weight: float | None = None,
        *, evidence: Sequence[Any] | None = None, episode_kind: str = "turn",
        idempotency_key: str | None = None, shared_context_ids: Sequence[str] | None = None,
        shared_scope_id: str | None = None, **metadata: Any,
    ) -> str:
        self._log("store_episode", idempotency_key)
        self.store_calls += 1
        if self.store_delay_s:
            time.sleep(self.store_delay_s)
        if idempotency_key is not None and idempotency_key in self.stored:
            return self.stored[idempotency_key]  # 同 key 回原 episode_id，不重寫
        episode_id = f"ep{len(self.episodes) + 1:04d}"
        self.episodes.append(user_input)
        if idempotency_key is not None:
            self.stored[idempotency_key] = episode_id
        return episode_id

    def get_profile(self, max_chars: int = 800, limit: int = 30) -> str:
        return self.profile_text[:max_chars]

    def close(self, timeout: float = 5.0) -> None:
        """冪等：第二次起不動作、不記 call_log。"""
        if self.closed:
            return
        self.closed = True
        self.close_timeouts.append(timeout)
        self._log("close")

    # ---- 模擬記憶端 emitter ----

    def emit(self, draft: FakeDraft) -> None:
        assert self.sink is not None, "尚未注入 sink"
        self.sink.emit(draft)


class FakeGuildMembership:
    """``GuildMembership`` 的替身：``{guild_id: {actor_id: platform_uid}}``。"""

    def __init__(self, guilds: dict[str, dict[str, str]]) -> None:
        self._guilds = guilds

    def is_member(self, guild_id: str, actor_id: str) -> bool:
        return actor_id in self._guilds.get(guild_id, {})

    def members(self, guild_id: str) -> list[str]:
        return list(self._guilds.get(guild_id, {}).values())


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


class FakeMemoryAdapter:
    """``EchoMemoryAdapter`` 的替身（async 形狀）。寫入在 adapter 自己的單工作緒 executor 上轉呼叫 engine，
    並持有 Host 給的 engine 鎖（與真 adapter 的舊式 engine 全包鎖行為一致）。"""

    def __init__(self, engine: FakeMemoryEngine, engine_lock: threading.Lock | None) -> None:
        self.engine = engine
        self.engine_lock = engine_lock
        self.sessions: list[str | None] = []
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.scans: list[list[str]] = []
        self.store_results_none = False  # True：模擬 adapter 的 store 失敗（旁路回 None）
        self.closed = False
        self.unit_ids: list[str] = []

    def _locked(self, fn: Callable[[], Any]) -> Any:
        if self.engine_lock is None:
            return fn()
        with self.engine_lock:
            return fn()

    async def store(
        self, user_text: str, spoken_text: str, session_id: str | None = None,
        idempotency_key: str | None = None, **metadata: Any,
    ) -> dict[str, Any] | None:
        if self.store_results_none:
            return None
        loop = asyncio.get_running_loop()
        episode_id = await loop.run_in_executor(None, lambda: self._locked(lambda: self.engine.store_episode(
            user_input=user_text, agent_response=spoken_text, session_id=session_id or "unknown",
            idempotency_key=idempotency_key, **metadata,
        )))
        return {"episode_id": episode_id, "salience": 0.5, "unit_ids": list(self.unit_ids)}

    async def execute_tool(
        self, name: str, args: dict[str, Any], *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.tool_calls.append((name, dict(args)))
        return {"success": True, "result": f"{name} via adapter"}

    async def scan_privacy_intent(self, unit_ids: Any, *, timeout: float = 20.0, force: bool = False) -> None:
        self.scans.append(list(unit_ids))

    def set_session(self, session_id: str | None) -> None:
        self.sessions.append(session_id)

    async def aclose(self) -> None:
        self.closed = True
        self.engine.call_log.append((self.engine.namespace_id, "adapter.aclose", None))


class FakeAdapterFactory:
    """``MemoryHost(adapter_factory=...)`` 用：記錄每個 namespace 建過的 adapter（單一 adapter 約束由測試檢查）。"""

    def __init__(self) -> None:
        self.created: dict[str, list[FakeMemoryAdapter]] = {}

    def __call__(self, namespace_id: str, engine: Any, engine_lock: threading.Lock) -> FakeMemoryAdapter:
        adapter = FakeMemoryAdapter(engine, engine_lock)
        self.created.setdefault(namespace_id, []).append(adapter)
        return adapter
