"""回合範圍的工具結果暫存（D33：internal_read 的結果原文不進 receipt／DB）。

以 turn 為索引、``action_id`` 為鍵；回合結束（``discard_turn``）整批清掉，只在記憶體。receipt 只記 ``result_ref`` 與
狀態短碼，結果文字由 Gateway 的 ``outcome_view`` 從這裡取；取不到時視為 ``unknown``，不編造。

executor 在呼叫它的執行緒內同步執行（``TaskManager.execute_action`` 的階段 2），所以 turn 以執行緒區域的
``turn_scope`` 帶入，executor 不需要知道 turn_id。executor 的 receipt 查核表（``lookup_receipt``）也放在這裡，
一起隨回合清掉：它的生命週期就是「回合內可能需要查核 UNKNOWN」的期間。沒有 ``turn_scope`` 時歸在 ``""`` 這個
未分組的索引，由 ``discard_turn("")`` 或 ``discard`` 清除（測試或直接呼叫 executor 的情況）。
"""
from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

UNSCOPED = ""


class TurnOutcomeCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scope = threading.local()
        self._outcomes: dict[str, dict[str, dict[str, Any]]] = {}
        self._receipts: dict[str, dict[str, Any]] = {}  # turn -> {action_id: receipt}
        self._turn_of: dict[str, str] = {}
        self._about_refs: dict[str, dict[int, str]] = {}  # turn -> 旁證編號 -> projection_id（「關於我」工具的 ref 來源）

    @contextmanager
    def turn_scope(self, turn_id: str) -> Iterator[None]:
        previous = getattr(self._scope, "turn", UNSCOPED)
        self._scope.turn = turn_id
        try:
            yield
        finally:
            self._scope.turn = previous

    def _current(self) -> str:
        return getattr(self._scope, "turn", UNSCOPED)

    def put(self, action_id: str, outcome: dict[str, Any]) -> None:
        turn = self._current()
        with self._lock:
            self._outcomes.setdefault(turn, {})[action_id] = dict(outcome)
            self._turn_of[action_id] = turn

    def get(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            turn = self._turn_of.get(action_id)
            found = self._outcomes.get(turn, {}).get(action_id) if turn is not None else None
            return dict(found) if found is not None else None

    def text(self, action_id: str) -> str | None:
        """結果文字；沒有紀錄回 None（呼叫端據此回 unknown）。"""
        found = self.get(action_id)
        return None if found is None else str(found.get("result", ""))

    def put_receipt(self, action_id: str, receipt: Any) -> None:
        turn = self._current()
        with self._lock:
            self._receipts.setdefault(turn, {})[action_id] = receipt
            self._turn_of.setdefault(action_id, turn)

    def get_receipt(self, action_id: str) -> Any | None:
        with self._lock:
            turn = self._turn_of.get(action_id)
            return self._receipts.get(turn, {}).get(action_id) if turn is not None else None

    def put_about_refs(self, refs: dict[int, str]) -> None:
        """登記本回合 LLM 看得到的旁證編號（整份覆蓋；編號每次查詢都從 1 起，不合併舊份）。"""
        turn = self._current()
        with self._lock:
            self._about_refs[turn] = dict(refs)

    def has_about_refs(self) -> bool:
        with self._lock:
            return bool(self._about_refs.get(self._current()))

    def about_ref(self, ref: int) -> str | None:
        """本回合登記的旁證編號對應的 projection_id；沒登記回 None。"""
        with self._lock:
            return self._about_refs.get(self._current(), {}).get(ref)

    def discard_turn(self, turn_id: str) -> None:
        """回合結束：整批清除該回合的結果與 receipt 查核表。"""
        with self._lock:
            self._about_refs.pop(turn_id, None)
            ids = set(self._outcomes.pop(turn_id, {})) | set(self._receipts.pop(turn_id, {}))
            for action_id in ids:
                self._turn_of.pop(action_id, None)

    def discard(self, action_ids: Iterable[str]) -> None:
        with self._lock:
            for action_id in action_ids:
                turn = self._turn_of.pop(action_id, None)
                if turn is not None:
                    self._outcomes.get(turn, {}).pop(action_id, None)
                    self._receipts.get(turn, {}).pop(action_id, None)

    def __len__(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._outcomes.values()) + sum(len(v) for v in self._receipts.values())
