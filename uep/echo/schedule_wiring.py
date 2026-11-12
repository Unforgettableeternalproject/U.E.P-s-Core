"""行程 → 做夢休息時段的接線（PP-2）：``PlanBackedRestWindowPolicy`` 與依旗標組裝。

- ``uep/schedule`` 不 import ``uep.memory``；``RestWindow`` 等記憶端型別只出現在這個接線檔（草案 §4、R-PP-2）。
- 固定時窗：``on_window_end`` 恆不順延（夜貓子延到週期四）。
- 行程不可用（儲存讀取失敗、時區不可用、reader 例外）時退回 ``fixed_policy``（現行 FixedWindowPolicy），
  保住夜間做夢，不因行程故障而停擺；進入／離開降級各記一次 log。
- 旗標 ``UEP_SCHEDULE_REST_WINDOW``：``fixed``（預設，行為完全不變）／``plan``。
"""
from __future__ import annotations

import logging
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
    """以行程的 ``rest_window`` 項實作 ``RestWindowPolicy``（固定時窗，不順延）。"""

    def __init__(self, reader: PlanReader, *, fallback: RestWindowPolicy | None = None,
                 tz: str | Callable[[], str] = DEFAULT_TZ) -> None:
        self._reader = reader
        self._fallback: RestWindowPolicy = fallback or FixedWindowPolicy()
        self._tz = tz
        self._degraded = False

    def _tz_label(self) -> str:
        try:
            return self._tz() if callable(self._tz) else self._tz
        except Exception:  # noqa: BLE001 - 標籤取不到不影響窗口判定
            return DEFAULT_TZ

    def _degrade(self, reason: str, now: datetime) -> RestWindow | None:
        if not self._degraded:
            self._degraded = True
            logger.warning("行程不可用（%s），休息時段退回固定時窗", reason)
        return self._fallback.window_at(now)

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
            return None  # 不在窗口內（含 override／明確行程項截斷窗口期間）
        if slot.ends_at is None:  # 窗口一定有終點；沒有代表資料不一致
            return self._degrade("rest_window 缺少終點", now)
        return RestWindow(slot.starts_at, slot.ends_at, self._tz_label())

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
    return PlanBackedRestWindowPolicy(store, fallback=base, tz=getattr(store, "tz", DEFAULT_TZ))


__all__ = ["PlanBackedRestWindowPolicy", "build_rest_window_policy"]
