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

PR-3 增加。只有 ``UEP_MODULE_COORDINATOR`` 關閉（不建立 coordinator）時行為才完全不變；coordinator 開、預算未設時，
新的保護、停機反序、依賴 failed 處置與 hung 閘仍會生效，僅「需要 GPU 的載入」多了 fail-closed 拒絕：
- pin／lease（R1）：描述子 ``pinned``、運維 ``pin()``、有 TTL 的 lease；pinned／resident／有效 lease／仍有作用中依賴者的模組
  不可驅逐、不接受 ``request_unload``（停機與內部清理例外）。lease 有效性在檢查當下比時鐘，不依賴 tick。
- 預算（R6）：``ResourceBudget``（宣告成本＋實測空閒取較保守者）；只套在 coordinator 會呼叫 ``handle.load`` 且 ``uses_gpu`` 的模組。
  量測在 tick／start 的鎖外取樣，放行只讀快取；成本未知、量測失敗或過期一律拒絕新增 GPU 載入，被拒不排隊。
- 外部壓力（R7）：樣本顯示空閒低於保留水位 => 擋新增 GPU 載入，並一次只收回一個 coordinator 管理、無在途操作的 background／speculative
  GPU 模組（background 先）；等該次卸載完成、有更新的樣本、且過冷卻才收下一個；回復需高於水位＋回復餘裕（遲滯）。
- 優先序（R8）：只看描述子 ``priority``；``request_load`` 的 urgency／deadline 只是提示，不影響優先序或保護。
- 依賴 failed：作用中的依賴者標為 ``degraded(dependency_failed)``（不燒自己的重試次數，依賴恢復後回 ready）；停機依反向依賴序卸載；
  hung 的 handle 呼叫達上限時拒絕新增載入／探活並告警，且排隊中就已逾時的工作在開始前就被丟棄（不會事後才真的 load）。

PR-3 後段（同樣只在啟用預算時生效）：
- 需求驅動的驅逐（§7.4 規則 3～5）：``priority=on_demand`` 且 ``cause=demand`` 的載入在空間不足時，驅逐類別較低（speculative 先、background 後）、
  無保護（pinned／resident／lease／依賴者）、無在途操作的候選；一律先挑好整組候選（不夠就一個都不動），登記預留（claim），等卸載完成且有更新的樣本
  確認釋放後才放行原載入；仍不足回 ``rejected:insufficient_resource``。speculative／background 與非 demand 起因的載入永遠不驅逐（規則 2）。
- 閒置 TTL（規則 6）：描述子 ``idle_ttl_s``，speculative／on_demand 在 ready、無保護、閒置超時後於 tick 卸載（cause=idle、reason=idle_ttl）。
- ``load_cost="heavy"`` 全域序列化：同時最多一個 heavy 載入在途，其餘回 ``rejected:heavy_load_in_flight``（不排隊）。
- 宣告值 vs 觀測值校正：handle 選填 ``observed_resource()``；偏差超過 25% 記 ``vram_deviation`` 警告，之後放行與預算以 max(宣告, 觀測) 為準。
- 卸載後 NVML 差值驗證：卸載前後各取樣一次，釋放量低於持有量 × ``unload_release_ratio`` 記 ``unload_incomplete`` 並把未釋放量保守保留於預算，
  直到後續樣本顯示已釋放；量測不可用時不判定（放行本來就因量測失敗而 fail-closed）。

