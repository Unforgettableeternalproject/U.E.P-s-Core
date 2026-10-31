"""Host 全域做夢迴圈的決策核心（Host全域做夢迴圈設計 §1.4、§2、§3.1、§4.2、§6.1；切片 DL-0）。

這裡只有純資料與純函式：沒有執行緒、沒有 IO、沒有時鐘讀取（時間一律由 ``DreamSnapshot.now`` 帶入，單調秒數），
所以可用假時鐘把門檻邊界（T-1／T／T+1）全測。執行緒與 engine 呼叫在 ``dream_loop.py``。

觸發器只有 ``host_daydream``／``host_idle``／``host_rest`` 與預留的 ``host_backlog``（DL-2 才啟用，需要記憶端
``unscanned_pending``；欄位缺失時此觸發自動停用）；不使用記憶端的 ``idle``／``daydream``／``cron`` 字串。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

TRIGGER_DAYDREAM = "host_daydream"
TRIGGER_IDLE = "host_idle"
TRIGGER_BACKLOG = "host_backlog"
TRIGGER_REST = "host_rest"
TRIGGERS = (TRIGGER_DAYDREAM, TRIGGER_IDLE, TRIGGER_BACKLOG, TRIGGER_REST)
_CLASS_RANK = {TRIGGER_DAYDREAM: 0, TRIGGER_IDLE: 1, TRIGGER_BACKLOG: 2, TRIGGER_REST: 3}

# 記憶端 skipped_reason 的固定代碼（commit 55a6b43）；迴圈只比對代碼，不解析 skipped_detail
SKIP_ALREADY_RUNNING = "already_running"
SKIP_LEASE_HELD = "lease_held"
SKIP_WINDOW_COMPLETED = "window_completed"
SKIP_LEASE_TIMEOUT = "lease_timeout"
SKIP_LEASE_IO_ERROR = "lease_io_error"
KNOWN_SKIP_CODES = frozenset({
    SKIP_ALREADY_RUNNING, SKIP_LEASE_HELD, SKIP_WINDOW_COMPLETED, SKIP_LEASE_TIMEOUT, SKIP_LEASE_IO_ERROR,
})

# wait 原因碼的資訊優先序（同一 tick 多個 namespace 各有原因時，取最前者作為整體原因）
_WAIT_PRIORITY = (
    "lease_held_by_other", "namespace_dreaming", "backoff", "min_gap", "quiet_short", "backlog_throttled",
    "not_enough_pending", "readiness_unavailable",
)


@dataclass(frozen=True)
class DreamLoopConfig:
    """全部門檻與預算（D93～D98 的預設值）。``DreamLoop`` 的決策只認這份，不靠記憶端 ``configure_dream``（R8）。"""

    enabled: bool = False
    shadow: bool = True  # D100：先 shadow 觀察（只記 would_run，不呼叫 dream()）
    tick_interval_s: float = 30.0
    poll_interval_s: float = 60.0  # 每個 namespace 查 readiness 的最小間隔
    startup_grace_s: float = 300.0
    idle_quiet_s: float = 1800.0
    daydream_quiet_s: float = 120.0
    rest_quiet_s: float = 300.0
    min_pending: int = 5
    daydream_pending: int = 20
    min_gap_s: float = 1800.0
    # host_backlog（DL-2 啟用，需 M-D1 的 unscanned_pending）
    backlog_enabled: bool = False
    backlog_batch: int = 8
    backlog_min_gap_s: float = 10800.0
    backlog_max_per_day: int = 4
    # 預算（D95）：Phase A 只強制週期數類；其餘量測
    daily_cycle_cap: int = 8
    rest_max_cycles: int = 8
    cycle_deadline_s: float = 900.0
    max_llm_calls_per_cycle: int = 40
    daily_llm_call_cap: int = 300
    max_consecutive_same_class: int = 3
    # 失敗與退避（D98、§6.1）
    error_backoff_base_s: float = 600.0
    error_backoff_max_s: float = 14400.0
    error_degraded_after: int = 3
    lease_degraded_after: int = 3
    lease_backoff_s: float = 1800.0
    io_error_backoff_s: float = 3600.0
    skip_backoff_s: float = 300.0
    expected_lease_peers: frozenset[str] = frozenset()  # 這些 namespace 的 lease_held 視為正常（預設空）
    # DL-4 預留（讓位／預算強制需記憶端 M-D2；DL-1 不使用）
    yield_backoff_s: float = 600.0
    yield_backoff_max_s: float = 14400.0
    dream_stop_wait_s: float = 60.0
    tick_failure_limit: int = 5

    def __post_init__(self) -> None:
        positive = (
            "tick_interval_s", "poll_interval_s", "idle_quiet_s", "daydream_quiet_s", "rest_quiet_s", "min_gap_s",
            "backlog_min_gap_s", "cycle_deadline_s", "error_backoff_base_s", "error_backoff_max_s",
            "lease_backoff_s", "io_error_backoff_s", "skip_backoff_s", "dream_stop_wait_s",
        )
        for name in positive:
            if getattr(self, name) <= 0:
                raise ValueError(f"DreamLoopConfig.{name} 必須大於 0")
        for name in ("startup_grace_s", "yield_backoff_s", "yield_backoff_max_s"):
            if getattr(self, name) < 0:
                raise ValueError(f"DreamLoopConfig.{name} 不得為負")
        for name in (
            "min_pending", "daydream_pending", "backlog_batch", "backlog_max_per_day", "daily_cycle_cap",
            "rest_max_cycles", "max_llm_calls_per_cycle", "daily_llm_call_cap", "max_consecutive_same_class",
            "error_degraded_after", "lease_degraded_after", "tick_failure_limit",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"DreamLoopConfig.{name} 必須至少為 1")
        if self.daydream_pending < self.min_pending:
            raise ValueError("daydream_pending 不得小於 min_pending")
        object.__setattr__(self, "expected_lease_peers", frozenset(self.expected_lease_peers))


@dataclass(frozen=True)
class ForegroundSnapshot:
    """跨所有平台的整體前景狀態（不是 per-namespace）。``interaction_state`` 不是 ``IDLE`` 一律視為前景活躍。"""

    interaction_state: str
    behavior_state: str
    open_episodes: int
    quiet_for_s: float
    global_stop_engaged: bool
    runtime_ready: bool


@dataclass(frozen=True)
class NamespaceReadiness:
    namespace_id: str
    pending: int  # salience >= 0.5 且 not is_dreamed（現有 readiness["pending"]）
    unscanned_pending: int | None = None  # M-D1 前為 None：host_backlog 停用
    lease_held_by_other: bool = False
    is_dreaming: bool = False


@dataclass(frozen=True)
class NamespaceHistory:
    """迴圈對單一 namespace 累積的歷史（單調秒數）。純資料，由 ``apply_outcome`` 產生新值。"""

    namespace_id: str
    last_started_at: float | None = None
    last_finished_at: float | None = None
    backoff_until: float = 0.0
    consecutive_failures: int = 0
    consecutive_lease_skips: int = 0
    last_backlog_started_at: float | None = None
    backlog_starts_24h: int = 0  # 由迴圈在組 snapshot 時按滾動 24 小時填入
    degraded: bool = False


@dataclass(frozen=True)
class NamespaceInput:
    history: NamespaceHistory
    readiness: NamespaceReadiness | None  # None＝尚未查到（或查詢失敗／取不到鎖）

    @property
    def namespace_id(self) -> str:
        return self.history.namespace_id


@dataclass(frozen=True)
class DreamSnapshot:
    now: float  # 單調秒數
    uptime_s: float | None  # 迴圈已啟動秒數；None＝尚未啟動
    foreground: ForegroundSnapshot
    namespaces: tuple[NamespaceInput, ...] = ()
    paused: bool = False
    in_flight: bool = False
    rest_remaining_s: float | None = None  # 目前在休息時段時的剩餘秒數；None＝不在休息時段
    daily_cycles: int = 0  # 滾動 24 小時內已開始的週期數（不含休息時段）
    rest_cycles: int = 0  # 目前休息窗口內已開始的週期數
    last_trigger: str | None = None  # 最近連續被選中的觸發類別，供 max_consecutive_same_class 使用
    last_trigger_streak: int = 0


@dataclass(frozen=True)
class DreamDecision:
    action: Literal["run", "wait"]
    namespace_id: str | None
    trigger: str | None
    reason: str  # 固定代碼；wait 時說明為何不做
    not_before: float | None = None  # wait 時建議的下次評估時間（單調秒數）


def _wait(reason: str, not_before: float | None = None) -> DreamDecision:
    return DreamDecision("wait", None, None, reason, not_before)


class DreamPlanner:
    """純決策：``decide(snapshot) -> DreamDecision``。"""

    def __init__(self, config: DreamLoopConfig) -> None:
        self._cfg = config

    @property
    def config(self) -> DreamLoopConfig:
        return self._cfg

    def global_gate(self, snap: DreamSnapshot) -> DreamDecision | None:
        """全域閘門（§2.3）：任一不通過就 wait。不需要 readiness，所以迴圈可在查 readiness 之前先問。"""
        cfg, fg = self._cfg, snap.foreground
        if snap.uptime_s is None:
            return _wait("not_started")
        if snap.paused:
            return _wait("paused")
        if snap.in_flight:
            return _wait("dream_in_flight")
        if snap.uptime_s < cfg.startup_grace_s:
            return _wait("startup_grace", snap.now + (cfg.startup_grace_s - snap.uptime_s))
        if fg.global_stop_engaged:
            return _wait("global_stop")
        if not fg.runtime_ready:
            return _wait("runtime_not_ready")
        if fg.interaction_state != "IDLE" or fg.open_episodes > 0:
            return _wait("foreground_active")
        return None

    def decide(self, snap: DreamSnapshot) -> DreamDecision:
        gate = self.global_gate(snap)
        if gate is not None:
            return gate
        cfg = self._cfg
        resting = snap.rest_remaining_s is not None
        if resting:
            assert snap.rest_remaining_s is not None
            if snap.rest_cycles >= cfg.rest_max_cycles:
                return _wait("rest_cycle_cap")
            if snap.rest_remaining_s < cfg.cycle_deadline_s:
                return _wait("rest_window_ending")
        elif snap.daily_cycles >= cfg.daily_cycle_cap:
            return _wait("daily_cap")
        if not snap.namespaces:
            return _wait("no_targets")

        candidates: list[tuple[NamespaceInput, str]] = []
        waits: list[tuple[str, float | None]] = []
        for item in snap.namespaces:
            trigger, reason, not_before = self._evaluate(item, snap, resting)
            if trigger is not None:
                candidates.append((item, trigger))
            else:
                waits.append((reason, not_before))
        if not candidates:
            return self._aggregate_wait(waits)

        if (
            not resting and snap.last_trigger is not None
            and snap.last_trigger_streak >= cfg.max_consecutive_same_class
        ):
            others = [c for c in candidates if c[1] != snap.last_trigger]
            if others:  # 連續同類別已達上限且仍有其他類別 eligible：強制取另一類別
                candidates = others
        candidates.sort(key=lambda c: (
            _CLASS_RANK[c[1]] if not resting else 0,
            -1.0 if c[0].history.last_finished_at is None else c[0].history.last_finished_at,
            -(c[0].readiness.pending if c[0].readiness else 0),
            c[0].namespace_id,
        ))
        item, trigger = candidates[0]
        return DreamDecision("run", item.namespace_id, trigger, "eligible")

    # ---- 單一 namespace ----

    def _evaluate(
        self, item: NamespaceInput, snap: DreamSnapshot, resting: bool
    ) -> tuple[str | None, str, float | None]:
        """回傳 ``(trigger, reason, not_before)``；trigger 為 None 時 reason 是不做夢的原因。"""
        cfg, hist, ready = self._cfg, item.history, item.readiness
        if ready is None:
            return None, "readiness_unavailable", None
        quiet = snap.foreground.quiet_for_s
        idle_quiet = cfg.idle_quiet_s
        if snap.foreground.behavior_state == "SLEEP":
            idle_quiet = min(idle_quiet, cfg.rest_quiet_s)  # SLEEP 等同休息，可放寬閒置門檻（§2.2）

        unscanned = ready.unscanned_pending
        # (trigger, 需要的安靜秒數)
        wants: list[tuple[str, float]] = []
        if resting:
            if ready.pending >= 1 or (unscanned is not None and unscanned >= 1):
                wants.append((TRIGGER_REST, cfg.rest_quiet_s))
        else:
            if ready.pending >= cfg.daydream_pending:
                wants.append((TRIGGER_DAYDREAM, cfg.daydream_quiet_s))
            if ready.pending >= cfg.min_pending:
                wants.append((TRIGGER_IDLE, idle_quiet))
            if (
                cfg.backlog_enabled and unscanned is not None and unscanned >= cfg.backlog_batch
                and ready.pending < cfg.min_pending
            ):
                wants.append((TRIGGER_BACKLOG, cfg.idle_quiet_s))
        if not wants:
            return None, "not_enough_pending", None
        if ready.lease_held_by_other:
            return None, "lease_held_by_other", None
        if ready.is_dreaming:
            return None, "namespace_dreaming", None
        if hist.backoff_until > snap.now:
            return None, "backoff", hist.backoff_until
        if hist.last_started_at is not None and snap.now < hist.last_started_at + cfg.min_gap_s:
            return None, "min_gap", hist.last_started_at + cfg.min_gap_s

        quiet_short: float | None = None
        throttled = False
        for trigger, need in wants:
            if quiet < need:
                eta = snap.now + (need - quiet)
                quiet_short = eta if quiet_short is None else min(quiet_short, eta)
                continue
            if trigger == TRIGGER_BACKLOG and self._backlog_throttled(hist, snap.now):
                throttled = True
                continue
            return trigger, "eligible", None
        if throttled:
            return None, "backlog_throttled", None
        return None, "quiet_short", quiet_short

    def _backlog_throttled(self, hist: NamespaceHistory, now: float) -> bool:
        cfg = self._cfg
        recent = hist.last_backlog_started_at is not None and now < hist.last_backlog_started_at + cfg.backlog_min_gap_s
        return recent or hist.backlog_starts_24h >= cfg.backlog_max_per_day

    @staticmethod
    def _aggregate_wait(waits: list[tuple[str, float | None]]) -> DreamDecision:
        best = min(waits, key=lambda w: _WAIT_PRIORITY.index(w[0]) if w[0] in _WAIT_PRIORITY else len(_WAIT_PRIORITY))
        reason = best[0]
        etas = [nb for r, nb in waits if r == reason and nb is not None]
        return _wait(reason, min(etas) if etas else None)


# ---- 結果分類與退避（純函式，§4.2、§6.1）----

@dataclass(frozen=True)
class OutcomeVerdict:
    history: NamespaceHistory
    kind: Literal["completed", "error", "skipped"]
    code: str  # skipped 時為固定代碼，其餘為空字串
    backoff_s: float
    degrade: bool  # 此結果使該 namespace 進入 degraded（冪等：已 degraded 時為 False）
    recover: bool  # 此結果使該 namespace 脫離 degraded
    counts_as_run: bool  # 計入 min_gap 與每日週期數（skipped 不計）


def error_backoff_s(failures: int, config: DreamLoopConfig) -> float:
    """error 的指數退避：base × 2^(n-1)，上限 ``error_backoff_max_s``。"""
    if failures < 1:
        return 0.0
    exponent = min(failures - 1, 32)
    return min(config.error_backoff_base_s * (2 ** exponent), config.error_backoff_max_s)


def apply_outcome(
    history: NamespaceHistory, *, now: float, started_at: float, status: str, skipped_reason: str | None,
    config: DreamLoopConfig, is_backlog: bool = False,
) -> OutcomeVerdict:
    """把一次 dream 結果套進 namespace 歷史：回新的歷史、退避秒數與 health 升降級旗標。

    只比對固定代碼 ``skipped_reason``（不解析 ``skipped_detail``）。skipped 不更新 ``last_started_at``
    （不吃 min_gap），改以退避延後下次評估。
    """
    h = history
    if status == "skipped":
        code = skipped_reason or "unknown"
        degrade = False
        lease_skips = h.consecutive_lease_skips
        if code == SKIP_LEASE_IO_ERROR:
            backoff = config.io_error_backoff_s
            degrade = True  # 環境問題不會自癒：第一次就升
        elif code in (SKIP_LEASE_HELD, SKIP_LEASE_TIMEOUT):
            backoff = config.lease_backoff_s
            if not (code == SKIP_LEASE_HELD and h.namespace_id in config.expected_lease_peers):
                lease_skips += 1
                degrade = lease_skips >= config.lease_degraded_after
        else:  # already_running、window_completed 與未知代碼：一般略過
            backoff = config.skip_backoff_s
        new_degraded = h.degraded or degrade
        new = replace(h, backoff_until=now + backoff, consecutive_lease_skips=lease_skips, degraded=new_degraded)
        return OutcomeVerdict(new, "skipped", code, backoff, degrade and not h.degraded, False, False)

    new_last_backlog = started_at if is_backlog else h.last_backlog_started_at
    if status == "error":
        failures = h.consecutive_failures + 1
        backoff = error_backoff_s(failures, config)
        degrade = failures >= config.error_degraded_after
        new = replace(
            h, last_started_at=started_at, last_finished_at=now, consecutive_failures=failures,
            backoff_until=now + backoff, degraded=h.degraded or degrade, last_backlog_started_at=new_last_backlog,
        )
        return OutcomeVerdict(new, "error", "", backoff, degrade and not h.degraded, False, True)

    new = replace(
        h, last_started_at=started_at, last_finished_at=now, consecutive_failures=0, consecutive_lease_skips=0,
        backoff_until=0.0, degraded=False, last_backlog_started_at=new_last_backlog,
    )
    return OutcomeVerdict(new, "completed", "", 0.0, False, h.degraded, True)


# ---- 休息時段（§2.4）----

@dataclass(frozen=True)
class RestWindow:
    start: datetime
    end: datetime
    tz: str


@dataclass(frozen=True)
class WindowOutcome:
    cycles_run: int = 0
    cycles_failed: int = 0
    cycles_skipped: int = 0
    pending_at_start: int = 0  # 窗口開始時各 namespace pending 之和
    concepts_changed: int = 0  # distillation.concepts_created + concepts_updated（取自 dream() 回傳）
    backlog_remaining: int | None = None  # 窗口結束時 unscanned_pending 之和


@dataclass(frozen=True)
class WindowDecision:
    extend_until: datetime | None = None  # 夜貓子：順延休息（None＝照時窗結束）
    wants_intake: bool = False  # 純旗標：只出現在 status()，迴圈不產生任何輸出
    reason: str = ""


class RestWindowPolicy(Protocol):
    def window_at(self, now: datetime) -> RestWindow | None: ...

    def on_window_end(self, window: RestWindow, outcome: WindowOutcome) -> WindowDecision: ...


@dataclass(frozen=True)
class FixedWindowPolicy:
    """Schedule 之前的簡單規則（D97）：每天固定時段（預設 03:00–05:30 Asia/Taipei），不順延。
    ``end`` 早於或等於 ``start`` 視為跨午夜。窗口以明確時區計算（R9）；``now`` 必須是 tz-aware。"""

    start: time = time(3, 0)
    end: time = time(5, 30)
    tz: str = "Asia/Taipei"
    _zone: ZoneInfo = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.start == self.end:
            raise ValueError("休息窗口的 start 與 end 不得相同")
        object.__setattr__(self, "_zone", ZoneInfo(self.tz))

    def window_at(self, now: datetime) -> RestWindow | None:
        if now.tzinfo is None:
            raise ValueError("window_at 需要 tz-aware 的時間")
        local = now.astimezone(self._zone)
        for day_offset in (-1, 0):  # 跨午夜窗口可能起於昨天
            day = local.date() + timedelta(days=day_offset)
            start = datetime.combine(day, self.start, tzinfo=self._zone)
            end_day = day if self.end > self.start else day + timedelta(days=1)
            end = datetime.combine(end_day, self.end, tzinfo=self._zone)
            if start <= local < end:
                return RestWindow(start, end, self.tz)
        return None

    def on_window_end(self, window: RestWindow, outcome: WindowOutcome) -> WindowDecision:
        return WindowDecision(extend_until=None, wants_intake=False, reason="fixed_window")
