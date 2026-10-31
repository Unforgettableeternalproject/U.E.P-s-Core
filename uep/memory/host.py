"""MemoryHost（P3接入方案 §3.1）：Core 內唯一建立記憶 engine 的地方。

啟動順序（宿主契約，由測試固定）：建 engine -> ``configure_dream`` -> ``set_llm_fn`` -> （有才）``set_concept_inhibitor``
-> ``set_candidate_sink`` -> ``set_dream_listener`` -> （僅 ``enable_dream_scheduler=True`` 的舊路徑）``start_dream_scheduler()``。
先注入再 start。Gate（concept inhibitor）每個 engine 各一個（由 factory 產生）：真 engine 的 setter 會覆寫
``inhibitor.event_emitter``，Gate 也綁單一 engine，共用會讓事件丟失或歸錯 owner。

寫入（EM #619）：每個 engine 只有一個 ``EchoMemoryAdapter``（``adapter_factory`` 建立），所有寫入經它；唯讀查詢直接呼叫 engine。
adapter 是 async，Host 擁有一條專屬 event loop 執行緒執行其協程；adapter 的寫入**不在** ``call()`` 的 engine 鎖內執行
（同一把鎖由 adapter 自己持有，否則舊式 engine 會死結）。

關閉：先 ``stop_dream_scheduler()``，再等在途事件投遞與排隊中的呼叫排空，接著 ``adapter.aclose()``（排空寫入），
最後逐個 ``engine.close(timeout)``（B-4，冪等）。

做夢（Bernie 裁決，取代 D75 (c)）：per-user engine 與 ``scope-g*`` 共享 namespace 一律關閉自己的排程（``configure_dream``
關 idle／cron，不呼叫 ``start_dream_scheduler``）；全域做夢迴圈由 Host 管（完整迴圈另案，此處只接 ``set_dream_listener``
收每次 dream 結束的報告：計數、Activity 與 log）。階段 X（D76）：Host 只管 Core 自己的 namespace 與資料目錄，啟動時核對與外部占用不相交。

併發：每個 namespace 一個單工作緒 executor 加一把 engine 鎖，A namespace 卡住不影響 B。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from uep.events.envelope import EventDraft, TrustedContext
from uep.events.publisher import EventPublisher
from uep.memory.access import (
    DEFAULT_RETRIEVE_TIMEOUT_S,
    DEFAULT_STORE_TIMEOUT_S,
    GuildMembership,
    MemoryAccessLayer,
    MemoryToolBackend,
)
from uep.memory.activity import DreamActivityRecorder
from uep.memory.identity import IdentityDirectory
from uep.memory.metrics import MemoryMetrics
from uep.memory.namespaces import (
    DEFAULT_NAMESPACE_ID,
    ForeignClaim,
    find_overlaps,
    group_namespace_id,
    is_scope_namespace_id,
    normalize_namespace_id,
    paths_overlap,
    resolve_namespace_path,
    validate_owner_id,
)
from uep.memory.protocol import MemoryAdapterLike, MemoryEngineLike
from uep.memory.sink import MemorySink

logger = logging.getLogger("uep.memory.host")

HEALTH_SOURCE = "core.health"  # State Coordinator 只投影這個來源的 health 事件
HEALTH_REF = "memory"

EngineFactory = Callable[[str, Path], MemoryEngineLike]
InhibitorFactory = Callable[[str], Any]
AdapterFactory = Callable[[str, MemoryEngineLike, threading.Lock], MemoryAdapterLike]
_TRIGGER_SAFE = re.compile(r"[^a-z0-9_]+")


class _AdapterRunner:
    """專屬 event loop 執行緒：把 adapter 的協程橋接成同步呼叫（逾時語意與 ``call()`` 一致）。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="uep-mem-adapter-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()
        self._loop.close()

    def submit(self, coro: Awaitable[Any]) -> concurrent.futures.Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]

    def stop(self) -> None:
        if self._loop.is_closed():
            return

        async def cancel_pending() -> None:
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(cancel_pending(), self._loop).result(timeout=2.0)
        except Exception as exc:  # noqa: BLE001 - 收尾盡力而為，仍要停掉 loop
            logger.warning("adapter loop 收尾失敗（已忽略）：%s", type(exc).__name__)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)


