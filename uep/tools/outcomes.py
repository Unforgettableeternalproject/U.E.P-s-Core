"""回合範圍的工具結果暫存（D33：internal_read 的結果原文不進 receipt／DB）。

以 ``action_id`` 為鍵；回合結束（``discard``）就清掉，只在記憶體。receipt 只記 ``result_ref`` 與狀態短碼，
結果文字由 Gateway 的 ``outcome_view`` 從這裡取；取不到時視為 ``unknown``，不編造。
"""
from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Any


class TurnOutcomeCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outcomes: dict[str, dict[str, Any]] = {}

    def put(self, action_id: str, outcome: dict[str, Any]) -> None:
        with self._lock:
            self._outcomes[action_id] = dict(outcome)

    def get(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._outcomes.get(action_id)
            return dict(found) if found is not None else None

    def text(self, action_id: str) -> str | None:
        """結果文字；沒有紀錄回 None（呼叫端據此回 unknown）。"""
        found = self.get(action_id)
        return None if found is None else str(found.get("result", ""))

    def discard(self, action_ids: Iterable[str]) -> None:
        with self._lock:
            for action_id in action_ids:
                self._outcomes.pop(action_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._outcomes)
