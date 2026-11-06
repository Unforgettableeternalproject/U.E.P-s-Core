"""Core 系統日誌（stdlib logging）：集中設定、自動標註位置、分層等級、脈絡欄位與遮罩。

用法（呼叫端不必改）::

    from uep.log import get_logger
    logger = get_logger()            # 省略 name 時取呼叫端模組的 __name__
    logger.key("重要里程碑")          # KEY（25）
    logger.trace("逐步細節")          # TRACE（5）

既有的 ``logging.getLogger(__name__)`` 照樣可用：位置標註由 handler 端的 formatter 負責，與 logger 怎麼取得無關。

只在行程進入點呼叫 :func:`configure_logging`（console／endpoint／stop 的 ``__main__``）；組裝層與 library 路徑不呼叫，
測試也不會因此留下 root handler。環境變數見 ``UepSettings`` 的 ``log_*`` 欄位與 Console 使用指南。
"""
from __future__ import annotations

import atexit
import contextlib
import contextvars
import copy
import json
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]

# ---- 等級 ----

KEY = 25
"""重要里程碑：介於 INFO 與 WARNING。"""
TRACE = 5
"""比 DEBUG 更細的追蹤。"""
logging.addLevelName(KEY, "KEY")
logging.addLevelName(TRACE, "TRACE")

DEPTH_LEVELS: dict[int, int] = {1: KEY, 2: logging.INFO, 3: logging.DEBUG, 4: TRACE}
"""legacy 四級除錯深度（KEY／OPERATION／SYSTEM／ELABORATIVE）對應的 stdlib 門檻。"""

_LEVEL_ALIASES: dict[str, int] = {
    "TRACE": TRACE, "ELABORATIVE": TRACE,
    "DEBUG": logging.DEBUG, "SYSTEM": logging.DEBUG,
    "INFO": logging.INFO, "OPERATION": logging.INFO,
    "KEY": KEY,
    "WARN": logging.WARNING, "WARNING": logging.WARNING,
    "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL, "FATAL": logging.CRITICAL,
}

# 子系統頂層套件名（與 tests/uep/test_import_boundary.py 的 SUBSYSTEM_NAMES 一致）；子系統不 import uep，這裡只靠名稱辨識
_SUBSYSTEM_NAMES = frozenset({"echo_memory", "echo_stt", "echo_tts", "echo_thought_core", "llm_integration", "mcp_server"})

# 第三方噪音：預設壓到 WARNING，可被 UEP_LOG_LEVELS 覆寫
NOISY_NAMESPACES: tuple[str, ...] = (
    "httpx", "httpcore", "urllib3", "openai", "anthropic", "torch", "transformers", "sentence_transformers",
    "huggingface_hub", "filelock", "fsspec", "matplotlib", "PIL", "numba", "asyncio", "multipart",
    "sqlalchemy.engine", "sqlalchemy.pool",
)

_MARKER = "_uep_log_handler"
_LOG_SELF = "uep.log"


def parse_level(value: str | int | None) -> int | None:
    """等級名稱（含 KEY／TRACE 與 legacy 別名）或整數轉成數值；無法辨識回傳 None。"""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().upper()
    if not text:
        return None
    if text in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[text]
    if text.lstrip("-").isdigit():
        return int(text)
    return None


def parse_levels(raw: str | None) -> tuple[dict[str, int], list[str]]:
    """解析 ``"uep.decision=DEBUG,echo_tts=WARNING"``；回傳（有效覆寫, 格式錯誤的項目）。不拋錯。"""
    valid: dict[str, int] = {}
    bad: list[str] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, level_text = item.partition("=")
        name = name.strip()
        level = parse_level(level_text) if sep else None
        if not name or level is None:
            bad.append(item)
            continue
        valid[name] = level
    return valid, bad


def depth_to_level(depth: int) -> int | None:
    """``UEP_LOG_DEPTH`` 1..4 對應門檻；超出範圍回傳 None。"""
    return DEPTH_LEVELS.get(depth)


# ---- 位置與子系統標註 ----

_REL_CACHE: dict[str, str | None] = {}


def _relative_path(pathname: str) -> str | None:
    """LogRecord.pathname → repo 相對路徑（POSIX 斜線）；不在 repo 內回傳 None。結果以 dict 快取。"""
    try:
        return _REL_CACHE[pathname]
    except KeyError:
        pass
    rel: str | None
    try:
        rel = Path(pathname).resolve().relative_to(_REPO_ROOT).as_posix()
        if rel.startswith((".venv/", "venv/", "env/")) or "/site-packages/" in rel:
            rel = None
    except (ValueError, OSError):
        rel = None
    _REL_CACHE[pathname] = rel
    return rel


