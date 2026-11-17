"""行程記憶體追蹤：背景取樣器＋載入階段標記，寫入逐行 flush 的獨立檔 ``logs/mem-<pid>.log``。

用途：診斷 Windows commit（已認可記憶體）耗盡造成的原生 access violation。原生崩潰時 Python logging 來不及 flush，
所以每一行都立即 ``flush``（行程死掉後 OS 仍會把已寫入的內容落盤），並且不經 logging 的佇列。

用法::

    UEP_MEM_TRACE=1            # 開啟（預設關；scripts/console.bat 預設帶上）
    UEP_MEM_TRACE_INTERVAL_S   # 取樣間隔，預設 0.25 秒

進入點呼叫 :func:`start_from_settings`；載入重的元件用 :func:`phase` 或 :func:`traced` 標記開始／結束。
沒啟動時所有標記都是 no-op，不影響熱路徑。取樣只在「變動超過門檻」或「每 N 秒」時輸出一行，避免洗版。
"""
from __future__ import annotations

import atexit
import contextlib
import ctypes
import functools
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_GB = 1024 ** 3
_PRIVATE_DELTA_BYTES = 256 * 1024 ** 2  # private 變動 ≥256MB 就輸出一行
_COMMIT_DELTA_BYTES = 512 * 1024 ** 2  # 系統 commit 變動 ≥512MB 就輸出一行
_HEARTBEAT_S = 5.0  # 沒有大變動時，至少每 5 秒輸出一行

F = TypeVar("F", bound=Callable[..., Any])


class _PerfInfo(ctypes.Structure):
    """Win32 PERFORMANCE_INFORMATION（``GetPerformanceInfo``）。"""

    _fields_ = [
        ("cb", ctypes.c_ulong), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t), ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t), ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t), ("HandleCount", ctypes.c_ulong),
        ("ProcessCount", ctypes.c_ulong), ("ThreadCount", ctypes.c_ulong),
    ]


def system_commit() -> tuple[int, int, int] | None:
    """系統層 (CommitTotal, CommitLimit, CommitPeak)，單位 bytes；非 Windows 或呼叫失敗回傳 None。"""
    if sys.platform != "win32":
        return None
    try:
        info = _PerfInfo()
        info.cb = ctypes.sizeof(info)
        if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):  # type: ignore[attr-defined]
            return None
        page = int(info.PageSize)
        return int(info.CommitTotal) * page, int(info.CommitLimit) * page, int(info.CommitPeak) * page
    except Exception:  # noqa: BLE001 - 診斷工具不可影響主流程
        return None


class _Snap:
    """單次取樣結果（bytes）；取不到的欄位為 0。"""

    __slots__ = ("commit", "cuda_alloc", "cuda_reserved", "limit", "private", "rss", "sys_peak", "threads")

    def __init__(self) -> None:
        self.private = self.rss = self.commit = self.limit = self.sys_peak = 0
        self.cuda_alloc = self.cuda_reserved = 0
        self.threads = 0


class _Phase:
    __slots__ = ("commit0", "name", "peak_commit", "peak_private", "private0", "t0")

    def __init__(self, name: str, snap: _Snap) -> None:
        self.name = name
        self.t0 = time.perf_counter()
        self.private0 = snap.private
        self.commit0 = snap.commit
        self.peak_private = snap.private
        self.peak_commit = snap.commit


def _gb(value: int) -> str:
    return f"{value / _GB:.2f}"


def _signed_gb(value: int) -> str:
    return f"{value / _GB:+.2f}"


