"""turn_generation 登記簿（CC-4 P-13 的 ``turn_generation_source`` 實作）。

``turn_generation`` 是 persona 範圍內單調遞增的序號：簽發新 ticket 取下一個值，回合被取消（插話、取代、
讓位、撤銷等）時該 turn 改取下一個值。這樣「較新的回合不受舊回合 stop 影響」與 ``CliEndpoint`` 的
``_stopped_turn`` 比較（``intent.turn_generation <= stopped``）語意一致；各 turn 各自從 1 起算會讓
一個 turn 的 stop 誤擋其他 turn 的輸出。

純記憶體、執行緒安全（Gate、sink 包裝、ES loop 都會讀）。回合結束後仍保留一段（``keep``），讓結束後才到的
dispatch／證據回報仍能比對；被取消的 turn 的值已前進，晚到輸出一律 ``REJECT_STALE``。
"""
from __future__ import annotations

import threading
from collections import OrderedDict


class TurnGenerations:
    def __init__(self, *, keep: int = 1024) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self._by_turn: OrderedDict[str, int] = OrderedDict()
        self._keep = keep

    def begin(self, turn_id: str) -> int:
        """登記新 turn，回傳它的初始 generation（全域下一個值）。"""
        with self._lock:
            self._counter += 1
            self._by_turn[turn_id] = self._counter
            self._by_turn.move_to_end(turn_id)
            while len(self._by_turn) > self._keep:
                self._by_turn.popitem(last=False)
            return self._counter

    def get(self, turn_id: str) -> int | None:
        """目前 generation；未知（從未登記或已被修剪）回 None，Gate 對 None 一律拒絕。"""
        with self._lock:
            return self._by_turn.get(turn_id)

    def bump(self, turn_id: str) -> int | None:
        """取消後前進到全域下一個值；未知的 turn 回 None（不憑空建立）。"""
        with self._lock:
            if turn_id not in self._by_turn:
                return None
            self._counter += 1
            self._by_turn[turn_id] = self._counter
            self._by_turn.move_to_end(turn_id)  # 剛被取消的 turn 最近還會有晚到輸出，不先被修剪
            return self._counter

    def is_current(self, turn_id: str, generation: int) -> bool:
        return self.get(turn_id) == generation
