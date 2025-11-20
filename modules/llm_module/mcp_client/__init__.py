# modules/llm_module/mcp_client/__init__.py
"""
MCP Client 管理模組

負責 MCP 工具獲取、管理和配置相關邏輯。
與 SYS 模組的 mcp_server 對應。
"""

from .mcp_manager import MCPManager
from .mcp_client import MCPClient

__all__ = ['MCPManager', 'MCPClient']