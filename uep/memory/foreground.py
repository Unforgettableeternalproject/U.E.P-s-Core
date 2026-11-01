"""做夢迴圈的前景 Probe（Host全域做夢迴圈設計 §2.2、C-4）：State Coordinator＋Event Bus 的實作。

由組裝層建立並注入 ``MemoryHost(dream_probe=...)``；``MemoryHost`` 本體不 import ``uep.state``（這裡只靠 duck typing 的
``current(axis)``，axis 用字串）。Probe 只取事件時間戳與 episode 計數，不讀 payload。

語音活動（P3 E-9：音訊不是 UEPEvent，ES 在 turn 結束前不會產生事件）目前 Core 沒有提供，所以用可注入的
``voice_activity_fn`` 接口（回傳最近一次語音活動的單調時間，或 None）；預設不提供＝只靠事件，最大誤差為一句話的長度
（設計 R4、Core 側 C-1，``EchoPipelineHost`` 暴露語音活動時間後再接上）。
"""
from __future__ import annotations

import logging
import threading
import time as _time
from collections.abc import Callable
from typing import Any, Protocol

from uep.memory.dream_planner import ForegroundSnapshot

logger = logging.getLogger("uep.memory.foreground")

# 更新「最近前景活動」時間戳的事件（§2.2）
ACTIVITY_EVENTS = (
    "input.text_final", "input.utterance_final", "turn.started", "turn.completed", "turn.cancelled",
    "conversation.participant_joined",
)
_EPISODE_STARTED = "conversation.episode_started"
_EPISODE_ENDED = "conversation.episode_ended"
_STOP_ENGAGED = "core.global_stop_engaged"
_STOP_RELEASED = "core.global_stop_released"
_READY_RUNTIME = frozenset({"READY", "DEGRADED"})


class _StateReader(Protocol):
    def current(self, axis: Any, subject: str = "") -> Any: ...


class _BusLike(Protocol):
    def subscribe(self, event_type: str, handler: Callable[[Any], None]) -> None: ...

    def unsubscribe(self, event_type: str, handler: Callable[[Any], None]) -> None: ...


class StateBusForegroundProbe:
    def __init__(
        self,
        state: _StateReader,
        bus: _BusLike,
        *,
        clock: Callable[[], float] = _time.monotonic,
        voice_activity_fn: Callable[[], float | None] | None = None,
        global_stop_fn: Callable[[], bool] | None = None,
    ) -> None:
        self._state = state
        self._bus = bus
        self._clock = clock
        self._voice_fn = voice_activity_fn
        self._stop_fn = global_stop_fn  # 例如 ``GlobalStop.is_engaged``；與事件追蹤取 OR（保守）
        self._lock = threading.Lock()
        self._last_activity = clock()  # 沒收過任何事件時，從建立當下起算安靜時間（保守）
        self._open_episodes = 0
        self._stop_engaged = False
        self._subscribed = False

    def start(self) -> None:
        """訂閱 Bus（冪等）。handler 在 Bus 消費者執行緒上執行，只更新記憶體內計數。"""
        if self._subscribed:
            return
        for event_type in ACTIVITY_EVENTS:
            self._bus.subscribe(event_type, self._on_activity)
        self._bus.subscribe(_EPISODE_STARTED, self._on_episode_started)
        self._bus.subscribe(_EPISODE_ENDED, self._on_episode_ended)
        self._bus.subscribe(_STOP_ENGAGED, self._on_stop_engaged)
        self._bus.subscribe(_STOP_RELEASED, self._on_stop_released)
        self._subscribed = True

    def close(self) -> None:
        if not self._subscribed:
            return
        for event_type in ACTIVITY_EVENTS:
            self._bus.unsubscribe(event_type, self._on_activity)
        self._bus.unsubscribe(_EPISODE_STARTED, self._on_episode_started)
        self._bus.unsubscribe(_EPISODE_ENDED, self._on_episode_ended)
        self._bus.unsubscribe(_STOP_ENGAGED, self._on_stop_engaged)
        self._bus.unsubscribe(_STOP_RELEASED, self._on_stop_released)
        self._subscribed = False

    # ---- Bus handler ----

    def _on_activity(self, event: Any) -> None:
        with self._lock:
            self._last_activity = self._clock()

    def _on_episode_started(self, event: Any) -> None:
        with self._lock:
            self._open_episodes += 1
            self._last_activity = self._clock()

    def _on_episode_ended(self, event: Any) -> None:
        with self._lock:
            self._open_episodes = max(0, self._open_episodes - 1)
            self._last_activity = self._clock()

    def _on_stop_engaged(self, event: Any) -> None:
        with self._lock:
            self._stop_engaged = True

    def _on_stop_released(self, event: Any) -> None:
        with self._lock:
            self._stop_engaged = False

    # ---- snapshot ----

    def snapshot(self, now: float) -> ForegroundSnapshot:
        """讀不到的軸一律取保守值（UNKNOWN／未就緒），讓 Planner 不開始做夢。"""
        interaction = self._axis_state("Interaction")
        behavior = self._axis_state("Behavior")
        runtime = self._axis_state("Runtime")
        with self._lock:
            last = self._last_activity
            episodes = self._open_episodes
            stop = self._stop_engaged
        voice = self._safe_call(self._voice_fn)
        if voice is not None:
            last = max(last, float(voice))
        if self._stop_fn is not None:
            engaged = self._safe_call(self._stop_fn)
            stop = stop or engaged is None or bool(engaged)  # 查不到旗標視為已停止（保守）
        return ForegroundSnapshot(
            interaction_state=interaction, behavior_state=behavior, open_episodes=episodes,
            quiet_for_s=max(0.0, now - last), global_stop_engaged=stop, runtime_ready=runtime in _READY_RUNTIME,
        )

    def _axis_state(self, axis: str) -> str:
        try:
            return str(self._state.current(axis).state)
        except Exception as exc:  # noqa: BLE001 - 讀 State 失敗不得讓迴圈死亡；回保守值
            logger.warning("讀取 %s 軸失敗，視為未知：%s", axis, type(exc).__name__)
            return "UNKNOWN"

    @staticmethod
    def _safe_call(fn: Callable[[], Any] | None) -> Any:
        if fn is None:
            return None
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("前景 Probe 輔助來源讀取失敗：%s", type(exc).__name__)
            return None
