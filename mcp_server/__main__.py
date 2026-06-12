"""
Echo Memory MCP Server 啟動入口

讓 `python -m mcp_server` 能直接執行 MCP Server。

使用方式：
    python -m mcp_server
"""

from mcp_server.server import main

if __name__ == "__main__":
    main()
