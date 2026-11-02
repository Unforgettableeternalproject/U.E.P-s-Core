"""
寫入冪等索引

store_episode 帶 idempotency_key 時，以 namespace 目錄下的 idempotency.json
記錄「key -> 第一次寫入的 episode_id」，同一 key 重送時回傳原 id、不再寫入。

設計重點：
  - 本類別不自行加鎖；呼叫端須在 storage_lock（與實際寫入同一臨界區）內呼叫，
    檢查與登記才不會被並行請求穿插。
  - 每次查詢都讀檔（檔案小），跨 engine 實例／跨程序皆以磁碟為準。
  - 容量上限與保留天數於登記時淘汰；insertion order = 由舊到新。
  - 損壞檔：只對解析錯誤備份後視為空；OSError 重試後拋出，不當空檔覆寫。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from echo_memory.storage.atomic import atomic_write_json

logger = logging.getLogger("echo_memory.idempotency")

INDEX_FILENAME = "idempotency.json"
INDEX_VERSION = 1
READ_RETRIES = 3
READ_RETRY_DELAY = 0.05


class IdempotencyIndex:
    """key -> episode_id 的持久化索引（呼叫端須持有 storage_lock）。"""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_keys: int = 10000,
        retention_days: float = 30.0,
    ) -> None:
        self.path = Path(directory) / INDEX_FILENAME
        self.max_keys = max(1, int(max_keys))
        self.retention_days = float(retention_days)

    # ── 讀取 ────────────────────────────────────────────────────────────
    def _read_text(self) -> str | None:
        """讀檔；檔案不存在回傳 None，OSError 短重試後拋出。"""
        last: OSError | None = None
        for attempt in range(READ_RETRIES):
            try:
                return self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except UnicodeDecodeError:
                raise
            except OSError as exc:
                last = exc
                if attempt < READ_RETRIES - 1:
                    time.sleep(READ_RETRY_DELAY * (2 ** attempt))
        assert last is not None
        raise last

    def _backup_corrupt(self, reason: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}.bak")
        try:
            self.path.replace(backup)
            logger.warning("[Idempotency] 索引損壞（%s），已備份至 %s 並視為空", reason, backup)
        except OSError as exc:
            # 備份失敗不能默默覆寫壞檔，往外拋
            raise OSError(f"冪等索引損壞且無法備份: {exc}") from exc

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            text = self._read_text()
        except UnicodeDecodeError:
            self._backup_corrupt("編碼錯誤")
            return {}
        if text is None:
            return {}
        try:
            data = json.loads(text)
            entries = data["entries"]
            if not isinstance(entries, dict):
                raise TypeError("entries 不是物件")
            for key, item in entries.items():
                if not isinstance(item, dict) or not isinstance(item.get("episode_id"), str):
                    raise TypeError(f"項目格式錯誤: {key!r}")
        except (ValueError, KeyError, TypeError) as exc:
            self._backup_corrupt(f"{type(exc).__name__}: {exc}")
            return {}
        return entries

    # ── 公開介面 ────────────────────────────────────────────────────────
    def get(self, key: str) -> str | None:
        """回傳 key 對應的 episode_id；不存在或已過期回傳 None。"""
        item = self._load().get(key)
        if item is None or self._expired(item):
            return None
        return item["episode_id"]

    def record(self, key: str, episode_id: str) -> None:
        """登記 key -> episode_id（同 key 會被更新並移到最新），並套用過期與容量淘汰。"""
        entries = self._load()
        entries.pop(key, None)
        entries[key] = {
            "episode_id": episode_id,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        entries = {k: v for k, v in entries.items() if not self._expired(v)}
        overflow = len(entries) - self.max_keys
        if overflow > 0:
            for old in list(entries)[:overflow]:
                del entries[old]
        atomic_write_json(self.path, {"version": INDEX_VERSION, "entries": entries}, indent=None)

    def _expired(self, item: dict[str, Any]) -> bool:
        if self.retention_days <= 0:
            return False
        try:
            at = datetime.fromisoformat(item["at"])
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            return True
        return datetime.now(timezone.utc) - at > timedelta(days=self.retention_days)
