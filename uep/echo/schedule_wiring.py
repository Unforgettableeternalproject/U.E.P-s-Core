"""行程 → 做夢休息時段的接線（PP-2）：``PlanBackedRestWindowPolicy`` 與依旗標組裝。

- ``uep/schedule`` 不 import ``uep.memory``；``RestWindow`` 等記憶端型別只出現在這個接線檔（草案 §4、R-PP-2）。
- 固定時窗：``on_window_end`` 恆不順延（夜貓子延到週期四）。
- 行程不可用（儲存讀取失敗、時區不可用、reader 例外）時：快取窗口仍涵蓋 now 就沿用到窗口結束，之後退回
  ``fixed_policy``（現行 FixedWindowPolicy），保住夜間做夢，不因行程故障而停擺；進入／離開降級各記一次 log。
- plan 模式下行程沒有 rest_window 項時同樣降級到 fixed 並記一次 warning。
- 旗標 ``UEP_SCHEDULE_REST_WINDOW``：``fixed``（預設，行為完全不變）／``plan``。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from uep.contracts.schedule import PlanReader, PlanSource
from uep.memory.dream_planner import (
    FixedWindowPolicy,
    RestWindow,
    RestWindowPolicy,
    WindowDecision,
    WindowOutcome,
)

logger = logging.getLogger(__name__)

REST_WINDOW_FIXED = "fixed"
REST_WINDOW_PLAN = "plan"
DEFAULT_TZ = "Asia/Taipei"


class PlanBackedRestWindowPolicy:
    """以行程的 ``rest_window`` 項實作 ``RestWindowPolicy``（固定時窗，不順延）。

    降級順序（保住夜間做夢）：
    1. 行程暫時讀不到（reader 例外／unavailable）：若最後一次成功取得的窗口仍涵蓋 ``now``，沿用到窗口結束（遲滯），
       避免窗口進行中因短暫故障被切斷；窗口已結束或沒有快取才退回 ``fallback``（FixedWindowPolicy）。
    2. 行程可讀但根本沒有 ``rest_window`` 項（reader 提供 ``has_rest_window()`` 時才檢查）：退回 ``fallback`` 並記一次
       warning；啟動、每次從降級恢復時檢查，缺項狀態下每 ``recheck_s`` 秒再檢查一次。
    窗口的 tz 取自 rest_window 項自己的時區（``slot.tz``），拿不到時用 ``tz`` 固定字串。
    """

    def __init__(self, reader: PlanReader, *, fallback: RestWindowPolicy | None = None, tz: str = DEFAULT_TZ,
                 recheck_s: float = 60.0, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._reader = reader
        self._fallback: RestWindowPolicy = fallback or FixedWindowPolicy()
        self._tz = tz
        self._recheck_s = recheck_s
        self._monotonic = monotonic
        self._degraded = False
        self._cached: RestWindow | None = None
        self._item_known: bool | None = None  # None＝尚未確認 rest_window 項存在
        self._last_missing_check = 0.0

    # ---- 降級 ----

    def _degrade(self, reason: str, now: datetime) -> RestWindow | None:
        if not self._degraded:
            self._degraded = True
            self._item_known = None  # 恢復後重新檢查項是否存在
            logger.warning("行程不可用（%s），休息時段改用快取窗口或固定時窗", reason)
        cached = self._cached
        if cached is not None and cached.start <= now < cached.end:
            return cached  # 遲滯：沿用到窗口結束
        return self._fallback.window_at(now)

    def _rest_window_exists(self) -> bool:
        """reader 有提供 ``has_rest_window()`` 才檢查；檢查本身失敗視為「不確定」＝當作存在（交給降級流程）。"""
        check = getattr(self._reader, "has_rest_window", None)
        if check is None:
            return True
        try:
            return bool(check())
        except Exception:  # noqa: BLE001 - 檢查失敗不改變判定，下個 tick 再試
            return True

    def verify(self) -> bool:
        """啟動時檢查 rest_window 項是否存在；不存在記一次 warning（之後 window_at 會降級）。"""
        exists = self._rest_window_exists()
        if not exists and self._item_known is not False:
            logger.warning("plan 模式但行程沒有 rest_window 項，休息時段退回固定時窗")
        self._item_known = exists
        self._last_missing_check = self._monotonic()
        return exists

    # ---- RestWindowPolicy ----

    def window_at(self, now: datetime) -> RestWindow | None:
        if now.tzinfo is None:
            raise ValueError("window_at 需要 tz-aware 的時間")
        try:
            slot = self._reader.resolve(now)
        except Exception as exc:  # noqa: BLE001 - 做夢迴圈每個 tick 都會呼叫；行程故障不得讓它停擺
            return self._degrade(f"{type(exc).__name__}: {exc}", now)
        if slot.availability != "available":
            return self._degrade(slot.reason_code or "unavailable", now)
        if self._degraded:
            self._degraded = False
            logger.info("行程恢復可用，休息時段改回由行程提供")
        if slot.source != PlanSource.REST_WINDOW:
            # 窗口外：要分辨「真的不在窗口」與「根本沒有 rest_window 項」
            if self._item_known is None or (
                    self._item_known is False and self._monotonic() - self._last_missing_check >= self._recheck_s):
                self.verify()
            self._cached = None
            if self._item_known is False:
                return self._fallback.window_at(now)
            return None  # 不在窗口內（含 override／明確行程項截斷窗口期間）
        if slot.ends_at is None:  # 窗口一定有終點；沒有代表資料不一致
            return self._degrade("rest_window 缺少終點", now)
        self._item_known = True
        window = RestWindow(slot.starts_at, slot.ends_at, slot.tz or self._tz)
        self._cached = window
        return window

    def on_window_end(self, window: RestWindow, outcome: WindowOutcome) -> WindowDecision:
        # 夜貓子（順延、wants_intake）延到週期四：恆不順延
        return WindowDecision(extend_until=None, wants_intake=False, reason="extension_deferred")


def build_rest_window_policy(settings: Any, fixed_policy: RestWindowPolicy | None, session_factory: Any | None, *,
                             store: Any | None = None) -> RestWindowPolicy | None:
    """依 ``settings.schedule_rest_window`` 決定休息時段來源。

    - ``fixed``（預設）：原樣回傳 ``fixed_policy``（可能為 None，由 DreamLoop 用預設 FixedWindowPolicy）。
    - ``plan``：以 DB 的行程提供。啟動時以 ``fixed_policy``（即 ``UEP_DREAM_WINDOW_*`` 或預設 03:00–05:30）播種
      ``rest_window`` 項；``UEP_DREAM_WINDOW_*`` **明確設定**時覆寫既有項（供白天驗證）並記 log。
      播種失敗或沒有 DB 時記 log 並退回 ``fixed_policy``，不讓做夢停擺。
    - 其他值：``ValueError``（fail-fast）。
    """
    mode = (getattr(settings, "schedule_rest_window", REST_WINDOW_FIXED) or REST_WINDOW_FIXED).strip().lower()
    if mode == REST_WINDOW_FIXED:
        return fixed_policy
    if mode != REST_WINDOW_PLAN:
        raise ValueError(f"UEP_SCHEDULE_REST_WINDOW 只接受 fixed 或 plan：{mode!r}")
    base = fixed_policy if isinstance(fixed_policy, FixedWindowPolicy) else FixedWindowPolicy()
    if store is None:
        if session_factory is None:
            logger.error("UEP_SCHEDULE_REST_WINDOW=plan 但沒有資料庫，休息時段維持固定時窗")
            return fixed_policy
        from uep.schedule.store import PlanStore

        store = PlanStore(session_factory)
    explicit = any(getattr(settings, name, None) for name in
                   ("dream_window_start", "dream_window_end", "dream_window_tz"))
    try:
        outcome = store.seed_rest_window(base.start, base.end, base.tz, overwrite=explicit)
    except Exception:
        logger.exception("行程休息窗口播種失敗，休息時段維持固定時窗")
        return fixed_policy
    if explicit and outcome != "kept":
        logger.warning("UEP_DREAM_WINDOW_* 已覆寫行程的休息窗口：%s %s-%s %s", outcome, base.start, base.end, base.tz)
    policy = PlanBackedRestWindowPolicy(store, fallback=base, tz=base.tz)
    policy.verify()  # 啟動時確認 rest_window 項存在；缺項會記一次 warning 並降級
    return policy


# ---- Behavior driver（PP-3b）----

_PRESENCE_EVENTS = ("presence.granted", "presence.activated", "presence.revoked", "presence.recovering",
                    "presence.recovered", "state.changed")


def attach_behavior_driver(settings: Any, core: Any, *, session_factory: Any, rest_policy: RestWindowPolicy | None,
                           reader: Any | None = None, metrics_inc: Callable[[str], None] | None = None) -> Any | None:
    """依 ``settings.schedule_behavior_driver``（``off|shadow|on``，預設 shadow）組裝並啟動 BehaviorDriver。

    ``off`` 回 None。輸入事實：State 軸（Presence／Interaction／Runtime／Behavior）、全域停止旗標、
    DreamLoop 的 ``dream_status()["in_flight"]``、``rest_policy.window_at``（與做夢迴圈用同一個 policy，N4）。
    ``has_uninterruptible_work`` 第一版固定 False（Task 層尚無可查詢介面）。shadow 只記 log／指標，不提請求。
    """
    from uep.schedule.driver import BehaviorDriver, BehaviorFacts, parse_driver_mode
    from uep.state.coordinator import Axis, StateTransitionRequest

    mode = parse_driver_mode(getattr(settings, "schedule_behavior_driver", None))
    if mode == "off":
        return None
    if reader is None:
        from uep.schedule.store import PlanStore

        reader = PlanStore(session_factory)
    window_policy = rest_policy if rest_policy is not None else FixedWindowPolicy()
    state, stop, memory, bus = core.state, core.stop, getattr(core, "memory", None), getattr(core, "bus", None)

    def facts(now: datetime) -> Any:
        presence = state.current(Axis.PRESENCE).state
        behavior = state.current(Axis.BEHAVIOR)
        runtime = state.current(Axis.RUNTIME).state
        in_flight = False
        if memory is not None:
            in_flight = bool((memory.dream_status() or {}).get("in_flight"))
        return BehaviorFacts(
            presence_status=presence, current_behavior=behavior.state, behavior_version=behavior.version,
            interaction_idle=state.current(Axis.INTERACTION).state == "IDLE", global_stop=bool(stop.is_engaged()),
            runtime_ready=runtime in ("READY", "DEGRADED"), dream_in_flight=in_flight,
            in_rest_window=window_policy.window_at(now) is not None)

    def submit(from_state: str, to_state: str, version: int, reason_code: str) -> str:
        decision = state.request_transition(StateTransitionRequest(
            request_id=f"policy_behavior:{from_state}>{to_state}:v{version}", axis=Axis.BEHAVIOR.value,
            proposer="core.policy", expected_version=version, from_state=from_state, to_state=to_state,
            reason={"source": "behavior_driver", "reason_code": reason_code}))
        return decision.decision.value

    driver = BehaviorDriver(reader, facts, submit, mode=mode, metrics_inc=metrics_inc)
    if bus is not None:
        for event_type in _PRESENCE_EVENTS:
            bus.subscribe(event_type, lambda _event: driver.poke())
    driver.start()
    logger.info("BehaviorDriver 已啟動：mode=%s", mode)
    return driver


__all__ = ["PlanBackedRestWindowPolicy", "attach_behavior_driver", "build_rest_window_policy"]
