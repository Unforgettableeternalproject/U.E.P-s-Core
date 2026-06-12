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
        # 也嘗試從主專案路徑載入（worktree 情境）
        _MAIN_ENV = Path(
            "C:/Users/Bernie/source/repos/Unforgettableeternalproject/"
            "TestSeperateMemorySystem/.env"
        )
        if _MAIN_ENV.exists():
            load_dotenv(_MAIN_ENV)
except ImportError:
    pass  # python-dotenv 未安裝時優雅跳過

# ─── 導入 MCP SDK ─────────────────────────────────────────────────────────────
from mcp.server import Server
from mcp.server.stdio import stdio_server

# ─── 導入 Echo Memory ─────────────────────────────────────────────────────────
from echo_memory import MemoryEngine
from echo_memory.config import get_config
from echo_memory.namespace import NamespaceEngineRegistry

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


def _create_engine(namespace_id: str) -> MemoryEngine:
    config = get_config()
    logger.info(
        f"[MCP Server] 初始化 MemoryEngine - namespace={namespace_id}, "
        f"存儲根路徑: {config.memory_storage_path}"
    )
    return MemoryEngine(
        storage_path=config.memory_storage_path,
        embedding_model=config.embedding_model,
        device=config.embedding_device,
        query_prefix=config.embedding_query_prefix,
        max_episodes=config.memory_max_episodes,
        namespace_id=namespace_id,
        embedding_backend=config.embedding_backend,
        ollama_base_url=config.ollama_base_url,
    )


_engines = NamespaceEngineRegistry(_create_engine)


def get_engine(namespace_id: str | None = None) -> MemoryEngine:
    """
    取得全域 MemoryEngine 實例（Singleton）。

    首次呼叫時初始化，後續呼叫直接返回已有實例。
    讀取 config 中的 memory_storage_path 和 embedding_model。
    """
    if namespace_id is None:
        namespace_id = get_config().memory_namespace_id
    return _engines.get(namespace_id)


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

    asyncio.run(_run())


if __name__ == "__main__":
    main()
