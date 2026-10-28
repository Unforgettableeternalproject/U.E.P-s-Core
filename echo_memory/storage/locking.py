from __future__ import annotations

import json
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
def storage_lock(
    storage_path: str | Path,
    timeout: float = 30.0,
    lock_name: str = ".echo-memory.lock",
) -> Iterator[None]:
    storage = Path(storage_path)
    storage.mkdir(parents=True, exist_ok=True)
    lock_path = storage / lock_name
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


class FileLease:
    """
    跨行程租約：以獨立檔案鎖保護短臨界區，狀態寫在 JSON 檔（持有者 pid、取得時間、
    最後完成時間）。租約有過期時間，持有者崩潰後逾期即可被其他行程接手。

    不阻塞：取不到租約立即回報原因，由呼叫端決定跳過。
    """

    def __init__(self, directory: str | Path, name: str = "dream", ttl_seconds: float = 3600.0) -> None:
        self._dir = Path(directory)
        self._name = name
        self._path = self._dir / f".{name}-lease.json"
        self._lock_name = f".{name}-lease.lock"
        self.ttl_seconds = float(ttl_seconds)
        self.token = f"{os.getpid()}:{uuid.uuid4().hex}"

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        tmp = temporary_sibling(self._path)
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def _guard(self):
        return storage_lock(self._dir, timeout=5.0, lock_name=self._lock_name)

    def read_state(self) -> dict:
        """讀取目前租約狀態（供診斷與測試）。"""
        with self._guard():
            return self._read()

    def try_acquire(
        self,
        owner: str = "",
        *,
        skip_if_completed_within: float | None = None,
        completion_key: str = "last_completed_at",
        now: float | None = None,
    ) -> tuple[bool, str]:
        """
        嘗試取得租約。回傳 (是否取得, 原因)；原因在取得時為空字串。

        skip_if_completed_within: 指定時，若 completion_key 的最後完成時間距今不到該秒數，
            視為同一窗口已有人完成而不取得（cron 窗口去重）。
        """
        now = time.time() if now is None else now
        try:
            with self._guard():
                state = self._read()
                holder = state.get("holder_token")
                expires_at = float(state.get("expires_at") or 0.0)
                if holder and holder != self.token and expires_at > now:
                    return False, (
                        f"租約由 pid={state.get('holder_pid')} 持有"
                        f"（約 {expires_at - now:.0f} 秒後過期）"
                    )
                if skip_if_completed_within is not None:
                    done = float(state.get(completion_key) or 0.0)
                    if done and now - done < skip_if_completed_within:
                        return False, f"同一窗口內已於 {now - done:.0f} 秒前完成（{completion_key}）"
                state.update(
                    holder_token=self.token,
                    holder_pid=os.getpid(),
                    holder_owner=owner,
                    acquired_at=now,
                    expires_at=now + self.ttl_seconds,
                )
                self._write(state)
                return True, ""
        except TimeoutError:
            return False, "租約檢查逾時"
        except OSError as exc:
            return False, f"租約檔無法存取: {exc}"

    def renew(self, *, now: float | None = None) -> bool:
        """
        續租：本持有者仍持有時把 expires_at 延到 now + ttl，回傳 True；
        已非持有者（租約逾期被接手或被清除）回傳 False；租約檔無法存取視為失租。
        """
        now = time.time() if now is None else now
        try:
            with self._guard():
                state = self._read()
                if state.get("holder_token") != self.token:
                    return False
                state["expires_at"] = now + self.ttl_seconds
                self._write(state)
                return True
        except (TimeoutError, OSError):
            return False

    def release(self, *, completed: bool = False, completion_keys: tuple[str, ...] = ("last_completed_at",),
                now: float | None = None) -> None:
        """釋放租約；completed=True 時一併記錄完成時間。非本持有者呼叫不動作。"""
        now = time.time() if now is None else now
        with self._guard():
            state = self._read()
            if state.get("holder_token") != self.token:
                return
            for key in ("holder_token", "holder_pid", "holder_owner", "acquired_at", "expires_at"):
                state.pop(key, None)
            if completed:
                for key in completion_keys:
                    state[key] = now
            self._write(state)
