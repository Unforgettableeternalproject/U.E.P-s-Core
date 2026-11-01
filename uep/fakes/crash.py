"""Worker crash 注入點（供恢復測試使用）。

fake 在關鍵步驟呼叫 ``injector.hit(point)``；測試以 ``arm(point, on_hit=n)`` 讓第 n 次命中時
拋 SimulatedCrash，模擬 process 在該步驟中止（之後的程式碼不會執行）。
"""
from __future__ import annotations

from collections import Counter


class SimulatedCrash(BaseException):
    """模擬 process 崩潰。繼承 BaseException，避免被一般 ``except Exception`` 吞掉。"""


class CrashInjector:
    def __init__(self) -> None:
        self._armed: dict[str, int] = {}
        self.hits: Counter[str] = Counter()

    def arm(self, point: str, on_hit: int = 1) -> None:
        """第 ``on_hit`` 次（從 1 起算）命中 ``point`` 時崩潰；觸發後自動解除。"""
        if on_hit < 1:
            raise ValueError("on_hit 必須 >= 1")
        self._armed[point] = on_hit

    def disarm(self, point: str | None = None) -> None:
        if point is None:
            self._armed.clear()
        else:
            self._armed.pop(point, None)

    def hit(self, point: str) -> None:
        self.hits[point] += 1
        target = self._armed.get(point)
        if target is not None and self.hits[point] >= target:
            del self._armed[point]
            raise SimulatedCrash(point)
