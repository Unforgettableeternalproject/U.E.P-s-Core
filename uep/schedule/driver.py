"""BehaviorDriver（PP-3b）：行程建議 → 主機合理化 → 提 CC-7 請求（契約 §8.5）。

- 邏輯全在純函式 ``rationalize``；這裡只有取事實、記錄與（``on`` 時）提請求的副作用。
- 依賴方向：只依賴 ``uep.contracts``／``uep.schedule``；State、Presence、DreamLoop 等都經注入的 callable 取得
  （組裝在 ``uep/echo/schedule_wiring.py``），所以本模組不 import ``uep.state`` 或 ``uep.memory``。
- 開關 ``off｜shadow｜on``（預設 shadow）：shadow 只記 log／指標（would_transition），不提請求；
  Behavior 不是安全閘門，但會改變 muted／做夢放寬等下游行為，所以先 shadow（先例：D100）。
- 行程不可用（``slot.availability == "unavailable"``）時直接 HOLD（``plan_unavailable``），不進 rationalize 的
  Presence fallback：行程故障不應造成 Behavior 切換。
- ``in_rest_window``：facts 給值（fixed 模式的純計算）就用它；為 None（plan 模式）時用 driver 自己這次 resolve 的 slot
  （``source == REST_WINDOW``），不呼叫與做夢迴圈共用的 policy（避免改動它的快取狀態與跨執行緒）。
- 請求冪等：coordinator 對同一個 request_id 回放舊裁決（含 REJECTED／DEFERRED）。非 ACCEPTED 的結果會讓同一組
  (from, to, version) 的下一次提交帶遞增的嘗試序號，並以指數退避（起始 ``retry_backoff_s``、每次加倍、上限
  ``retry_backoff_max_s``、±``backoff_jitter`` 抖動）決定最早重試時間。

已知限制（第一版；``on`` 之前必須處理）：
  * ``behavior_entered_at`` 只是「看到 Behavior 軸 version 變化的時間」的近似；第一次觀察為 None（不套用防抖）。
  * ``has_uninterruptible_work`` 固定 False，Task 層沒有可查詢的介面前，``on`` 可能在有不可中斷工作時離開 WORK；
    ``on`` 前必須接上 Task 層。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from uep.contracts.schedule import PlanReader, PlanSource
from uep.schedule.behavior import (
    BehaviorDecision,
    RationalizeInput,
    RationalizePolicy,
    rationalize,
)

logger = logging.getLogger(__name__)

DriverMode = Literal["off", "shadow", "on"]
DRIVER_MODES = ("off", "shadow", "on")
DEFAULT_INTERVAL_S = 5.0
DEFAULT_RETRY_BACKOFF_S = 30.0  # 指數退避的起始值
DEFAULT_RETRY_BACKOFF_MAX_S = 600.0  # 上限
DEFAULT_BACKOFF_JITTER = 0.2  # ±20% 抖動
_ERROR_LOG_INTERVAL_S = 60.0


def parse_driver_mode(value: str | None) -> DriverMode:
    """``UEP_BEHAVIOR_DRIVER``：空值視同預設 shadow；未知值 ``ValueError``（fail-fast）。"""
    mode = (value or "shadow").strip().lower()
    if mode not in DRIVER_MODES:
        raise ValueError(f"UEP_BEHAVIOR_DRIVER 只接受 off／shadow／on：{value!r}")
    return mode  # type: ignore[return-value]


@dataclass(frozen=True)
class BehaviorFacts:
    """driver 每個 tick 取得的當下事實（由組裝層從 State／Presence／Stop／DreamLoop 填入）。"""

    presence_status: Literal["NONE", "ACTIVE", "RECOVERING"]
    current_behavior: str
    behavior_version: int
    interaction_idle: bool
    global_stop: bool
    runtime_ready: bool
    dream_in_flight: bool = False
    in_rest_window: bool | None = None  # None＝由 driver 依自己這次 resolve 的 slot 判斷（plan 模式）
    has_uninterruptible_work: bool = False  # 第一版固定 False


# (from_state, to_state, expected_version, reason_code, attempt) -> 裁決名稱（如 "ACCEPTED"）
SubmitFn = Callable[[str, str, int, str, int], str]


class BehaviorDriver:
    """單一寫入者：只有它以 ``core.policy`` 對 Behavior 軸提請求（BH-2）。"""

    def __init__(self, reader: PlanReader, facts_fn: Callable[[datetime], BehaviorFacts], submit_fn: SubmitFn, *,
                 mode: DriverMode = "shadow", clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 policy: RationalizePolicy | None = None, interval_s: float = DEFAULT_INTERVAL_S,
                 metrics_inc: Callable[[str], None] | None = None, autonomy_enabled: bool = False,
                 retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
                 retry_backoff_max_s: float = DEFAULT_RETRY_BACKOFF_MAX_S,
                 backoff_jitter: float = DEFAULT_BACKOFF_JITTER, rng: Callable[[], float] = random.random,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if mode not in DRIVER_MODES:
            raise ValueError(f"未知的 driver 模式：{mode}")
        self._reader = reader
        self._facts_fn = facts_fn
        self._submit = submit_fn
        self._mode: DriverMode = mode
        self._clock = clock
        self._policy = policy or RationalizePolicy()
        self._interval_s = interval_s
        self._metrics_inc = metrics_inc
        self._autonomy = autonomy_enabled
        self._retry_backoff_s = retry_backoff_s
        self._retry_backoff_max_s = retry_backoff_max_s
        self._backoff_jitter = backoff_jitter
        self._rng = rng
        self._monotonic = monotonic
        self._tracked: tuple[str, int] | None = None
        self._entered_at: datetime | None = None
        self._last_key: tuple | None = None
        self._failed: dict[tuple[str, str, int], tuple[int, float]] = {}  # key -> (嘗試次數, 下次可重試的 monotonic)
        self._error_log: dict[str, float] = {}
        self._cleanups: list[Callable[[], None]] = []
        self.last_decision: BehaviorDecision | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    @property
    def mode(self) -> DriverMode:
        return self._mode

    def add_cleanup(self, fn: Callable[[], None]) -> None:
        """``stop`` 時執行一次（例如退訂 bus handler）。"""
        self._cleanups.append(fn)

    def _count(self, name: str) -> None:
        if self._metrics_inc is not None:
            try:
                self._metrics_inc(name)
            except Exception:
                logger.debug("指標寫入失敗：%s", name, exc_info=True)

    def _track(self, facts: BehaviorFacts, now: datetime) -> datetime | None:
        key = (facts.current_behavior, facts.behavior_version)
        if self._tracked is None:
            self._tracked = key  # 第一次觀察：進入時間未知 → None（不套用防抖）
        elif key != self._tracked:
            self._tracked = key
            self._entered_at = now
        return self._entered_at

    def _record(self, decision: BehaviorDecision, current: str) -> None:
        key = (decision.outcome, decision.target, decision.reason_code, current)
        if key != self._last_key:  # 只在決定改變時記 log，避免每個 tick 洗版
            self._last_key = key
            logger.info("Behavior 合理化：%s %s→%s reason=%s mode=%s", decision.outcome, current, decision.target,
                        decision.reason_code, self._mode)

    def tick(self, now: datetime | None = None) -> BehaviorDecision | None:
        """評估一次。off 回 None；shadow 只記錄；on 且有轉換時提 CC-7 請求。"""
        if self._mode == "off":
            return None
        now = now or self._clock()
        facts = self._facts_fn(now)
        slot = self._reader.resolve(now)
        if slot.availability == "unavailable":  # 行程故障 → 保持現狀，不套用 Presence fallback
            decision = BehaviorDecision("HOLD", None, "plan_unavailable",
                                        recheck_at=now + timedelta(seconds=self._policy.recheck_s))
            self.last_decision = decision
            self._record(decision, facts.current_behavior)
            self._count("schedule.behavior.hold")
            return decision
        in_rest_window = (facts.in_rest_window if facts.in_rest_window is not None
                          else slot.source == PlanSource.REST_WINDOW)
        decision = rationalize(RationalizeInput(
            slot=slot, presence_status=facts.presence_status, current_behavior=facts.current_behavior, now=now,
            behavior_entered_at=self._track(facts, now), interaction_idle=facts.interaction_idle,
            has_uninterruptible_work=facts.has_uninterruptible_work, dream_in_flight=facts.dream_in_flight,
            global_stop=facts.global_stop, runtime_ready=facts.runtime_ready, autonomy_enabled=self._autonomy,
            in_rest_window=in_rest_window), self._policy)
        self.last_decision = decision
        self._record(decision, facts.current_behavior)
        transition = decision.outcome in ("ACCEPT_SUGGESTION", "SUBSTITUTE") and decision.target is not None
        if not transition:
            if decision.outcome == "HOLD":
                self._count("schedule.behavior.hold")
            return decision
        if self._mode == "shadow":
            self._count("schedule.behavior.would_transition")
            return decision
        self._submit_transition(facts, decision)
        return decision

    def _submit_transition(self, facts: BehaviorFacts, decision: BehaviorDecision) -> None:
        key = (facts.current_behavior, decision.target, facts.behavior_version)
        attempts, not_before = self._failed.get(key, (0, 0.0))
        if attempts and self._monotonic() < not_before:
            return  # 上次被拒／延後：等 backoff 後再試，避免每個 tick 都寫一筆 state_requests
        attempt = attempts + 1
        try:
            result = self._submit(facts.current_behavior, decision.target, facts.behavior_version,
                                  decision.reason_code, attempt)
        except Exception:
            logger.exception("Behavior 轉換請求失敗：%s→%s", facts.current_behavior, decision.target)
            self._count("schedule.behavior.submit_error")
            return
        self._count(f"schedule.behavior.submitted.{result.lower()}")
        if result == "ACCEPTED":
            self._failed.clear()  # 歸零；Behavior version 已變，舊 key 不會再出現
            return
        if len(self._failed) >= 64:
            self._failed.clear()
        self._failed[key] = (attempt, self._monotonic() + self._backoff(attempt))
        logger.info("Behavior 轉換未被接受：%s→%s %s（第 %d 次）", facts.current_behavior, decision.target, result,
                    attempt)

    def _backoff(self, attempt: int) -> float:
        """第 ``attempt`` 次失敗後的等待秒數：起始值 × 2^(attempt-1)，封頂後套用 ±抖動（rng 為 [0,1)）。"""
        base = min(self._retry_backoff_max_s, self._retry_backoff_s * (2 ** min(attempt - 1, 30)))
        return base * (1.0 + self._backoff_jitter * (self._rng() * 2.0 - 1.0))

    def tick_safe(self) -> BehaviorDecision | None:
        """讀取事實或解析失敗時不採取任何動作（fail-closed：不提請求）；同類錯誤 60 秒才記一次（首次帶 traceback）。"""
        try:
            return self.tick()
        except Exception as exc:
            self._count("schedule.behavior.tick_error")
            name = type(exc).__name__
            now = self._monotonic()
            last = self._error_log.get(name)
            if last is None:
                logger.exception("BehaviorDriver tick 失敗，本次不採取動作")
                self._error_log[name] = now
            elif now - last >= _ERROR_LOG_INTERVAL_S:
                logger.warning("BehaviorDriver tick 持續失敗（%s：%s）", name, exc)
                self._error_log[name] = now
            return None

    # ---- 背景迴圈 ----

    def poke(self) -> None:
        """事件觸發（presence.*／state.changed 等）：讓迴圈立即評估一次。"""
        self._wake.set()

    def start(self) -> None:
        if self._mode == "off" or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="behavior-driver", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        cleanups, self._cleanups = self._cleanups, []
        for fn in cleanups:
            try:
                fn()
            except Exception:
                logger.warning("BehaviorDriver 清理失敗", exc_info=True)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick_safe()
            self._wake.wait(self._interval_s)
            self._wake.clear()