def derive_subsystem(name: str, rel_path: str | None = None) -> str:
    """logger 名稱（必要時加檔案位置）→ 子系統標籤：``uep.<pkg>`` → ``core.<pkg>``；子系統用自己的名稱；其餘用第三方名稱。"""
    top, _, rest = name.partition(".")
    if top == "uep":
        pkg = rest.partition(".")[0]
        return f"core.{pkg}" if pkg else "core"
    if top in _SUBSYSTEM_NAMES:
        return top
    if rel_path:
        parts = rel_path.split("/")
        if parts[0] == "subsystems" and len(parts) > 1:
            return parts[1]
        if parts[0] == "uep" and top in ("__main__", "__mp_main__"):
            return f"core.{parts[1].removesuffix('.py')}" if len(parts) > 1 else "core"
    return top or "root"


def _annotate(record: logging.LogRecord) -> None:
    """為 record 補上 ``where``、``subsystem``（只算一次）。"""
    if hasattr(record, "where"):
        return
    rel = _relative_path(record.pathname)
    record.where = f"{rel}:{record.lineno} {record.funcName}" if rel else record.name
    record.subsystem = derive_subsystem(record.name, rel)


# ---- 脈絡（contextvars）----

_CONTEXT: contextvars.ContextVar[Mapping[str, Any] | None] = contextvars.ContextVar("uep_log_context", default=None)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """在此區塊內，同一個執行緒／asyncio task 發出的日誌自動附上欄位（如 ``run_id``、``turn_id``、``request_id``）。

    可巢狀；值為 None 的欄位略過。新執行緒不繼承（要帶過去請用 ``contextvars.copy_context().run``）。
    """
    merged = {**(_CONTEXT.get() or {}), **{k: v for k, v in fields.items() if v is not None}}
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_CONTEXT.get() or {})


# ---- 遮罩 ----

_REDACT_KV = re.compile(r"""(?i)\b([\w-]*?(?:api[_-]?key|token|secret|password))\b(\s*=\s*|["']\s*:\s*)(["']?)[^\s,;&"')\]}]+""")
_REDACT_BEARER = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{4,}")


def redact(text: str) -> str:
    """把 ``api_key=``／``token=``／``secret=``／``password=`` 的值與 ``Bearer xxx`` 遮成 ``***``（保守，只認這些形式）。"""
    text = _REDACT_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}***", text)
    return _REDACT_BEARER.sub(r"\1 ***", text)


class _CaptureFilter(logging.Filter):
    """在發出日誌的執行緒上：補位置、注入 contextvars、遮罩訊息。掛在 handler 上，才涵蓋所有 logger 的 record。"""

    def filter(self, record: logging.LogRecord) -> bool:
        _annotate(record)
        if not hasattr(record, "uep_ctx"):
            record.uep_ctx = dict(_CONTEXT.get() or {})
            message = record.getMessage()
            cleaned = redact(message)
            if cleaned != message:
                record.msg, record.args = cleaned, ()
        return True


# ---- Formatter ----

class UepFormatter(logging.Formatter):
    """``時間 等級 [子系統] 位置 | 訊息 [context]``；位置與脈絡由 record 的標註欄位提供。"""

    def __init__(self, *, with_date: bool) -> None:
        super().__init__(datefmt="%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")
        self._msecs = with_date

    def format(self, record: logging.LogRecord) -> str:
        _annotate(record)
        ctx = getattr(record, "uep_ctx", None) or {}
        ctx_text = (" [" + " ".join(f"{k}={v}" for k, v in ctx.items()) + "]") if ctx else ""
        stamp = self.formatTime(record, self.datefmt)
        if self._msecs:
            stamp = f"{stamp}.{int(record.msecs):03d}"
        text = f"{stamp} {record.levelname:<7} [{record.subsystem}] {record.where} | {record.getMessage()}{ctx_text}"
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            text = f"{text}\n{record.exc_text}"
        if record.stack_info:
            text = f"{text}\n{self.formatStack(record.stack_info)}"
        return text


class JsonFormatter(logging.Formatter):
    """JSONL：``ts``、``level``、``subsystem``、``logger``、``where``、``msg``、``context``（有例外時加 ``exc``）。"""

    def format(self, record: logging.LogRecord) -> str:
        _annotate(record)
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "subsystem": record.subsystem,
            "logger": record.name,
            "where": record.where,
            "msg": record.getMessage(),
            "context": dict(getattr(record, "uep_ctx", None) or {}),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


