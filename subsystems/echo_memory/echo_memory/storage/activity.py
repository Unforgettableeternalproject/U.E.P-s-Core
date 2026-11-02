"""
ActivityStore - 平台觀察的活動彙整（per-namespace）

把 observation 證據（如 Discord presence 的遊戲時段）累加成每個 subject 一筆的統計：
play_count、total_minutes、first_seen、last_played、evidence_ids（截上限）。
純數值累加，不使用 LLM 或關鍵字判斷；名稱正規化只做 strip + casefold。

為何不用 EntityStore：EntityStore 以名稱全域唯一，會與對話抽取的實體（例如同名遊戲）
互相覆寫 entity_type，且實體會進入 prompt／Dream 修剪；活動統計需要獨立、可整批清除。

冪等：apply 以 evidence_id 去重（evidence_ids 內已有就跳過），所以「證據已寫入但彙整
未完成」重試時只會補一次，不會重複累加。呼叫端須持有 storage_lock。
存放：{namespace}/activity.json
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.storage.atomic import atomic_write_json

logger = logging.getLogger("echo_memory.activity")

ACTIVITY_FILENAME = "activity.json"
ACTIVITY_VERSION = 1
MAX_EVIDENCE_IDS = 200


def normalize_subject(name: str) -> str:
    """正規化 key：只做 strip 與大小寫無關（casefold），不做語意合併。"""
    return name.strip().casefold()


class ActivityStore:
    """kind -> normalized subject -> 統計 的持久化彙整（呼叫端須持有 storage_lock）。"""

    def __init__(self, directory: str | Path, *, max_evidence_ids: int = MAX_EVIDENCE_IDS):
        self.path = Path(directory) / ACTIVITY_FILENAME
        self.max_evidence_ids = max(1, int(max_evidence_ids))

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            kinds = data["kinds"]
            if not isinstance(kinds, dict):
                raise TypeError("kinds 不是物件")
            return kinds
        except (ValueError, KeyError, TypeError) as exc:
            # 彙整可由 evidence 重建；壞檔備份後視為空，不阻塞寫入
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}.bak")
            self.path.replace(backup)
            logger.warning("[Activity] 彙整檔損壞（%s），已備份至 %s 並視為空", exc, backup)
            return {}

    def _save(self, kinds: dict[str, dict[str, dict[str, Any]]]) -> None:
        atomic_write_json(self.path, {"version": ACTIVITY_VERSION, "kinds": kinds}, indent=None)

    def apply(
        self,
        kind: str,
        subject: str,
        *,
        minutes: float,
        started_at: datetime,
        ended_at: datetime,
        evidence_id: str,
    ) -> bool:
        """累加一筆時段。evidence_id 已累加過則不動並回傳 False。"""
        kinds = self._load()
        bucket = kinds.setdefault(kind, {})
        key = normalize_subject(subject)
        entry = bucket.get(key)
        if entry is None:
            entry = {
                "name": subject.strip(),
                "play_count": 0,
                "total_minutes": 0.0,
                "first_seen": started_at.isoformat(),
                "last_played": ended_at.isoformat(),
                "evidence_ids": [],
            }
            bucket[key] = entry
        elif evidence_id in entry["evidence_ids"]:
            return False
        entry["play_count"] += 1
        entry["total_minutes"] = round(entry["total_minutes"] + minutes, 4)
        if started_at.isoformat() < entry["first_seen"]:
            entry["first_seen"] = started_at.isoformat()
        if ended_at.isoformat() > entry["last_played"]:
            entry["last_played"] = ended_at.isoformat()
        entry["evidence_ids"].append(evidence_id)
        del entry["evidence_ids"][: -self.max_evidence_ids]
        self._save(kinds)
        return True

    def has_applied(self, kind: str, subject: str, evidence_id: str) -> bool:
        entry = self._load().get(kind, {}).get(normalize_subject(subject))
        return entry is not None and evidence_id in entry["evidence_ids"]

    def list(self, kind: str) -> list[dict[str, Any]]:
        return [dict(e) for e in self._load().get(kind, {}).values()]

    def clear(self, kind: str) -> int:
        """移除某 kind 的全部彙整，回傳移除的 subject 數。"""
        kinds = self._load()
        removed = len(kinds.pop(kind, {}))
        if removed or self.path.exists():
            self._save(kinds)
        return removed
