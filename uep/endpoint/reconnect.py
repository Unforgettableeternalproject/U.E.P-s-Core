"""背景自動重連（J1）：連線中斷後以指數退避重連，成功後同步權威快照（``endpoint.reconnect``）。

純邏輯、不 sleep：``step`` 回傳下一次應等待的秒數，``run`` 以可中斷的 Event 等待。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from uep.endpoint.auth import EndpointAuthError
from uep.endpoint.endpoint import ReconnectOutcome
from uep.endpoint.protocol import CoreApiError

logger = logging.getLogger("uep.endpoint.reconnect")

_ERRORS = (OSError, CoreApiError, EndpointAuthError)


class ReconnectSupervisor:
    def __init__(
        self, is_connected: Callable[[], bool], connect: Callable[[], None],
        on_connected: Callable[[], ReconnectOutcome], *, base: float = 0.5, cap: float = 10.0,
        poll: float = 1.0,
    ) -> None:
        self._is_connected = is_connected
        self._connect = connect
        self._on_connected = on_connected
        self._base, self._cap, self._poll = base, cap, poll
        self._failures = 0
        self._need_sync = False

    def step(self) -> float:
        """做一輪檢查／重連；回傳下一輪前應等待的秒數。"""
        connected = self._is_connected()
        if connected and not self._need_sync:
            return self._poll
        self._need_sync = True  # 只要斷過線，就必須重新同步快照
        try:
            if not connected:
                self._connect()
            outcome = self._on_connected()
        except _ERRORS as exc:
            logger.warning("重連失敗：%s", exc)
            return self._backoff()
        if outcome is ReconnectOutcome.RETRY:
            return self._backoff()
        self._failures, self._need_sync = 0, False
        return self._poll

    def _backoff(self) -> float:
        delay = min(self._cap, self._base * (2 ** self._failures))
        self._failures += 1
        return delay

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                delay = self.step()
            except Exception:  # 監督迴圈不得因單次錯誤結束
                logger.exception("重連監督錯誤")
                delay = self._backoff()
            stop.wait(delay)


def start_supervisor(supervisor: ReconnectSupervisor, stop: threading.Event) -> threading.Thread:
    thread: Any = threading.Thread(target=supervisor.run, args=(stop,), daemon=True, name="UepEndpointReconnect")
    thread.start()
    return thread
