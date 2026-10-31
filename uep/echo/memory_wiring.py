"""記憶端真實接線（P3 S6）：真 ``MemoryEngine`` 的 ``EngineFactory``、每個 engine 唯一的 ``EchoMemoryAdapter``、
``MemoryToolBackend`` 實作。

這是 uep 內**唯一**接觸 ``echo_memory`` 與 ES ``EchoMemoryAdapter`` 的地方（import 邊界測試只放行 ``uep/echo/``）；
其餘程式只認 ``uep/memory/protocol.py`` 的 Protocol。所有 import 都是延遲的：沒安裝記憶端的環境仍可 import 本模組。

與 ES ``build_engine`` 的差異（刻意）：
- 事件開關用建構參數 ``event_emitter_enabled_fn``，不靠 ``SELF_MODEL_EVENTS_ENABLED`` 環境變數。
- 不用 ``os.environ.setdefault`` 隱式關排程：做夢排程由 ``MemoryHost`` 以 ``configure_dream`` 明確關閉。
- 資料根目錄、裝置與 embedding 設定由呼叫端（``UepSettings``）明確給，不讀 ES 的 ``.env``。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("uep.echo.memory_wiring")

MEMORY_TOOLS: tuple[str, ...] = ("deep_recall", "force_remember")
DEFAULT_TOOL_MAX_CHARS = 1500


class EchoEngineFactory:
    """``MemoryHost(engine_factory=...)``：建真 ``MemoryEngine``。``storage_path`` 傳資料根，engine 自己解成
    ``<root>/namespaces/<ns>``（Host 會核對 ``engine.storage_path``）。"""

    def __init__(
        self,
        *,
        device: str = "cpu",
        embedding_backend: str | None = None,
        embedding_model: str | None = None,
        events_enabled_fn: Callable[[], bool] | None = None,
    ) -> None:
        self._device = device
        self._embedding_backend = embedding_backend
        self._embedding_model = embedding_model
        self._events_enabled_fn = events_enabled_fn or (lambda: True)

    def __call__(self, namespace_id: str, data_root: Path) -> Any:
        from echo_memory import MemoryEngine

        kwargs: dict[str, Any] = {
            "storage_path": Path(data_root), "namespace_id": namespace_id, "device": self._device,
            "event_emitter_enabled_fn": self._events_enabled_fn,
        }
        if self._embedding_backend:
            kwargs["embedding_backend"] = self._embedding_backend
        if self._embedding_model:
            kwargs["embedding_model"] = self._embedding_model
        return MemoryEngine(**kwargs)


def make_adapter_factory(
    *,
    llm_fn: Callable[[str], str] | None = None,
    assess_llm_fn: Callable[[str], str] | None = None,
    assess_mode: str = "llm",
    enabled_tools: tuple[str, ...] = MEMORY_TOOLS,
) -> Callable[[str, Any, threading.Lock], Any]:
    """``MemoryHost(adapter_factory=...)``：每個 engine 建一個 ``EchoMemoryAdapter``（Host 只會呼叫一次）。
    ``engine=`` 一定傳入（否則 adapter 的 ``engine`` 屬性會自己 ``build_engine()``）；``engine_lock`` 用 Host 的鎖。
    ``assess_mode``／``enabled_tools`` 明確設定，不依賴 ES 的環境變數預設。"""

    def factory(namespace_id: str, engine: Any, engine_lock: threading.Lock) -> Any:
        from echo_stream.adapters.memory_echo import ASSESS_MODES, EchoMemoryAdapter

        if assess_mode not in ASSESS_MODES:
            raise ValueError(f"assess_mode 必須是 {ASSESS_MODES}：{assess_mode!r}")
        adapter = EchoMemoryAdapter(engine, llm_fn=llm_fn, assess_llm_fn=assess_llm_fn, engine_lock=engine_lock)
        adapter.assess_mode = assess_mode
        adapter.enabled_tools = list(enabled_tools)
        return adapter

    return factory


class EchoMemoryToolBackend:
    """``MemoryToolBackend`` 的真實作：宣告取自 echo_memory 的 ``TOOL_DECLARATIONS``（只留 ``deep_recall``／
    ``force_remember``），``force_remember`` 的描述沿用 ES 收斂過的版本。

    有 adapter 時 Access Layer 只用 ``definitions()``，執行走 ``adapter.execute_tool``（寫入唯一經 adapter）；
    ``execute`` 只給沒有 adapter 的最小組裝（直接用 echo_memory 的 ``ToolExecutor``）。"""

    def __init__(self, *, names: tuple[str, ...] = MEMORY_TOOLS, max_chars: int = DEFAULT_TOOL_MAX_CHARS) -> None:
        self._names = names
        self._max_chars = max_chars

    def definitions(self) -> list[dict[str, Any]]:
        from echo_stream.adapters.memory_echo import FORCE_REMEMBER_DESCRIPTION
        from llm_integration.tools.definitions import TOOL_DECLARATIONS

        out: list[dict[str, Any]] = []
        for decl in TOOL_DECLARATIONS:
            if decl["name"] not in self._names:
                continue
            fn = dict(decl)
            if fn["name"] == "force_remember":
                fn["description"] = FORCE_REMEMBER_DESCRIPTION
            out.append({"type": "function", "function": fn})
        return out

    def execute(self, engine: Any, name: str, args: Mapping[str, Any], session_id: str) -> dict[str, Any]:
        if name not in self._names:
            return {"success": False, "result": f"Tool {name!r} is not available."}
        from llm_integration.tools.executor import ToolExecutor

        outcome = dict(ToolExecutor(engine, session_id=session_id).execute(name, dict(args)))
        text = str(outcome.get("result", ""))
        if len(text) > self._max_chars:
            outcome["result"] = text[: self._max_chars] + "…"
        return outcome


@dataclass(frozen=True)
class MemoryWiring:
    """``MemoryHost`` 的真實接線參數組：``MemoryHost(**wiring.host_kwargs(), data_root=..., namespaces=..., ...)``。"""

    engine_factory: Callable[[str, Path], Any]
    adapter_factory: Callable[[str, Any, threading.Lock], Any]
    tool_backend: EchoMemoryToolBackend

    def host_kwargs(self) -> dict[str, Any]:
        return {
            "engine_factory": self.engine_factory, "adapter_factory": self.adapter_factory,
            "tool_backend": self.tool_backend,
        }


def build_memory_wiring(
    *,
    llm_fn: Callable[[str], str] | None = None,
    assess_llm_fn: Callable[[str], str] | None = None,
    device: str = "cpu",
    embedding_backend: str | None = None,
    embedding_model: str | None = None,
    events_enabled_fn: Callable[[], bool] | None = None,
    assess_mode: str = "llm",
) -> MemoryWiring:
    return MemoryWiring(
        engine_factory=EchoEngineFactory(
            device=device, embedding_backend=embedding_backend, embedding_model=embedding_model,
            events_enabled_fn=events_enabled_fn,
        ),
        adapter_factory=make_adapter_factory(llm_fn=llm_fn, assess_llm_fn=assess_llm_fn, assess_mode=assess_mode),
        tool_backend=EchoMemoryToolBackend(),
    )
