"""Host 本機 epoch 檔（D26、P-4、P-5）。

fencing_epoch 與 core_epoch 存在 DB 之外的 Host 本機檔，還原舊 DB 時檔案不會跟著回退，
因此新值必然大於任何曾核發過的值。每次 Core 啟動與 DB 還原後由 ``bump`` 強制 +1。
路徑由設定（``UEP_HOST_EPOCH_FILE``）或建構參數給定，預設在使用者資料目錄，不硬編碼。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from uep.config import get_settings

_FILE_NAME = "host_epoch.json"


class HostEpochError(RuntimeError):
    """epoch 檔損毀或無法寫入；此時不得啟動（輸出維持全關）。"""


@dataclass(frozen=True)
class HostEpochs:
    fencing_epoch: int
    core_epoch: int


def default_epoch_path() -> Path:
    """設定優先；否則使用者資料目錄（Windows 為 LOCALAPPDATA，其餘為 XDG／~/.local/share）。"""
    configured = get_settings().host_epoch_file
    if configured is not None:
        return Path(configured)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "UEP" / _FILE_NAME


class HostEpochStore:
    """以原子替換寫檔；同進程內以鎖序列化。檔案不存在視為 0（首次啟動 bump 後為 1）。"""

    _lock = threading.Lock()

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else default_epoch_path()

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> HostEpochs:
        with self._lock:
            return self._read()

    def bump(self, *, fencing: bool = True, core: bool = True) -> HostEpochs:
        """讀、+1、原子寫回並回傳新值。"""
        with self._lock:
            current = self._read()
            updated = HostEpochs(
                current.fencing_epoch + (1 if fencing else 0),
                current.core_epoch + (1 if core else 0),
            )
            self._write(updated)
            return updated

    def raise_to(self, *, fencing_at_least: int = 0, core_at_least: int = 0) -> HostEpochs:
        """把檔內值抬到不低於給定值（檔案遺失但 DB 仍在時，避免回退到已核發過的值）。"""
        with self._lock:
            current = self._read()
            updated = HostEpochs(
                max(current.fencing_epoch, fencing_at_least), max(current.core_epoch, core_at_least)
            )
            if updated != current:
                self._write(updated)
            return updated

    def _read(self) -> HostEpochs:
        if not self._path.exists():
            return HostEpochs(0, 0)
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return HostEpochs(int(data["fencing_epoch"]), int(data["core_epoch"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HostEpochError(f"epoch 檔無法讀取或格式不合法：{self._path.name}") from exc

    def _write(self, epochs: HostEpochs) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".epoch-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"fencing_epoch": epochs.fencing_epoch, "core_epoch": epochs.core_epoch}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            raise HostEpochError(f"epoch 檔無法寫入：{self._path.name}") from exc
