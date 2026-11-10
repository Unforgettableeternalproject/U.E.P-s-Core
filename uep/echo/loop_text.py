"""agent_loop 回合的對外文字暫存（DL-4 PR4）：``content_ref`` -> 文字。

``OutputIntent`` 不內嵌原文（D33），只帶 ``content_ref``；端點在 ``send`` 時依 ``content_ref`` 解析成文字再渲染。
文字只在 Run 交付期間存在：turn 終結時整批清除（與 ``TurnOutcomeCache`` 同生命週期），另有容量上限避免無限增長。
渲染只發生在 Output Gate 放行之後的端點 ``send``，沒有旁路。
"""
from __future__ import annotations

import threading
from collections import OrderedDict

DEFAULT_MAX_ENTRIES = 256


class LoopTextStore:
    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._lock = threading.Lock()
        self._texts: OrderedDict[str, tuple[str, str]] = OrderedDict()  # ref -> (turn_id, text)
        self._max = max_entries

    def put(self, ref: str, turn_id: str, text: str) -> None:
        with self._lock:
            self._texts[ref] = (turn_id, text)
            while len(self._texts) > self._max:
                self._texts.popitem(last=False)

    def get(self, ref: str) -> str | None:
        with self._lock:
            found = self._texts.get(ref)
        return found[1] if found is not None else None

    def discard_turn(self, turn_id: str) -> None:
        with self._lock:
            for ref in [r for r, (tid, _text) in self._texts.items() if tid == turn_id]:
                del self._texts[ref]

    def __len__(self) -> int:
        with self._lock:
            return len(self._texts)
