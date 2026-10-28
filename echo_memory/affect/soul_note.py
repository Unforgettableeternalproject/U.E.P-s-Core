"""
Soul Note — 潛意識日記系統

對應 OpenSoul soul/core/soul_note.py。

兩層結構：
  1. 小筆記（soul_notes/YYYY-MM-DD.jsonl）：每輪對話後寫入
  2. 日反思（soul_notes/reflections/YYYY-MM-DD.json）：LLM 壓縮當日小筆記

持久化格式：JSONL（每行一筆 JSON）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("echo_memory.affect.soul_note")


@dataclass
class SoulNote:
    """潛意識小筆記。"""

    timestamp: str = ""
    category: str = "reflection"  # reflection | discovery | error | memory_update | neurochemistry
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class DailyReflection:
    """日反思（壓縮版）。"""

    date: str = ""
    content: str = ""
    note_count: int = 0
    key_insights: list[str] = field(default_factory=list)
    created_at: str = ""


class SoulNoteManager:
    """
    潛意識日記管理器。

    負責小筆記的寫入/讀取和日反思的壓縮。
    """

    def __init__(self, storage_path: str | Path):
        """
        Args:
            storage_path: 基礎存儲路徑（會在其下建立 soul_notes/ 目錄）
        """
        self._base_path = Path(storage_path) / "soul_notes"
        self._base_path.mkdir(parents=True, exist_ok=True)
        self._reflections_path = self._base_path / "reflections"
        self._reflections_path.mkdir(parents=True, exist_ok=True)

    def add_note(self, note: SoulNote, raise_on_error: bool = False) -> None:
        """
        寫入一則小筆記。

        存儲格式：JSONL，按日期分檔。

        Args:
            note: 要寫入的小筆記
            raise_on_error: True 時寫入失敗會拋出例外（供 EpisodeGate 判斷 committed/failed）；
                預設 False 維持只記 log 的舊行為
        """
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = self._base_path / f"{date_str}.jsonl"

        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(note), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[SoulNoteManager] 寫入小筆記失敗: {e}")
            if raise_on_error:
                raise

    def get_notes_today(self) -> list[SoulNote]:
        """取得今日的小筆記。"""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._read_notes(date_str)

    def get_notes_for_date(self, date_str: str) -> list[SoulNote]:
        """取得指定日期的小筆記。"""
        return self._read_notes(date_str)

    def _read_notes(self, date_str: str) -> list[SoulNote]:
        """讀取指定日期的小筆記。"""
        file_path = self._base_path / f"{date_str}.jsonl"
        if not file_path.exists():
            return []

        notes = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        data = json.loads(line)
                        notes.append(SoulNote(**data))
        except Exception as e:
            logger.warning(f"[SoulNoteManager] 讀取小筆記失敗: {e}")

        return notes

    def save_daily_reflection(self, reflection: DailyReflection) -> None:
        """儲存日反思。"""
        file_path = self._reflections_path / f"{reflection.date}.json"
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(asdict(reflection), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[SoulNoteManager] 儲存日反思失敗: {e}")

    def get_latest_reflection(self) -> DailyReflection | None:
        """取得最近的日反思。"""
        reflections = sorted(self._reflections_path.glob("*.json"), reverse=True)
        if not reflections:
            return None

        try:
            with open(reflections[0], "r", encoding="utf-8") as f:
                data = json.load(f)
            return DailyReflection(**data)
        except Exception:
            return None

    def get_note_count(self) -> int:
        """取得所有小筆記的總數。"""
        count = 0
        for f in self._base_path.glob("*.jsonl"):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    count += sum(1 for line in fh if line.strip())
            except Exception:
                pass
        return count
