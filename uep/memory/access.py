"""Memory Access Layer（P3接入方案 §3.1）：實作 ES 的 ``MemoryPort``（同步版）。

真 engine 的 ``retrieve`` 不收 audience，內容範圍由 engine 所屬 namespace 決定：
- owner 的 engine = ``scope_private_<owner_id>``；共享 engine ``scope-g<gid>`` = ``scope_group_g<gid>``。
- 每次讀取先以 ``scope_admits(audience, 來源 scope)`` 判斷（CC-2 X-1、VC-2-1）：不通過就**不呼叫 engine**，
  結果只帶 ``excluded_summary``（鍵為原因碼，值為被排除的來源數，不含內容；X-9）。
- 身分只認 Gateway 重建的 ``actor_id``（D77）；映射缺失一律訪客，訪客讀不到、也寫不進任何 owner 的記憶。
- 寫入（store／force_remember）走該 engine 唯一的 ``EchoMemoryAdapter``（EM #619）：salience 評估、evidence 準備、
  idempotency 都在 adapter 內，回覆後再 fire-and-forget 跑隱私意圖掃描；讀取直接呼叫 engine。
- 逾時有界（X-6）：逾時回 ``memory_status=timeout`` 與空文字，不回傳可能不完整的內容。
"""
from __future__ import annotations

import inspect
import logging
import re
import threading
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from typing import Any, Protocol

from uep.context.snapshot import MemoryStatus, scope_admits
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.publisher import EventPublisher
from uep.memory.identity import IdentityDirectory, ResolvedIdentity
from uep.memory.metrics import MemoryMetrics
from uep.memory.namespaces import (
    group_scope_ref,
    guild_id_of_group_scope,
    owner_scope_ref,
)
from uep.memory.protocol import (
    MemoryAdapterLike,
    MemoryEngineLike,
    RetrieveResult,
    StoreResult,
    worst_status,
)
from uep.memory.sink import MEMORY_ACTOR, MEMORY_SOURCE

logger = logging.getLogger("uep.memory.access")

DEFAULT_RETRIEVE_TIMEOUT_S = 3.0
DEFAULT_STORE_TIMEOUT_S = 10.0
_ALLOWED_ENGINE_KWARGS = frozenset({"include_history", "include_hypotheses"})
_KEY_SAFE = re.compile(r"[A-Za-z0-9_:.\-]{1,200}")
_PRIVATE_SCOPE = re.compile(r"scope_private_([A-Za-z0-9]+)")

# D79（艾斯維爾 2026-10-30 裁決）：ES 內建記憶工具由 Host 白名單放行。recall 類為 internal_read、remember 為
# reversible_local；執行後由轉接層事後補記 action.* 事件（payload 不放原文）。Part S S3 之後改走 ActionIntent。
TOOL_CLASSES: Mapping[str, str] = {
    "deep_recall": "internal_read", "force_remember": "reversible_local", "member_recall": "internal_read",
}
MEMBER_RECALL = "member_recall"
# 與 ES 的 MEMBER_RECALL_SCHEMA 對齊（echo_stream.adapters.memory_echo，e31eac0；不 import）。真 echo_memory 的工具宣告
# 沒有 member_recall，所以由 Access Layer 自己提供 schema 並直接實作（不走通用 tool backend）。
MEMBER_RECALL_SCHEMA: Mapping[str, Any] = {
    "name": MEMBER_RECALL,
    "description": (
        "查詢同一個伺服器裡某位成員在共享記憶中可被看到的內容（例如使用者問「你認識某某嗎？"
        "他最近在玩什麼？」）。member 填成員的名稱；query 可選，填想了解的主題，留空則取最近的內容。"
        "只在使用者明確問到特定成員時呼叫；查不到只代表目前沒有可用的共享內容。"
        "Look up what is shared about one named member of this server; call only when the "
        "user asks about that member."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "member": {"type": "string", "description": "成員名稱（顯示名稱或暱稱）。"},
            "query": {"type": "string", "description": "想了解的主題，可留空。"},
        },
        "required": ["member"],
    },
}
_UNKNOWN_MEMBER_LABEL = "這位成員"
_MEMBER_LABEL_MAX = 40


