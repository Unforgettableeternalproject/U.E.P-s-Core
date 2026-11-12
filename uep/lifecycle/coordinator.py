"""Module Coordinator 核心（設計稿 §5～§9；D108、D109；PR-2）。

單一寫入者：所有狀態與 revision 只在 ``self._lock`` 內修改，事件在鎖內入佇列、鎖外送出（見 ``publisher``）。
handle 的 load／warm／unload／probe 都在受限 worker（daemon 執行緒）上呼叫，鎖內不做任何阻塞呼叫。

不變式（測試鎖住）：
- 每個模組同一時間最多一個進行中的 handle 呼叫（``in_flight``）；逾時只標 ``failed`` 並保留占用（``occupied``），
  舊呼叫結束前不另起 unload／load（Python 無法強殺執行緒，設計稿 §9.2）。
- 每個操作有 token；取消、逾時都會讓 token 前進，舊操作的晚完成一律丟棄，不推進狀態、不復活。
- 取消載入中的模組（loading／warming -> unloading）只登記意圖，等舊呼叫結束才呼叫 ``unload``。
- 設定閘（``blocked_reason``）永遠優先於 probe：被閘住的模組不載入、不探活、不自動重試。
- 失敗重試有限退避（R12）；顯式 ``request_load`` 不重設重試上限、不繞過冷卻。
- 觀測模式（``mode="observe"``）不呼叫任何 load／unload（R10：不得 provisioning／驅逐），狀態只由 probe 推導。

PR-2 不含：預算／NVML／驅逐／lease／pin（PR-3）、真實 handle（PR-4）、提議政策（PR-5）、R13 健康橋接。
"""
from __future__ import annotations

import dataclasses
import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from uep.lifecycle.descriptor import ModuleDescriptor
from uep.lifecycle.handles import ModuleHandle
from uep.lifecycle.publisher import ModuleEventPublisher, Sink
from uep.lifecycle.registry import ModuleRegistry
from uep.lifecycle.states import (
    MODULE_STATE_CHANGED,
    RELOAD_ENABLED,
    is_legal_publish,
    readiness_of,
    validate_state_changed_payload,
)

logger = logging.getLogger("uep.lifecycle")

CoordinatorMode = Literal["manage", "observe"]
DEFAULT_BACKOFF_S: tuple[float, ...] = (5.0, 30.0, 300.0)  # 設計稿 §9.1；R12：有上限的機械式退避
_ACTIVE_STATES = frozenset({"ready", "degraded"})
_TRANSIENT_STATES = frozenset({"loading", "warming", "unloading"})


class CoordinatorError(RuntimeError):
    """coordinator 使用錯誤或內部不變式被破壞（訊息不含 handle 內容）。"""


@dataclass(frozen=True)
class RequestResult:
    accepted: bool
    reason: str


@dataclass(frozen=True)
class ModuleStatus:
    module_id: str
    state: str
    readiness: str
    revision: int
    epoch: int
    reason_code: str | None
    failures: int
    occupied: bool  # handle 可能仍占用資源（逾時、晚完成或載入成功）
    in_flight: bool  # 有 handle 呼叫仍在執行
    blocked_reason: str | None
    retries_exhausted: bool


class _Rec:
    """單一模組的可變狀態；只在 coordinator 鎖內存取。"""

    __slots__ = (
        "attempt_no",
        "dead_count",
        "deadline",
        "desc",
        "exhausted",
        "failures",
        "handle",
        "in_flight",
        "job_kind",
        "occupied",
        "probe_due",
        "reason",
        "retry_at",
        "revision",
        "state",
        "token",
        "unload_cause",
        "unload_submitted",
        "want_cause",
        "wanted",
    )

    def __init__(self, desc: ModuleDescriptor, handle: ModuleHandle) -> None:
        self.desc, self.handle = desc, handle
        self.state, self.reason = "unloaded", desc.blocked_reason or "not_loaded"
        self.revision = 0
        self.token = 0
        self.in_flight = False
        self.occupied = False
        self.wanted = False
        self.want_cause = "demand"
        self.failures = 0
        self.attempt_no = 0
        self.retry_at: float | None = None
        self.deadline: float | None = None
        self.unload_submitted = False
        self.exhausted = False
        self.unload_cause = "operator"
        self.dead_count = 0
        self.probe_due: float | None = None
        self.job_kind = ""