# ---- 便利 logger ----

class UepLogger(logging.LoggerAdapter):
    """薄包裝：多 ``key()``、``trace()``；其餘方法與標準 logger 相同。位置標註仍指向呼叫端。"""

    def __init__(self, logger: logging.Logger) -> None:
        super().__init__(logger, {})

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        return msg, kwargs

    def key(self, msg: object, *args: object, **kwargs: Any) -> None:
        kwargs.setdefault("stacklevel", 2)
        self.log(KEY, msg, *args, **kwargs)

    def trace(self, msg: object, *args: object, **kwargs: Any) -> None:
        kwargs.setdefault("stacklevel", 2)
        self.log(TRACE, msg, *args, **kwargs)


def get_logger(name: str | None = None) -> UepLogger:
    """取得 logger；省略 ``name`` 時用呼叫端模組的 ``__name__``。"""
    if name is None:
        name = sys._getframe(1).f_globals.get("__name__", "root")
    return UepLogger(logging.getLogger(name))


# ---- 設定 ----

class _QueueHandler(logging.handlers.QueueHandler):
    """入列時只先把訊息格式化成字串，保留 exc_info 與標註欄位給監聽端的 formatter。"""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        prepared = copy.copy(record)
        prepared.msg, prepared.args = record.getMessage(), ()
        return prepared


_state_lock = threading.RLock()
_listener: logging.handlers.QueueListener | None = None
_applied_namespaces: set[str] = set()
_atexit_registered = False
_PROCESS_STAMP = time.strftime("%Y%m%d-%H%M%S")


def _mark(handler: logging.Handler) -> logging.Handler:
    setattr(handler, _MARKER, True)
    return handler


def _is_ours(handler: logging.Handler) -> bool:
    return bool(getattr(handler, _MARKER, False))


def _teardown_locked() -> None:
    global _listener
    root = logging.getLogger()
    for handler in [h for h in root.handlers if _is_ours(h)]:
        root.removeHandler(handler)
        with contextlib.suppress(Exception):  # 收尾不因單一 handler 失敗而中斷
            handler.close()
    if _listener is not None:
        listener, _listener = _listener, None
        try:
            listener.stop()
        finally:
            for handler in listener.handlers:
                with contextlib.suppress(Exception):
                    handler.close()
    for ns in list(_applied_namespaces):
        logging.getLogger(ns).setLevel(logging.NOTSET)
    _applied_namespaces.clear()


def shutdown_logging() -> None:
    """拆掉 ``configure_logging`` 掛的 handler 與監聽緒、還原命名空間等級（atexit 自動呼叫；測試收尾用）。"""
    with _state_lock:
        _teardown_locked()


def _setting(settings: Any, name: str, default: Any) -> Any:
    value = getattr(settings, name, None)
    return default if value is None else value


