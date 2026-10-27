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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from uep.config import get_settings

_FILE_NAME = "host_epoch.json"
_LOCK_SUFFIX = ".lock"
_LOCK_TIMEOUT = 30.0


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


def _acquire_file_lock(handle, timeout: float) -> None:
    """取得 handle 的獨佔鎖；以短輪詢取代無限阻塞，逾時拋 HostEpochError。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise HostEpochError("等待 epoch 檔鎖逾時") from exc
            time.sleep(0.01)


def _release_file_lock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # 關閉 handle 時作業系統也會釋放


def _fsync_dir(directory: Path) -> None:
    """替換後 fsync 目錄使改名落盤；Windows 無法開啟目錄，略過。"""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class HostEpochStore:
    """以原子替換寫檔；同進程以執行緒鎖、跨行程以檔案鎖（``<epoch 檔>.lock``）序列化讀改寫。

    檔案不存在視為 0（首次啟動 bump 後為 1）。寫入先 fsync 暫存檔再替換，可行時再 fsync 目錄。"""

    _lock = threading.Lock()

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else default_epoch_path()

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """同進程執行緒鎖＋跨行程檔案鎖（Windows msvcrt、其他 fcntl）；逾時視為無法寫入。"""
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(str(self._path) + _LOCK_SUFFIX, "a+b")  # noqa: SIM115
            except OSError as exc:
                raise HostEpochError(f"epoch 鎖檔無法開啟：{self._path.name}") from exc
            try:
                _acquire_file_lock(handle, _LOCK_TIMEOUT)
                try:
                    yield
                finally:
                    _release_file_lock(handle)
            finally:
                handle.close()

    def read(self) -> HostEpochs:
        with self._exclusive():
            return self._read()

    def bump(self, *, fencing: bool = True, core: bool = True) -> HostEpochs:
        """讀、+1、原子寫回並回傳新值。"""
        with self._exclusive():
            current = self._read()
            updated = HostEpochs(
                current.fencing_epoch + (1 if fencing else 0),
                current.core_epoch + (1 if core else 0),
            )
            self._write(updated)
            return updated

    def raise_to(self, *, fencing_at_least: int = 0, core_at_least: int = 0) -> HostEpochs:
        """把檔內值抬到不低於給定值（檔案遺失但 DB 仍在時，避免回退到已核發過的值）。"""
        with self._exclusive():
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
            _fsync_dir(self._path.parent)
        except OSError as exc:
            raise HostEpochError(f"epoch 檔無法寫入：{self._path.name}") from exc