class MemoryHostError(RuntimeError):
    """啟動前提不成立（目錄相交、namespace 設定錯誤、engine 目錄與預期不符）；拒絕啟動而不是降級。"""


@dataclass(frozen=True)
class NamespaceSpec:
    """Core 管理的一個 namespace。``owner_id`` 為 None 表示沒有 owner（只允許共享層 ``scope-g*``）。"""

    namespace_id: str
    owner_id: str | None = None


@dataclass
class StartReport:
    """每個 namespace 的啟動結果：``scheduler`` 為 ``{"started": bool, "reason": str}``（scope 與停用時為 skipped 原因）。"""

    namespaces: dict[str, dict[str, Any]] = field(default_factory=dict)


class MemoryHost:
    def __init__(
        self,
        *,
        engine_factory: EngineFactory,
        data_root: str | Path,
        namespaces: Sequence[NamespaceSpec],
        identity: IdentityDirectory,
        publisher: EventPublisher | None = None,
        session_factory: sessionmaker[Session] | None = None,
        llm_fn: Callable[[str], str] | None = None,
        concept_inhibitor_factory: InhibitorFactory | None = None,
        guild_membership: GuildMembership | None = None,
        foreign_claims: Sequence[ForeignClaim] = (),
        enable_dream_scheduler: bool = False,
        adapter_factory: AdapterFactory | None = None,
        persona_id: str = "uep",
        tool_backend: MemoryToolBackend | None = None,
        retrieve_timeout_s: float = DEFAULT_RETRIEVE_TIMEOUT_S,
        store_timeout_s: float = DEFAULT_STORE_TIMEOUT_S,
        drain_timeout_s: float = 5.0,
    ) -> None:
        self._engine_factory = engine_factory
        self._data_root = Path(data_root)
        self._specs = tuple(namespaces)
        self._identity = identity
        self._publisher = publisher
        self._llm_fn = llm_fn
        self._inhibitor_factory = concept_inhibitor_factory
        self._foreign = tuple(foreign_claims)
        self._enable_scheduler = enable_dream_scheduler  # 舊路徑：engine 內建排程；預設關（Host 全域迴圈另案）
        self._adapter_factory = adapter_factory
        self._adapters: dict[str, MemoryAdapterLike] = {}
        self._runner: _AdapterRunner | None = None
        self._drain_timeout_s = drain_timeout_s
        self.metrics = MemoryMetrics()
        self._activity = (
            DreamActivityRecorder(session_factory, persona_id=persona_id) if session_factory is not None else None
        )
        self._engines: dict[str, MemoryEngineLike] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._executors: dict[str, concurrent.futures.ThreadPoolExecutor] = {}
        self._owner_ns: dict[str, str] = {}
        self._state_lock = threading.Lock()
        self.started = False
        self.start_report = StartReport()
        self.access = MemoryAccessLayer(
            self, identity, publisher=publisher, metrics=self.metrics, tool_backend=tool_backend,
            guild_membership=guild_membership, retrieve_timeout_s=retrieve_timeout_s,
            store_timeout_s=store_timeout_s,
        )

    @property
    def publisher(self) -> EventPublisher | None:
        return self._publisher

    # ---- 設定核對 ----

    def namespace_paths(self) -> dict[str, Path]:
        return {spec.namespace_id: resolve_namespace_path(self._data_root, spec.namespace_id) for spec in self._specs}

    def _validate(self) -> None:
        """拒絕：空設定、``local-default``（目錄即資料根）、重複、owner 重複或不合規、共享層帶 owner、階段 X 相交。"""
        if not self._specs:
            raise MemoryHostError("MemoryHost 沒有任何 namespace")
        seen: set[str] = set()
        owners: set[str] = set()
        for spec in self._specs:
            try:
                ns = normalize_namespace_id(spec.namespace_id)
            except ValueError as exc:
                raise MemoryHostError(str(exc)) from exc
            if ns == DEFAULT_NAMESPACE_ID:
                raise MemoryHostError("Core 不得使用 local-default：其目錄就是資料根，會與其他 namespace 相交")
            if ns != spec.namespace_id or ns in seen:
                raise MemoryHostError(f"namespace 重複或未正規化：{spec.namespace_id!r}")
            seen.add(ns)
            if is_scope_namespace_id(ns):
                if spec.owner_id is not None:
                    raise MemoryHostError(f"共享層 namespace 不得帶 owner_id：{ns!r}")
                continue
            if spec.owner_id is None:
                raise MemoryHostError(f"非共享 namespace 必須指定 owner_id：{ns!r}")
            try:
                validate_owner_id(spec.owner_id)
            except ValueError as exc:
                raise MemoryHostError(str(exc)) from exc
            if spec.owner_id in owners:
                raise MemoryHostError(f"owner 對應多個 namespace：{spec.owner_id!r}")
            owners.add(spec.owner_id)
        paths = self.namespace_paths()
        names = list(paths)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if paths_overlap(paths[a], paths[b]):
                    raise MemoryHostError(f"Core 內 namespace 目錄相交：{a!r} 與 {b!r}")
        overlaps = find_overlaps(paths, self._foreign)
        if overlaps:
            raise MemoryHostError("階段 X 目錄不相交核對失敗：" + "；".join(overlaps))

    # ---- 生命週期 ----

    def start(self) -> StartReport:
        """冪等。任何一步失敗（含已建立但目錄不符的 engine）就收掉已建立的部分再拋出。"""
        with self._state_lock:
            if self.started:
                return self.start_report
            self._validate()
            if self._activity is not None:
                self._safe("sweep_stale_activities", self._sweep_stale)
            report = StartReport()
            inhibitors: set[int] = set()
            try:
                for spec in self._specs:
                    report.namespaces[spec.namespace_id] = self._start_namespace(spec, inhibitors)
            except BaseException:
                self._stop_locked()
                raise
            self.start_report = report
            self.started = True
        self._publish_health("ready")
        return report

    def _sweep_stale(self) -> None:
        assert self._activity is not None
        swept = self._activity.sweep_stale()
        if swept:
            logger.warning("清掃上次殘留的 maintenance Activity：%d 筆", swept)

    def _start_namespace(self, spec: NamespaceSpec, inhibitors: set[int]) -> dict[str, Any]:
        ns = spec.namespace_id
        engine = self._engine_factory(ns, self._data_root)
        # 先登記再核對：核對失敗時這個 engine 也在清理範圍內
        self._engines[ns] = engine
        self._locks[ns] = threading.Lock()
        self._executors[ns] = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"uep-mem-{ns}")
        expected = resolve_namespace_path(self._data_root, ns)
        if Path(engine.storage_path).resolve() != expected.resolve():
            raise MemoryHostError(f"engine {ns!r} 的資料目錄 {engine.storage_path} 與預期 {expected} 不符")
        if spec.owner_id is not None:
            self._owner_ns[spec.owner_id] = ns
        # 先設定做夢（須在 DreamEngine 建立之前）、再注入、再 start
        scope = is_scope_namespace_id(ns)
        if scope or not self._enable_scheduler:
            engine.configure_dream(dream_enable_idle=False, dream_enable_cron=False)
        if self._adapter_factory is not None:
            if self._runner is None:
                self._runner = _AdapterRunner()
            self._adapters[ns] = self._adapter_factory(ns, engine, self._locks[ns])
        engine.set_llm_fn(self._llm_fn)
        if not is_scope_namespace_id(ns) and self._inhibitor_factory is not None:
            inhibitor = self._inhibitor_factory(ns)
            if inhibitor is None or id(inhibitor) in inhibitors:
                raise MemoryHostError(f"concept inhibitor 必須每個 namespace 各一個新實例：{ns!r}")
            inhibitors.add(id(inhibitor))
            engine.set_concept_inhibitor(inhibitor)
        else:
            # 不傳 None：真 engine 把 None 視為「刻意無 Gate」並停用預設 Gate（見 S2 介面清單 B-1）；scope-g* 不注入
            logger.info("namespace=%s 不注入 concept inhibitor（使用記憶端預設 Gate 或共享層不需要）", ns)
        if spec.owner_id is not None:
            engine.set_candidate_sink(self._make_sink(ns, spec.owner_id))
        if scope:
            # 共享場域：不啟動排程；糾正寫入要找當事人自己的 engine（uid -> engine）
            engine.set_engine_resolver(self._resolve_user_engine)
            return {"scheduler": {"started": False, "reason": "scope_namespace"}}
        engine.set_dream_listener(lambda report, n=ns: self._on_dream_report(n, report))
        if not self._enable_scheduler:
            return {"scheduler": {"started": False, "reason": "disabled_by_host"}}
        result = dict(engine.start_dream_scheduler())
        if str(result.get("reason", "")).startswith("error"):
            self.metrics.inc("memory.dream.scheduler_error")
            logger.warning("做夢排程啟動失敗：namespace=%s", ns)
        return {"scheduler": result}

    def _make_sink(self, namespace_id: str, owner_id: str | None) -> Any:
        if self._publisher is None:
            return _NullSink()
        return MemorySink(self._publisher, namespace_id, owner_id, metrics=self.metrics)

    def stop(self) -> None:
        """冪等；每一步獨立容錯，避免一個 engine 卡住讓其他 engine 的排程沒停。先發 health 下線事件。"""
        if self.started:
            self._publish_health("unavailable")
        with self._state_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        engines = list(self._engines.items())
        for ns, engine in reversed(engines):
            self._safe(f"stop_dream_scheduler({ns})", engine.stop_dream_scheduler)
        for ns, engine in reversed(engines):
            drain = getattr(getattr(engine, "event_emitter", None), "drain", None)
            if drain is not None:
                self._safe(f"drain({ns})", lambda d=drain, n=ns: self._drain(d, n))
        executors, self._executors = self._executors, {}
        for ns, executor in executors.items():
            try:
                executor.submit(lambda: None).result(timeout=self._drain_timeout_s)
            except Exception as exc:  # noqa: BLE001 - 逾時或已關閉都只記類別，仍要繼續關閉其他項目
                logger.warning("等待記憶呼叫排空失敗（%s），放棄尚未開始的呼叫：%s", ns, type(exc).__name__)
            executor.shutdown(wait=False, cancel_futures=True)
        self._close_adapters()
        for ns, engine in reversed(engines):
            self._safe(f"close({ns})", lambda e=engine: e.close(self._drain_timeout_s))  # B-4：冪等，逐個關閉
        self._engines.clear()
        self._adapters.clear()
        self._locks.clear()
        self._owner_ns.clear()
        self.started = False

    def _close_adapters(self) -> None:
        """排空各 adapter 排隊中的寫入（``aclose``），再停 event loop；必須在 ``engine.close`` 之前。"""
        runner, self._runner = self._runner, None
        if runner is None:
            return
        for ns, adapter in reversed(list(self._adapters.items())):
            def close(a: MemoryAdapterLike = adapter) -> None:
                runner.submit(a.aclose()).result(timeout=self._drain_timeout_s)
            self._safe(f"adapter.aclose({ns})", close)
        runner.stop()

    def _drain(self, drain: Callable[[float], Any], namespace_id: str) -> None:
        if drain(self._drain_timeout_s) is False:
            logger.warning("事件投遞排空逾時，仍有在途事件：namespace=%s", namespace_id)

    @staticmethod
    def _safe(label: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 單一步驟失敗不得中斷其餘關閉／清掃；只記例外類別
            logger.warning("MemoryHost 步驟失敗（已忽略）：%s %s", label, type(exc).__name__)

    # ---- Access Layer 用的能力（MemoryRuntime）----

    def namespace_for_owner(self, owner_id: str) -> str | None:
        return self._owner_ns.get(owner_id)

    def namespace_for_guild(self, guild_id: str) -> str | None:
        try:
            ns = group_namespace_id(guild_id)
        except ValueError:
            return None
        return ns if ns in self._engines else None

    def engine(self, namespace_id: str) -> MemoryEngineLike:
        try:
            return self._engines[namespace_id]
        except KeyError:
            raise MemoryHostError(f"未啟動或未知的 namespace：{namespace_id!r}") from None

    def engine_lock(self, namespace_id: str) -> threading.Lock:
        """該 engine 的互斥鎖。echo_memory 非 thread-safe：S3 的 ``EchoMemoryAdapter(engine_lock=...)`` 與
        Access Layer 必須持同一把鎖。"""
        try:
            return self._locks[namespace_id]
        except KeyError:
            raise MemoryHostError(f"未啟動或未知的 namespace：{namespace_id!r}") from None

    def adapter(self, namespace_id: str) -> MemoryAdapterLike | None:
        """該 namespace 唯一的 ``EchoMemoryAdapter``（沒有設定 ``adapter_factory`` 時為 None）。"""
        return self._adapters.get(namespace_id)

    def adapter_call(
        self,
        namespace_id: str,
        fn: Callable[[MemoryAdapterLike], Awaitable[Any]],
        *,
        timeout_s: float,
        on_late: Callable[[Any], None] | None = None,
    ) -> Any:
        """在 Host 的 adapter event loop 上執行 ``fn(adapter)`` 協程並有界等待；逾時拋 ``TimeoutError``。
        不持 engine 鎖（adapter 自己序列化）；逾時後若寫入才完成，結果交給 ``on_late``。"""
        adapter, runner = self._adapters.get(namespace_id), self._runner
        if not self.started or adapter is None or runner is None:
            raise MemoryHostError("MemoryHost 未啟動或該 namespace 沒有 adapter")
        future = runner.submit(fn(adapter))
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            if on_late is not None:
                future.add_done_callback(lambda f: self._deliver_late(f, on_late))
            else:
                future.cancel()
            raise TimeoutError(f"記憶呼叫逾時（{timeout_s}s）") from None

    def adapter_fire(self, namespace_id: str, fn: Callable[[MemoryAdapterLike], Awaitable[Any]]) -> None:
        """fire-and-forget（例如回覆後的隱私意圖掃描）：失敗只記 log，不影響呼叫端。"""
        adapter, runner = self._adapters.get(namespace_id), self._runner
        if not self.started or adapter is None or runner is None:
            return

        def done(f: concurrent.futures.Future[Any]) -> None:
            if not f.cancelled() and f.exception() is not None:
                logger.warning("adapter 背景呼叫失敗（已忽略）：%s", type(f.exception()).__name__)

        runner.submit(fn(adapter)).add_done_callback(done)

    def call(
        self,
        namespace_id: str,
        fn: Callable[[MemoryEngineLike], Any],
        *,
        timeout_s: float,
        on_late: Callable[[Any], None] | None = None,
    ) -> Any:
        """在該 namespace 的 engine 鎖內、該 namespace 專屬的單工作緒 executor 上有界執行。

        逾時拋 ``TimeoutError``；排隊期間已逾時的呼叫不會在拿到鎖後才執行。若呼叫逾時時已經在執行、之後才完成，
        ``on_late(結果)`` 會在 executor 執行緒上被呼叫（例如補發 ``memory.record_created``）。"""
        executor = self._executors.get(namespace_id)
        if not self.started or executor is None:
            raise MemoryHostError("MemoryHost 未啟動")
        engine, lock = self.engine(namespace_id), self.engine_lock(namespace_id)
        expired = threading.Event()  # 呼叫端放棄後設旗標；不靠時鐘比較（Windows 的 monotonic 解析度約 15ms）

        def task() -> Any:
            with lock:
                if expired.is_set():
                    raise TimeoutError("等鎖期間已逾時，未執行")
                return fn(engine)

        future = executor.submit(task)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            expired.set()
            if on_late is not None:
                future.add_done_callback(lambda f: self._deliver_late(f, on_late))
            future.cancel()
            raise TimeoutError(f"記憶呼叫逾時（{timeout_s}s）") from None

    def _deliver_late(self, future: concurrent.futures.Future[Any], on_late: Callable[[Any], None]) -> None:
        if future.cancelled() or future.exception() is not None:
            return
        self.metrics.inc("memory.call.late_completed")
        self._safe("on_late", lambda: on_late(future.result()))

    # ---- dream 結束通知（set_dream_listener）----

    def _resolve_user_engine(self, uid: str) -> MemoryEngineLike | None:
        """共享 engine 的 ``set_engine_resolver``：``uid`` 是 owner_id 或 discord 平台 uid，找不到回 None（拒絕）。
        只回非共享 namespace 的 engine。注意：記憶端的 ``correct_about_me`` 會直接寫這個 engine（繞過 adapter，EM 介面決定）。"""
        ns = self._owner_ns.get(str(uid))
        if ns is None:
            ident = self._identity.resolve_platform(f"discord:{uid}")
            ns = self._owner_ns.get(ident.owner_id) if ident.owner_id is not None else None
        return self._engines.get(ns) if ns is not None else None

    def _on_dream_report(self, namespace_id: str, report: Mapping[str, Any]) -> None:
        """每次 dream_now 結束都會呼叫（含 skipped／error），在觸發 dream 的執行緒上同步執行，絕不拋例外。
        計數：``memory.dream.runs.<trigger>``、``memory.dream.skipped_lease``（E1／E4）；只有 completed／error 補 Activity。"""
        try:
            status = str(report.get("status") or "")
            trigger = _TRIGGER_SAFE.sub("_", str(report.get("triggered_by") or "unknown").lower()).strip("_") or "unknown"
            if status == "skipped":
                reason = str(report.get("skipped_reason") or "")
                lease = "lease" in reason.lower() or "租約" in reason
                self.metrics.inc("memory.dream.skipped_lease" if lease else "memory.dream.skipped")
                logger.info("dream 略過：namespace=%s trigger=%s lease=%s", namespace_id, trigger, lease)
                return
            if status == "error":
                self.metrics.inc("memory.dream.error")
                logger.warning("dream 失敗：namespace=%s trigger=%s", namespace_id, trigger)
            else:
                self.metrics.inc(f"memory.dream.runs.{trigger}")
            if self._activity is not None:
                self._activity.record_finished(namespace_id, trigger, "failed" if status == "error" else "completed")
        except Exception as exc:  # noqa: BLE001 - 在 engine 執行緒上呼叫；補記失敗不得影響 dream
            logger.warning("dream 報告處理失敗（已忽略）：%s", type(exc).__name__)

    # ---- health／狀態 ----

    def _publish_health(self, status: str) -> None:
        if self._publisher is None:
            return
        try:
            self._publisher.publish(
                EventDraft(event_type="core.component_health_changed", payload={"ref_id": HEALTH_REF, "status": status}),
                TrustedContext(source=HEALTH_SOURCE, actor_id="uep:memory", scope_ref="scope_private_core"),
            )
        except Exception as exc:  # noqa: BLE001 - health 事件失敗不得影響啟停；只記例外類別
            logger.warning("memory health 事件發布失敗（已忽略）：%s", type(exc).__name__)

    def status(self) -> Mapping[str, Any]:
        """診斷用：各 namespace 的排程啟動結果與計數器（階段 Y 的 E1／E4 觀察資料）。"""
        return {"started": self.started, "namespaces": dict(self.start_report.namespaces), "metrics": self.metrics.snapshot()}


class _NullSink:
    """沒有 publisher（純測試或尚未接線）時的 sink：吞掉事件，不讓 engine 因為沒有 sink 而行為不同。"""

    def emit(self, draft: Any) -> None:
        return None