def _file_handlers(log_dir: Path, *, want_json: bool, level: int) -> list[logging.Handler]:
    """每個行程一組檔案（檔名含啟動時間與 pid），避免 Windows 多行程同檔 rollover 鎖死。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    base = f"uep-{_PROCESS_STAMP}-pid{os.getpid()}"
    handlers: list[logging.Handler] = []
    text = logging.FileHandler(log_dir / f"{base}.log", encoding="utf-8", delay=True)
    text.setLevel(level)
    text.setFormatter(UepFormatter(with_date=True))
    handlers.append(text)
    error = logging.FileHandler(log_dir / f"{base}.error.log", encoding="utf-8", delay=True)
    error.setLevel(logging.ERROR)
    error.setFormatter(UepFormatter(with_date=True))
    handlers.append(error)
    if want_json:
        jsonl = logging.FileHandler(log_dir / f"{base}.jsonl", encoding="utf-8", delay=True)
        jsonl.setLevel(level)
        jsonl.setFormatter(JsonFormatter())
        handlers.append(jsonl)
    return handlers


def configure_logging(settings: Any = None) -> None:
    """依設定掛上 console（stderr）與檔案 handler；冪等（重複呼叫會以新設定重建，不重複掛）。

    ``settings`` 省略時取 ``get_settings()``；也可傳任何帶 ``log_*`` 屬性的物件。只在行程進入點呼叫。
    """
    global _listener, _atexit_registered
    if settings is None:
        from uep.config import get_settings

        settings = get_settings()

    warnings: list[str] = []

    # root 門檻：UEP_LOG_LEVEL 優先，其次 UEP_LOG_DEPTH，預設 INFO（OPERATION）
    root_level = logging.INFO
    level_raw = _setting(settings, "log_level", None)
    depth_raw = _setting(settings, "log_depth", None)
    if level_raw is not None and str(level_raw).strip():
        parsed = parse_level(level_raw)
        if parsed is None:
            warnings.append(f"UEP_LOG_LEVEL={level_raw!r} 無法辨識，已忽略")
        else:
            root_level = parsed
    elif depth_raw is not None and str(depth_raw).strip():
        try:
            depth_level = depth_to_level(int(str(depth_raw).strip()))
        except ValueError:
            depth_level = None
        if depth_level is None:
            warnings.append(f"UEP_LOG_DEPTH={depth_raw!r} 需為 1..4，已忽略")
        else:
            root_level = depth_level

    overrides, bad_items = parse_levels(_setting(settings, "log_levels", None))
    if bad_items:
        warnings.append("UEP_LOG_LEVELS 以下項目格式錯誤（需為 名稱=等級），已忽略：" + "；".join(bad_items))

    console_level = parse_level(_setting(settings, "log_console_level", "WARNING"))
    if console_level is None:
        warnings.append(f"UEP_LOG_CONSOLE_LEVEL={_setting(settings, 'log_console_level', None)!r} 無法辨識，改用 WARNING")
        console_level = logging.WARNING

    with _state_lock:
        _teardown_locked()
        root = logging.getLogger()
        root.setLevel(root_level)
        capture = _CaptureFilter()

        console = logging.StreamHandler(sys.stderr)
        console.setLevel(console_level)
        console.setFormatter(UepFormatter(with_date=False))
        console.addFilter(capture)
        root.addHandler(_mark(console))

        if bool(_setting(settings, "log_file", True)):
            log_dir = Path(_setting(settings, "log_dir", _REPO_ROOT / "logs"))
            try:
                targets = _file_handlers(log_dir, want_json=bool(_setting(settings, "log_json", False)), level=root_level)
            except OSError as exc:
                warnings.append(f"無法建立日誌目錄 {log_dir}：{exc}；只輸出到 console")
            else:
                log_queue: queue.SimpleQueue = queue.SimpleQueue()
                queue_handler = _QueueHandler(log_queue)
                queue_handler.addFilter(capture)
                root.addHandler(_mark(queue_handler))
                _listener = logging.handlers.QueueListener(log_queue, *targets, respect_handler_level=True)
                _listener.start()

        # 命名空間等級：先壓第三方噪音，再套使用者覆寫
        applied: dict[str, int] = {ns: logging.WARNING for ns in NOISY_NAMESPACES}
        applied.update(overrides)
        for ns, level in applied.items():
            logging.getLogger(ns).setLevel(level)
        _applied_namespaces.update(applied)

        if not _atexit_registered:
            atexit.register(shutdown_logging)
            _atexit_registered = True

    for text in warnings:
        logging.getLogger(_LOG_SELF).warning(text)


# ---- 執行期調整（Console `/log` 指令用）----

def set_namespace_level(namespace: str, level: str | int) -> int:
    """執行期調整命名空間等級；等級無法辨識拋 ValueError。回傳套用的數值。"""
    parsed = parse_level(level)
    if parsed is None:
        raise ValueError(f"無法辨識的等級：{level!r}")
    logging.getLogger(namespace).setLevel(parsed)
    with _state_lock:
        _applied_namespaces.add(namespace)
    return parsed


def set_console_level(level: str | int) -> int:
    """調整 console handler 門檻（root 門檻較高時仍受其限制）。沒有掛 console handler 時拋 RuntimeError。"""
    parsed = parse_level(level)
    if parsed is None:
        raise ValueError(f"無法辨識的等級：{level!r}")
    for handler in logging.getLogger().handlers:
        if _is_ours(handler) and isinstance(handler, logging.StreamHandler):
            handler.setLevel(parsed)
            if logging.getLogger().level > parsed:
                logging.getLogger().setLevel(parsed)
            return parsed
    raise RuntimeError("尚未呼叫 configure_logging，沒有 console handler")


def describe_levels() -> str:
    """目前 root／console／已覆寫命名空間的等級摘要。"""
    root = logging.getLogger()
    lines = [f"root={logging.getLevelName(root.level)}"]
    for handler in root.handlers:
        if _is_ours(handler) and isinstance(handler, logging.StreamHandler):
            lines.append(f"console={logging.getLevelName(handler.level)}")
    with _state_lock:
        names = sorted(_applied_namespaces)
    lines.extend(f"{ns}={logging.getLevelName(logging.getLogger(ns).level)}" for ns in names)
    return "  ".join(lines)
