"""MemoryHost（P3接入方案 §3.1）：Core 內唯一建立記憶 engine 的地方。

啟動順序（宿主契約，由測試固定）：建 engine -> ``set_llm_fn`` -> （有 inhibitor 才）``set_concept_inhibitor``
-> ``set_candidate_sink`` -> ``start_dream_scheduler()``。先注入再 start。
關閉：先 ``stop_dream_scheduler()``，再等在途事件投遞與排隊中的呼叫排空，最後（若 engine 有）``close()``。

做夢（D75 (c)）：累積門檻由 echo_memory 內建排程驅動，Host 只負責啟停；``scope-g*`` 共享 namespace 一律不啟動排程。
階段 X（D76）：Host 只管 Core 自己的 namespace 與資料目錄，啟動時核對與外部占用（bot per-user 目錄）不相交。
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from uep.events.envelope import EventDraft, TrustedContext
from uep.events.publisher import EventPublisher
from uep.memory.access import (
    DEFAULT_RETRIEVE_TIMEOUT_S,
    DEFAULT_STORE_TIMEOUT_S,
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
from uep.memory.protocol import MemoryEngineLike
from uep.memory.sink import MemorySink

logger = logging.getLogger("uep.memory.host")

HEALTH_SOURCE = "core.health"  # State Coordinator 只投影這個來源的 health 事件
HEALTH_REF = "memory"

EngineFactory = Callable[[str, Path], MemoryEngineLike]


class MemoryHostError(RuntimeError):
    """啟動前提不成立（目錄相交、namespace 設定錯誤、engine 目錄與預期不符）；拒絕啟動而不是降級。"""


@dataclass(frozen=True)
class NamespaceSpec:
    """Core 管理的一個 namespace。``owner_id`` 為 None 表示沒有 owner（共享層 ``scope-g*``）。"""

    namespace_id: str
    owner_id: str | None = None


@dataclass(frozen=True)
class DreamOutcome:
    status: str  # completed | skipped | failed
    reason: str = ""
    activity_id: str | None = None


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
        concept_inhibitor: Any = None,
        foreign_claims: Sequence[ForeignClaim] = (),
        enable_dream_scheduler: bool = True,
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
        self._inhibitor = concept_inhibitor
        self._foreign = tuple(foreign_claims)
        self._enable_scheduler = enable_dream_scheduler
        self._drain_timeout_s = drain_timeout_s
        self.metrics = MemoryMetrics()
        self._activity = (
            DreamActivityRecorder(session_factory, persona_id=persona_id) if session_factory is not None else None
        )
        self._engines: dict[str, MemoryEngineLike] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._owner_ns: dict[str, str] = {}
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._manual_dreams: set[str] = set()
        self._state_lock = threading.Lock()
        self._dream_lock = threading.Lock()  # 與 _state_lock 分開：sink 在 stop 排空期間仍要取它，不能互相等待
        self.started = False
        self.start_report = StartReport()
        self.access = MemoryAccessLayer(
            self, identity, publisher=publisher, metrics=self.metrics, tool_backend=tool_backend,
            retrieve_timeout_s=retrieve_timeout_s, store_timeout_s=store_timeout_s,
        )

    # ---- 設定核對 ----

    def namespace_paths(self) -> dict[str, Path]:
        return {spec.namespace_id: resolve_namespace_path(self._data_root, spec.namespace_id) for spec in self._specs}

    def _validate(self) -> None:
        """拒絕：空設定、``local-default``（目錄即資料根，會與一切相交）、重複、owner 重複或不合規、階段 X 相交。"""
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
            if spec.owner_id is not None:
                try:
                    validate_owner_id(spec.owner_id)
                except ValueError as exc:
                    raise MemoryHostError(str(exc)) from exc
                if spec.owner_id in owners:
                    raise MemoryHostError(f"owner 對應多個 namespace：{spec.owner_id!r}")
                owners.add(spec.owner_id)
            elif not is_scope_namespace_id(ns):
                raise MemoryHostError(f"非共享 namespace 必須指定 owner_id：{ns!r}")
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
        """冪等。任何一步失敗就收掉已啟動的部分再拋出。"""
        with self._state_lock:
            if self.started:
                return self.start_report
            self._validate()
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="uep-memory")
            report = StartReport()
            try:
                for spec in self._specs:
                    report.namespaces[spec.namespace_id] = self._start_namespace(spec)
            except BaseException:
                self._stop_locked()
                raise
            self.start_report = report
            self.started = True
        self._publish_health("ready")
        return report

    def _start_namespace(self, spec: NamespaceSpec) -> dict[str, Any]:
        ns = spec.namespace_id
        engine = self._engine_factory(ns, self._data_root)
        expected = resolve_namespace_path(self._data_root, ns)
        if Path(engine.storage_path).resolve() != expected.resolve():
            raise MemoryHostError(f"engine {ns!r} 的資料目錄 {engine.storage_path} 與預期 {expected} 不符")
        self._engines[ns] = engine
        self._locks[ns] = threading.Lock()
        if spec.owner_id is not None:
            self._owner_ns[spec.owner_id] = ns
        # 先注入、再 start
        engine.set_llm_fn(self._llm_fn)
        if self._inhibitor is not None:
            engine.set_concept_inhibitor(self._inhibitor)
        else:
            # 不傳 None：真 engine 把 None 視為「刻意無 Gate」並停用預設 Gate（見 S2 介面清單 B-1）
            logger.info("未提供 concept inhibitor：namespace=%s 使用記憶端預設 Gate", ns)
        if spec.owner_id is not None:
            engine.set_candidate_sink(self._make_sink(ns, spec.owner_id))
        if is_scope_namespace_id(ns):
            return {"scheduler": {"started": False, "reason": "scope_namespace"}}
        if not self._enable_scheduler:
            return {"scheduler": {"started": False, "reason": "disabled_by_host"}}
        result = dict(engine.start_dream_scheduler())
        reason = str(result.get("reason", ""))
        if reason.startswith("error"):
            self.metrics.inc("memory.dream.scheduler_error")
            logger.warning("做夢排程啟動失敗：namespace=%s", ns)
        return {"scheduler": result}

    def _make_sink(self, namespace_id: str, owner_id: str | None) -> Any:
        if self._publisher is None:
            return _NullSink()
        return MemorySink(
            self._publisher, namespace_id, owner_id, metrics=self.metrics,
            on_dream_completed=self._on_scheduled_dream_completed,
        )

    def stop(self) -> None:
        """冪等；每一步獨立容錯，避免一個 engine 卡住讓其他 engine 的排程沒停。"""
        with self._state_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        engines = list(self._engines.items())
        for ns, engine in reversed(engines):
            self._safe(f"stop_dream_scheduler({ns})", engine.stop_dream_scheduler)
        for ns, engine in reversed(engines):
            emitter = getattr(engine, "event_emitter", None)
            drain = getattr(emitter, "drain", None)
            if drain is not None:
                self._safe(f"drain({ns})", lambda d=drain: d(self._drain_timeout_s))
        executor, self._executor = self._executor, None
        if executor is not None:
            try:
                executor.submit(lambda: None).result(timeout=self._drain_timeout_s)
            except Exception:
                logger.warning("等待記憶呼叫排空逾時，放棄尚未開始的呼叫", exc_info=True)
            executor.shutdown(wait=False, cancel_futures=True)
        for ns, engine in reversed(engines):
            close = getattr(engine, "close", None)  # 真 engine 目前沒有 close（見介面清單 B-4）
            if callable(close):
                self._safe(f"close({ns})", close)
        self._engines.clear()
        self._locks.clear()
        self._owner_ns.clear()
        self.started = False

    @staticmethod
    def _safe(label: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:
            logger.exception("MemoryHost 關閉步驟失敗（已忽略）：%s", label)

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

    def call(self, namespace_id: str, fn: Callable[[MemoryEngineLike], Any], *, timeout_s: float) -> Any:
        """在 engine 鎖內、單工作緒 executor 上有界執行。逾時拋 ``TimeoutError``；

        排隊期間已逾時的呼叫不會在拿到鎖後才執行（避免回報逾時之後才寫入）。"""
        executor = self._executor
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
            future.cancel()
            raise TimeoutError(f"記憶呼叫逾時（{timeout_s}s）") from None

    # ---- 做夢 ----

    def run_dream(self, namespace_id: str, triggered_by: str = "manual") -> DreamOutcome:
        """手動／休息時段觸發的 dream：前後以 maintenance Activity 包住。

        - 租約被他行程持有（``skipped_reason``）-> skipped：``memory.dream.skipped_lease`` +1，不算錯誤、不發 health。
        - ``error`` 或例外 -> failed：發 ``core.component_health_changed``（degraded）。
        """
        if is_scope_namespace_id(namespace_id):
            return DreamOutcome("skipped", "scope_namespace")
        engine, lock = self.engine(namespace_id), self.engine_lock(namespace_id)
        with self._dream_lock:
            self._manual_dreams.add(namespace_id)
        activity_id = self._activity_begin(namespace_id, triggered_by)
        try:
            with lock:
                result = dict(engine.dream(triggered_by))
        except Exception as exc:
            self.metrics.inc("memory.dream.failed")
            self._activity_end(activity_id, "failed")
            self._publish_health("degraded")
            logger.exception("dream 失敗：namespace=%s", namespace_id)
            return DreamOutcome("failed", type(exc).__name__, activity_id)
        finally:
            with self._dream_lock:
                self._manual_dreams.discard(namespace_id)
        skipped, error = result.get("skipped_reason"), result.get("error")
        if skipped:
            self.metrics.inc("memory.dream.skipped_lease")
            self._activity_end(activity_id, "skipped_lease")
            return DreamOutcome("skipped", str(skipped), activity_id)
        if error:
            self.metrics.inc("memory.dream.failed")
            self._activity_end(activity_id, "failed")
            self._publish_health("degraded")
            return DreamOutcome("failed", "dream_error", activity_id)
        self.metrics.inc(f"memory.dream.runs.{triggered_by}")
        self._activity_end(activity_id, "completed")
        return DreamOutcome("completed", "", activity_id)

    def _on_scheduled_dream_completed(self, namespace_id: str, _draft: Any) -> None:
        """排程（內建 idle／cron／daydream）觸發的 dream 只能事後從 memory.dream_completed 得知，補一筆已結束的 Activity。
        Host 手動 dream 進行中時由 ``run_dream`` 自己記，這裡略過避免重複。"""
        with self._dream_lock:
            if namespace_id in self._manual_dreams:
                return
        self.metrics.inc("memory.dream.runs.scheduler")
        if self._activity is not None:
            self._activity.record_finished(namespace_id, "scheduler", "completed")

    def _activity_begin(self, namespace_id: str, triggered_by: str) -> str | None:
        if self._activity is None:
            return None
        try:
            return self._activity.begin(namespace_id, triggered_by)
        except Exception:
            logger.exception("dream Activity 建立失敗（不影響 dream）")
            return None

    def _activity_end(self, activity_id: str | None, outcome: str) -> None:
        if self._activity is None or activity_id is None:
            return
        try:
            self._activity.end(activity_id, outcome)
        except Exception:
            logger.exception("dream Activity 結束失敗（已忽略）")

    # ---- health／狀態 ----

    def _publish_health(self, status: str) -> None:
        if self._publisher is None:
            return
        try:
            self._publisher.publish(
                EventDraft(event_type="core.component_health_changed", payload={"ref_id": HEALTH_REF, "status": status}),
                TrustedContext(source=HEALTH_SOURCE, actor_id="uep:memory", scope_ref="scope_private_core"),
            )
        except Exception:
            logger.exception("memory health 事件發布失敗（已忽略）")

    def status(self) -> Mapping[str, Any]:
        """診斷用：各 namespace 的排程啟動結果與計數器（階段 Y 的 E1／E4 觀察資料）。"""
        return {"started": self.started, "namespaces": dict(self.start_report.namespaces), "metrics": self.metrics.snapshot()}


class _NullSink:
    """沒有 publisher（純測試或尚未接線）時的 sink：吞掉事件，不讓 engine 因為沒有 sink 而行為不同。"""

    def emit(self, draft: Any) -> None:
        return None
