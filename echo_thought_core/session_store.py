"""
SessionStore — Session 持久化管理

JSON 檔案式的 session 儲存，支援：
- 儲存/載入完整 session（訊息 + 狀態）
- 列出所有歷史 session
- 刪除 session
- 跨 CLI 重啟還原

儲存路徑：{data_dir}/sessions/
  ├── index.json              # session 索引（metadata）
  └── {session_id}.json       # 各 session 的完整訊息

Usage:
    store = SessionStore(data_dir="data")
    store.save(session_id, messages, metadata)
    messages, metadata = store.load(session_id)
    sessions = store.list_sessions()
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SessionStore:
    """JSON 檔案式 session 持久化"""

    def __init__(self, data_dir: str | Path = "data"):
        self._dir = Path(data_dir) / "sessions"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "index.json"
        self._index: dict[str, dict[str, Any]] = self._load_index()

    # ── 公開 API ─────────────────────────────────────────────────────────

    def save(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """儲存 session 到檔案。

        Args:
            session_id: Session ID
            messages:   訊息列表（dict 格式，含 role/content/token_count 等）
            metadata:   額外元資料（token 總數、壓縮次數等）
        """
        meta = metadata or {}

        # 寫 session 資料檔
        session_path = self._dir / f"{session_id}.json"
        session_data = {
            "session_id": session_id,
            "messages": messages,
            "metadata": meta,
            "saved_at": time.time(),
        }

        with open(session_path, "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)

        # 更新索引
        self._index[session_id] = {
            "session_id": session_id,
            "message_count": len(messages),
            "total_tokens": meta.get("total_tokens", 0),
            "compression_count": meta.get("compression_count", 0),
            "created_at": meta.get("created_at", time.time()),
            "saved_at": time.time(),
            "model": meta.get("model", ""),
            "backend": meta.get("backend", ""),
        }
        self._save_index()

        logger.info(
            "[SessionStore] 儲存 session %s (%d msgs)",
            session_id, len(messages),
        )

    def load(self, session_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """載入 session。

        Args:
            session_id: Session ID

        Returns:
            (messages, metadata) 元組

        Raises:
            FileNotFoundError: session 不存在
        """
        session_path = self._dir / f"{session_id}.json"
        if not session_path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")

        with open(session_path, encoding="utf-8") as f:
            data = json.load(f)

        logger.info(
            "[SessionStore] 載入 session %s (%d msgs)",
            session_id, len(data.get("messages", [])),
        )

        return data.get("messages", []), data.get("metadata", {})

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有儲存的 session。

        Returns:
            session metadata 列表，按 saved_at 降序排列（最新的在前）
        """
        sessions = list(self._index.values())
        sessions.sort(key=lambda s: s.get("saved_at", 0), reverse=True)
        return sessions

    def delete(self, session_id: str) -> bool:
        """刪除 session。

        Args:
            session_id: Session ID

        Returns:
            是否成功刪除
        """
        session_path = self._dir / f"{session_id}.json"
        if session_path.exists():
            session_path.unlink()

        if session_id in self._index:
            del self._index[session_id]
            self._save_index()
            logger.info("[SessionStore] 刪除 session %s", session_id)
            return True

        return False

    def exists(self, session_id: str) -> bool:
        """檢查 session 是否存在"""
        return session_id in self._index

    def find(self, keyword: str) -> list[dict[str, Any]]:
        """模糊搜尋 session（前綴或子字串匹配）。

        Args:
            keyword: 搜尋關鍵字

        Returns:
            匹配的 session metadata 列表
        """
        keyword_lower = keyword.lower()
        matches = []
        for sid, meta in self._index.items():
            if keyword_lower in sid.lower():
                matches.append(meta)
        return matches

    # ── 內部方法 ─────────────────────────────────────────────────────────

    def _load_index(self) -> dict[str, dict[str, Any]]:
        """載入索引檔"""
        if self._index_path.exists():
            try:
                with open(self._index_path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("[SessionStore] 索引損壞，重建: %s", e)
        return {}

    def _save_index(self) -> None:
        """儲存索引檔"""
        with open(self._index_path, "w", encoding="utf-8") as f:
            json.dump(self._index, f, ensure_ascii=False, indent=2)
