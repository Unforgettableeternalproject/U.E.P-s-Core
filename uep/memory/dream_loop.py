"""Host 全域做夢迴圈（Host全域做夢迴圈設計 §1、§5、§6；切片 DL-1）。

``uep-mem-dream`` 單一 daemon 執行緒，一次只做一個 namespace 的 dream；不經 ``MemoryHost.call()``、不用 per-namespace
executor、不持 engine 鎖執行 dream（只在查 readiness 時短暫取鎖，取不到就跳過該 namespace）。決策全在
``DreamPlanner``（純函式）；這裡只負責取 snapshot、呼叫 planner、執行 ``engine.dream(triggered_by=...)``、處理結果。

DL-1 範圍：host_daydream／host_idle／host_rest 的執行；``host_backlog`` 預設停用（DL-2）；沒有讓位與預算強制
（DL-4，需記憶端 M-D2，所以不傳 ``should_abort``／``deadline``）；休息窗口的 ``WindowOutcome``／``on_window_end``／
延長屬 DL-3，這裡只依 ``RestWindowPolicy.window_at`` 判斷「現在是否在休息時段」並計窗口內週期數。
"""
from __future__ import annotations

import logging
import threading
import time as _time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from uep.memory.activity import DreamActivityRecorder
from uep.memory.dream_planner import (
    TRIGGER_BACKLOG,
    TRIGGER_REST,
    DreamDecision,
    DreamLoopConfig,
    DreamPlanner,
    DreamSnapshot,
    FixedWindowPolicy,
    ForegroundSnapshot,
    NamespaceHistory,
    NamespaceInput,
    NamespaceReadiness,
    RestWindow,
    RestWindowPolicy,
    apply_outcome,
)
from uep.memory.metrics import MemoryMetrics
from uep.memory.namespaces import is_scope_namespace_id
from uep.memory.protocol import MemoryEngineLike

logger = logging.getLogger("uep.memory.dream_loop")

DREAM_HEALTH_REF = "memory.dream"
_DAY_S = 86400.0
_READINESS_LOCK_TIMEOUT_S = 0.5


class ForegroundProbe(Protocol):
    """跨所有平台的整體前景活動快照。實作在 ``uep/memory/foreground.py``（State＋Bus），由組裝層注入。"""

    def snapshot(self, now: float) -> ForegroundSnapshot: ...


class UnavailableProbe:
    """沒有注入 Probe 時的保守預設：永遠回報 Runtime 未就緒，迴圈不會開始任何 dream。"""

    def snapshot(self, now: float) -> ForegroundSnapshot:
        return ForegroundSnapshot("UNKNOWN", "UNKNOWN", 0, 0.0, False, False)


@dataclass(frozen=True)
class DreamTarget:
    namespace_id: str
    engine: MemoryEngineLike
    lock: threading.Lock


@dataclass
class DreamJob:
    """一次由 Host 觸發的 dream。listener 以 thread-local 取得它，把報告綁回這次決策（§5.4）。"""

    trigger: str
    namespace_id: str
    started_monotonic: float
    activity_id: str | None = None
    reported: bool = False


_local = threading.local()


def current_job() -> DreamJob | None:
    """目前執行緒上由 Host 迴圈觸發中的 dream；人工或他處觸發的報告為 None。"""
    return getattr(_local, "job", None)


def report_from_result(result: Mapping[str, Any] | None, trigger: str, exc_name: str | None) -> dict[str, Any]:
    """把 ``engine.dream()`` 的回傳（或例外）整理成與 listener 相同形狀的報告。真 engine 的回傳沒有 ``status``，
    依 ``skipped_reason``／``error`` 推得（與記憶端 ``_notify_listener`` 同規則）。"""
    if exc_name is not None:
        return {"triggered_by": trigger, "status": "error", "error": exc_name, "skipped_reason": None}
    data = dict(result or {})
    status = data.get("status")
    if status not in ("completed", "skipped", "error"):
        status = "skipped" if data.get("skipped_reason") else "error" if data.get("error") else "completed"
    data["status"] = status
    data.setdefault("triggered_by", trigger)
    return data