class MemoryRuntime(Protocol):
    """Access Layer 需要 Host 提供的能力（避免循環依賴 MemoryHost）。"""

    def namespace_for_owner(self, owner_id: str) -> str | None: ...

    def namespace_for_guild(self, guild_id: str) -> str | None: ...

    def call(
        self, namespace_id: str, fn: Callable[[MemoryEngineLike], Any], *, timeout_s: float,
        on_late: Callable[[Any], None] | None = None,
    ) -> Any:
        """在該 namespace 的 engine 鎖內、有界時間執行；逾時拋 ``TimeoutError``。
        逾時時已在執行、之後才完成的呼叫，結果交給 ``on_late``。"""
        ...

    def adapter(self, namespace_id: str) -> MemoryAdapterLike | None:
        """該 namespace 唯一的 adapter；沒有設定時為 None（寫入退回直接呼叫 engine，僅供沒有 adapter 的最小組裝）。"""
        ...

    def adapter_call(
        self, namespace_id: str, fn: Callable[[MemoryAdapterLike], Awaitable[Any]], *, timeout_s: float,
        on_late: Callable[[Any], None] | None = None,
    ) -> Any: ...

    def adapter_fire(self, namespace_id: str, fn: Callable[[MemoryAdapterLike], Awaitable[Any]]) -> None: ...


class GuildMembership(Protocol):
    """場域（guild）成員資格 Port（fail-closed）：共享層讀取一律要求 viewer 為成員。

    S3 必須提供實作（平台成員資格來源在 Adapter 側）；沒注入時所有 group scope 讀取一律拒絕。
    ``members`` 回傳該場域成員的平台使用者 id（與身分映射的 ``platform_uid`` 同一格式），
    ``present_uids`` 只會被裁成它的子集。

    選用 ``display_name(guild_id, uid) -> str``（``GuildRoster`` 有）：共享脈絡渲染時把 uid 換成顯示名；沒有或查無
    一律用泛稱，**不得把原始 uid 帶進 prompt**。

    成員集合的定義（Bernie 裁決）：由平台轉接器經 ``GuildRoster.set_members`` 提供。bot 的人員名單
    （PeopleStore／stt_people）裡的人預設視為伺服器成員，轉接器可交給 Core ``guild.members`` 與 PeopleStore 的聯集；
    Core 不自己讀 PeopleStore。``is_member``、``members``、``roster`` 都以這份集合為準。"""

    def is_member(self, guild_id: str, actor_id: str) -> bool: ...

    def members(self, guild_id: str) -> Collection[str]: ...


class MemoryToolBackend(Protocol):
    """記憶工具（deep_recall／force_remember）的後端，由宿主依真 engine 的 llm_integration 實作
    （S6：``uep/echo/memory_wiring.py`` 的 ``EchoMemoryToolBackend``）。有 adapter 時只用 ``definitions()``，
    執行改走 ``adapter.execute_tool``（寫入唯一經 adapter）；``execute`` 只在沒有 adapter 的最小組裝使用。
    ``member_recall`` 不在這裡：真 engine 的工具宣告沒有它，由 Access Layer 直接以 ``retrieve_shared`` 實作。"""

    def definitions(self) -> list[dict[str, Any]]: ...

    def execute(
        self, engine: MemoryEngineLike, name: str, args: Mapping[str, Any], session_id: str
    ) -> dict[str, Any]: ...


def _denied(code: str) -> RetrieveResult:
    return RetrieveResult(text="", memory_status=MemoryStatus.OK, excluded_summary={code: 1})


