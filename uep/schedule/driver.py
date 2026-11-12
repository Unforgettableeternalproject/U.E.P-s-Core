"""BehaviorDriver（PP-3b）：行程建議 → 主機合理化 → 提 CC-7 請求（契約 §8.5）。

- 邏輯全在純函式 ``rationalize``；這裡只有取事實、記錄與（``on`` 時）提請求的副作用。
- 依賴方向：只依賴 ``uep.contracts``／``uep.schedule``；State、Presence、DreamLoop 等都經注入的 callable 取得
  （組裝在 ``uep/echo/schedule_wiring.py``），所以本模組不 import ``uep.state`` 或 ``uep.memory``。
- 開關 ``off｜shadow｜on``（預設 shadow）：shadow 只記 log／指標（would_transition），不提請求；
  Behavior 不是安全閘門，但會改變 muted／做夢放寬等下游行為，所以先 shadow（先例：D100）。
- 已知近似（第一版）：
  * ``behavior_entered_at`` 以「看到 Behavior 軸 version 變化的時間」近似（StateView 沒有進入時間，不改 State 結構）；
    第一次觀察到時為 None（不套用防抖）。
  * ``has_uninterruptible_work`` 暫時固定 False（Task 層還沒有可查詢的介面）。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from uep.contracts.schedule import PlanReader
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
    in_rest_window: bool = False  # 由 RestWindowPolicy.window_at 判定（與 DreamLoop 用同一個 policy）
    has_uninterruptible_work: bool = False  # 第一版固定 False


# (from_state, to_state, expected_version, reason_code) -> 裁決名稱（如 "ACCEPTED"）
SubmitFn = Callable[[str, str, int, str], str]


class BehaviorDriver:
    """單一寫入者：只有它以 ``core.policy`` 對 Behavior 軸提請求（BH-2）。"""

    def __init__(self, reader: PlanReader, facts_fn: Callable[[datetime], BehaviorFacts], submit_fn: SubmitFn, *,
                 mode: DriverMode = "shadow", clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 policy: RationalizePolicy | None = None, interval_s: float = DEFAULT_INTERVAL_S,
                 metrics_inc: Callable[[str], None] | None = None, autonomy_enabled: bool = False) -> None:
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
        self._tracked: tuple[str, int] | None = None
        self._entered_at: datetime | None = None
        self._last_key: tuple | None = None
        self.last_decision: BehaviorDecision | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    @property
    def mode(self) -> DriverMode:
        return self._mode

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

    def tick(self, now: datetime | None = None) -> BehaviorDecision | None:
        """評估一次。off 回 None；shadow 只記錄；on 且有轉換時提 CC-7 請求。"""
        if self._mode == "off":
            return None
        now = now or self._clock()
        facts = self._facts_fn(now)
        slot = self._reader.resolve(now)
        decision = rationalize(RationalizeInput(
            slot=slot, presence_status=facts.presence_status, current_behavior=facts.current_behavior, now=now,
            behavior_entered_at=self._track(facts, now), interaction_idle=facts.interaction_idle,
            has_uninterruptible_work=facts.has_uninterruptible_work, dream_in_flight=facts.dream_in_flight,
            global_stop=facts.global_stop, runtime_ready=facts.runtime_ready, autonomy_enabled=self._autonomy,
            in_rest_window=facts.in_rest_window), self._policy)
        self.last_decision = decision
        transition = decision.outcome in ("ACCEPT_SUGGESTION", "SUBSTITUTE") and decision.target is not None
        key = (decision.outcome, decision.target, decision.reason_code, facts.current_behavior)
        if key != self._last_key:  # 只在決定改變時記 log，避免每個 tick 洗版
            self._last_key = key
            logger.info("Behavior 合理化：%s %s→%s reason=%s mode=%s", decision.outcome, facts.current_behavior,
                        decision.target, decision.reason_code, self._mode)
        if not transition:
            if decision.outcome == "HOLD":
                self._count("schedule.behavior.hold")
            return decision
        if self._mode == "shadow":
            self._count("schedule.behavior.would_transition")
            return decision
        try:
            result = self._submit(facts.current_behavior, decision.target, facts.behavior_version,
                                  decision.reason_code)
        except Exception:
            logger.exception("Behavior 轉換請求失敗：%s→%s", facts.current_behavior, decision.target)
            self._count("schedule.behavior.submit_error")
            return decision
        self._count(f"schedule.behavior.submitted.{result.lower()}")
        if result != "ACCEPTED":
            logger.info("Behavior 轉換未被接受：%s→%s %s", facts.current_behavior, decision.target, result)
        return decision

    def tick_safe(self) -> BehaviorDecision | None:
        """讀取事實或解析失敗時記 log 並不採取任何動作（fail-closed：不提請求）。"""
        try:
            return self.tick()
        except Exception:
            logger.exception("BehaviorDriver tick 失敗，本次不採取動作")
            self._count("schedule.behavior.tick_error")
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
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick_safe()
            self._wake.wait(self._interval_s)
            self._wake.clear()
