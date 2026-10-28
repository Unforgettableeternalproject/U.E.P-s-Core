"""
Echo Memory MCP Server 主入口

使用 MCP Python SDK 將 Echo Memory 四層記憶系統包裝為標準 MCP Server，
讓 Claude Desktop 和其他 MCP 客戶端能夠透過工具和資源存取記憶功能。

啟動方式：
    python -m mcp_server
    或
    echo-mcp  (安裝後)

通訊方式：標準 stdio（MCP 協議）
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

# ─── 設定 sys.path，讓 echo_memory 和 llm_integration 可被 import ─────────────
# 將專案根目錄加入 Python 路徑（mcp_server/ 的上一層）
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 載入 .env（必須在 echo_memory import 之前）
# .env 固定放在專案根目錄
_ENV_FILE = _PROJECT_ROOT / ".env"
try:
    from dotenv import load_dotenv
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE)
        logging.getLogger(__name__).debug(f"[MCP Server] 已載入 .env: {_ENV_FILE}")
    else:
        # Worktree 情境：沿目錄樹向上尋找 .env
        _search = _PROJECT_ROOT.parent
        for _ in range(5):
            _candidate = _search / ".env"
            if _candidate.exists():
                load_dotenv(_candidate)
                logging.getLogger(__name__).debug(
                    f"[MCP Server] Worktree fallback .env: {_candidate}"
                )
                break
            _search = _search.parent
except ImportError:
    pass  # python-dotenv 未安裝時優雅跳過

# ─── 導入 MCP SDK ─────────────────────────────────────────────────────────────
from mcp.server import Server
from mcp.server.stdio import stdio_server

# ─── 導入 Echo Memory ─────────────────────────────────────────────────────────
from echo_memory import MemoryEngine
from echo_memory.config import get_config
from echo_memory.namespace import NamespaceEngineRegistry
from echo_memory.schemas.sharing import is_scope_namespace_id
from mcp_server.access_policy import check_namespace_access, policy_from_config

# ─── 導入 handlers 和 resources ───────────────────────────────────────────────
from mcp_server.handlers import register_tools
from mcp_server.resources import register_resources

# ─── 設定 logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,  # MCP Server 預設靜默，避免污染 stdio 通訊
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stderr,      # 永遠輸出到 stderr，保留 stdout 給 MCP 協議
)
logger = logging.getLogger("echo_memory.mcp")

# ─── 全域 Server 實例（handlers 和 resources 需要共享）────────────────────────
server = Server("echo-memory")

# ─── 每個 namespace 一個 MemoryEngine（延遲初始化）───────────────────────────


_scheduled_engines: list[MemoryEngine] = []  # 已啟動 Dream 排程的 engine，結束時統一停止
_schedule_requested: set[int] = set()  # 已排入啟動的 engine（id），確保每個 engine 只啟動一次
_schedule_lock = threading.Lock()


def _start_dream_scheduler_safe(engine: MemoryEngine, namespace_id: str) -> None:
    """啟動 Dream 排程；任何失敗只記 log，不影響記憶讀寫。"""
    try:
        result = engine.start_dream_scheduler()
        if result["started"]:
            with _schedule_lock:
                _scheduled_engines.append(engine)
        else:
            logger.info(f"[MCP Server] Dream 排程未啟動 ({namespace_id}): {result['reason']}")
    except Exception as exc:
        logger.warning(f"[MCP Server] Dream 排程啟動失敗 ({namespace_id}): {exc}")


def _ensure_dream_scheduler(engine: MemoryEngine, namespace_id: str) -> None:
    """
    在 registry 鎖之外以背景執行緒啟動 Dream 排程（每個 engine 只啟動一次）。

    start_dream_scheduler 會 ensure_initialized（載入 embedding），不能放進
    NamespaceEngineRegistry 的全域鎖，否則會卡住其他 namespace。
    場域共享 namespace 對 MCP 唯讀（寫入被存取策略拒絕），不啟動會改寫圖譜的 Dream 排程。
    """
    if is_scope_namespace_id(namespace_id):
        return
    with _schedule_lock:
        if id(engine) in _schedule_requested:
            return
        _schedule_requested.add(id(engine))
    try:
        threading.Thread(
            target=_start_dream_scheduler_safe, args=(engine, namespace_id),
            name=f"dream-scheduler-{namespace_id}", daemon=True,
        ).start()
    except Exception as exc:
        logger.warning(f"[MCP Server] Dream 排程執行緒建立失敗 ({namespace_id}): {exc}")


def _create_engine(namespace_id: str) -> MemoryEngine:
    config = get_config()
    logger.info(
        f"[MCP Server] 初始化 MemoryEngine - namespace={namespace_id}, "
        f"存儲根路徑: {config.memory_storage_path}"
    )
    engine = MemoryEngine(
        storage_path=config.memory_storage_path,
        embedding_model=config.embedding_model,
        device=config.embedding_device,
        query_prefix=config.embedding_query_prefix,
        max_episodes=config.memory_max_episodes,
        namespace_id=namespace_id,
        embedding_backend=config.embedding_backend,
        ollama_base_url=config.ollama_base_url,
    )
    return engine


_engines = NamespaceEngineRegistry(_create_engine)


def get_engine(namespace_id: str | None = None) -> MemoryEngine:
    """
    取得全域 MemoryEngine 實例（Singleton）。

    首次呼叫時初始化，後續呼叫直接返回已有實例。
    讀取 config 中的 memory_storage_path 和 embedding_model。
    namespace 一律先過存取策略（與 handlers 同一套；handlers 另依工具是否寫入把關），
    資源（resources）等不經 handlers 的入口也不能繞過。
    """
    namespace_id = check_namespace_access(namespace_id, policy_from_config())
    engine = _engines.get(namespace_id)
    _ensure_dream_scheduler(engine, namespace_id)
    return engine


def main() -> None:
    """
    MCP Server 主函式。

    初始化 MemoryEngine，註冊所有工具和資源，
    然後透過 stdio 啟動 MCP 服務。
    """
    import asyncio

    logger.info("[MCP Server] 啟動 Echo Memory MCP Server...")

    # 註冊所有工具和資源
    register_tools(server, get_engine)
    register_resources(server, get_engine)

    logger.info("[MCP Server] 工具和資源已註冊，等待連接...")

    # 啟動 stdio 服務
    async def _run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    try:
        asyncio.run(_run())
    finally:
        for scheduled in _scheduled_engines:
            try:
                scheduled.stop_dream_scheduler()
            except Exception as exc:
                logger.warning(f"[MCP Server] 停止 Dream 排程失敗: {exc}")


if __name__ == "__main__":
    main()
