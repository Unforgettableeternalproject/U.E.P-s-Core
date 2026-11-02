"""
PrivacyLedger - 使用者隱私覆寫帳本（M10）

位於擁有者 namespace 根目錄的 ``privacy_ledger.json``：
``{"version": 1, "units": {unit_id: {restricted_at, by, patch_id, reason_code}}}``。

  - 只記 unit id 與中繼資料，不含任何內容文字
  - 只能收緊：restrict 加入；沒有「放寬」API。unrestrict 僅供 Gate 的 restrict rollback
    （人工、經 audit）使用，且只移除指定 patch 寫入的項目
  - 原子寫入（atomic_write_json）；讀取以 (mtime_ns, size) 簽章快取
  - fail-closed：檔案損毀時 is_restricted 對所有 unit 回傳 True，且拒絕寫入
    （寫入會覆蓋損毀檔而丟失既有限制）
  - 鎖：行程內 per-path RLock ＋ 跨行程檔案鎖（同目錄 ``.privacy_ledger.lock``，沿用
    locking.py 的 _try_lock／_unlock），read-modify-write 全程持有，避免多行程互相蓋掉
    收緊。刻意不用 namespace 的 ``.echo-memory.lock``：兩者皆為葉節點鎖（持有期間不取
    任何其他鎖），可安全在 SemanticMemory._lock 內呼叫，不與 engine 的 namespace 鎖
    產生鎖序問題
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.storage.atomic import atomic_write_json
from echo_memory.storage.locking import _try_lock, _unlock

logger = logging.getLogger("echo_memory.storage.privacy_ledger")

LEDGER_FILENAME = "privacy_ledger.json"
LEDGER_VERSION = 1
LOCK_FILENAME = ".privacy_ledger.lock"
LOCK_TIMEOUT_SECONDS = 30.0

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


class PrivacyLedgerError(RuntimeError):
    """帳本無法安全寫入（損毀）。"""


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class PrivacyLedger:
    """擁有者 namespace 的隱私覆寫帳本。"""

    def __init__(self, storage_path: str | Path):
        """
        Args:
            storage_path: 擁有者 namespace 根目錄（帳本檔放在其下）。
        """
        self._path = Path(storage_path) / LEDGER_FILENAME
        self._lock = _path_lock(self._path)
        self._lock_path = self._path.parent / LOCK_FILENAME
        self._lock_depth = 0
        self._units: dict[str, dict[str, Any]] = {}
        self._signature: tuple[int, int] | None | str = "unloaded"
        self._corrupt = False

    @property
    def path(self) -> Path:
        return self._path

    # ── 讀取 ────────────────────────────────────────────────────

    def _stat_signature(self) -> tuple[int, int] | None:
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return (-1, -1)
        return (st.st_mtime_ns, st.st_size)

    def _refresh(self) -> None:
        """依檔案簽章重新載入（呼叫端須持 self._lock）。"""
        sig = self._stat_signature()
        if sig == self._signature:
            return
        self._signature = sig
        self._units = {}
        self._corrupt = False
        if sig is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            units = data.get("units") if isinstance(data, dict) else None
            if not isinstance(units, dict):
                raise ValueError("units 欄位缺失或型別錯誤")
            parsed: dict[str, dict[str, Any]] = {}
            for uid, meta in units.items():
                parsed[str(uid)] = dict(meta) if isinstance(meta, dict) else {}
            self._units = parsed
        except Exception as e:  # 損毀／無法讀取：fail-closed
            self._corrupt = True
            logger.error("[PrivacyLedger] 帳本損毀，視為全部受限: %s", e)

    @property
    def is_corrupt(self) -> bool:
        with self._lock:
            self._refresh()
            return self._corrupt

    def is_restricted(self, unit_id: str) -> bool:
        """該 unit 是否被使用者收緊。帳本損毀時一律 True（fail-closed）。"""
        with self._lock:
            self._refresh()
            if self._corrupt:
                return True
            return str(unit_id) in self._units

    def get(self, unit_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._refresh()
            meta = self._units.get(str(unit_id))
            return dict(meta) if meta is not None else None

    def restricted_ids(self) -> set[str]:
        with self._lock:
            self._refresh()
            return set(self._units)

    def __len__(self) -> int:
        with self._lock:
            self._refresh()
            return len(self._units)

    @contextmanager
    def _write_guard(self) -> Iterator[None]:
        """寫入臨界區：行程內 RLock ＋ 跨行程檔案鎖，取得後強制重讀磁碟。"""
        with self._lock:
            if self._lock_depth > 0:  # 同執行緒重入
                self._lock_depth += 1
                try:
                    yield
                finally:
                    self._lock_depth -= 1
                return
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lock_path, "a+b") as handle:
                # 不寫入任何內容：Windows 的 msvcrt 鎖住第 0 位元組後，其他行程對該區段寫入
                # 會 PermissionError；鎖定 EOF 之外的位元組在 Windows／POSIX 都合法
                deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
                while not _try_lock(handle):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for ledger lock: {self._lock_path}")
                    time.sleep(0.05)
                self._lock_depth = 1
                try:
                    self._signature = "unloaded"  # 其他行程可能剛寫過：強制重讀
                    yield
                finally:
                    self._lock_depth = 0
                    _unlock(handle)

    # ── 寫入（只收緊）────────────────────────────────────────────

    def _flush(self) -> None:
        atomic_write_json(self._path, {"version": LEDGER_VERSION, "units": self._units})
        self._signature = self._stat_signature()

    def restrict_many(
        self,
        unit_ids: Iterable[str],
        *,
        by: str = "user",
        patch_id: str = "",
        reason_code: str = "",
    ) -> list[str]:
        """把 unit 記入帳本（冪等）。回傳「本次新加入」的 unit id（已存在者不重記）。

        帳本損毀時拋 PrivacyLedgerError（不覆蓋損毀檔）。
        """
        ids: list[str] = []
        for uid in unit_ids:
            if not isinstance(uid, str) or not uid:
                raise ValueError(f"unit_id 必須是非空字串: {uid!r}")
            if uid not in ids:
                ids.append(uid)
        with self._write_guard():
            self._refresh()
            if self._corrupt:
                raise PrivacyLedgerError("privacy ledger 損毀，拒絕寫入（避免丟失既有限制）")
            now = datetime.now(timezone.utc).isoformat()
            added: list[str] = []
            for uid in ids:
                if uid in self._units:
                    continue
                self._units[uid] = {
                    "restricted_at": now,
                    "by": by,
                    "patch_id": patch_id,
                    "reason_code": reason_code,
                }
                added.append(uid)
            if added:
                try:
                    self._flush()
                except BaseException:
                    for uid in added:
                        self._units.pop(uid, None)
                    self._signature = "unloaded"  # 強制下次重讀磁碟
                    raise
            return added

    def restrict(
        self,
        unit_id: str,
        *,
        by: str = "user",
        patch_id: str = "",
        reason_code: str = "",
    ) -> bool:
        """把單一 unit 記入帳本（冪等）。回傳是否為本次新加入。"""
        return bool(self.restrict_many(
            [unit_id], by=by, patch_id=patch_id, reason_code=reason_code,
        ))

    def unrestrict(self, unit_ids: Iterable[str], *, patch_id: str) -> list[str]:
        """僅供 Gate 的 restrict rollback：移除「由指定 patch 寫入」的項目。

        patch_id 必填且必須相符，其他來源（使用者先前的覆寫）不會被移除。
        帳本損毀時拋 PrivacyLedgerError。回傳實際移除的 unit id。
        """
        if not patch_id:
            raise ValueError("unrestrict 需要 patch_id")
        with self._write_guard():
            self._refresh()
            if self._corrupt:
                raise PrivacyLedgerError("privacy ledger 損毀，拒絕寫入")
            removed = [
                uid for uid in dict.fromkeys(unit_ids)
                if uid in self._units and self._units[uid].get("patch_id") == patch_id
            ]
            if not removed:
                return []
            backup = {uid: self._units[uid] for uid in removed}
            for uid in removed:
                del self._units[uid]
            try:
                self._flush()
            except BaseException:
                self._units.update(backup)
                self._signature = "unloaded"
                raise
            return removed
