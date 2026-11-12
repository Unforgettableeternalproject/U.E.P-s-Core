"""``LoopContextStore`` 的持久實作：child／parent 寫入存進 echo_thought_core 的 ``SessionStore``（JSON 檔）。

``uep/`` 其他模組不得 import 子系統，所以這個接線放在 ``*_wiring.py``。取代行程內的 ``InMemoryLoopContextStore``，
park 後行程重啟仍可依 checkpoint 的 ``child_context_ref`` 取回 child（SYS 契約 §17.2）。

- child：session ``child__<ref>``；訊息以 ``messages_to_json`` 的正規化字串存放（與 ``history_hash`` 同一份表示），
  revision 與 manifest 放 metadata。檔案毀損或讀不到一律回 None，由 ``load_verified`` fail-closed 為 ``ContextLost``。
- parent 寫入：session ``parent__<parent_id>``，以冪等鍵去重；鍵含 run_id，``discard_run`` 依此清理。
- 同一行程內以鎖序列化；不支援多行程同時寫同一目錄。
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from echo_thought_core.session_store import SessionStore

from uep.agent.loop_context import ForkManifest, MergeBack
from uep.models.port import ChatMessage, messages_from_json, messages_to_json

logger = logging.getLogger(__name__)

_CHILD = "child__"
_PARENT = "parent__"
_UNSAFE = re.compile(r"[^0-9A-Za-z_.\-]")


def _sid(prefix: str, ref: str) -> str:
    """ref 含 ``:`` 等檔名不合法字元（例如 ``loop:<run_id>``），統一換成 ``_``。"""
    return prefix + _UNSAFE.sub("_", ref)


class SessionLoopContextStore:
    """``LoopContextStore`` 的 SessionStore 實作（``data_dir`` 下的 ``sessions/``，建議用專用目錄避免與 CLI session 混放）。"""

    def __init__(self, data_dir: str | Path) -> None:
        self._store = SessionStore(data_dir)
        self._lock = threading.Lock()

    # ---- child ----

    def create_child(self, ref: str, messages: Sequence[ChatMessage], manifest: ForkManifest) -> int:
        with self._lock:
            self._write_child(ref, messages, 1, asdict(manifest))
            return 1

    def save_child(self, ref: str, messages: Sequence[ChatMessage]) -> int:
        with self._lock:
            prior = self._read(_sid(_CHILD, ref))
            meta = prior[1] if prior else {}
            revision = int(meta.get("revision", 0)) + 1
            self._write_child(ref, messages, revision, meta.get("manifest"))
            return revision

    def load_child(self, ref: str) -> tuple[list[ChatMessage], int] | None:
        with self._lock:
            loaded = self._read(_sid(_CHILD, ref))
        if loaded is None:
            return None
        rows, meta = loaded
        try:
            return messages_from_json(rows[0]["messages_json"]), int(meta.get("revision", 0))
        except (KeyError, IndexError, TypeError, ValueError):
            logger.error("child context 內容毀損：ref=%s", ref)  # 只記引用，不記內容
            return None

    def _write_child(self, ref: str, messages: Sequence[ChatMessage], revision: int, manifest: Any) -> None:
        self._store.save(
            _sid(_CHILD, ref), [{"messages_json": messages_to_json(messages)}],
            {"revision": revision, "manifest": manifest, "ref": ref},
        )

    # ---- parent ----

    def merge_into_parent(self, key: str, parent_id: str, payload: MergeBack) -> bool:
        return self._append_parent(key, parent_id, {"key": key, "kind": "return", "payload": asdict(payload)})

    def mark_parent_status(self, key: str, parent_id: str, state: str, reason_code: str | None) -> bool:
        return self._append_parent(
            key, parent_id, {"key": key, "kind": "status", "state": state, "reason_code": reason_code},
        )

    def parent_entries(self, parent_id: str) -> list[dict[str, Any]]:
        """parent 的寫入條目（診斷與測試用）。"""
        with self._lock:
            loaded = self._read(_sid(_PARENT, parent_id))
        return list(loaded[0]) if loaded else []

    def _append_parent(self, key: str, parent_id: str, entry: dict[str, Any]) -> bool:
        sid = _sid(_PARENT, parent_id)
        with self._lock:
            loaded = self._read(sid)
            rows = list(loaded[0]) if loaded else []
            if any(r.get("key") == key for r in rows):
                return False  # 冪等：同鍵已寫過
            rows.append(entry)
            self._store.save(sid, rows, {"parent_id": parent_id})
            return True

    # ---- 清理 ----

    def discard_run(self, run_id: str, parent_id: str | None = None) -> None:
        """Run 終態：刪該 Run 的 child 與 parent 條目（鍵含 run_id）。park 中的 Run 不會被呼叫。"""
        with self._lock:
            for meta in self._store.list_sessions():
                sid = meta["session_id"]
                if sid.startswith(_CHILD) and run_id in sid:
                    self._store.delete(sid)
                elif sid.startswith(_PARENT) and (parent_id is None or sid == _sid(_PARENT, parent_id)):
                    loaded = self._read(sid)
                    if loaded is None:
                        continue
                    rows = [r for r in loaded[0] if run_id not in str(r.get("key", ""))]
                    if len(rows) == len(loaded[0]):
                        continue
                    if rows:
                        self._store.save(sid, rows, loaded[1])
                    else:
                        self._store.delete(sid)

    def _read(self, sid: str) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
        try:
            return self._store.load(sid)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.error("session 檔毀損，視為遺失：id=%s", sid)
            return None