class _Workers:
    """受限的 daemon worker 池。hung 的 handle 呼叫會占住一條執行緒，但不會擋住直譯器結束。"""

    def __init__(self, size: int) -> None:
        self._queue: queue.Queue[tuple[Callable[..., None], tuple[Any, ...]] | None] = queue.Queue()
        self._threads = [
            threading.Thread(target=self._run, name=f"UepModuleWorker-{i}", daemon=True) for i in range(size)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, fn: Callable[..., None], *args: Any) -> None:
        self._queue.put((fn, args))

    def close(self) -> None:
        for _ in self._threads:
            self._queue.put(None)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, args = item
            try:
                fn(*args)
            except BaseException as exc:  # noqa: BLE001  worker 不可因單一工作死亡
                logger.error("module worker 工作未預期失敗：%s", type(exc).__name__)


class ModuleCoordinator:
    def __init__(
        self,
        sink: Sink,
        *,
        epoch_source: Callable[[], int],
        mode: CoordinatorMode = "manage",
        clock: Callable[[], float] = time.monotonic,
        workers: int = 4,
        backoff_s: Sequence[float] = DEFAULT_BACKOFF_S,
        max_attempts: int = 3,
        resync_interval_s: float = 60.0,
        probe_interval_s: float = 0.0,
        probe_dead_threshold: int = 3,
        tick_interval_s: float = 0.5,
        run_scheduler: bool = True,
    ) -> None:
        if mode not in ("manage", "observe"):
            raise ValueError("mode 只能是 manage 或 observe")
        if workers < 1 or max_attempts < 1 or not backoff_s or any(b < 0 for b in backoff_s):
            raise ValueError("workers／max_attempts／backoff_s 設定不合法")
        self._registry = ModuleRegistry()
        self._recs: dict[str, _Rec] = {}
        self._pub = ModuleEventPublisher(sink)
        self._epoch_source = epoch_source
        self._mode: CoordinatorMode = mode
        self._clock = clock
        self._worker_count = workers
        self._backoff = tuple(float(b) for b in backoff_s)
        self._max_attempts = max_attempts
        self._resync_interval = resync_interval_s
        self._probe_interval = probe_interval_s
        self._probe_dead_threshold = probe_dead_threshold
        self._tick_interval = tick_interval_s
        self._run_scheduler = run_scheduler
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._epoch: int | None = None
        self._started = False
        self._stopping = False
        self._workers: _Workers | None = None
        self._scheduler: threading.Thread | None = None
        self._scheduler_stop = threading.Event()
        self._resync_due: float | None = None
        self.counters: dict[str, int] = {}

    # --- 登記 ---

    def register(self, desc: ModuleDescriptor, handle: ModuleHandle) -> None:
        """啟動前登記；重複 id、同一資源的第二個 canonical id 會被拒絕。"""
        with self._lock:
            if self._started:
                raise CoordinatorError("coordinator 啟動後不得再登記模組")
            self._registry.register(desc, handle)
            self._recs[desc.module_id] = _Rec(desc, handle)

    def close_gate(self, module_id: str, reason_code: str) -> None:
        """啟動前由設定層關閉閘（未設定／禁止）：該模組恆為 unavailable，probe 成功也不能覆蓋，且不自動重試。"""
        with self._lock:
            if self._started:
                raise CoordinatorError("閘只能在啟動前關閉")
            rec = self._recs.get(module_id)
            if rec is None:
                raise CoordinatorError(f"{module_id}: 未登記")
            desc = dataclasses.replace(rec.desc, blocked_reason=reason_code)
            self._registry.replace_descriptor(desc)
            rec.desc, rec.reason = desc, reason_code

    def known_ids(self) -> frozenset[str]:
        with self._lock:
            return self._registry.known_ids()

    def blocked_ids(self) -> Mapping[str, str]:
        """設定閘已關閉的模組 -> reason_code（組裝層用來在 registry 播種 fail-closed 標記）。"""
        with self._lock:
            return {mid: r.desc.blocked_reason for mid, r in self._recs.items() if r.desc.blocked_reason}

    @property
    def mode(self) -> CoordinatorMode:
        return self._mode

    @property
    def epoch(self) -> int | None:
        return self._epoch

    # --- 生命週期 ---

    def start(self) -> None:
        """驗證依賴、決定 epoch、發啟動快照（``cause=startup``，先於任何載入轉移），再排入自動載入。"""
        with self._lock:
            if self._started:
                return
            self._registry.validate()  # 缺失依賴／循環：在動任何狀態前失敗
            epoch = self._epoch_source()
            if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
                raise CoordinatorError("epoch 必須是非負整數")
            if self._epoch is not None and epoch < self._epoch:
                raise CoordinatorError("epoch 不得倒退")
            if epoch != self._epoch:  # 同 epoch 重啟不重設 revision；epoch 變大才重算
                for rec in self._recs.values():
                    rec.revision = 0
                self._epoch = epoch
            self._started, self._stopping = True, False
            self._workers = _Workers(self._worker_count)
            now = self._clock()
            for module_id in self._registry.validate():
                rec = self._recs[module_id]
                rec.token += 1
                self._snapshot_locked(rec, "startup")
                blocked = rec.desc.blocked
                rec.wanted = not blocked and (rec.desc.autoload or self._observing(rec))
                rec.want_cause = "startup"
            self._resync_due = now + self._resync_interval if self._resync_interval > 0 else None
            self._reconcile_locked(now)
        self._pub.drain()
        if self._run_scheduler:
            self._scheduler_stop.clear()
            self._scheduler = threading.Thread(target=self._scheduler_loop, name="UepModuleScheduler", daemon=True)
            self._scheduler.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        """停機序（§9.5）：不再接受請求 -> 取消進行中的載入 -> 限時等收尾 -> 逾時者標 failed 保留占用 -> 排空事件 -> 關 worker。"""
        with self._lock:
            if not self._started or self._stopping:
                return
            self._stopping = True
            now = self._clock()
            for rec in self._recs.values():
                rec.wanted = False
                if rec.state == "unloading":
                    rec.unload_cause = "shutdown"  # 已在卸載中：只改原因，不重複轉移
                elif rec.state != "unloaded":
                    self._begin_unload_locked(rec, "shutdown", "shutdown", now)
            self._reconcile_locked(now)
        self._pub.drain()
        end = time.monotonic() + timeout_s
        with self._cond:
            while not all(r.state in ("unloaded", "failed") for r in self._recs.values()):
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.05))
            now = self._clock()
            for rec in self._recs.values():
                if rec.state in _TRANSIENT_STATES:  # 收不回來：保留占用，不假裝已卸載
                    self._fail_locked(rec, "shutdown_timeout", "shutdown", now, occupied=True)
            self._started = False
            workers, self._workers = self._workers, None
        self._pub.flush(timeout_s)
        self._scheduler_stop.set()
        scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None and scheduler is not threading.current_thread():
            scheduler.join(timeout=1.0)
        if workers is not None:
            workers.close()

    # --- 查詢 ---

    def snapshot(self) -> Mapping[str, ModuleStatus]:
        with self._lock:
            return {mid: self._status_locked(rec) for mid, rec in self._recs.items()}

    def status(self, module_id: str) -> ModuleStatus:
        with self._lock:
            return self._status_locked(self._recs[module_id])

    def wait_settled(self, timeout_s: float = 5.0) -> bool:
        """等所有模組不在過渡態、且沒有可立即載入的待辦；回傳是否在時限內達成（啟動時用來讓快照先於 READY）。"""
        end = time.monotonic() + timeout_s
        with self._cond:
            while not self._settled_locked():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.05))
        self._pub.drain()
        return True

    # --- 請求入口（非阻塞，不碰 handle）---

    def request_load(self, module_id: str, *, cause: str = "demand") -> RequestResult:
        with self._lock:
            rec = self._recs.get(module_id)
            if rec is None:
                return RequestResult(False, "unknown_module")
            denied = self._deny_locked(rec)
            if denied:
                return RequestResult(False, denied)
            if rec.desc.blocked:
                return RequestResult(False, f"blocked:{rec.desc.blocked_reason}")
            if rec.state in _ACTIVE_STATES:
                return RequestResult(True, "already_ready")
            if rec.exhausted:  # R12：顯式請求不重設上限
                return RequestResult(False, "retries_exhausted")
            rec.wanted, rec.want_cause = True, cause
            now = self._clock()
            self._reconcile_locked(now)
            if rec.state == "failed" and rec.retry_at is not None and rec.retry_at > now:
                result = RequestResult(True, "queued_until_cooldown")  # R12：不繞過冷卻
            else:
                result = RequestResult(True, "accepted")
        self._pub.drain()
        return result

    def request_unload(self, module_id: str, *, cause: str = "operator") -> RequestResult:
        with self._lock:
            rec = self._recs.get(module_id)
            if rec is None:
                return RequestResult(False, "unknown_module")
            denied = self._deny_locked(rec)
            if denied:
                return RequestResult(False, denied)
            if not rec.handle.can_unload:
                return RequestResult(False, "observe_only")
            rec.wanted = False
            if rec.state == "unloaded":
                return RequestResult(True, "already_unloaded")
            if rec.state == "unloading":
                return RequestResult(True, "already_unloading")
            now = self._clock()
            self._begin_unload_locked(rec, cause, "cancelled" if rec.state in ("loading", "warming") else "unloaded", now)
            self._reconcile_locked(now)
        self._pub.drain()
        return RequestResult(True, "accepted")

    def reload(self, module_id: str) -> RequestResult:
        """patch／reload 掛點（R1）：保留介面，runtime 拒絕啟用。"""
        if not RELOAD_ENABLED:
            return RequestResult(False, "reload_disabled")
        raise CoordinatorError("reload 尚未實作")  # pragma: no cover

    def reset_retries(self, module_id: str) -> RequestResult:
        """運維／中樞（R12 持續失敗後的政策選擇）才能重設重試上限；``request_load`` 不會。"""
        with self._lock:
            rec = self._recs.get(module_id)
            if rec is None:
                return RequestResult(False, "unknown_module")
            rec.failures, rec.exhausted = 0, False
            if rec.state == "failed":
                rec.retry_at = self._clock()
            self._reconcile_locked(self._clock())
        self._pub.drain()
        return RequestResult(True, "accepted")

    def resync(self) -> None:
        """對所有模組重發當前狀態（``cause=resync``，遞增 revision；Bus 丟事件的補償）。"""
        with self._lock:
            if not self._started:
                return
            self._resync_locked()
        self._pub.drain()

    def tick(self, now: float | None = None) -> None:
        """推進時間驅動的工作：逾時、探活、到期重試、定期 resync。排程執行緒定期呼叫；測試可手動呼叫（搭配假時鐘）。"""
        with self._lock:
            if not self._started:
                return
            t = self._clock() if now is None else now
            self._timeouts_locked(t)
            self._probes_locked(t)
            self._reconcile_locked(t)
            if self._resync_due is not None and t >= self._resync_due:
                self._resync_locked()
                self._resync_due = t + self._resync_interval
        self._pub.drain()

    def _scheduler_loop(self) -> None:
        while not self._scheduler_stop.wait(self._tick_interval):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                logger.error("module scheduler tick 失敗：%s", type(exc).__name__)

    # --- 鎖內輔助 ---

    def _count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def _observing(self, rec: _Rec) -> bool:
        """此模組不會被 coordinator 載入／卸載（觀測模式，或 handle 本身只能觀測）：狀態只由 probe 推導。"""
        return self._mode == "observe" or not rec.handle.can_load

    def _does_unload(self, rec: _Rec) -> bool:
        return self._mode == "manage" and rec.handle.can_unload

    def _deny_locked(self, rec: _Rec) -> str | None:
        if not self._started or self._stopping:
            return "not_running"
        if self._mode == "observe":
            return "observation_mode"  # R10：觀測模式不得 provisioning／驅逐
        return None

    def _status_locked(self, rec: _Rec) -> ModuleStatus:
        return ModuleStatus(
            module_id=rec.desc.module_id, state=rec.state, readiness=readiness_of(rec.state), revision=rec.revision,
            epoch=self._epoch if self._epoch is not None else 0,
            reason_code=rec.reason, failures=rec.failures, occupied=rec.occupied, in_flight=rec.in_flight,
            blocked_reason=rec.desc.blocked_reason, retries_exhausted=rec.exhausted,
        )

    def _payload(self, rec: _Rec, state: str, cause: str, reason: str | None, revision: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "module_id": rec.desc.module_id, "revision": revision, "epoch": self._epoch, "state": state,
            "prev_state": rec.state, "readiness": readiness_of(state), "cause": cause,
        }
        if reason is not None:
            payload["reason_code"] = reason
        if rec.attempt_no > 0 and state in ("loading", "warming", "ready", "failed"):
            payload["attempt"] = rec.attempt_no
        return payload

    def _emit_locked(self, rec: _Rec, state: str, cause: str, reason: str | None) -> None:
        """驗證 -> 改狀態與 revision -> 入佇列。驗證失敗在改狀態之前拋出（不留下半套狀態）。"""
        if not is_legal_publish(rec.state, state, cause):
            raise CoordinatorError(f"{rec.desc.module_id}: 非法轉移 {rec.state} -> {state}（{cause}）")
        payload = self._payload(rec, state, cause, reason, rec.revision + 1)
        validate_state_changed_payload(payload)
        rec.revision += 1
        rec.state, rec.reason = state, reason
        self._pub.enqueue(MODULE_STATE_CHANGED, payload)
        self._cond.notify_all()
        logger.info("模組 %s：%s -> %s（%s，%s）", rec.desc.module_id, payload["prev_state"], state, cause, reason)

    def _snapshot_locked(self, rec: _Rec, cause: str) -> None:
        """同態快照（startup／resync）：獨立發布路徑，不經轉移表；一律遞增 revision。"""
        reason = rec.reason
        if rec.state in ("unloaded", "unloading", "failed", "degraded") and reason is None:
            reason = "unknown"
        payload = self._payload(rec, rec.state, cause, reason, rec.revision + 1)
        validate_state_changed_payload(payload)
        rec.revision += 1
        self._pub.enqueue(MODULE_STATE_CHANGED, payload)
        self._count("resync" if cause == "resync" else "startup_snapshot")

    def _resync_locked(self) -> None:
        for rec in self._recs.values():
            self._snapshot_locked(rec, "resync")

    def _settled_locked(self) -> bool:
        for rec in self._recs.values():
            if rec.state in _TRANSIENT_STATES:
                return False
            if self._can_begin_load_locked(rec):
                return False
        return True

    def _deps_ready_locked(self, rec: _Rec) -> bool:
        return all(self._recs[d].state in _ACTIVE_STATES for d in rec.desc.depends_on)

    def _can_begin_load_locked(self, rec: _Rec) -> bool:
        return (
            self._started and not self._stopping and rec.state == "unloaded" and rec.wanted and not rec.desc.blocked
            and not rec.in_flight and self._deps_ready_locked(rec)
        )

    def _submit_locked(self, fn: Callable[..., None], rec: _Rec, kind: str, deadline_s: float, now: float) -> None:
        rec.in_flight, rec.job_kind = True, kind
        rec.deadline = now + deadline_s
        assert self._workers is not None
        self._workers.submit(fn, rec.desc.module_id, rec.token)

    def _begin_load_locked(self, rec: _Rec, now: float) -> None:
        rec.token += 1
        rec.retry_at = None
        rec.attempt_no = rec.failures + 1
        self._emit_locked(rec, "loading", rec.want_cause, None)
        self._submit_locked(self._load_job, rec, "load", rec.desc.load_timeout_s, now)

    def _begin_unload_locked(self, rec: _Rec, cause: str, reason: str, now: float) -> None:
        """-> unloading。原操作仍在執行時只登記取消意圖（token 前進使其結果被丟棄），實際 unload 等它結束才送出。"""
        rec.token += 1
        rec.unload_cause, rec.unload_submitted = cause, False
        rec.retry_at = None
        rec.deadline = now + rec.desc.unload_timeout_s
        self._emit_locked(rec, "unloading", cause, "shutdown" if cause == "shutdown" else reason)

    def _fail_locked(self, rec: _Rec, reason: str, cause: str, now: float, *, occupied: bool = False) -> None:
        """-> failed：保留占用（occupied），安排有限退避重試；設定閘與停機中不重試（R12）。"""
        rec.token += 1  # 讓仍在跑的舊操作成為過期操作
        rec.deadline = None
        if occupied:
            rec.occupied = True
        rec.failures += 1
        rec.dead_count = 0
        rec.retry_at, rec.exhausted = None, False
        if not self._stopping and not rec.desc.blocked:
            if rec.failures >= self._max_attempts:
                rec.exhausted = True
                logger.warning("模組 %s 持續失敗（%d 次），停止自動重試，交由中樞／運維", rec.desc.module_id, rec.failures)
            else:
                rec.retry_at = now + self._backoff[min(rec.failures - 1, len(self._backoff) - 1)]
        self._count(reason)
        self._emit_locked(rec, "failed", cause, reason)

    def _ready_locked(self, rec: _Rec, now: float) -> None:
        self._emit_locked(rec, "ready", rec.want_cause if rec.state in ("loading", "warming") else "health", None)
        rec.failures, rec.exhausted, rec.retry_at, rec.deadline = 0, False, None, None
        rec.attempt_no = 0
        rec.dead_count = 0
        rec.probe_due = now + self._probe_interval if self._probe_interval > 0 else None

    def _reconcile_locked(self, now: float) -> None:
        """把「想要的」推進一步（可能引發連鎖：依賴 ready 後其依賴者可載入）。"""
        if not self._started:
            return
        progressed = True
        while progressed:
            progressed = False
            for rec in self._recs.values():
                if self._reconcile_one_locked(rec, now):
                    progressed = True

    def _reconcile_one_locked(self, rec: _Rec, now: float) -> bool:
        if rec.state == "unloading":
            if not rec.in_flight and not rec.unload_submitted:
                rec.unload_submitted = True
                self._submit_locked(self._unload_job, rec, "unload", rec.desc.unload_timeout_s, now)
                return True
            return False
        if self._stopping:
            return False
        if rec.state == "unloaded":
            if self._can_begin_load_locked(rec):
                self._begin_load_locked(rec, now)
                return True
            return False
        if (
            rec.state == "failed" and rec.wanted and not rec.desc.blocked and not rec.exhausted and not rec.in_flight
            and rec.retry_at is not None and now >= rec.retry_at and self._deps_ready_locked(rec)
        ):
            if rec.occupied and self._does_unload(rec):  # 先清理殘留占用，不在占用上重 load
                self._begin_unload_locked(rec, "health", "retry_cleanup", now)
                return True
            rec.occupied = False if not self._does_unload(rec) else rec.occupied
            rec.retry_at = None
            rec.token += 1
            rec.attempt_no = rec.failures + 1
            self._emit_locked(rec, "loading", "health", None)
            self._submit_locked(self._load_job, rec, "load", rec.desc.load_timeout_s, now)
            return True
        return False

    def _timeouts_locked(self, now: float) -> None:
        for rec in self._recs.values():
            if rec.deadline is None or now < rec.deadline or not rec.in_flight:
                continue
            reason = {
                "loading": "load_timeout", "warming": "load_timeout", "unloading": "unload_timeout",
            }.get(rec.state, "probe_timeout")
            logger.warning("模組 %s：%s 逾時，保留占用並標為 failed", rec.desc.module_id, rec.state)
            self._fail_locked(rec, reason, "health", now, occupied=True)

    def _probes_locked(self, now: float) -> None:
        if self._probe_interval <= 0 or self._stopping:
            return
        for rec in self._recs.values():
            if rec.state in _ACTIVE_STATES and not rec.in_flight and rec.probe_due is not None and now >= rec.probe_due:
                rec.probe_due = now + self._probe_interval
                self._submit_locked(self._probe_job, rec, "probe", rec.desc.load_timeout_s, now)

    # --- worker 工作（鎖外呼叫 handle，結果回鎖內以 token 驗證）---

    def _stale_locked(self, rec: _Rec, token: int) -> bool:
        return rec.token != token

    def _finish_stale_locked(self, rec: _Rec, now: float) -> None:
        rec.in_flight = False
        self._count("late_completion_ignored")
        self._reconcile_locked(now)
        self._cond.notify_all()

    def _load_job(self, module_id: str, token: int) -> None:
        rec = self._recs[module_id]
        err: str | None = None
        verdict: str | None = None
        try:
            if self._observing(rec):
                verdict = rec.handle.probe()  # 觀測：不呼叫 load
            else:
                rec.handle.load()
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        warm = False
        with self._lock:
            now = self._clock()
            if self._stale_locked(rec, token):
                if err is None and verdict is None:
                    rec.occupied = True  # 晚完成：資源其實已被取走，仍不得推進到 ready
                self._finish_stale_locked(rec, now)
            elif err is not None:
                rec.in_flight = False
                self._fail_locked(rec, "probe_dead" if verdict is None and self._observing(rec) else "load_error", "health", now)
                self._reconcile_locked(now)
            elif verdict is not None:
                rec.in_flight = False
                if verdict == "dead":
                    self._fail_locked(rec, "probe_dead", "health", now)
                else:
                    self._ready_locked(rec, now)
                    if verdict == "degraded":
                        self._emit_locked(rec, "degraded", "health", "probe_degraded")
                self._reconcile_locked(now)
            else:
                rec.occupied = True
                if rec.desc.needs_warm:
                    self._emit_locked(rec, "warming", rec.want_cause, None)
                    rec.deadline = now + rec.desc.load_timeout_s
                    rec.job_kind = "load"
                    warm = True
                else:
                    rec.in_flight = False
                    self._ready_locked(rec, now)
                    self._reconcile_locked(now)
        if warm:
            self._warm_job(rec, token)
        self._pub.drain()

    def _warm_job(self, rec: _Rec, token: int) -> None:
        err: str | None = None
        try:
            rec.handle.warm()
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        with self._lock:
            now = self._clock()
            if self._stale_locked(rec, token):
                self._finish_stale_locked(rec, now)
            else:
                rec.in_flight = False
                if err is not None:
                    self._fail_locked(rec, "warm_error", "health", now)
                else:
                    self._ready_locked(rec, now)
                self._reconcile_locked(now)

    def _unload_job(self, module_id: str, token: int) -> None:
        rec = self._recs[module_id]
        err: str | None = None
        called = False
        if rec.occupied and self._does_unload(rec):
            called = True
            try:
                rec.handle.unload()
            except Exception as exc:  # noqa: BLE001
                err = type(exc).__name__
        with self._lock:
            now = self._clock()
            if self._stale_locked(rec, token):
                if called and err is None:
                    rec.occupied = False  # 晚完成的清理：資源其實已釋放
                self._finish_stale_locked(rec, now)
            elif err is not None:
                rec.in_flight = False
                self._fail_locked(rec, "unload_error", rec.unload_cause if rec.unload_cause == "shutdown" else "health", now, occupied=True)
                self._reconcile_locked(now)
            else:
                rec.in_flight = False
                rec.occupied = False
                rec.deadline = None
                reason = "shutdown" if rec.unload_cause == "shutdown" else "unloaded"
                if rec.desc.blocked:
                    reason = rec.desc.blocked_reason or reason
                self._emit_locked(rec, "unloaded", rec.unload_cause, reason)
                self._reconcile_locked(now)
        self._pub.drain()

    def _probe_job(self, module_id: str, token: int) -> None:
        rec = self._recs[module_id]
        verdict = "dead"
        try:
            verdict = rec.handle.probe()
        except Exception as exc:  # noqa: BLE001
            logger.warning("模組 %s probe 失敗：%s", module_id, type(exc).__name__)
        with self._lock:
            now = self._clock()
            if self._stale_locked(rec, token):
                self._finish_stale_locked(rec, now)
            else:
                rec.in_flight, rec.deadline = False, None
                if rec.state in _ACTIVE_STATES and not rec.desc.blocked:
                    if verdict == "dead":
                        rec.dead_count += 1
                        if rec.dead_count >= self._probe_dead_threshold:
                            self._fail_locked(rec, "probe_dead", "health", now, occupied=True)
                    else:
                        rec.dead_count = 0
                        if verdict == "degraded" and rec.state == "ready":
                            self._emit_locked(rec, "degraded", "health", "probe_degraded")
                        elif verdict == "ok" and rec.state == "degraded":
                            self._emit_locked(rec, "ready", "health", None)
                self._reconcile_locked(now)
        self._pub.drain()
