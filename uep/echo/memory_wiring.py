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

import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from uep.stream.contracts.memory_tools import DEFAULT_TOOL_MAX_CHARS

logger = logging.getLogger("uep.echo.memory_wiring")

MEMORY_TOOLS: tuple[str, ...] = ("deep_recall", "force_remember")
DEFAULT_ENGINE_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
"""``MemoryEngine`` 的 embedding_model 參數預設（呼叫端沒給模型時實際生效的值）。"""
KNOWN_EMBEDDING_DIMS: dict[str, int] = {
    "BAAI/bge-m3": 1024, "bge-m3": 1024, "BAAI/bge-small-zh-v1.5": 512, "all-MiniLM-L6-v2": 384,
}


class EmbeddingIndexMismatch(RuntimeError):
    """資料目錄的向量索引與目前設定的 embedding 模型不符（換模型必須用全新資料目錄）。"""


def check_embedding_index(storage_dir: Path, model: str | None) -> None:
    """建 engine 前核對既有索引的模型／維度；不符就拒絕，避免 echo_memory 悄悄重建（改寫舊資料）或混用。

    空目錄、沒有索引的目錄一律放行。依 ``embedding_index.json``（model、dimension）；沒有 manifest 的舊版快照
    改看 ``embeddings.npy`` 的維度。模型未知（不在 ``KNOWN_EMBEDDING_DIMS``）且 manifest 的 model 相同時放行。
    """
    want = model or DEFAULT_ENGINE_EMBEDDING_MODEL
    manifest = Path(storage_dir) / "embedding_index.json"
    matrix = Path(storage_dir) / "embeddings.npy"
    have_model: str | None = None
    have_dim: int | None = None
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        have_model = data.get("model") if isinstance(data, dict) else None
        dim = data.get("dimension") if isinstance(data, dict) else None
        have_dim = dim if isinstance(dim, int) else None
    elif matrix.exists():
        import numpy as np

        try:
            arr = np.load(matrix, mmap_mode="r")
            have_dim = int(arr.shape[1]) if arr.ndim == 2 else None
        except (OSError, ValueError):
            have_dim = None
    else:
        return
    want_dim = KNOWN_EMBEDDING_DIMS.get(want)
    if (have_model is not None and have_model != want) or (
        have_dim is not None and want_dim is not None and have_dim != want_dim
    ):
        raise EmbeddingIndexMismatch(
            f"資料目錄 {storage_dir} 的向量索引（model={have_model!r}, dimension={have_dim}）與目前設定的 embedding "
            f"（model={want!r}, dimension={want_dim}）不符。換模型請改用全新的 UEP_MEMORY_DATA_ROOT；舊目錄不會被重建或改寫。"
        )


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
        min_similarity: float | None = None,
    ) -> None:
        self._min_similarity = min_similarity
        self._device = device
        self._embedding_backend = embedding_backend
        self._embedding_model = embedding_model
        self._events_enabled_fn = events_enabled_fn or (lambda: True)

    def __call__(self, namespace_id: str, data_root: Path) -> Any:
        from echo_memory import MemoryEngine
        from echo_memory.namespace import resolve_namespace_path

        check_embedding_index(resolve_namespace_path(Path(data_root), namespace_id), self._embedding_model)
        if self._min_similarity is not None:
            # EM 的檢索下限只認 EM 全域設定（retrieve 未帶 min_similarity 時讀它），Access Layer 不逐次傳
            from echo_memory.config import get_config

            get_config().retrieval_min_similarity = float(self._min_similarity)
        kwargs: dict[str, Any] = {
            "storage_path": Path(data_root), "namespace_id": namespace_id, "device": self._device,
            "event_emitter_enabled_fn": self._events_enabled_fn,
        }
        if self._embedding_backend:
            kwargs["embedding_backend"] = self._embedding_backend
        if self._embedding_model:
            kwargs["embedding_model"] = self._embedding_model
        return MemoryEngine(**kwargs)


def warmup_embedding(model: str, device: str) -> None:
    """宿主啟動時預載行程共用的 sentence-transformers 模型（EM 的模組級 ``warmup``），之後所有 engine 共用同一份。"""
    from echo_memory.storage.episodic import warmup

    warmup(model, device)


class QueryEmbedder:
    """量測用：與記憶檢索同一條 query embedding 路徑（``EpisodicMemory.generate_query_embedding``），不碰任何記憶資料
    （storage 指向呼叫端給的暫存目錄）。給 console ``/mem embed``。"""

    def __init__(self, storage_dir: Path, *, backend: str, model: str, device: str) -> None:
        from echo_memory.storage.episodic import EpisodicMemory

        self._mem = EpisodicMemory(
            storage_path=Path(storage_dir), embedding_model=model, device=device, embedding_backend=backend,
        )

    def embed(self, text: str) -> list[float]:
        return self._mem.generate_query_embedding(text)


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
        from uep.stream.adapters.memory_echo import ASSESS_MODES, EchoMemoryAdapter

        if assess_mode not in ASSESS_MODES:
            raise ValueError(f"assess_mode 必須是 {ASSESS_MODES}：{assess_mode!r}")
        adapter = EchoMemoryAdapter(engine, llm_fn=llm_fn, assess_llm_fn=assess_llm_fn, engine_lock=engine_lock)
        adapter.assess_mode = assess_mode
        adapter.enabled_tools = list(enabled_tools)
        return adapter

    return factory


class EchoMemoryToolBackend:
    """``MemoryToolBackend`` 的真實作：宣告取自 echo_memory 的 ``TOOL_DECLARATIONS``（只留 ``deep_recall``／
    ``force_remember``），``force_remember`` 的描述沿用 ES 收斂過的版本（與 adapter 共用 ``function_tool_definition``）。

    有 adapter 時 Access Layer 只用 ``definitions()``，執行走 ``adapter.execute_tool``（寫入唯一經 adapter）；
    ``execute`` 只給沒有 adapter 的最小組裝（直接用 echo_memory 的 ``ToolExecutor``）。"""

    def __init__(self, *, names: tuple[str, ...] = MEMORY_TOOLS, max_chars: int = DEFAULT_TOOL_MAX_CHARS) -> None:
        self._names = names
        self._max_chars = max_chars

    def definitions(self) -> list[dict[str, Any]]:
        from uep.stream.adapters.memory_echo import function_tool_definition
        from llm_integration.tools.definitions import TOOL_DECLARATIONS

        out: list[dict[str, Any]] = []
        for decl in TOOL_DECLARATIONS:
            if decl["name"] not in self._names:
                continue
            out.append(function_tool_definition(decl))
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
    min_similarity: float | None = None,
) -> MemoryWiring:
    return MemoryWiring(
        engine_factory=EchoEngineFactory(
            device=device, embedding_backend=embedding_backend, embedding_model=embedding_model,
            events_enabled_fn=events_enabled_fn, min_similarity=min_similarity,
        ),
        adapter_factory=make_adapter_factory(llm_fn=llm_fn, assess_llm_fn=assess_llm_fn, assess_mode=assess_mode),
        tool_backend=EchoMemoryToolBackend(),
    )
