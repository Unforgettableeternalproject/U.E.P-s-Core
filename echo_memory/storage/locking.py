from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCK_STATE = threading.local()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _try_lock(handle: BinaryIO) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def storage_lock(storage_path: str | Path, timeout: float = 30.0) -> Iterator[None]:
    storage = Path(storage_path)
    storage.mkdir(parents=True, exist_ok=True)
    lock_path = storage / ".echo-memory.lock"
    lock = _thread_lock(lock_path)
    key = str(lock_path.resolve()).casefold()

    with lock:
        depths = getattr(_LOCK_STATE, "depths", None)
        if depths is None:
            depths = {}
            _LOCK_STATE.depths = depths
        if depths.get(key, 0) > 0:
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return

        with open(lock_path, "a+b") as handle:
            # 不寫入鎖檔：多行程同時建立時，對他人已鎖住的位元組寫入在 Windows 會丟
            # PermissionError（msvcrt 為強制鎖）。鎖 EOF 之外的位元組在 Windows 合法，
            # POSIX flock 與內容無關，故鎖檔保持原狀即可。
            deadline = time.monotonic() + timeout
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for storage lock: {lock_path}")
                time.sleep(0.05)
            try:
                depths[key] = 1
                yield
            finally:
                depths.pop(key, None)
                _unlock(handle)


class ReleasableRLock:
    """
    可重入鎖，且持有者可在做磁碟 I/O 時整段讓出、之後原樣取回（含重入深度）。

    用於「讀者鎖」：寫入端在記憶體變更與快照期間持有，存檔 I/O 前 release_all()，
    讀者不必等磁碟；寫入端彼此的互斥由另一把寫入鎖負責，所以讓出期間不會有第二個寫入者。
    """

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._owner: int | None = None
        self._depth = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        me = threading.get_ident()
        if self._owner == me:
            self._depth += 1
            return True
        if not self._mutex.acquire(blocking, timeout):
            return False
        self._owner = me
        self._depth = 1
        return True

    def release(self) -> None:
        if self._owner != threading.get_ident():
            raise RuntimeError("cannot release un-acquired lock")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._mutex.release()

    def __enter__(self) -> "ReleasableRLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    @contextmanager
    def released(self) -> Iterator[None]:
        """目前執行緒若持有本鎖就整段讓出，離開時取回原深度；未持有則不動作。"""
        if self._owner != threading.get_ident():
            yield
            return
        depth = self._depth
        self._owner = None
        self._depth = 0
        self._mutex.release()
        try:
            yield
        finally:
            self._mutex.acquire()
            self._owner = threading.get_ident()
            self._depth = depth


def temporary_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