class DreamLoop:
    def __init__(
        self,
        config: DreamLoopConfig,
        *,
        targets: Callable[[], Sequence[DreamTarget]],
        probe: ForegroundProbe | None = None,
        rest_policy: RestWindowPolicy | None = None,
        metrics: MemoryMetrics | None = None,
        activity: DreamActivityRecorder | None = None,
        report_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
        health_sink: Callable[[str], None] | None = None,
        clock: Callable[[], float] = _time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._cfg = config
        self._planner = DreamPlanner(config)
        self._targets = targets
        self._probe: ForegroundProbe = probe or UnavailableProbe()
        self._rest_policy: RestWindowPolicy = rest_policy or FixedWindowPolicy()
        self._metrics = metrics or MemoryMetrics()
        self._activity = activity
        self._report_sink = report_sink
        self._health_sink = health_sink
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))

        self._lock = threading.Lock()  # 保護下列可變狀態（status() 從其他執行緒讀）
        self._history: dict[str, NamespaceHistory] = {}
        self._readiness: dict[str, tuple[float, NamespaceReadiness | None]] = {}
        self._runs: deque[tuple[float, bool]] = deque()  # (開始時間, 是否休息時段)；滾動 24 小時
        self._backlog_starts: dict[str, deque[float]] = {}
        self._window_key: datetime | None = None
        self._rest_cycles = 0
        self._last_trigger: str | None = None
        self._streak = 0
        self._in_flight: tuple[str, str, float] | None = None  # (namespace, trigger, 開始時間)
        self._paused = False
        self._pause_reason = ""
        self._tick_failures = 0
        self._last_tick_at: float | None = None
        self._last_decision: DreamDecision | None = None
        self._degraded: set[str] = set()  # 'loop' 或 namespace id
        self._health = "ready"
        self._started_at: float | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._probe_warned = isinstance(probe, UnavailableProbe) or probe is None

    # ---- 生命週期 ----

    @property
    def config(self) -> DreamLoopConfig:
        return self._cfg

    def start(self) -> None:
        """啟動執行緒（冪等）。必須在所有 namespace 完成『建 engine -> configure_dream -> 注入 -> set_dream_listener』之後。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._started_at = self._clock()
            self._thread = threading.Thread(target=self._run, name="uep-mem-dream", daemon=True)
            self._thread.start()
        if self._probe_warned:
            logger.warning("做夢迴圈沒有注入 ForegroundProbe：視為前景未就緒，不會開始任何 dream")

    def mark_started(self) -> None:
        """只標記啟動時間（不開執行緒）：同步驅動 ``tick()`` 的測試與診斷用；``start()`` 內含此步驟。"""
        with self._lock:
            self._started_at = self._clock()

    def set_shadow(self, shadow: bool) -> None:
        """執行期切換 shadow（D100：shadow 觀察後切為 False，下個 tick 開始真執行）。"""
        with self._lock:
            self._cfg = replace(self._cfg, shadow=shadow)

    def stop(self, timeout: float | None = None) -> bool:
        """停止迴圈：不再接新 tick、不再開始新 dream，並有界等待進行中的 ``dream()`` 返回。
        逾時回 False 並記 warning（含 namespace 與已跑秒數），不阻塞後續關閉；冪等。"""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is None:
            return True
        wait = self._cfg.dream_stop_wait_s if timeout is None else timeout
        if thread is not threading.current_thread():
            thread.join(timeout=wait)
        if thread.is_alive():
            with self._lock:
                flight = self._in_flight
            if flight is not None:
                logger.warning(
                    "停止做夢迴圈逾時，dream 仍在執行：namespace=%s 已跑 %.0f 秒", flight[0], self._clock() - flight[2]
                )
            else:
                logger.warning("停止做夢迴圈逾時，迴圈執行緒仍存活")
            return False
        self._thread = None
        return True

    def pause(self, reason: str = "manual") -> None:
        with self._lock:
            self._paused = True
            self._pause_reason = reason

    def resume(self, reason: str = "manual") -> None:
        with self._lock:
            self._paused = False
            self._pause_reason = ""
            self._tick_failures = 0
        self._set_degraded("loop", False)
        logger.info("做夢迴圈恢復：%s", reason)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(timeout=self._cfg.tick_interval_s)
            self._wake.clear()

    # ---- 單次 tick ----

    def tick(self) -> DreamDecision:
        """評估並（必要時）執行一次 dream。外層捕捉一切例外：連續 ``tick_failure_limit`` 次後暫停並升 degraded。"""
        if self._stop.is_set():
            return DreamDecision("wait", None, None, "stopping")
        try:
            decision = self._tick()
            with self._lock:
                self._tick_failures = 0
            return decision
        except Exception as exc:  # noqa: BLE001 - 迴圈本身不得因單次例外死亡；只記例外類別
            with self._lock:
                self._tick_failures += 1
                failures = self._tick_failures
            logger.warning("做夢迴圈 tick 失敗（第 %d 次）：%s", failures, type(exc).__name__)
            if failures >= self._cfg.tick_failure_limit:
                self.pause("tick_failures")
                self._set_degraded("loop", True)
            return DreamDecision("wait", None, None, "tick_error")

    def _tick(self) -> DreamDecision:
        cfg = self._cfg
        now = self._clock()
        wall = self._wall_clock()
        with self._lock:
            self._last_tick_at = now
        targets = [t for t in self._targets() if not is_scope_namespace_id(t.namespace_id)]
        rest_remaining = self._update_window(wall)
        fg = self._probe.snapshot(now)
        base = self._snapshot(now, fg, targets, rest_remaining, with_readiness=False)
        gate = self._planner.global_gate(base)
        if gate is not None:
            return self._wait(gate)

        decision = self._planner.decide(self._snapshot(now, fg, targets, rest_remaining, with_readiness=True))
        if decision.action != "run":
            return self._wait(decision)
        assert decision.namespace_id is not None and decision.trigger is not None
        # 呼叫前最後一次檢查：readiness 查詢與實際呼叫之間前景可能變活躍
        recheck_now = self._clock()
        gate = self._planner.global_gate(replace(base, now=recheck_now, foreground=self._probe.snapshot(recheck_now)))
        if gate is not None:
            return self._wait(gate)
        target = next(t for t in targets if t.namespace_id == decision.namespace_id)
        if cfg.shadow:
            return self._shadow(decision, recheck_now)
        with self._lock:
            self._last_decision = decision
        self._execute(target, decision, recheck_now)
        return decision

    def _wait(self, decision: DreamDecision) -> DreamDecision:
        self._metrics.inc(f"memory.dream.decision.wait.{decision.reason}")
        with self._lock:
            self._last_decision = decision
        return decision

    def _shadow(self, decision: DreamDecision, now: float) -> DreamDecision:
        """shadow：記錄 would_run 與計數，不呼叫 ``dream()``。以虛擬開始時間套用 min_gap，避免每個 tick 重複記。"""
        assert decision.namespace_id is not None
        self._metrics.inc(f"memory.dream.shadow_would_run.{decision.trigger}")
        logger.info("做夢迴圈 shadow：would_run namespace=%s trigger=%s", decision.namespace_id, decision.trigger)
        with self._lock:
            hist = self._history.get(decision.namespace_id) or NamespaceHistory(decision.namespace_id)
            extra = {"last_backlog_started_at": now} if decision.trigger == TRIGGER_BACKLOG else {}
            self._history[decision.namespace_id] = replace(hist, last_started_at=now, **extra)
            self._last_decision = decision
        return decision

    # ---- snapshot ----

    def _update_window(self, wall: datetime) -> float | None:
        """目前是否在休息時段：回剩餘秒數（None＝不在）。進入新窗口時重設窗口內週期計數。"""
        window: RestWindow | None = self._rest_policy.window_at(wall)
        with self._lock:
            if window is None:
                self._window_key = None
                self._rest_cycles = 0
                return None
            if self._window_key != window.start:
                self._window_key = window.start
                self._rest_cycles = 0
        return max(0.0, (window.end - wall).total_seconds())

    def _snapshot(
        self, now: float, fg: ForegroundSnapshot, targets: Sequence[DreamTarget], rest_remaining: float | None,
        *, with_readiness: bool,
    ) -> DreamSnapshot:
        with self._lock:
            while self._runs and now - self._runs[0][0] >= _DAY_S:
                self._runs.popleft()
            daily = sum(1 for _, rest in self._runs if not rest)
            rest_cycles = self._rest_cycles
            paused = self._paused
            in_flight = self._in_flight is not None
            last_trigger, streak = self._last_trigger, self._streak
            started = self._started_at
        inputs: list[NamespaceInput] = []
        if with_readiness:
            for target in targets:
                inputs.append(NamespaceInput(self._history_for(target.namespace_id, now), self._poll(target, now)))
        return DreamSnapshot(
            now=now, uptime_s=None if started is None else now - started, foreground=fg, namespaces=tuple(inputs),
            paused=paused, in_flight=in_flight, rest_remaining_s=rest_remaining, daily_cycles=daily,
            rest_cycles=rest_cycles, last_trigger=last_trigger, last_trigger_streak=streak,
        )

    def _history_for(self, namespace_id: str, now: float) -> NamespaceHistory:
        with self._lock:
            hist = self._history.get(namespace_id) or NamespaceHistory(namespace_id)
            starts = self._backlog_starts.get(namespace_id)
            if starts is not None:
                while starts and now - starts[0] >= _DAY_S:
                    starts.popleft()
            return replace(hist, backlog_starts_24h=len(starts) if starts else 0)

    def _poll(self, target: DreamTarget, now: float) -> NamespaceReadiness | None:
        """查 readiness（每 namespace 至少間隔 ``poll_interval_s``）。取 engine 鎖逾時就回 None，避免與前景長操作互卡。"""
        ns = target.namespace_id
        with self._lock:
            cached = self._readiness.get(ns)
        if cached is not None and now - cached[0] < self._cfg.poll_interval_s:
            return cached[1]
        ready: NamespaceReadiness | None = None
        if target.lock.acquire(timeout=_READINESS_LOCK_TIMEOUT_S):
            try:
                ready = _parse_readiness(ns, target.engine.dream_readiness())
            except Exception as exc:  # noqa: BLE001 - readiness 失敗只讓該 namespace 本輪不評估
                logger.warning("查詢 dream readiness 失敗：namespace=%s %s", ns, type(exc).__name__)
            finally:
                target.lock.release()
        with self._lock:
            self._readiness[ns] = (now, ready)
        return ready

    # ---- 執行與結果 ----

    def _execute(self, target: DreamTarget, decision: DreamDecision, now: float) -> None:
        ns, trigger = target.namespace_id, decision.trigger
        assert trigger is not None
        job = DreamJob(trigger, ns, now)
        if self._activity is not None:
            try:
                job.activity_id = self._activity.record_started(ns, trigger)
            except Exception as exc:  # noqa: BLE001 - Activity 寫入失敗不得阻止 dream
                logger.warning("寫入 dream Activity 失敗（已忽略）：%s", type(exc).__name__)
        with self._lock:
            self._in_flight = (ns, trigger, now)
        result: Mapping[str, Any] | None = None
        exc_name: str | None = None
        _local.job = job
        try:
            try:
                result = target.engine.dream(triggered_by=trigger)
            except Exception as exc:  # noqa: BLE001 - dream() 拋例外（engine 已關閉等）視同 error 並退避
                exc_name = type(exc).__name__
                logger.warning("dream() 拋出例外：namespace=%s %s", ns, exc_name)
            report = report_from_result(result, trigger, exc_name)
            if not job.reported and self._report_sink is not None:
                self._report_sink(ns, report)  # listener 沒被呼叫（例外路徑或舊 engine）：補上單一寫入點
        finally:
            _local.job = None
            with self._lock:
                self._in_flight = None
        self._finish(target, trigger, now, report)

    def _finish(self, target: DreamTarget, trigger: str, started: float, report: Mapping[str, Any]) -> None:
        cfg, ns = self._cfg, target.namespace_id
        finished = self._clock()
        status = str(report.get("status"))
        skipped_reason = report.get("skipped_reason") if status == "skipped" else None
        replay = report.get("replay")
        replay_skip = replay.get("skipped_reason") if isinstance(replay, Mapping) else None
        resting = trigger == TRIGGER_REST
        backlog_replay_skipped = trigger == TRIGGER_BACKLOG and status == "completed" and bool(replay_skip)
        if backlog_replay_skipped:
            # replay 整輪被 skip：一則積壓都沒碰，不算消化、不計每日上限，退避（D86、§2.3）
            self._metrics.inc(f"memory.dream.backlog_replay_skipped.{_safe(str(replay_skip))}")
        counted_backlog = trigger == TRIGGER_BACKLOG and not backlog_replay_skipped
        with self._lock:
            hist = self._history.get(ns) or NamespaceHistory(ns)
            verdict = apply_outcome(
                hist, now=finished, started_at=started, status=status, skipped_reason=skipped_reason, config=cfg,
                is_backlog=counted_backlog,
            )
            new_hist = verdict.history
            if backlog_replay_skipped:
                new_hist = replace(new_hist, backoff_until=finished + cfg.backlog_min_gap_s)
            self._history[ns] = new_hist
            if verdict.counts_as_run:
                # skipped 不算一次週期：不吃每日上限、窗口週期數與連續同類別計數
                self._runs.append((started, resting))
                if resting:
                    self._rest_cycles += 1
                self._streak = self._streak + 1 if self._last_trigger == trigger else 1
                self._last_trigger = trigger
                if counted_backlog:
                    self._backlog_starts.setdefault(ns, deque()).append(started)
            self._readiness.pop(ns, None)  # 下一個 tick 重新查 readiness
        if verdict.kind == "skipped":
            logger.info("dream 略過：namespace=%s reason=%s 退避 %.0f 秒", ns, verdict.code, verdict.backoff_s)
        elif verdict.kind == "error":
            logger.warning("dream 失敗：namespace=%s 退避 %.0f 秒", ns, verdict.backoff_s)
        duration = finished - started
        if duration > cfg.cycle_deadline_s:
            self._metrics.inc("memory.dream.budget.cycle_deadline_exceeded")  # Phase A 只量測，不強制
        if self._foreground_during(started, finished):
            self._metrics.inc("memory.dream.foreground_during_dream")
        if verdict.degrade:
            self._set_degraded(ns, True)
        if verdict.recover:
            self._set_degraded(ns, False)

    def _foreground_during(self, started: float, finished: float) -> bool:
        """Phase A 量測：dream 進行期間前景是否有活動（只記錄，不中止）。"""
        try:
            fg = self._probe.snapshot(finished)
        except Exception:  # noqa: BLE001 - 量測失敗不影響結果
            return False
        return fg.interaction_state not in ("IDLE", "UNKNOWN") or fg.quiet_for_s < (finished - started)

    # ---- health ----

    def _set_degraded(self, subject: str, degraded: bool) -> None:
        """per-subject 升降級，整體狀態轉換時才通知 sink（冪等；同狀態不重發）。"""
        with self._lock:
            before = self._health
            if degraded:
                self._degraded.add(subject)
            else:
                self._degraded.discard(subject)
            after = "degraded" if self._degraded else "ready"
            self._health = after
        if after == before:
            return
        self._metrics.inc("memory.dream.health.degraded" if after == "degraded" else "memory.dream.health.recovered")
        if self._health_sink is not None:
            try:
                self._health_sink(after)
            except Exception as exc:  # noqa: BLE001 - health 通知失敗不影響迴圈
                logger.warning("dream health 通知失敗（已忽略）：%s", type(exc).__name__)

    # ---- 診斷 ----

    def status(self) -> dict[str, Any]:
        """診斷用（不含記憶內容）。暫停期間仍可讀。"""
        now = self._clock()
        with self._lock:
            flight = self._in_flight
            thread = self._thread
            return {
                "running": bool(thread and thread.is_alive()),
                "paused": self._paused,
                "pause_reason": self._pause_reason,
                "shadow": self._cfg.shadow,
                "health": self._health,
                "degraded": sorted(self._degraded),
                "last_tick_age_s": None if self._last_tick_at is None else now - self._last_tick_at,
                "last_decision": None if self._last_decision is None else {
                    "action": self._last_decision.action, "namespace_id": self._last_decision.namespace_id,
                    "trigger": self._last_decision.trigger, "reason": self._last_decision.reason,
                },
                "in_flight": None if flight is None else {
                    "namespace_id": flight[0], "trigger": flight[1], "elapsed_s": now - flight[2],
                },
                "cycles_24h": sum(1 for start, _ in self._runs if now - start < _DAY_S),
                "rest_cycles": self._rest_cycles,
                "wants_intake": False,  # DL-3 才有 RestWindowPolicy.on_window_end；先固定為 False
                "namespaces": {
                    ns: {
                        "pending": None if (r := self._readiness.get(ns)) is None or r[1] is None else r[1].pending,
                        "unscanned_pending": (
                            None if (r := self._readiness.get(ns)) is None or r[1] is None
                            else r[1].unscanned_pending
                        ),
                        "last_finished_age_s": None if h.last_finished_at is None else now - h.last_finished_at,
                        "consecutive_failures": h.consecutive_failures,
                        "backoff_remaining_s": max(0.0, h.backoff_until - now),
                        "degraded": h.degraded,
                    }
                    for ns, h in self._history.items()
                },
            }


def _parse_readiness(namespace_id: str, raw: Mapping[str, Any]) -> NamespaceReadiness:
    """``engine.dream_readiness()`` -> ``NamespaceReadiness``。缺少 ``unscanned_pending``（M-D1 前）視為 None。"""
    lease = raw.get("lease")
    unscanned = raw.get("unscanned_pending")
    return NamespaceReadiness(
        namespace_id=namespace_id,
        pending=int(raw.get("pending") or 0),
        unscanned_pending=None if unscanned is None else int(unscanned),
        lease_held_by_other=bool(lease.get("held_by_other")) if isinstance(lease, Mapping) else False,
        is_dreaming=bool(raw.get("is_dreaming", False)),
    )


def _safe(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_") or "unknown"
