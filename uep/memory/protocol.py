"""MemoryHost 的型別邊界：Core 只認這些 Protocol，不 import echo_memory（真 engine 需 3.12 環境，S6 才接）。

``MemoryEngineLike`` 照記憶端 ``MemoryEngine`` 的真實簽名取 S2 用到的子集（見 S2 記憶介面清單）；
``MemoryPort`` 是 Core 對 ES 暴露的同步版介面（Core 為同步 SQLAlchemy 加執行緒；asyncio 包裝由 S3 的
``uep/echo/`` 轉接層負責，P3接入方案 §1.5(e)）。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from uep.context.snapshot import MemoryStatus


@runtime_checkable
class MemoryEngineLike(Protocol):
    """真 ``MemoryEngine`` 的子集。回傳型別刻意寬鬆（duck typing）：``retrieve`` 回物件需有
    ``to_text()``／``is_empty()``／``degraded``／``degraded_reason``。``close`` 為可選（真 engine 目前沒有）。"""

    namespace_id: str
    storage_path: Path

    def set_llm_fn(self, llm_fn: Callable[[str], str] | None) -> None: ...

    def set_concept_inhibitor(self, inhibitor: Any) -> None: ...

    def set_candidate_sink(self, sink: Any) -> None: ...

    def start_dream_scheduler(self) -> dict[str, Any]: ...

    def stop_dream_scheduler(self) -> None: ...

    def dream_readiness(self) -> dict[str, Any]: ...

    def dream(self, triggered_by: str = "manual") -> dict[str, Any]: ...

    def retrieve(
        self, query: str, dopamine: float | None = None, serotonin: float | None = None, top_k: int = 10,
        min_similarity: float | None = None, exclude_session_id: str | None = None,
        exclude_episode_ids: set[str] | None = None, include_history: bool = False,
        include_hypotheses: bool = False, shared_turn: bool = False,
    ) -> Any: ...

    def retrieve_shared(
        self, query: str, *, scope_id: str, viewer_uid: str, present_uids: Sequence[Any] = (),
        top_k: int | None = None, min_similarity: float | None = None,
    ) -> Any: ...

    def store_episode(
        self, user_input: str, agent_response: str, session_id: str, content_summary: str | None = None,
        salience: float = 0.5, da_weight: float | None = None, ht_weight: float | None = None,
        *, evidence: Sequence[Any] | None = None, episode_kind: str = "turn",
        idempotency_key: str | None = None, shared_context_ids: Sequence[str] | None = None,
        shared_scope_id: str | None = None, **metadata: Any,
    ) -> str: ...

    def get_profile(self, max_chars: int = 800, limit: int = 30) -> str: ...


# 檢索結果的嚴重度排序（合併多個來源時取最差者）
_SEVERITY = {
    MemoryStatus.OK: 0, MemoryStatus.DEGRADED_NO_MEMORY: 1, MemoryStatus.TIMEOUT: 2, MemoryStatus.UNAVAILABLE: 3,
}


def worst_status(*statuses: MemoryStatus) -> MemoryStatus:
    return max(statuses, key=_SEVERITY.__getitem__, default=MemoryStatus.OK)


@dataclass(frozen=True)
class RetrieveResult:
    """Access Layer 的檢索結果。``excluded_summary`` 的鍵只能是原因碼、值為被排除的來源數（X-9，不含內容）；
    ``text`` 為空且 ``memory_status != ok`` 時，呼叫端不得宣稱記得特定事實（X-6）。"""

    text: str
    memory_status: MemoryStatus = MemoryStatus.OK
    excluded_summary: Mapping[str, int] = field(default_factory=dict)
    degraded_reason: str = ""
    shared_ids: tuple[str, ...] = ()  # 共享層命中的投影 id，回覆後 store 要帶 shared_context_ids


@dataclass(frozen=True)
class StoreResult:
    status: str  # stored | rejected | failed | timeout
    episode_id: str | None = None
    reason: str = ""
    event_duplicate: bool | None = None  # memory.record_created 是否為重送；None 表示沒有發布事件


@runtime_checkable
class MemoryPort(Protocol):
    """ES 端消費的記憶介面（同步版）。身分以 Gateway 重建的 ``actor_id`` 為準，不收 payload 的 speaker_ref。"""

    def retrieve(self, scope: str, query: str, audience: str, **kwargs: Any) -> RetrieveResult: ...

    def store(
        self, *, actor_id: str | None, user_text: str, spoken_text: str, session_id: str | None = None,
        idempotency_key: str | None = None, **metadata: Any,
    ) -> StoreResult: ...

    def profile(self, *, actor_id: str | None, audience: str, max_chars: int = 800) -> str: ...

    def set_session(self, session_id: str | None) -> None: ...

    def tool_definitions(self) -> list[dict[str, Any]]: ...

    def execute_tool(
        self, name: str, args: Mapping[str, Any], *, actor_id: str | None, audience: str,
        session_id: str | None = None,
    ) -> dict[str, Any]: ...