class MemTrace:
    """背景取樣器；每行即時 flush。"""

    def __init__(self, path: Path, interval_s: float = 0.25) -> None:
        self._path = path
        self._interval = max(0.05, float(interval_s))
        self._lock = threading.Lock()
        self._phases: dict[str, _Phase] = {}
        self._loads: dict[str, int] = {}  # 模型識別 -> 載入次數（偵測重複載入）
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._file: Any = None
        self._proc: Any = None
        self._peak_private = 0
        self._peak_commit = 0
        self._last_private = 0
        self._last_commit = 0
        self._last_line_at = 0.0

    # ---- 生命週期 ----

    def start(self) -> None:
        import psutil

        self._proc = psutil.Process()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - 與取樣緒同壽命
        snap = self._snapshot()
        self._write("START", f"pid={os.getpid()} interval={self._interval:.2f}s", snap, force=True)
        self._thread = threading.Thread(target=self._run, name="uep-mem-trace", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if self._file is not None:
            with contextlib.suppress(Exception):
                self._write("STOP", f"peak_private={_gb(self._peak_private)}GB peak_commit={_gb(self._peak_commit)}GB", self._snapshot(), force=True)
                self._file.close()
            self._file = None

    # ---- 取樣 ----

    def _snapshot(self) -> _Snap:
        snap = _Snap()
        try:
            info = self._proc.memory_info()
            snap.rss = int(info.rss)
            snap.private = int(getattr(info, "private", 0) or getattr(info, "pagefile", 0) or 0)  # Windows 的 private bytes
            snap.threads = int(self._proc.num_threads())
        except Exception:  # noqa: BLE001, S110 - 取不到就留 0
            pass
        commit = system_commit()
        if commit is not None:
            snap.commit, snap.limit, snap.sys_peak = commit
        torch = sys.modules.get("torch")  # 只在 torch 已載入時才碰，避免追蹤器自己把 torch 匯入進來
        if torch is not None:
            try:
                cuda = torch.cuda
                if cuda.is_available() and cuda.is_initialized():
                    snap.cuda_alloc = int(cuda.memory_allocated())
                    snap.cuda_reserved = int(cuda.memory_reserved())
            except Exception:  # noqa: BLE001, S110 - CUDA 狀態取不到就略過
                pass
        return snap

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                snap = self._snapshot()
                with self._lock:
                    self._track(snap)
                    now = time.monotonic()
                    big = (
                        abs(snap.private - self._last_private) >= _PRIVATE_DELTA_BYTES
                        or abs(snap.commit - self._last_commit) >= _COMMIT_DELTA_BYTES
                    )
                    if big or now - self._last_line_at >= _HEARTBEAT_S:
                        self._write_locked("SAMPLE", "", snap)
            except Exception:  # noqa: BLE001, S112 - 取樣失敗不得讓緒結束；下一輪再試
                continue

    def _track(self, snap: _Snap) -> None:
        self._peak_private = max(self._peak_private, snap.private)
        self._peak_commit = max(self._peak_commit, snap.commit)
        for ph in self._phases.values():
            ph.peak_private = max(ph.peak_private, snap.private)
            ph.peak_commit = max(ph.peak_commit, snap.commit)

    # ---- 輸出 ----

    def _fmt(self, kind: str, note: str, snap: _Snap) -> str:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        pct = f"({snap.commit * 100 // snap.limit}%)" if snap.limit else ""
        parts = [
            f"{stamp} {kind:<6}",
            f"priv={_gb(snap.private)}GB rss={_gb(snap.rss)}GB",
            f"commit={_gb(snap.commit)}/{_gb(snap.limit)}GB{pct}",
            f"peak(priv={_gb(self._peak_private)} commit={_gb(self._peak_commit)} sys={_gb(snap.sys_peak)})GB",
        ]
        if snap.cuda_reserved or snap.cuda_alloc:
            parts.append(f"cuda(alloc={_gb(snap.cuda_alloc)} reserved={_gb(snap.cuda_reserved)})GB")
        parts.append(f"thr={snap.threads}")
        if note:
            parts.append(f"| {note}")
        return " ".join(parts)

    def _write(self, kind: str, note: str, snap: _Snap, *, force: bool = False) -> None:
        with self._lock:
            self._track(snap)
            self._write_locked(kind, note, snap)

    def _write_locked(self, kind: str, note: str, snap: _Snap) -> None:
        if self._file is None:
            return
        line = self._fmt(kind, note, snap)
        self._file.write(line + "\n")
        self._file.flush()  # 原生崩潰前最後一行也要在檔案裡
        self._last_private = snap.private
        self._last_commit = snap.commit
        self._last_line_at = time.monotonic()

    # ---- 階段標記 ----

    def begin(self, name: str, key: str | None = None) -> None:
        """標記階段開始；``key`` 為「模型識別」（如 ``laya:路徑``），同一 key 在行程內第 2 次以上出現會多印一行 WARN（重複載入）。"""
        snap = self._snapshot()
        with self._lock:
            self._track(snap)
            self._phases[name] = _Phase(name, snap)
            others = ",".join(sorted(k for k in self._phases if k != name)) or "-"
            self._write_locked("BEGIN", f"{name}（同時進行：{others}）", snap)
            if key:
                self._loads[key] = self._loads.get(key, 0) + 1
                if self._loads[key] > 1:
                    self._write_locked("WARN", f"重複載入：{key} 第 {self._loads[key]} 次（實例 {name}）", snap)

    def end(self, name: str, status: str = "ok") -> None:
        snap = self._snapshot()
        with self._lock:
            self._track(snap)
            ph = self._phases.pop(name, None)
            if ph is None:
                self._write_locked("END", f"{name}（未標記開始）", snap)
                return
            note = (
                f"{name} {status} dur={time.perf_counter() - ph.t0:.1f}s "
                f"Δpriv={_signed_gb(snap.private - ph.private0)}GB Δcommit={_signed_gb(snap.commit - ph.commit0)}GB "
                f"階段內峰值(priv={_gb(ph.peak_private)} commit={_gb(ph.peak_commit)})GB"
            )
            self._write_locked("END", note, snap)

    def mark(self, text: str) -> None:
        snap = self._snapshot()
        with self._lock:
            self._track(snap)
            self._write_locked("MARK", text, snap)


_tracer: MemTrace | None = None
_tracer_lock = threading.Lock()


def active() -> MemTrace | None:
    return _tracer


def start(log_dir: Path, interval_s: float = 0.25) -> MemTrace | None:
    """啟動追蹤（冪等）；失敗只記 warning 並回傳 None，不影響主流程。"""
    global _tracer
    with _tracer_lock:
        if _tracer is not None:
            return _tracer
        path = Path(log_dir) / f"mem-{datetime.now():%Y%m%d-%H%M%S}-pid{os.getpid()}.log"
        try:
            tracer = MemTrace(path, interval_s)
            tracer.start()
        except Exception as exc:  # noqa: BLE001 - 診斷工具失敗不得擋住啟動
            logger.warning("記憶體追蹤啟動失敗：%s", exc)
            return None
        _tracer = tracer
        atexit.register(stop)  # 正常結束時補寫 STOP 行與峰值
        logger.info("記憶體追蹤已啟動：%s", path)
        return tracer


def start_from_settings(settings: Any = None) -> MemTrace | None:
    """依 ``UEP_MEM_TRACE``／``UEP_MEM_TRACE_INTERVAL_S`` 啟動；未開啟回傳 None。log 目錄與系統日誌同一個。"""
    if settings is None:
        from uep.config import get_settings

        settings = get_settings()
    if not bool(getattr(settings, "mem_trace", False)):
        return None
    log_dir = getattr(settings, "log_dir", None) or Path(__file__).resolve().parents[1] / "logs"
    return start(Path(log_dir), float(getattr(settings, "mem_trace_interval_s", 0.25) or 0.25))


def stop() -> None:
    global _tracer
    with _tracer_lock:
        tracer, _tracer = _tracer, None
    if tracer is not None:
        tracer.stop()


def mark(text: str) -> None:
    tracer = _tracer
    if tracer is not None:
        tracer.mark(text)


def instance_tag(obj: object) -> str:
    """物件實例 id 的短標記（同一個物件載入兩次會看到同 id，兩個物件各載一次會看到不同 id）。"""
    return f"#{id(obj) & 0xFFFFFF:06x}"


@contextlib.contextmanager
def phase(name: str, key: str | None = None) -> Iterator[None]:
    """標記一個載入／暖機階段；沒啟動追蹤時是 no-op。``key`` 是模型識別，用來偵測同一模型被重複載入。"""
    tracer = _tracer
    if tracer is None:
        yield
        return
    tracer.begin(name, key)
    status = "ok"
    try:
        yield
    except BaseException:
        status = "失敗"
        raise
    finally:
        tracer.end(name, status)


def traced(name: str | Callable[[Any], str], key: Callable[[Any], str] | None = None) -> Callable[[F], F]:
    """方法裝飾器：整個呼叫期間視為一個階段（用在背景載入的 ``_load``）；階段名自動帶實例 id，``key(self)`` 給模型識別。"""

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                model_key = key(self) if key is not None and _tracer is not None else None
            except Exception:  # noqa: BLE001 - 取識別失敗就不做重複偵測
                model_key = None
            label = name(self) if callable(name) else name
            with phase(f"{label}{instance_tag(self)}", model_key):
                return fn(self, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return deco

