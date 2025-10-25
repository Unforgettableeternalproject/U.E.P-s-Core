"""
MCP (Model Context Protocol) Server for SYS Module

提供基於 JSON-RPC 的輕量級 MCP 實現，用於 LLM-SYS 雙向溝通。
支援工作流控制工具，實現互動式工作流審核機制。
"""

from .mcp_server import MCPServer
from .tool_definitions import MCPTool, ToolParameter, ToolResult
from .protocol_handlers import MCPRequest, MCPResponse, MCPError
from .resource_providers import WorkflowResourceProvider

__all__ = [
    "MCPServer",
    "MCPTool",
    "ToolParameter",
    "ToolResult",
    "MCPRequest",
    "MCPResponse",
    "MCPError",
    "WorkflowResourceProvider",
]