PR-3 不含：真實 handle（PR-4）、提議政策（PR-5）、R13 健康橋接。
"""
from __future__ import annotations

import dataclasses
import itertools
import logging
import math
import queue
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from uep.lifecycle.budget import (
    OBSERVED_DEVIATION_RATIO,
    REJECT_HEAVY_IN_FLIGHT,
    REJECT_INSUFFICIENT,
    REJECT_UNCONFIGURED,
    UNLOAD_VERIFY_MIN_MB,
    ResourceBudget,
    VramReading,
)
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
_HOLDING_STATES = frozenset({"loading", "warming", "ready", "degraded", "unloading"})  # 仍持有資源（預算側）
REJECT_POOL_SATURATED = "worker_pool_saturated"
WARMING_POLL_S = 2.0  # 觀測模組 warming 時的重新探測間隔（probe_interval_s 為 0 時用；>0 時沿用它）
DEFAULT_LEASE_TTL_S = 60.0
DEFAULT_LEASE_MAX_TTL_S = 3600.0
_PRESSURE_EVICTABLE = ("background", "speculative")  # R7：只收回 P3 -> P2
_DEMAND_EVICTABLE = ("speculative", "background")  # 需求驅逐 speculative 先、background 後，與壓力收回（background 先）相反——依設計稿 §7.4 規則 3；順序即優先序，改這裡即可調整
_TTL_CLASSES = frozenset({"speculative", "on_demand"})  # 規則 6／描述子註解：可閒置自動卸載的類別
_WAIT_EVICTION = "waiting_eviction"  # 內部哨兵：等驅逐完成，不是拒絕原因（不計數、不進 admission_reason）
_WARM_SAMPLES = 8  # 載入耗時環形緩衝長度（取中位數當 warm_p50）


class CoordinatorError(RuntimeError):
    """coordinator 使用錯誤或內部不變式被破壞（訊息不含 handle 內容）。"""


@dataclass(frozen=True)
class RequestResult:
    accepted: bool
    reason: str


@dataclass(frozen=True)
class LeaseResult:
    granted: bool
    lease_id: str | None
    state: str
    reason: str
    expires_at: float | None = None


@dataclass(frozen=True)
class _EvictWait:
    """需求載入的驅逐預留（claim）：被挑中的候選與預留的成本；其他載入在評估時要把它當作已占用。"""

    victims: tuple[str, ...]
    cost_mb: int
    deadline: float  # 登記時間 + 候選最長卸載逾時 + 一個樣本新鮮度上限；超過就放棄預留（卸載卡住或樣本一直來不了時不永久占位）


@dataclass
class _Lease:
    lease_id: str
    module_id: str
    owner: str
    expires_at: float


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
    admission_reason: str | None = None  # 最近一次被放行閘拒絕的原因（budget／壓力／worker 池）；None = 未被拒
    protected: str | None = None  # 目前的保護原因（pinned／resident／lease／has_dependents）；None = 可驅逐
    leases: int = 0  # 有效 lease 數
    observed_vram_mb: int | None = None  # handle 回報的實際 VRAM（最近一次載入後）；None = 未觀測
    unreleased_mb: int = 0  # unload_incomplete 後仍保守保留於預算的未釋放量
    waiting_eviction: bool = False  # 需求載入正在等驅逐完成


class _Rec:
    """單一模組的可變狀態；只在 coordinator 鎖內存取。"""

    __slots__ = (
        "admission_reason",
        "attempt_no",
        "cleanup_pending",
        "cooldown_until",
        "dead_count",
        "deadline",
        "desc",
        "evict_hold_until",
        "evict_wait",
        "exhausted",
        "failures",
        "handle",
        "hung",
        "in_flight",
        "job_kind",
        "job_started",
        "last_used",
        "leak_base_free",
        "leak_base_others",
        "leak_expected",
        "leak_mb",
        "observed_mb",
        "occupied",
        "op_pinned",
        "probe_due",
        "reason",
        "release_confirm_at",
        "reserved_at",
        "retry_at",
        "revision",
        "state",
        "stop_tried",
        "token",
        "unload_cause",
        "unload_submitted",
        "want_cause",
        "wanted",
        "warm_samples",
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
        self.cooldown_until = 0.0  # R12：failed 後的冷卻終點；只在 ready 時清除，任何顯式請求都不能繞過
        self.deadline: float | None = None
        self.unload_submitted = False
        self.exhausted = False
        self.unload_cause = "operator"
        self.dead_count = 0
        self.probe_due: float | None = None
        self.job_kind = ""
        self.job_started = False  # worker 已開始執行這個呼叫（排隊中逾時的工作還沒開始）
        self.hung = False  # 呼叫已逾時但 handle 還沒回來（占住一條 worker）
        self.op_pinned = False  # 運維層 pin（行程內，重啟即失）
        self.last_used = 0.0
        self.reserved_at = 0.0  # 最近一次進入 loading 的時間（預算：樣本之後才成立的預留）
        self.admission_reason: str | None = None
        self.evict_hold_until = 0.0  # 壓力收回之後的重載冷卻（與 R12 的 cooldown_until 分開）
        self.stop_tried = False  # 停機時已嘗試卸載（failed 視為終態）
        self.evict_wait: _EvictWait | None = None  # 正在等驅逐完成的預留
        self.observed_mb: int | None = None  # handle 回報的實際 VRAM；放行與預算取 max(宣告, 觀測)
        self.leak_mb = 0  # unload_incomplete：仍保守保留的未釋放量
        self.leak_base_free = 0  # 判定當時卸載前的實測空閒（之後的樣本與它比較）
        self.leak_expected = 0  # 判定當時的持有量
        self.leak_base_others = 0  # 判定當時其他 GPU 模組的持有量（恢復判定要扣除之後它們造成的空閒變化）
        self.cleanup_pending = False  # 被需求驅逐者：卸載失敗時即使 wanted=False 也要重試清理
        self.release_confirm_at = 0.0  # 時間戳 >= 此值的樣本才反映這次卸載（驅逐放行用）
        self.warm_samples: deque[float] = deque(maxlen=_WARM_SAMPLES)  # 最近幾次 loading -> ready 秒數


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
        budget: ResourceBudget | None = None,
        lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
        lease_max_ttl_s: float = DEFAULT_LEASE_MAX_TTL_S,
        max_hung_calls: int | None = None,
    ) -> None:
        if mode not in ("manage", "observe"):
            raise ValueError("mode 只能是 manage 或 observe")
        if workers < 1 or max_attempts < 1 or not backoff_s or any(b < 0 for b in backoff_s):
            raise ValueError("workers／max_attempts／backoff_s 設定不合法")
        if lease_ttl_s <= 0 or lease_max_ttl_s < lease_ttl_s:
            raise ValueError("lease TTL 設定不合法")
        if max_hung_calls is not None and max_hung_calls < 1:
            raise ValueError("max_hung_calls 必須 >= 1")
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
        self._budget = budget
        self._lease_ttl = float(lease_ttl_s)
        self._lease_max_ttl = float(lease_max_ttl_s)
        self._max_hung = max_hung_calls if max_hung_calls is not None else max(1, workers - 1)  # 預設保留一條 worker 給卸載
        self._hung_alarmed = False
        self._leases: dict[str, _Lease] = {}
        self._lease_seq = itertools.count(1)
        self._evict_target: str | None = None
        self._evict_next_at = float("-inf")
        self._evict_settled_at = float("-inf")
        self._dirty = False  # 鎖內動作改變了別的模組（例如啟動驅逐）：reconcile 要再跑一輪
        self._gpu_io_seq = 0  # 任何 GPU 模組的狀態轉移都會遞增：卸載前後的 VRAM 差值只在這段期間沒有其他 GPU 操作時才有意義

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

    def start(self, flush_timeout_s: float = 5.0) -> None:
        """驗證依賴、決定 epoch、發啟動快照（``cause=startup``，先於任何載入轉移），再排入自動載入。
        返回前等佇列內的事件真的送出（``flush``）：別的執行緒正在當 drainer 時也一樣等，快照才保證先於 Core READY 被套用。"""
        self._refresh_budget()  # 鎖外取樣；量測失敗只會讓 GPU 載入被拒，不擋啟動
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
            self._evict_target, self._evict_next_at, self._evict_settled_at = None, float("-inf"), float("-inf")
            for rec in self._recs.values():
                rec.stop_tried = False
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
            for rec in self._recs.values():  # 重啟後 failed 的模組要重排重試（遵守冷卻）；stop 期間的 failed 沒有排程
                if rec.state == "failed" and not rec.exhausted and not rec.desc.blocked:
                    rec.retry_at = max(now, rec.cooldown_until)
            self._reconcile_locked(now)
        if not self._pub.flush(flush_timeout_s):
            logger.warning("module coordinator 啟動快照在 %.1f 秒內未全部送出", flush_timeout_s)
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
            if self._leases:  # 停機強制釋放所有 lease（§9.5）
                self._count("lease_force_released")
                self._leases.clear()
            for rec in self._recs.values():
                rec.wanted = False
                rec.evict_wait = None
                rec.op_pinned = False
                if rec.state == "unloading":
                    rec.unload_cause = "shutdown"  # 已在卸載中：只改原因，不重複轉移
                    rec.stop_tried = True
            self._reconcile_locked(now)  # 依反向依賴序逐層開始卸載（見 _shutdown_step_locked）
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
                if rec.state not in ("unloaded", "failed"):  # 收不回來（含還在等依賴者卸載的）：保留占用，不假裝已卸載
                    rec.stop_tried = True
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
        return self._pub.flush(max(end - time.monotonic(), 0.05))  # 狀態穩定還不夠：事件也要真的送完

    # --- 請求入口（非阻塞，不碰 handle）---

    def request_load(
        self, module_id: str, *, cause: str = "demand", urgency: float | None = None, deadline_ms: int | None = None,
    ) -> RequestResult:
        """要求載入。``urgency``／``deadline_ms`` 只是提示（R8=A）：優先序由描述子決定，這兩個值不影響優先序、放行或保護。
        被預算／壓力／worker 池擋下時回 ``rejected:<原因>`` 且不排隊（§7.4 規則 5）。"""
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
            if not rec.wanted:
                rec.evict_wait = None  # 不沿用過期的候選名單（只有「仍在等」的重複請求才保留既有預留）
            rec.wanted, rec.want_cause = True, cause
            rec.admission_reason = None
            now = self._clock()
            self._reconcile_locked(now)
            if rec.admission_reason is not None and rec.state in ("unloaded", "failed") and not rec.in_flight:
                rec.wanted = False  # 被放行閘拒絕：不排隊
                result = RequestResult(False, f"rejected:{rec.admission_reason}")
            elif rec.evict_wait is not None and rec.state == "unloaded":
                result = RequestResult(True, "evicting")  # 已啟動驅逐，等卸載完成後再放行；最終被拒時看 status().admission_reason
            elif rec.state not in _ACTIVE_STATES and rec.cooldown_until > now:
                result = RequestResult(True, "queued_until_cooldown")  # R12：不繞過冷卻（failed、unloading、unloaded 都一樣）
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
            now = self._clock()
            if rec.state not in ("unloaded", "unloading"):
                protected = self._protection_locked(rec, now)  # D108 R1：pinned／resident／有效 lease／有依賴者 => 拒絕（不改任何狀態）
                if protected is not None:
                    self._count("unload_refused_protected")
                    return RequestResult(False, f"protected:{protected}")
            rec.wanted = False
            rec.evict_wait = None  # 請求者不要了：釋放驅逐預留
            if rec.state == "unloaded":
                return RequestResult(True, "already_unloaded")
            if rec.state == "unloading":
                return RequestResult(True, "already_unloading")
            self._begin_unload_locked(rec, cause, "cancelled" if rec.state in ("loading", "warming") else "unloaded", now)
            self._reconcile_locked(now)
        self._pub.drain()
        return RequestResult(True, "accepted")

    # --- pin／lease（D108 R1，設計稿 §7.2）---

    def pin(self, module_id: str) -> RequestResult:
        """運維層 pin（行程內，停機即失）：被 pin 的模組不可驅逐、不接受 ``request_unload``；仍可因失敗進入 failed。"""
        with self._lock:
            rec = self._recs.get(module_id)
            if rec is None:
                return RequestResult(False, "unknown_module")
            rec.op_pinned = True
            return RequestResult(True, "pinned")

    def unpin(self, module_id: str) -> RequestResult:
        """解除運維層 pin；描述子 ``pinned`` 是設定層，不能由這裡解除。"""
        with self._lock:
            rec = self._recs.get(module_id)
            if rec is None:
                return RequestResult(False, "unknown_module")
            rec.op_pinned = False
            if rec.desc.pinned:
                return RequestResult(True, "descriptor_pinned")  # 運維 pin 已解除，但設定層 pinned 仍在，模組仍受保護
            return RequestResult(True, "unpinned")

    def acquire_lease(self, module_id: str, owner: str, ttl_s: float | None = None) -> LeaseResult:
        """取得 lease：模組須處於 ready／degraded，否則 ``granted=False``（不阻塞、不觸發載入）。TTL 以 ``lease_max_ttl_s`` 為上限，
        需 ``renew_lease`` 心跳延長；過期自動釋放。lease 存活期間該模組不可驅逐、不可 ``request_unload``（停機例外）。"""
        with self._lock:
            rec = self._recs.get(module_id)
            if rec is None:
                return LeaseResult(False, None, "unknown", "unknown_module")
            if not self._started or self._stopping:
                return LeaseResult(False, None, rec.state, "not_running")
            if not isinstance(owner, str) or not owner:
                return LeaseResult(False, None, rec.state, "bad_owner")
            ttl = self._clamp_ttl(ttl_s)
            if ttl is None:
                return LeaseResult(False, None, rec.state, "bad_ttl")
            if rec.state not in _ACTIVE_STATES:
                return LeaseResult(False, None, rec.state, "not_ready")
            now = self._clock()
            lease = _Lease(f"lease-{next(self._lease_seq)}", module_id, owner, now + ttl)
            self._leases[lease.lease_id] = lease
            rec.last_used = now
            self._count("lease_granted")
            return LeaseResult(True, lease.lease_id, rec.state, "granted", lease.expires_at)

    def renew_lease(self, lease_id: str, ttl_s: float | None = None) -> LeaseResult:
        """心跳：從現在起延長 TTL。lease 已過期、被回收或模組已失效（unloaded／failed）回 ``granted=False``，持有者應視為失去保護。"""
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return LeaseResult(False, None, "unknown", "lease_unknown")
            rec = self._recs[lease.module_id]
            now = self._clock()
            if lease.expires_at <= now:
                rec.last_used = now
                del self._leases[lease_id]
                self._count("lease_expired")
                return LeaseResult(False, None, rec.state, "lease_expired")
            ttl = self._clamp_ttl(ttl_s)
            if ttl is None:
                return LeaseResult(False, None, rec.state, "bad_ttl")
            lease.expires_at = now + ttl
            rec.last_used = now
            return LeaseResult(True, lease_id, rec.state, "renewed", lease.expires_at)

    def release_lease(self, lease_id: str) -> bool:
        """釋放一個 lease；冪等（不存在回 False）。"""
        with self._lock:
            lease = self._leases.pop(lease_id, None)
            if lease is None:
                return False
            self._recs[lease.module_id].last_used = self._clock()  # 閒置計時從釋放起算（idle TTL、驅逐排序）
            self._count("lease_released")
            return True

    def release_by_owner(self, owner: str) -> int:
        """釋放某 owner 的全部 lease（Supervisor 保底）；冪等，回傳釋放數。"""
        with self._lock:
            ids = [lid for lid, lease in self._leases.items() if lease.owner == owner]
            now = self._clock()
            for lid in ids:
                self._recs[self._leases[lid].module_id].last_used = now
                del self._leases[lid]
            if ids:
                self.counters["lease_released"] = self.counters.get("lease_released", 0) + len(ids)
            return len(ids)

    def pool_status(self) -> Mapping[str, int | bool]:
        """worker 池觀測：在途呼叫數、hung（已逾時但 handle 未回）數、上限，與是否已達上限。"""
        with self._lock:
            hung = self._hung_count_locked()
            return {
                "workers": self._worker_count, "in_flight": sum(1 for r in self._recs.values() if r.in_flight),
                "hung": hung, "max_hung": self._max_hung, "saturated": hung >= self._max_hung,
            }

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
            rec.failures, rec.exhausted, rec.cooldown_until = 0, False, 0.0
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
        self._refresh_budget()  # 鎖外取樣
        with self._lock:
            if not self._started:
                return
            t = self._clock() if now is None else now
            self._timeouts_locked(t)
            self._expire_leases_locked(t)
            self._probes_locked(t)
            self._leaks_locked(t)
            self._idle_ttl_locked(t)
            self._pressure_locked(t)
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

    def _refresh_budget(self) -> None:
        """鎖外量測 VRAM 並更新快取（只在有需要 GPU 的模組時才量，不碰 GPU 就不呼叫 probe）。失敗只影響 GPU 載入與壓力判斷。"""
        if self._budget is None or self._mode != "manage":
            return
        if not any(r.desc.uses_gpu for r in self._recs.values()):
            return
        self._budget.refresh(self._clock())

    def _clamp_ttl(self, ttl_s: float | None) -> float | None:
        if ttl_s is None:
            return self._lease_ttl
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)) or ttl_s <= 0:
            return None
        return min(float(ttl_s), self._lease_max_ttl)

    def _active_leases_locked(self, module_id: str, now: float) -> int:
        return sum(1 for lease in self._leases.values() if lease.module_id == module_id and lease.expires_at > now)

    def _expire_leases_locked(self, now: float) -> None:
        for lid in [lid for lid, lease in self._leases.items() if lease.expires_at <= now]:
            self._recs[self._leases[lid].module_id].last_used = now  # 閒置計時從到期起算，避免剛用完就被 idle TTL／驅逐排序視為很久沒用
            del self._leases[lid]
            self._count("lease_expired")

    def _revoke_leases_locked(self, module_id: str) -> None:
        for lid in [lid for lid, lease in self._leases.items() if lease.module_id == module_id]:
            del self._leases[lid]
            self._recs[module_id].last_used = self._clock()
            self._count("lease_revoked")

    def _live_dependents_locked(self, rec: _Rec) -> list[_Rec]:
        """依賴本模組、且正在載入或作用中的模組。"""
        mid = rec.desc.module_id
        return [r for r in self._recs.values() if mid in r.desc.depends_on and r.state in ("loading", "warming", "ready", "degraded")]

    def _protection_locked(self, rec: _Rec, now: float) -> str | None:
        """不可驅逐／不可 request_unload 的原因（pinned > resident > lease > has_dependents）；None = 可動。停機與內部清理不經此檢查。"""
        if rec.desc.pinned or rec.op_pinned:
            return "pinned"
        if rec.desc.priority == "resident":
            return "resident"
        if self._active_leases_locked(rec.desc.module_id, now) > 0:
            return "lease"
        if self._live_dependents_locked(rec):
            return "has_dependents"
        return None

    def _hung_count_locked(self) -> int:
        return sum(1 for r in self._recs.values() if r.hung)

    def _hung_check_locked(self) -> None:
        hung = self._hung_count_locked()
        saturated = hung >= self._max_hung
        if saturated and not self._hung_alarmed:
            logger.error("module worker 池告警：%d/%d 個 handle 呼叫逾時未回（上限 %d）；暫停新增載入與探活", hung, self._worker_count, self._max_hung)
            self._count("hung_pool_alarm")
        self._hung_alarmed = saturated

    def _cost_locked(self, rec: _Rec) -> int | None:
        """放行與預算用的成本：max(宣告, 觀測)（§7.3）；宣告未知維持 None（=> unmeasured_cost）。"""
        declared = rec.desc.vram_mb
        if declared is None:
            return None
        return max(declared, rec.observed_mb or 0)

    def _claim_valid_locked(self, r: _Rec, now: float) -> bool:
        """驅逐預留是否仍有效；失效就地清掉並放棄請求（不排隊）：停機、請求者不要了／離開 unloaded、依賴已不 ready
        （例如依賴被別人驅逐或 failed，載入永遠開始不了）、或超過期限。"""
        wait = r.evict_wait
        if wait is None:
            return False
        why = None
        if self._stopping or not r.wanted or r.state != "unloaded":
            why = ""
        elif not self._deps_ready_locked(r):
            why = "dependency_not_ready"
        elif now >= wait.deadline:
            why = "eviction_expired"
        if why is None:
            return True
        r.evict_wait = None
        if why:
            r.wanted = False
            r.admission_reason = why
            self._count("evict_claim_dropped")
            logger.info("模組 %s 的驅逐預留作廢：%s", r.desc.module_id, why)
        return False

    def _claims_locked(self, exclude: _Rec | None, now: float) -> int:
        """其他載入的驅逐預留總和（等驅逐完成的需求載入）；已放棄的預留（不再想載入、離開 unloaded、停機）在這裡順手清掉。
        候選仍在卸載中（unloading）時，它的成本還算在持有量裡，預留只計超出這部分的缺口，避免同一塊空間被算兩次而擋掉
        另一個本來可以靠別的候選成功的載入。"""
        total = 0
        for r in self._recs.values():
            wait = r.evict_wait
            if wait is None or r is exclude:
                continue
            if not self._claim_valid_locked(r, now):
                continue
            covering = sum(
                self._cost_locked(self._recs[v]) or 0 for v in wait.victims if self._recs[v].state == "unloading"
            )
            total += max(0, wait.cost_mb - covering)
        return total

    def _held_locked(self, now: float, exclude: _Rec | None = None) -> tuple[int, int]:
        """所有仍持有 VRAM 的 GPU 模組的成本總和（max(宣告, 觀測)），與其中「樣本尚未反映」的部分（載入中、或在最近樣本之後才進入載入）。
        未確認釋放（occupied）的不退還；unload_incomplete 的未釋放量繼續保留（與模組自己的成本取大者，不重複計）；
        其他載入的驅逐預留（``exclude`` 以外）也算已占用且尚未反映，避免兩個載入搶同一塊剛騰出的空間。
        宣告值未知的算 0（實測側仍會反映真實占用，取較保守者）。"""
        assert self._budget is not None
        _, taken_at = self._budget.reading()
        held = pending = 0
        for r in self._recs.values():
            if not r.desc.uses_gpu:
                continue
            amount = 0
            if r.occupied or r.state in _HOLDING_STATES:
                cost = self._cost_locked(r) or 0
                amount = cost
                if r.state in ("loading", "warming") or (r.in_flight and r.job_kind == "load") or taken_at is None or r.reserved_at >= taken_at:
                    pending += cost
            held += max(amount, r.leak_mb)
        claims = self._claims_locked(exclude, now)
        return held + claims, pending + claims

    def _heavy_in_flight_locked(self, rec: _Rec) -> bool:
        """另一個 heavy 模組正在載入（含暖機，或載入呼叫仍在 worker 上），或已為它驅逐完正在等放行（預留中，不讓別的 heavy 插隊）：
        載入峰值與磁碟 I/O 不疊加。已逾時而 handle 仍卡住（hung）的載入不計，否則一個卡死的載入會永遠擋住所有 heavy；
        它占住的資源仍由 occupied 留在預算裡。"""
        return any(
            r is not rec and r.desc.load_cost == "heavy" and not self._observing(r)
            and (
                r.state in ("loading", "warming") or (r.in_flight and r.job_kind == "load" and not r.hung)
                or r.evict_wait is not None
            )
            for r in self._recs.values()
        )

    def _may_evict_locked(self, rec: _Rec) -> bool:
        """§7.4 規則 2／3：只有 on_demand 類別、起因是 demand（ensure）的載入可以驅逐別人；預載（proposal）、startup 自動載入、
        speculative／background 一律只撿現有空位。"""
        return (
            self._mode == "manage" and not self._stopping and rec.state == "unloaded"
            and rec.desc.priority == "on_demand" and rec.want_cause == "demand"
        )

    def _dep_closure_locked(self, rec: _Rec) -> set[str]:
        seen: set[str] = set()
        stack = list(rec.desc.depends_on)
        while stack:
            d = stack.pop()
            if d not in seen:
                seen.add(d)
                stack.extend(self._recs[d].desc.depends_on)
        return seen

    @staticmethod
    def _warm_p50(rec: _Rec) -> float | None:
        return statistics.median(rec.warm_samples) if rec.warm_samples else None

    def _pick_victims_locked(self, rec: _Rec, now: float, shortfall_mb: int) -> list[_Rec]:
        """挑出能補足缺口的候選（全有或全無：湊不夠就一個都不動）。排序：類別（speculative 先）-> last_used 最舊 -> warm_p50 小者先 ->
        module_id（確定性）。候選必須 uses_gpu、有已知成本、ready／degraded、無在途操作、coordinator 能卸載、無保護
        （pinned／resident／有效 lease／作用中依賴者），且不是請求者自己的依賴。"""
        deps = self._dep_closure_locked(rec)
        for other in self._recs.values():  # 別的等驅逐的請求者所依賴的模組也不能動（否則它永遠載不起來）
            if other is not rec and other.evict_wait is not None:
                deps |= self._dep_closure_locked(other)
        cands = [
            r for r in self._recs.values()
            if r is not rec and r.desc.module_id not in deps and r.desc.uses_gpu and r.desc.priority in _DEMAND_EVICTABLE
            and r.state in _ACTIVE_STATES and not r.in_flight and not self._observing(r) and self._does_unload(r)
            and (self._cost_locked(r) or 0) > 0 and self._protection_locked(r, now) is None
        ]
        cands.sort(key=lambda r: (
            _DEMAND_EVICTABLE.index(r.desc.priority), r.last_used,
            math.inf if self._warm_p50(r) is None else self._warm_p50(r), r.desc.module_id,
        ))
        picked: list[_Rec] = []
        freed = 0
        for r in cands:
            if freed >= shortfall_mb:
                break
            picked.append(r)
            freed += self._cost_locked(r) or 0
        return picked if freed >= shortfall_mb else []

    def _victims_settled_locked(self, wait: _EvictWait, now: float) -> bool:
        """候選都已卸載完成、且有不早於各次卸載的樣本確認釋放（failed／逾時的候選不會釋放，占用仍計入預算，交給放行評估拒絕）。"""
        assert self._budget is not None
        _, taken_at = self._budget.reading()  # 最近一次成功樣本（不論新鮮度）：它不早於卸載就足以證明釋放已被反映
        for vid in wait.victims:
            v = self._recs[vid]
            if v.state == "unloading":
                return False
            if v.state == "unloaded" and (taken_at is None or taken_at < v.release_confirm_at):
                return False  # 未確認釋放：量測暫時不可用時也繼續等，由預留期限收尾
        return True

    def _admit_via_eviction_locked(self, rec: _Rec, now: float, adm: Any, cost: int | None, record: bool) -> str | None:
        """空間不足的需求載入：啟動驅逐（只在 record=True 時）並等待；候選釋放後重新評估。回傳 None = 放行，``_WAIT_EVICTION`` = 等待。"""
        wait = rec.evict_wait
        if wait is None:  # 剛發現不足：挑候選、登記預留
            victims = self._pick_victims_locked(rec, now, (cost or 0) - (adm.effective_free_mb or 0))
            if not victims:
                return REJECT_INSUFFICIENT
            for v in victims:
                v.wanted = False  # 被驅逐者不自動重載
                v.cleanup_pending = True  # 但卸載失敗時仍要重試清理，不能因 wanted=False 永遠卡在 failed 占用
                v.evict_hold_until = now + (self._budget.config.evict_cooldown_s if self._budget else 0.0)  # 與壓力收回相同的重載冷卻
                self._count("evicted")
                self._count("evicted_for_demand")
                logger.info("需求載入 %s 空間不足：驅逐模組 %s（%s）", rec.desc.module_id, v.desc.module_id, v.desc.priority)
                self._begin_unload_locked(v, "eviction", "evicted_demand", now)
            rec.evict_wait = _EvictWait(
                tuple(v.desc.module_id for v in victims), cost or 0,
                now + max(v.desc.unload_timeout_s for v in victims) + (self._budget.config.reading_max_age_s if self._budget else 0.0),
            )
            self._dirty = True
            return _WAIT_EVICTION
        if now >= wait.deadline:  # 預留逾期（候選卸載卡住或樣本一直來不了）：放棄，不排隊
            if record:
                rec.evict_wait, rec.wanted = None, False
                self._count("evict_claim_dropped")
            return "eviction_expired"
        if adm.ok:  # 已有足夠空間（候選釋放，或別處騰出）
            if record:
                rec.evict_wait = None
            return None
        if not self._victims_settled_locked(wait, now):
            return _WAIT_EVICTION  # 候選還在卸載或尚未確認釋放：量測失敗／過期／壓力也先等，待結算後才依最新樣本判定
        if adm.reason != REJECT_INSUFFICIENT:  # 結算後仍量測失敗／過期／壓力：不再等，也不排隊
            if record:
                rec.evict_wait, rec.wanted = None, False
            return adm.reason
        if record:  # 驅逐後仍不足：不排隊，也不再挑下一輪候選（只驅逐一輪）
            rec.evict_wait, rec.wanted = None, False
            self._count("evict_insufficient")
        return REJECT_INSUFFICIENT

    def _admit_locked(self, rec: _Rec, now: float, *, record: bool = True) -> str | None:
        """載入放行閘：回傳拒絕原因，None = 放行。只套在 coordinator 真的會呼叫 handle.load 的模組（觀測型不經過）。"""
        reason: str | None = None
        if self._hung_count_locked() >= self._max_hung:
            reason = REJECT_POOL_SATURATED
        elif self._budget is not None and not self._observing(rec) and rec.desc.load_cost == "heavy" and self._heavy_in_flight_locked(rec):
            reason = REJECT_HEAVY_IN_FLIGHT  # heavy 全域序列化（§7.3）；被擋不排隊
        elif rec.desc.uses_gpu and not self._observing(rec):
            if self._budget is None:
                reason = REJECT_UNCONFIGURED  # 預算管理未啟用：fail-closed，不猜 VRAM 夠不夠
            elif rec.evict_hold_until > now:
                reason = "evict_hold"
            else:
                held, pending = self._held_locked(now, exclude=rec)
                cost = self._cost_locked(rec)
                adm = self._budget.evaluate(cost_mb=cost, held_mb=held, pending_mb=pending, now=now)
                reason = adm.reason
                if rec.evict_wait is not None or (reason == REJECT_INSUFFICIENT and record and self._may_evict_locked(rec)):
                    reason = self._admit_via_eviction_locked(rec, now, adm, cost, record)
        if record and rec.evict_wait is not None and reason is not None and reason != _WAIT_EVICTION:
            rec.evict_wait, rec.wanted = None, False  # 等驅逐期間被別的條件擋下（heavy、worker 池…）：不排隊，放棄
        if record:
            shown = None if reason == _WAIT_EVICTION else reason
            if shown != rec.admission_reason:
                if shown is not None:
                    self._count(f"rejected_{shown}")
                    logger.info("模組 %s 載入被拒：%s", rec.desc.module_id, shown)
                rec.admission_reason = shown
        return reason

    def _pressure_locked(self, now: float) -> None:
        """外部壓力（R7）：擋新增 GPU 載入已由放行閘處理；這裡負責收回。一次一個，且需有更新的樣本與冷卻。"""
        if self._budget is None or self._mode != "manage" or self._stopping:
            return
        pressure = self._budget.update_pressure(now)  # 旗標先更新：收回目標卡住時也要能隨外部壓力解除而放行新載入
        if self._evict_target is not None:  # 上一次收回尚未結案：先等它完成（未確認釋放不算數）
            target = self._recs[self._evict_target]
            if target.state in ("unloading", "ready", "degraded") or target.in_flight:
                return
            self._evict_target = None
            self._evict_settled_at = now
            self._evict_next_at = now + self._budget.config.evict_cooldown_s
        if not pressure or now < self._evict_next_at:
            return
        reading, taken_at = self._budget.fresh_reading(now)
        if reading is None or taken_at is None or taken_at <= self._evict_settled_at:
            return  # 樣本不可用或不比上次收回完成更新：不憑舊資料再收
        candidates = [
            r for r in self._recs.values()
            if r.desc.uses_gpu and r.desc.priority in _PRESSURE_EVICTABLE and r.state in _ACTIVE_STATES and not r.in_flight
            and not self._observing(r) and self._does_unload(r) and self._protection_locked(r, now) is None
        ]
        if not candidates:
            return
        victim = min(candidates, key=lambda r: (_PRESSURE_EVICTABLE.index(r.desc.priority), r.last_used, r.desc.module_id))
        victim.wanted = False
        victim.evict_hold_until = now + self._budget.config.evict_cooldown_s
        self._evict_target = victim.desc.module_id
        self._count("evicted")
        logger.warning("外部 VRAM 壓力：收回模組 %s（%s）", victim.desc.module_id, victim.desc.priority)
        self._begin_unload_locked(victim, "pressure", "evicted_pressure", now)

    def _idle_ttl_locked(self, now: float) -> None:
        """規則 6：speculative／on_demand 在 ready、無保護、閒置超過 ``idle_ttl_s`` 即卸載（不自動重載）。
        只在啟用預算時生效；pinned／resident／有 lease／有作用中依賴者的模組不動。"""
        if self._budget is None or self._mode != "manage" or self._stopping:
            return
        for rec in self._recs.values():
            ttl = rec.desc.idle_ttl_s
            if (
                ttl is None or rec.desc.priority not in _TTL_CLASSES or rec.state != "ready" or rec.in_flight
                or self._observing(rec) or not self._does_unload(rec) or now - rec.last_used <= ttl
                or self._protection_locked(rec, now) is not None
            ):
                continue
            rec.wanted = False
            self._count("idle_ttl_unload")
            logger.info("模組 %s 閒置超過 %.1f 秒：自動卸載", rec.desc.module_id, ttl)
            self._begin_unload_locked(rec, "idle", "idle_ttl", now)

    def _others_held_locked(self, rec: _Rec) -> int:
        """其他 GPU 模組目前持有的成本總和（不含 leak：leak 是已經不在的記帳，不代表空閒會再變）。
        近似：這裡用宣告／觀測成本，而 ``leak_base_free`` 是實測空閒，兩者對同一個模組未必相等；恢復判定因此是估計，
        誤差方向由門檻（0.5）吸收，且不會讓未釋放量超過判定當時的持有量。"""
        return sum(
            self._cost_locked(r) or 0 for r in self._recs.values()
            if r is not rec and r.desc.uses_gpu and (r.occupied or r.state in _HOLDING_STATES)
        )

    def clear_unreleased(self, module_id: str) -> RequestResult:
        """運維入口：確認未釋放的 VRAM 已不存在（例如重啟過行程外的占用者）後，清除 unload_incomplete 保留的預算。
        leak 沒有自動到期：沒有樣本能證明已釋放時寧可一直保守，由運維決定何時退還。"""
        with self._lock:
            rec = self._recs.get(module_id)
            if rec is None:
                return RequestResult(False, "unknown_module")
            if rec.leak_mb <= 0:
                return RequestResult(True, "nothing_to_clear")
            rec.leak_mb = 0
            self._count("unload_leak_cleared")
            return RequestResult(True, "cleared")

    def _leaks_locked(self, now: float) -> None:
        """unload_incomplete 的後續樣本：釋放量達門檻就退還保留的預算，否則依最新樣本更新未釋放量。模組又在持有資源時不判（基準已失效）。"""
        if self._budget is None:
            return
        ratio = self._budget.config.unload_release_ratio
        reading = None
        for rec in self._recs.values():
            if rec.leak_mb <= 0 or rec.occupied or rec.state in _HOLDING_STATES:
                continue
            if reading is None:
                reading, _ = self._budget.fresh_reading(now)
                if reading is None:
                    return  # 量測不可用：維持保守
            # 扣除判定之後其他模組持有量的變化：它們載入造成的空閒下降不是這個模組沒釋放（反之亦然）
            delta = reading.free_mb - rec.leak_base_free + (self._others_held_locked(rec) - rec.leak_base_others)
            if ratio > 0 and delta < ratio * rec.leak_expected:
                rec.leak_mb = min(rec.leak_expected, max(1, rec.leak_expected - max(delta, 0)))
            else:
                logger.info("模組 %s 先前未釋放的 VRAM 已釋放（event=unload_recovered）", rec.desc.module_id)
                self._count("unload_recovered")
                rec.leak_mb = 0

    def _judge_release_locked(self, rec: _Rec, before: VramReading | None, after: VramReading | None, clean: bool) -> None:
        """卸載前後的 NVML 差值驗證（§9.2）：釋放量 < 持有量 × ratio => unload_incomplete，未釋放量保守保留。
        任一次量測不可用、或這段期間有其他 GPU 載入／卸載同時進行（差值被汙染）就不判定：之後放行本來就因量測失敗而
        fail-closed，且實測空閒會如實反映；外部行程的同時變動無法排除，這是門檻取 0.5 的另一個理由。"""
        if self._budget is None or before is None or after is None:
            return
        if not clean:
            self._count("unload_unverified_concurrent")
            return
        expected = self._cost_locked(rec) or 0
        ratio = self._budget.config.unload_release_ratio
        if ratio <= 0 or expected < UNLOAD_VERIFY_MIN_MB:
            return
        delta = after.free_mb - before.free_mb
        rec.leak_base_free, rec.leak_expected = before.free_mb, expected
        rec.leak_base_others = self._others_held_locked(rec)
        if delta >= ratio * expected:
            rec.leak_mb = 0
            return
        rec.leak_mb = max(1, expected - max(delta, 0))
        self._count("unload_incomplete")
        logger.warning(
            "event=unload_incomplete module=%s expected_mb=%d released_mb=%d；未釋放量保守保留於預算", rec.desc.module_id, expected, delta,
            extra={"event": "unload_incomplete", "module_id": rec.desc.module_id},
        )

    def _should_verify_unload(self, rec: _Rec) -> bool:
        return (
            self._budget is not None and self._mode == "manage" and not self._stopping and rec.desc.uses_gpu
            and not self._observing(rec) and self._budget.config.unload_release_ratio > 0
            and (self._cost_locked(rec) or 0) >= UNLOAD_VERIFY_MIN_MB
        )

    def _observe_vram(self, rec: _Rec) -> int | None:
        """鎖外向 handle 取實際 VRAM 用量（選填介面）；沒有、失敗或值不合法都回 None（不校正）。"""
        if self._budget is None or not rec.desc.uses_gpu or self._observing(rec):
            return None
        fn = getattr(rec.handle, "observed_resource", None)
        if fn is None:
            return None
        box: dict[str, Any] = {}

        def call() -> None:
            try:
                box["v"] = fn()
            except Exception as exc:  # noqa: BLE001
                box["err"] = type(exc).__name__

        # 限時：觀測只是校正用，卡住不得拖到載入逾時而把已成功的載入判成 load_timeout（卡住的呼叫留在 daemon 執行緒上）
        thread = threading.Thread(target=call, name="UepObserveResource", daemon=True)
        thread.start()
        thread.join(self._budget.config.read_timeout_s)
        if thread.is_alive() or "err" in box:
            logger.warning("模組 %s observed_resource %s；略過校正", rec.desc.module_id, "逾時" if thread.is_alive() else f"失敗：{box['err']}")
            return None
        mb = getattr(box.get("v"), "vram_mb", None)
        if isinstance(mb, bool) or not isinstance(mb, int) or mb < 0:
            return None
        return mb

    def _calibrate_locked(self, rec: _Rec, observed: int | None) -> None:
        """記錄觀測值；與宣告值偏差超過 25% 記警告（event=vram_deviation）。之後的放行與預算用 max(宣告, 觀測)。"""
        declared = rec.desc.vram_mb
        if observed is None or declared is None:
            return
        rec.observed_mb = observed
        if abs(observed - declared) > OBSERVED_DEVIATION_RATIO * max(declared, 1):
            self._count("vram_deviation")
            logger.warning(
                "event=vram_deviation module=%s declared_mb=%d observed_mb=%d；放行改以 max(宣告, 觀測) 為準",
                rec.desc.module_id, declared, observed, extra={"event": "vram_deviation", "module_id": rec.desc.module_id},
            )

    def _dep_broken_locked(self, rec: _Rec) -> bool:
        """作用中的模組有依賴不在作用中（failed、重試載入中、清理中…）或遞移地因依賴 failed 而 degraded。
        依賴恢復到 ready 前一律維持 degraded，避免依賴在 failed／unloading／loading 間重試時依賴者反覆翻轉。"""
        for d in rec.desc.depends_on:
            dep = self._recs[d]
            if dep.state not in _ACTIVE_STATES or (dep.state == "degraded" and dep.reason == "dependency_failed"):
                return True
        return False

    def _dependents_done_locked(self, rec: _Rec) -> bool:
        """停機時：所有依賴本模組者都已卸載（或已嘗試卸載而 failed）才輪到本模組。"""
        mid = rec.desc.module_id
        for r in self._recs.values():
            if mid in r.desc.depends_on and not (r.state == "unloaded" or (r.state == "failed" and r.stop_tried)):
                return False
        return True

    def _shutdown_step_locked(self, rec: _Rec, now: float) -> bool:
        if rec.state in ("unloaded", "unloading") or (rec.state == "failed" and rec.stop_tried):
            return False
        if not self._dependents_done_locked(rec):
            return False
        rec.stop_tried = True
        self._begin_unload_locked(rec, "shutdown", "shutdown", now)
        return True

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
            admission_reason=rec.admission_reason, protected=self._protection_locked(rec, self._clock()),
            leases=self._active_leases_locked(rec.desc.module_id, self._clock()),
            observed_vram_mb=rec.observed_mb, unreleased_mb=rec.leak_mb, waiting_eviction=rec.evict_wait is not None,
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
        if rec.desc.uses_gpu:
            self._gpu_io_seq += 1
        if state == "loading":
            rec.reserved_at = self._clock()
        elif state == "ready":
            rec.last_used = self._clock()
            if payload["prev_state"] in ("loading", "warming"):
                rec.warm_samples.append(max(rec.last_used - rec.reserved_at, 0.0))  # 驅逐排序的重載成本（warm_p50）
                rec.reserved_at = rec.last_used  # 載入完成才成立的占用：下一個樣本之前仍算「樣本尚未反映」
        elif state in ("unloaded", "failed"):
            self._revoke_leases_locked(rec.desc.module_id)  # 模組已失效：lease 不再有意義，持有者 renew 時會得知
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
        now = self._clock()
        for rec in self._recs.values():
            if rec.state in _TRANSIENT_STATES:
                return False
            if self._can_begin_load_locked(rec, now, record=False):
                return False
        return True

    def _deps_ready_locked(self, rec: _Rec) -> bool:
        return all(self._recs[d].state in _ACTIVE_STATES for d in rec.desc.depends_on)

    def _can_begin_load_locked(self, rec: _Rec, now: float, *, record: bool = True) -> bool:
        return (
            self._started and not self._stopping and rec.state == "unloaded" and rec.wanted and not rec.desc.blocked
            and not rec.in_flight and now >= rec.cooldown_until and self._deps_ready_locked(rec)
            and self._admit_locked(rec, now, record=record) is None
        )

    def _submit_locked(self, fn: Callable[..., None], rec: _Rec, kind: str, deadline_s: float, now: float) -> None:
        rec.in_flight, rec.job_kind = True, kind
        rec.job_started, rec.hung = False, False
        rec.deadline = now + deadline_s
        assert self._workers is not None
        self._workers.submit(fn, rec.desc.module_id, rec.token)

    def _begin_load_locked(self, rec: _Rec, now: float) -> None:
        rec.token += 1
        rec.admission_reason = None
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
        rec.evict_wait = None
        if occupied:
            rec.occupied = True
        rec.failures += 1
        rec.dead_count = 0
        rec.retry_at, rec.exhausted = None, False
        rec.cooldown_until = now + self._backoff[min(rec.failures - 1, len(self._backoff) - 1)]
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
        rec.cooldown_until = 0.0
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
                if self._dirty:  # 別的模組被改動（例如啟動驅逐）：要再跑一輪才會送出它們的卸載
                    self._dirty, progressed = False, True

    def _reconcile_one_locked(self, rec: _Rec, now: float) -> bool:
        if rec.state == "unloading":
            if not rec.in_flight and not rec.unload_submitted:
                rec.unload_submitted = True
                self._submit_locked(self._unload_job, rec, "unload", rec.desc.unload_timeout_s, now)
                return True
            return False
        if self._stopping:
            return self._shutdown_step_locked(rec, now)  # 依反向依賴序卸載（§9.5）
        if rec.state in _ACTIVE_STATES:  # 依賴 failed：依賴者標 degraded（不燒自己的重試次數）；依賴恢復後回 ready
            broken = self._dep_broken_locked(rec)
            if rec.state == "ready" and broken:
                self._emit_locked(rec, "degraded", "health", "dependency_failed")
                return True
            if rec.state == "degraded" and rec.reason == "dependency_failed" and not broken:
                self._emit_locked(rec, "ready", "health", None)
                return True
            return False
        if rec.state == "unloaded":
            if rec.evict_wait is not None and not self._claim_valid_locked(rec, now):
                return False  # 預留已作廢（依賴不在了、逾期…）：請求已放棄
            if self._can_begin_load_locked(rec, now):
                self._begin_load_locked(rec, now)
                return True
            return False
        if (
            rec.state == "failed" and (rec.wanted or (rec.cleanup_pending and rec.occupied and self._does_unload(rec)))
            and not rec.desc.blocked and not rec.exhausted and not rec.in_flight
            and rec.retry_at is not None and now >= rec.retry_at
            and (self._deps_ready_locked(rec) or (not rec.wanted and rec.cleanup_pending))  # 清理只是卸載，不需要依賴就緒
            and (rec.occupied and self._does_unload(rec) or self._admit_locked(rec, now) is None)  # 先清殘留不需放行；重 load 需要
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
            if rec.job_started:
                rec.hung = True  # handle 已在 worker 上執行而沒回來：占住一條執行緒
                self._count("hung_call")
            else:
                self._count("queued_timeout")  # 還在佇列：開始前會被丟棄，不占 worker
            self._fail_locked(rec, reason, "health", now, occupied=rec.job_started)  # 沒開始的工作 handle 從沒被呼叫，不佔資源
            self._hung_check_locked()

    def _warming_poll_s(self) -> float:
        return self._probe_interval if self._probe_interval > 0 else WARMING_POLL_S

    def _enter_warming_observed_locked(self, rec: _Rec, now: float) -> None:
        """觀測模組 probe 回 warming：loading -> warming（已在 warming 則維持），排定下次探測；失敗計數與冷卻不動。"""
        rec.dead_count = 0
        rec.deadline = None
        if rec.state != "warming":
            self._emit_locked(rec, "warming", rec.want_cause, None)
        rec.probe_due = now + self._warming_poll_s()

    def _probes_locked(self, now: float) -> None:
        if self._stopping or self._hung_count_locked() >= self._max_hung:
            return
        for rec in self._recs.values():
            if rec.in_flight or rec.probe_due is None or now < rec.probe_due:
                continue
            warming_observed = rec.state == "warming" and self._observing(rec)  # probe_interval=0 也要重探，否則永遠卡在 warming
            if warming_observed or (rec.state in _ACTIVE_STATES and self._probe_interval > 0):
                rec.probe_due = now + (self._warming_poll_s() if warming_observed else self._probe_interval)
                self._submit_locked(self._probe_job, rec, "probe", rec.desc.load_timeout_s, now)

    # --- worker 工作（鎖外呼叫 handle，結果回鎖內以 token 驗證）---

    def _stale_locked(self, rec: _Rec, token: int) -> bool:
        return rec.token != token

    def _finish_stale_locked(self, rec: _Rec, now: float) -> None:
        rec.in_flight, rec.job_started, rec.hung = False, False, False
        self._count("late_completion_ignored")
        self._hung_check_locked()
        self._reconcile_locked(now)
        self._cond.notify_all()

    def _start_or_skip_locked(self, rec: _Rec, token: int, now: float) -> bool:
        """worker 取到工作時先驗 token：排隊中就已被取消／逾時的工作不得事後才真的呼叫 handle。回傳 True = 可以執行。"""
        if self._stale_locked(rec, token):
            self._count("stale_before_start")
            self._finish_stale_locked(rec, now)
            return False
        rec.job_started = True
        return True

    def _load_job(self, module_id: str, token: int) -> None:
        rec = self._recs[module_id]
        with self._lock:
            go = self._start_or_skip_locked(rec, token, self._clock())
        if not go:
            self._pub.drain()
            return
        err: str | None = None
        verdict: str | None = None
        try:
            if self._observing(rec):
                verdict = rec.handle.probe()  # 觀測：不呼叫 load
            else:
                rec.handle.load()
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        observed = self._observe_vram(rec) if err is None and verdict is None and not rec.desc.needs_warm else None
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
                elif verdict == "warming":  # 觀測模組持有者還在載入：維持 warming，不燒重試次數、不判 failed
                    self._enter_warming_observed_locked(rec, now)
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
                    self._calibrate_locked(rec, observed)
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
        observed = self._observe_vram(rec) if err is None else None
        with self._lock:
            now = self._clock()
            if self._stale_locked(rec, token):
                self._finish_stale_locked(rec, now)
            else:
                rec.in_flight = False
                if err is not None:
                    self._fail_locked(rec, "warm_error", "health", now)
                else:
                    self._calibrate_locked(rec, observed)
                    self._ready_locked(rec, now)
                self._reconcile_locked(now)

    def _unload_job(self, module_id: str, token: int) -> None:
        rec = self._recs[module_id]
        with self._lock:
            go = self._start_or_skip_locked(rec, token, self._clock())
            seq0 = self._gpu_io_seq
            clean = go and not any(  # 窗口一開始就有別的 GPU 操作在途：它的配置／釋放會混進差值
                r is not rec and r.desc.uses_gpu and (r.state in _TRANSIENT_STATES or r.in_flight) and r.job_kind in ("load", "unload")
                for r in self._recs.values()
            )
        if not go:
            self._pub.drain()
            return
        err: str | None = None
        called = False
        before: VramReading | None = None
        after: VramReading | None = None
        after_t = 0.0
        if rec.occupied and self._does_unload(rec):
            called = True
            budget = self._budget if self._should_verify_unload(rec) else None
            if budget is not None:  # 卸載前後各取樣一次（鎖外、有逾時）；量測失敗只代表不判定
                before = budget.measure(self._clock())
            try:
                rec.handle.unload()
            except Exception as exc:  # noqa: BLE001
                err = type(exc).__name__
            if budget is not None and err is None:
                after_t = self._clock()
                after = budget.measure(after_t)
            clean = clean and self._gpu_io_seq == seq0
        with self._lock:
            now = self._clock()
            if self._stale_locked(rec, token):
                if called and err is None:
                    rec.occupied = False  # 晚完成的清理：資源其實已釋放
                    rec.cleanup_pending = False
                self._finish_stale_locked(rec, now)
            elif err is not None:
                rec.in_flight = False
                self._fail_locked(rec, "unload_error", rec.unload_cause if rec.unload_cause == "shutdown" else "health", now, occupied=True)
                self._reconcile_locked(now)
            else:
                rec.in_flight = False
                rec.occupied = False
                rec.cleanup_pending = False
                rec.deadline = None
                # 之後的樣本時間戳要不早於此值才算反映了這次卸載：有卸載後樣本用它，否則要等比現在更新的樣本
                rec.release_confirm_at = after_t if after is not None else math.nextafter(now, math.inf)
                self._judge_release_locked(rec, before, after, clean)
                reason = "shutdown" if rec.unload_cause == "shutdown" else "unloaded"
                if rec.desc.blocked:
                    reason = rec.desc.blocked_reason or reason
                self._emit_locked(rec, "unloaded", rec.unload_cause, reason)
                self._reconcile_locked(now)
        self._pub.drain()

    def _probe_job(self, module_id: str, token: int) -> None:
        rec = self._recs[module_id]
        with self._lock:
            go = self._start_or_skip_locked(rec, token, self._clock())
        if not go:
            self._pub.drain()
            return
        verdict = "dead"
        try:
            verdict = rec.handle.probe()
        except Exception as exc:  # noqa: BLE001
            logger.warning("模組 %s probe 失敗：%s", module_id, type(exc).__name__)
        if verdict == "warming" and not self._observing(rec):  # 可載入的 handle 沒有「載入中」的觀測語意：視同 ok
            verdict = "ok"
        with self._lock:
            now = self._clock()
            if self._stale_locked(rec, token):
                self._finish_stale_locked(rec, now)
            else:
                rec.in_flight, rec.deadline = False, None
                warming_observed = rec.state == "warming" and self._observing(rec)
                if (rec.state in _ACTIVE_STATES or warming_observed) and not rec.desc.blocked:
                    if verdict == "dead":
                        rec.dead_count += 1
                        if rec.dead_count >= self._probe_dead_threshold:
                            self._fail_locked(rec, "probe_dead", "health", now, occupied=True)
                    elif warming_observed:
                        if verdict == "warming":
                            self._enter_warming_observed_locked(rec, now)
                        else:
                            self._ready_locked(rec, now)
                            if verdict == "degraded":
                                self._emit_locked(rec, "degraded", "health", "probe_degraded")
                    else:
                        rec.dead_count = 0
                        # ready 的觀測模組又回 warming：ready 不可轉 warming，只當作仍活著（狀態不動）
                        if verdict == "degraded" and rec.state == "ready":
                            self._emit_locked(rec, "degraded", "health", "probe_degraded")
                        elif verdict == "ok" and rec.state == "degraded" and rec.reason != "dependency_failed":
                            self._emit_locked(rec, "ready", "health", None)  # 依賴 failed 造成的 degraded 由依賴恢復來解除
                self._reconcile_locked(now)
        self._pub.drain()