class MemoryAccessLayer:
    def __init__(
        self,
        runtime: MemoryRuntime,
        identity: IdentityDirectory,
        *,
        publisher: EventPublisher | None = None,
        metrics: MemoryMetrics | None = None,
        tool_backend: MemoryToolBackend | None = None,
        guild_membership: GuildMembership | None = None,
        retrieve_timeout_s: float = DEFAULT_RETRIEVE_TIMEOUT_S,
        store_timeout_s: float = DEFAULT_STORE_TIMEOUT_S,
    ) -> None:
        self._runtime = runtime
        self._identity = identity
        self._publisher = publisher
        self._metrics = metrics or MemoryMetrics()
        self._tool_backend = tool_backend
        self._membership = guild_membership
        self._retrieve_timeout_s = retrieve_timeout_s
        self._store_timeout_s = store_timeout_s
        self._session_id: str | None = None
        self._session_lock = threading.Lock()

    # ---- session ----

    def set_session(self, session_id: str | None) -> None:
        with self._session_lock:
            self._session_id = session_id

    def _current_session(self, explicit: str | None) -> str:
        if explicit:
            return explicit
        with self._session_lock:
            return self._session_id or "unknown"

    # ---- retrieve ----

    def retrieve(
        self,
        scope: str,
        query: str,
        audience: str,
        *,
        actor_id: str | None = None,
        session_id: str | None = None,
        top_k: int = 3,
        min_similarity: float | None = None,
        timeout_s: float | None = None,
        max_chars: int | None = None,
        present_uids: Sequence[Any] = (),
        shared_turn: bool = False,
        **engine_kwargs: Any,
    ) -> RetrieveResult:
        """讀取 ``scope`` 這個來源範圍的記憶，並以 ``audience``（本次聽眾範圍）過濾。"""
        unknown = set(engine_kwargs) - _ALLOWED_ENGINE_KWARGS
        if unknown:
            raise TypeError(f"不支援的檢索參數：{sorted(unknown)}")
        if not query or not query.strip():
            return RetrieveResult(text="")
        admitted = self._admit(scope, audience, actor_id)
        if isinstance(admitted, RetrieveResult):
            if admitted.excluded_summary:
                self._metrics.inc("memory.retrieve.denied")
            return admitted
        ident, namespace_id = admitted
        timeout = self._retrieve_timeout_s if timeout_s is None else timeout_s
        guild_id = guild_id_of_group_scope(scope)
        try:
            if guild_id is not None:
                return self._retrieve_shared(
                    namespace_id, query, guild_id, ident, present_uids, timeout, max_chars
                )
            return self._retrieve_private(
                namespace_id, query, session_id, top_k, min_similarity, timeout, max_chars,
                shared_turn, engine_kwargs,
            )
        except TimeoutError:
            self._metrics.inc("memory.retrieve.timeout")
            return RetrieveResult(text="", memory_status=MemoryStatus.TIMEOUT, degraded_reason="timeout")
        except Exception as exc:  # noqa: BLE001 - 對外降級；只記例外類別，不記內容
            self._metrics.inc("memory.retrieve.unavailable")
            logger.warning("記憶檢索失敗（回報 unavailable）：%s", type(exc).__name__)
            return RetrieveResult(
                text="", memory_status=MemoryStatus.UNAVAILABLE, degraded_reason=type(exc).__name__
            )

    def retrieve_for_audience(
        self, query: str, audience: str, *, actor_id: str | None, **kwargs: Any
    ) -> RetrieveResult:
        """依 audience 列舉可讀來源（actor 自己的私人記憶、audience 為場域時再加共享層）並合併。

        私人來源在 public／group audience 下被 ``scope_admits`` 擋掉，只留 ``excluded_summary`` 計數。"""
        ident = self._identity.resolve_actor(actor_id)
        scopes: list[str] = []
        if ident.owner_id is not None:
            scopes.append(owner_scope_ref(ident.owner_id))
        guild = guild_id_of_group_scope(audience)
        if guild is not None:
            scopes.append(group_scope_ref(guild))
        if not scopes:
            self._metrics.inc("memory.retrieve.denied")
            return _denied("guest")
        results = [self.retrieve(s, query, audience, actor_id=actor_id, **kwargs) for s in scopes]
        excluded: Counter[str] = Counter()
        for r in results:
            excluded.update(r.excluded_summary)
        return RetrieveResult(
            text="\n".join(r.text for r in results if r.text),
            memory_status=worst_status(*(r.memory_status for r in results)),
            excluded_summary=dict(excluded),
            degraded_reason=next((r.degraded_reason for r in results if r.degraded_reason), ""),
            shared_ids=tuple(i for r in results for i in r.shared_ids),
        )

    def _admit(
        self, scope: str, audience: str, actor_id: str | None
    ) -> RetrieveResult | tuple[ResolvedIdentity, str]:
        """通過回 (身分, namespace)；被擋回帶原因碼的空結果（不碰 engine）。"""
        guild_id = guild_id_of_group_scope(scope)
        private = _PRIVATE_SCOPE.fullmatch(scope or "")
        if guild_id is None and private is None:
            # scope_public：P3 沒有公開記憶存放處；其餘格式視為無效
            return RetrieveResult(text="") if scope == "scope_public" else _denied("unknown_scope")
        try:
            allowed = scope_admits(audience, scope)
        except ValueError:
            return _denied("bad_audience")
        if not allowed:
            return _denied("scope_denied")
        ident = self._identity.resolve_actor(actor_id)
        if ident.owner_id is None:
            return _denied("guest")
        if private is not None:
            if ident.owner_id != private.group(1):
                return _denied("not_owner")
            namespace_id = self._runtime.namespace_for_owner(ident.owner_id)
        else:
            assert guild_id is not None
            if self._membership is None:
                return _denied("no_membership")  # fail-closed：沒有成員資格 Port 就不讀共享層
            if not ident.platform_uid or ident.actor_id is None:
                return _denied("no_viewer")
            if not self._membership.is_member(guild_id, ident.actor_id):
                return _denied("not_member")
            namespace_id = self._runtime.namespace_for_guild(guild_id)
        if namespace_id is None:
            return _denied("no_engine")
        return ident, namespace_id

    def _retrieve_private(
        self, namespace_id: str, query: str, session_id: str | None, top_k: int,
        min_similarity: float | None, timeout: float, max_chars: int | None, shared_turn: bool,
        engine_kwargs: Mapping[str, Any],
    ) -> RetrieveResult:
        sid = self._current_session(session_id)

        def work(engine: MemoryEngineLike) -> Any:
            return engine.retrieve(
                query, top_k=top_k, min_similarity=min_similarity,
                exclude_session_id=None if sid == "unknown" else sid, shared_turn=shared_turn,
                **engine_kwargs,
            )

        context = self._runtime.call(namespace_id, work, timeout_s=timeout)
        degraded = getattr(context, "degraded", False) is True
        reason = str(getattr(context, "degraded_reason", "") or "") if degraded else ""
        text = "" if context.is_empty() else _clip(context.to_text(), max_chars)
        return RetrieveResult(
            text=text,
            memory_status=MemoryStatus.DEGRADED_NO_MEMORY if degraded else MemoryStatus.OK,
            degraded_reason=reason,
        )

    def _retrieve_shared(
        self, namespace_id: str, query: str, guild_id: str, ident: ResolvedIdentity,
        present_uids: Sequence[Any], timeout: float, max_chars: int | None,
    ) -> RetrieveResult:
        viewer = ident.platform_uid or ""
        assert self._membership is not None
        members = {str(m) for m in self._membership.members(guild_id)}
        present = tuple(u for u in present_uids if str(u) in members)  # 裁成成員子集；非成員不夾帶進共享層排序
        if len(present) != len(tuple(present_uids)):
            self._metrics.inc("memory.retrieve.present_clipped")

        def work(engine: MemoryEngineLike) -> Any:
            return engine.retrieve_shared(
                query, scope_id=f"guild:{guild_id}", viewer_uid=viewer, present_uids=present
            )

        result = self._runtime.call(namespace_id, work, timeout_s=timeout)
        degraded = bool(getattr(result, "degraded", False))
        return RetrieveResult(
            text=_clip(result.to_text(self._display_name_fn(guild_id)) or "", max_chars),
            memory_status=MemoryStatus.DEGRADED_NO_MEMORY if degraded else MemoryStatus.OK,
            degraded_reason=str(getattr(result, "degraded_reason", "") or "") if degraded else "",
            shared_ids=tuple(getattr(result, "ids", ()) or ()),
        )

    # ---- store ----

    def store(
        self,
        *,
        actor_id: str | None,
        user_text: str,
        spoken_text: str,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        timeout_s: float | None = None,
        **metadata: Any,
    ) -> StoreResult:
        """寫入 actor 自己 owner 的 engine；訪客與沒有 engine 的 owner 一律拒絕。成功後發 ``memory.record_created``。"""
        if not user_text or not user_text.strip():
            return StoreResult(status="rejected", reason="empty")
        ident = self._identity.resolve_actor(actor_id)
        if ident.owner_id is None:
            self._metrics.inc("memory.store.rejected.guest")
            return StoreResult(status="rejected", reason="guest")
        owner_id = ident.owner_id
        namespace_id = self._runtime.namespace_for_owner(owner_id)
        if namespace_id is None:
            self._metrics.inc("memory.store.rejected.no_engine")
            return StoreResult(status="rejected", reason="no_engine")
        sid = self._current_session(session_id)

        adapter_mode = self._runtime.adapter(namespace_id) is not None
        unit_ids: list[str] = []

        def work(engine: MemoryEngineLike) -> str:
            return engine.store_episode(
                user_input=user_text, agent_response=spoken_text, session_id=sid,
                idempotency_key=idempotency_key, **metadata,
            )

        async def adapter_work(adapter: MemoryAdapterLike) -> dict[str, Any] | None:
            return await adapter.store(
                user_text, spoken_text, session_id=sid, idempotency_key=idempotency_key, **metadata
            )

        def late(result: Any) -> None:
            # 呼叫端已回報逾時、但寫入之後才完成：補發 record_created（key 為 None 時重送可能產生重複 episode）
            episode = result.get("episode_id") if isinstance(result, Mapping) else result
            if episode:
                self._publish_record_created(namespace_id, owner_id, str(episode))

        timeout = self._store_timeout_s if timeout_s is None else timeout_s
        try:
            if adapter_mode:
                result = self._runtime.adapter_call(namespace_id, adapter_work, timeout_s=timeout, on_late=late)
                if not isinstance(result, Mapping) or not result.get("episode_id"):
                    # adapter 的 store 是旁路：失敗吞例外回 None
                    self._metrics.inc("memory.store.failed")
                    return StoreResult(status="failed", reason="adapter_store_failed")
                episode_id = result["episode_id"]
                unit_ids = [str(u) for u in (result.get("unit_ids") or ())]
            else:
                episode_id = self._runtime.call(namespace_id, work, timeout_s=timeout, on_late=late)
        except TimeoutError:
            self._metrics.inc("memory.store.timeout")
            return StoreResult(status="timeout", reason="timeout")
        except Exception as exc:  # noqa: BLE001 - 對外降級；只記例外類別，不記內容
            self._metrics.inc("memory.store.failed")
            logger.warning("記憶寫入失敗（不影響對話）：%s", type(exc).__name__)
            return StoreResult(status="failed", reason=type(exc).__name__)
        self._metrics.inc("memory.store.stored")
        duplicate = self._publish_record_created(namespace_id, owner_id, str(episode_id))
        if unit_ids:
            self._scan_privacy(namespace_id, unit_ids)
        return StoreResult(status="stored", episode_id=str(episode_id), event_duplicate=duplicate)

    def _scan_privacy(self, namespace_id: str, unit_ids: Sequence[str]) -> None:
        """回覆後的隱私意圖掃描（熱路徑外、fire-and-forget；adapter 沒有此方法或失敗都只記 log）。"""
        ids = list(unit_ids)

        async def scan(adapter: MemoryAdapterLike) -> Any:
            fn = getattr(adapter, "scan_privacy_intent", None)
            return await fn(ids) if callable(fn) else None

        self._metrics.inc("memory.store.privacy_scan")
        self._runtime.adapter_fire(namespace_id, scan)

    def _publish_record_created(self, namespace_id: str, owner_id: str, episode_id: str) -> bool | None:
        """只帶 ID（ref_id）；同 episode 重送得同一把 key -> 重複。失敗只記 log 與計數。"""
        if self._publisher is None:
            return None
        key = f"memstore:{namespace_id}:{episode_id}"
        if not _KEY_SAFE.fullmatch(key):
            self._metrics.inc("memory.store.event_skipped")
            logger.warning("episode id 含不合規字元，略過 memory.record_created")
            return None
        try:
            recorded = self._publisher.publish(
                EventDraft(event_type="memory.record_created", idempotency_key=key, payload={"ref_id": episode_id}),
                TrustedContext(
                    source=MEMORY_SOURCE, actor_id=MEMORY_ACTOR, scope_ref=owner_scope_ref(owner_id), external=True
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 事件失敗不影響寫入；只記例外類別
            self._metrics.inc("memory.store.event_failed")
            logger.warning("memory.record_created 發布失敗（已忽略）：%s", type(exc).__name__)
            return None
        return recorded.duplicate

    # ---- profile／工具 ----

    def profile(self, *, actor_id: str | None, audience: str, max_chars: int = 800) -> str:
        """owner 的常駐 profile；audience 不能看 owner 私人範圍（或訪客）時回空字串。"""
        ident = self._identity.resolve_actor(actor_id)
        if ident.owner_id is None:
            return ""
        try:
            if not scope_admits(audience, owner_scope_ref(ident.owner_id)):
                self._metrics.inc("memory.profile.denied")
                return ""
        except ValueError:
            return ""
        namespace_id = self._runtime.namespace_for_owner(ident.owner_id)
        if namespace_id is None:
            return ""
        try:
            return str(self._runtime.call(
                namespace_id, lambda e: e.get_profile(max_chars=max_chars) or "",
                timeout_s=self._retrieve_timeout_s,
            ))
        except Exception as exc:  # noqa: BLE001 - 對外降級；只記例外類別，不記內容
            logger.warning("profile 讀取失敗（回空）：%s", type(exc).__name__)
            return ""

    def tool_definitions(self) -> list[dict[str, Any]]:
        """白名單內的工具宣告：後端提供的 deep_recall／force_remember，加上 Access Layer 自己的 member_recall
        （需要成員資格 Port 才曝光；執行時仍逐次 fail-closed）。"""
        defs: list[dict[str, Any]] = []
        if self._tool_backend is not None:
            defs = [
                d for d in self._tool_backend.definitions()
                if _tool_name(d) in TOOL_CLASSES and _tool_name(d) != MEMBER_RECALL
            ]
        if self._membership is not None:
            defs.append({"type": "function", "function": dict(MEMBER_RECALL_SCHEMA)})
        return defs

    def execute_tool(
        self, name: str, args: Mapping[str, Any], *, actor_id: str | None, audience: str,
        session_id: str | None = None, context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """白名單再擋一次；audience 不能看就拒絕。失敗結果帶 ``code``（denied／timeout／failed）供稽核，成功結果不變。

        ``deep_recall``／``force_remember`` 碰 owner 私人範圍，引數依後端宣告的 schema 白名單過濾；``member_recall``
        碰共享層（場域成員記憶），由這裡直接以 ``retrieve_shared(owner_uid=...)`` 實作，只認 ``context`` 內 Core 寫入的
        ``guild_id``／``member_uid``（已由轉接層用 roster 解析）／``present_uids``，LLM 給的 ``args`` 不能指定範圍；
        檢查沿用 ``_admit``（audience、成員資格 fail-closed）。"""
        if name not in TOOL_CLASSES:
            return _tool_denied(name)
        if name == MEMBER_RECALL:
            return self._execute_member_recall(args, actor_id, audience, session_id, context)
        if self._tool_backend is None:
            return _tool_denied(name)
        ident = self._identity.resolve_actor(actor_id)
        if ident.owner_id is None:
            return _tool_denied(name)
        try:
            if not scope_admits(audience, owner_scope_ref(ident.owner_id)):
                return _tool_denied(name)
        except ValueError:
            return _tool_denied(name)
        namespace_id = self._runtime.namespace_for_owner(ident.owner_id)
        if namespace_id is None:
            return _tool_denied(name)
        return self._run_tool(namespace_id, name, self._allowed_args(name, args), self._current_session(session_id))

    def _allowed_args(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """依後端宣告的 schema（``parameters.properties``）過濾 LLM 給的引數；沒有宣告任何參數就一個都不傳。"""
        backend = self._tool_backend
        props: Mapping[str, Any] = {}
        if backend is not None:
            for d in backend.definitions():
                if _tool_name(d) != name:
                    continue
                fn = d.get("function") if isinstance(d.get("function"), Mapping) else d
                params = fn.get("parameters") if isinstance(fn, Mapping) else None
                declared = params.get("properties") if isinstance(params, Mapping) else None
                props = declared if isinstance(declared, Mapping) else {}
                break
        return {k: v for k, v in args.items() if k in props}

    def _run_tool(self, namespace_id: str, name: str, args: Mapping[str, Any], sid: str) -> dict[str, Any]:
        backend = self._tool_backend
        assert backend is not None
        try:
            if self._runtime.adapter(namespace_id) is not None:
                async def via_adapter(adapter: MemoryAdapterLike) -> dict[str, Any]:
                    adapter.set_session(sid)  # adapter 的 ToolExecutor 綁 session_id
                    return await adapter.execute_tool(name, dict(args))

                return dict(self._runtime.adapter_call(namespace_id, via_adapter, timeout_s=self._store_timeout_s))
            return dict(self._runtime.call(
                namespace_id, lambda e: backend.execute(e, name, args, sid), timeout_s=self._store_timeout_s,
            ))
        except TimeoutError:
            self._metrics.inc("memory.tool.timeout")
            return {"success": False, "result": "tool timeout", "code": "timeout"}
        except Exception as exc:  # noqa: BLE001 - 對外降級；只記例外類別，不記內容
            logger.warning("記憶工具執行失敗：%s %s", name, type(exc).__name__)
            return {"success": False, "result": "tool failed", "code": "failed"}

    def _display_name_fn(self, guild_id: str) -> Callable[[str], str]:
        """uid -> 顯示名；查無或沒有名冊來源一律回泛稱（原始 uid 不得進 prompt）。"""
        source = getattr(self._membership, "display_name", None)

        def resolve(uid: str) -> str:
            if callable(source):
                try:
                    label = " ".join(str(source(guild_id, str(uid)) or "").split())
                except Exception:  # noqa: BLE001 - 名稱只是顯示
                    label = ""
                if label:
                    return label[:_MEMBER_LABEL_MAX]
            return _UNKNOWN_MEMBER_LABEL

        return resolve

    def _execute_member_recall(
        self, args: Mapping[str, Any], actor_id: str | None, audience: str, session_id: str | None,
        context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        ctx = context or {}
        guild_id, member_uid = ctx.get("guild_id"), ctx.get("member_uid")
        if not isinstance(guild_id, str) or not guild_id or not isinstance(member_uid, str) or not member_uid:
            return _tool_denied(MEMBER_RECALL)
        admitted = self._admit(group_scope_ref(guild_id), audience, actor_id)
        if isinstance(admitted, RetrieveResult):
            return _tool_denied(MEMBER_RECALL)
        ident, namespace_id = admitted
        assert self._membership is not None  # _admit 已確認有成員資格 Port
        members = {str(m) for m in self._membership.members(guild_id)}
        if member_uid not in members:
            return _tool_denied(MEMBER_RECALL)  # 目標不是這個場域的成員：不碰 engine
        present = tuple(str(u) for u in (ctx.get("present_uids") or ()) if str(u) in members)
        viewer = ident.platform_uid or ""
        query = str(args.get("query") or "").strip()
        display_name = self._display_name_fn(guild_id)
        label = display_name(member_uid)

        def work(engine: MemoryEngineLike) -> dict[str, Any]:
            fn = getattr(engine, "retrieve_shared", None)
            if fn is None:
                return {"success": False, "result": "tool failed", "code": "failed"}
            if not _declares_param(fn, "owner_uid"):
                # 簽名保險：engine 不認得 owner_uid 時回空，不可退化成不篩選的全體查詢（會撈出別人的投影）
                self._metrics.inc("memory.member_recall.no_owner_uid")
                return {"success": True, "result": _member_empty(label)}
            result = fn(
                query, scope_id=f"guild:{guild_id}", viewer_uid=viewer, present_uids=present, owner_uid=member_uid,
            )
            if bool(getattr(result, "degraded", False)):
                return {"success": False, "result": "目前無法查詢這位成員的共享內容。", "code": "failed"}
            text = result.to_text(display_name) or ""
            if not text:
                return {"success": True, "result": _member_empty(label)}
            return {
                "success": True,
                "result": f"以下是「{label}」（本伺服器成員）在伺服器裡提過、可分享的內容：\n{text}",
                "shared_ids": [str(i) for i in (getattr(result, "ids", ()) or ())],
            }

        try:
            return dict(self._runtime.call(namespace_id, work, timeout_s=self._store_timeout_s))
        except TimeoutError:
            self._metrics.inc("memory.tool.timeout")
            return {"success": False, "result": "tool timeout", "code": "timeout"}
        except Exception as exc:  # noqa: BLE001 - 對外降級；只記例外類別，不記內容
            logger.warning("member_recall 執行失敗：%s", type(exc).__name__)
            return {"success": False, "result": "tool failed", "code": "failed"}


def _declares_param(fn: Any, name: str) -> bool:
    """``fn`` 的簽名是否明確宣告名為 ``name`` 的參數；只有 ``**kwargs`` 或簽名讀不到都算否（與 ES 同規則）。"""
    try:
        param = inspect.signature(fn).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return param is not None and param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY,
    )


def _member_empty(label: str) -> str:
    return f"「{label}」是本伺服器成員，但目前沒有可分享的內容。"


def _tool_denied(name: str) -> dict[str, Any]:
    return {"success": False, "result": f"Tool {name!r} is not available.", "code": "denied"}


def _clip(text: str, max_chars: int | None) -> str:
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _tool_name(definition: Mapping[str, Any]) -> str:
    fn = definition.get("function")
    return str((fn if isinstance(fn, Mapping) else definition).get("name", ""))
