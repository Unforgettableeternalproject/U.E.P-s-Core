"""記憶轉接：S2 同步 ``MemoryPort`` 的 asyncio 包裝，以及 ``GuildMembership`` Port 的實作。

- ``MemoryRetrieveBridge`` 滿足 ES 的 ``MemoryRetriever``（``async retrieve(text, **kw) -> str | None``）：只做
  asyncio 包裝（阻塞呼叫丟到 executor，ES loop 不被卡住），**不判斷授權**——授權全在 Access Layer。
  ``audience`` 與 ``actor_id`` 來自 ``Utterance.metadata["memory_retrieve_kwargs"]``（Core 在提交時寫入，
  ``actor_id`` 取自 Gateway 重建的身分、不取自 ``speaker_ref``，D77）。``audience`` 缺漏一律拒絕（回 None），
  不預設 public。
- ``GuildRoster`` 實作 ``GuildMembership``：成員資格由平台 Adapter 側提供（``set_members``），在場名單
  由 session 的 participant 事件維護（``join``／``leave``）；``present_uids`` 因此由 session 的 guild 推導，
  且只會是成員子集（共享層另有再裁一次）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import Executor
from typing import Any, cast

from uep.context.snapshot import MemoryStatus
from uep.memory.protocol import MemoryPort, RetrieveResult

logger = logging.getLogger("uep.echo.memory")

# 這些鍵由 Core 在提交時寫入 memory_retrieve_kwargs；其餘不認得的鍵一律不轉給 Access Layer
_FORWARDED = ("present_uids", "shared_turn", "session_id", "top_k", "min_similarity", "max_chars")


class MemoryRetrieveBridge:
    """per-scope 的 retriever 轉接物件（``memory_resolver(scope)`` 回傳）。"""

    def __init__(self, port: MemoryPort, scope: str | None, *, executor: Executor | None = None) -> None:
        self._port = port
        self.scope = scope
        self._executor = executor
        self.last_result: RetrieveResult | None = None

    async def retrieve(self, text: str, **kwargs: Any) -> str | None:
        audience = kwargs.get("audience")
        if not audience:
            logger.warning("記憶檢索缺少 audience：拒絕（不預設 public）")
            return None
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._executor, lambda: self._retrieve_sync(text, kwargs))
        self.last_result = result
        if not result.text:
            return None
        if result.memory_status is not MemoryStatus.OK:
            logger.info("記憶檢索降級：%s", result.memory_status.value)
        return result.text

    def _retrieve_sync(self, text: str, kwargs: Mapping[str, Any]) -> RetrieveResult:
        audience = str(kwargs["audience"])
        actor_id = kwargs.get("actor_id")
        extra = {k: kwargs[k] for k in _FORWARDED if k in kwargs}
        by_audience = getattr(self._port, "retrieve_for_audience", None)
        if callable(by_audience):  # MemoryAccessLayer：依 audience 列舉可讀來源並合併
            return cast(RetrieveResult, by_audience(text, audience, actor_id=actor_id, **extra))
        return self._port.retrieve(str(kwargs.get("scope") or self.scope or ""), text, audience, actor_id=actor_id, **extra)


def make_memory_resolver(
    port: MemoryPort, *, executor: Executor | None = None
) -> Callable[[str | None], MemoryRetrieveBridge | None]:
    """ES ``LLMThinkStage.memory_resolver``：scope（speaker_id）-> per-scope 轉接物件。"""
    cache: dict[str | None, MemoryRetrieveBridge] = {}
    lock = threading.Lock()

    def resolve(scope: str | None) -> MemoryRetrieveBridge | None:
        with lock:
            bridge = cache.get(scope)
            if bridge is None:
                bridge = cache[scope] = MemoryRetrieveBridge(port, scope, executor=executor)
            return bridge

    return resolve


class GuildRoster:
    """``GuildMembership`` 實作。``members`` 為 ``{guild_id: {actor_id: platform_uid}}``（平台 Adapter 提供）。"""

    def __init__(self, members: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self._lock = threading.Lock()
        self._members: dict[str, dict[str, str]] = {g: dict(m) for g, m in (members or {}).items()}
        self._present: dict[str, set[str]] = {}

    # ---- GuildMembership ----

    def is_member(self, guild_id: str, actor_id: str) -> bool:
        with self._lock:
            return actor_id in self._members.get(guild_id, {})

    def members(self, guild_id: str) -> Collection[str]:
        with self._lock:
            return list(self._members.get(guild_id, {}).values())

    # ---- 平台 Adapter 與 session ----

    def set_members(self, guild_id: str, members: Mapping[str, str]) -> None:
        with self._lock:
            self._members[guild_id] = dict(members)
            present = self._present.get(guild_id)
            if present is not None:
                present.intersection_update(self._members[guild_id].values())

    def join(self, guild_id: str, platform_uid: str) -> None:
        with self._lock:
            self._present.setdefault(guild_id, set()).add(platform_uid)

    def leave(self, guild_id: str, platform_uid: str) -> None:
        with self._lock:
            self._present.get(guild_id, set()).discard(platform_uid)

    def present_uids(self, guild_id: str) -> list[str]:
        """目前在場（已 join 且仍是成員）的平台使用者 id；非成員不會出現。"""
        with self._lock:
            members = set(self._members.get(guild_id, {}).values())
            return sorted(self._present.get(guild_id, set()) & members)
