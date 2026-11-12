"""Module Coordinator 測試用的 fake handle 與共用輔助（不需 DB、不碰 GPU）。

``FakeHandle`` 的每個方法都可以注入：回傳值、例外，或用 ``Event`` 擋住（模擬長時間載入／晚完成）。
擋住的呼叫必須在測試 teardown 釋放（``release_all``），否則 worker 執行緒會卡住。
"""
from __future__ import annotations

import threading
from typing import Any

from uep.lifecycle.handles import ObservationOnlyHandle


class FakeHandle:
    can_load = True
    can_unload = True

    def __init__(self, *, can_load: bool = True, can_unload: bool = True) -> None:
        self.can_load, self.can_unload = can_load, can_unload
        self.calls: list[str] = []
        self.running = 0  # 目前正在執行中的呼叫數（偵測並行）
        self.max_running = 0
        self.probe_result = "ok"
        self.fail: dict[str, BaseException] = {}
        self.gates: dict[str, threading.Event] = {}  # 方法名 -> 放行事件（未 set 前呼叫會卡住）
        self.entered: dict[str, threading.Event] = {n: threading.Event() for n in ("load", "warm", "unload", "probe")}
        self._lock = threading.Lock()

    def block(self, method: str) -> threading.Event:
        gate = self.gates[method] = threading.Event()
        return gate

    def release_all(self) -> None:
        for gate in self.gates.values():
            gate.set()

    def _run(self, name: str) -> None:
        with self._lock:
            self.calls.append(name)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        self.entered[name].set()
        try:
            gate = self.gates.get(name)
            if gate is not None:
                gate.wait(timeout=10)  # 保底：測試忘記釋放也不會永久卡住
            err = self.fail.get(name)
            if err is not None:
                raise err
        finally:
            with self._lock:
                self.running -= 1

    def load(self) -> None:
        self._run("load")

    def warm(self) -> None:
        self._run("warm")

    def unload(self) -> None:
        self._run("unload")

    def probe(self) -> Any:
        self._run("probe")
        return self.probe_result


class FakeObserver(ObservationOnlyHandle):
    """觀測型 handle：只有 probe；任何 load／unload 呼叫都會被基底拒絕並記錄。"""

    def __init__(self, result: str = "ok") -> None:
        self.result = result
        self.probes = 0

    def probe(self) -> Any:
        self.probes += 1
        return self.result


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
