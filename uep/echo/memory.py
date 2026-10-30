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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RosterEntry:
    """``GuildRoster.roster`` 的一筆：平台使用者 id、顯示名、別名（顯示名與別名都由平台轉接器提供）。"""

    uid: str
    display_name: str = ""
    aliases: tuple[str, ...] = ()


def _norm_name(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


class GuildRoster:
    """``GuildMembership`` 實作。``members`` 為 ``{guild_id: {actor_id: platform_uid}}``（平台 Adapter 提供）。

    成員集合 = 轉接器經 ``set_members`` 提供的集合（可為 ``guild.members`` 與 bot PeopleStore 的聯集，Core 不讀
    PeopleStore）；``is_member``、``roster`` 都以它為準。
    顯示名與別名（D87 前置）同樣由平台轉接器經 ``set_members(profiles=...)`` 提供，Core 不自行維護；
    ``roster`` 回報每位成員的 uid、顯示名、別名，``resolve_member`` 供 ``member_recall`` 把名稱解析成 uid。
    """

    def __init__(
        self, members: Mapping[str, Mapping[str, str]] | None = None,
        profiles: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._members: dict[str, dict[str, str]] = {g: dict(m) for g, m in (members or {}).items()}
        self._profiles: dict[str, dict[str, RosterEntry]] = {}
        for guild_id, table in (profiles or {}).items():
            self._profiles[guild_id] = self._build_profiles(self._members.get(guild_id, {}), table)
        self._present: dict[str, set[str]] = {}

    @staticmethod
    def _build_profiles(
        members: Mapping[str, str], table: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, RosterEntry]:
        """``table`` 以平台 uid 為鍵：``{"display_name": str, "aliases": [str, ...]}``；非成員的項目丟棄。"""
        uids = set(members.values())
        entries: dict[str, RosterEntry] = {}
        for uid, raw in table.items():
            if uid not in uids:
                continue
            aliases = raw.get("aliases") or ()
            if isinstance(aliases, str):
                aliases = (aliases,)
            entries[uid] = RosterEntry(
                uid, " ".join(str(raw.get("display_name") or "").split()),
                tuple(" ".join(str(a).split()) for a in aliases if str(a).strip()),
            )
        return entries

    # ---- GuildMembership ----

    def is_member(self, guild_id: str, actor_id: str) -> bool:
        with self._lock:
            return actor_id in self._members.get(guild_id, {})

    def members(self, guild_id: str) -> Collection[str]:
        with self._lock:
            return list(self._members.get(guild_id, {}).values())

    # ---- 平台 Adapter 與 session ----

    def set_members(
        self, guild_id: str, members: Mapping[str, str], profiles: Mapping[str, Mapping[str, Any]] | None = None
    ) -> None:
        """整批取代該場域的成員（``{actor_id: platform_uid}``）；``profiles`` 以平台 uid 為鍵，提供顯示名與別名。"""
        with self._lock:
            self._members[guild_id] = dict(members)
            self._profiles[guild_id] = self._build_profiles(self._members[guild_id], profiles or {})
            present = self._present.get(guild_id)
            if present is not None:
                present.intersection_update(self._members[guild_id].values())

    def roster(self, guild_id: str) -> list[RosterEntry]:
        """每位成員的 uid、顯示名、別名（依 uid 排序）；沒有提供檔案的成員顯示名為空、別名為空元組。"""
        with self._lock:
            uids = sorted(set(self._members.get(guild_id, {}).values()))
            table = self._profiles.get(guild_id, {})
            return [table.get(uid) or RosterEntry(uid) for uid in uids]

    def resolve_member(self, guild_id: str, name: str) -> str | None:
        """名稱（顯示名或別名，忽略大小寫與多餘空白）-> 平台 uid；查無或不只一位符合回 None（不猜）。"""
        key = _norm_name(name)
        if not key:
            return None
        hits = {
            e.uid for e in self.roster(guild_id)
            if key == _norm_name(e.display_name) or any(key == _norm_name(a) for a in e.aliases)
        }
        return next(iter(hits)) if len(hits) == 1 else None

    def display_name(self, guild_id: str, uid: str) -> str:
        with self._lock:
            entry = self._profiles.get(guild_id, {}).get(uid)
            return entry.display_name if entry is not None else ""

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
