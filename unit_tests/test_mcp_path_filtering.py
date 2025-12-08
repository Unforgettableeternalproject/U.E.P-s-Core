# -*- coding: utf-8 -*-
"""
MCP 路徑過濾單元測試

測試重點：
1. 工具路徑限制定義
2. 路徑過濾機制（get_tools_for_path）
3. 路徑感知的工具規範獲取
4. MCP Client 路徑參數支援
"""

import pytest
from unittest.mock import Mock, patch

from modules.sys_module.mcp_server.mcp_server import MCPServer
from modules.sys_module.mcp_server.tool_definitions import MCPTool, ToolParameter, ToolParameterType
from modules.llm_module.mcp_client import MCPClient, PATH_CHAT, PATH_WORK


@pytest.fixture
def mock_sys_module():
    """建立 Mock SYS 模組"""
    mock = Mock()
    mock.workflow_engines = {}
    return mock


@pytest.fixture
def mcp_server(mock_sys_module):
    """初始化 MCP Server"""
    return MCPServer(sys_module=mock_sys_module)


@pytest.fixture
def mcp_client(mcp_server):
    """初始化 MCP Client"""
    client = MCPClient(mcp_server=mcp_server)
    return client


class TestMCPToolPathAttribute:
    """測試 MCPTool 路徑屬性"""
    
    def test_tool_has_allowed_paths_attribute(self):
        """測試工具有 allowed_paths 屬性"""
        tool = MCPTool(
            name="test_tool",
            description="Test tool",
            allowed_paths=["CHAT"]
        )
        
        assert hasattr(tool, "allowed_paths")
        assert tool.allowed_paths == ["CHAT"]
    
    def test_tool_default_allowed_paths(self):
        """測試工具預設允許所有路徑"""
        tool = MCPTool(
            name="test_tool",
            description="Test tool"
        )
        
        # 預設應為兩者
        assert "CHAT" in tool.allowed_paths
        assert "WORK" in tool.allowed_paths
    
    def test_tool_both_paths_allowed(self):
        """測試工具可同時允許兩條路徑"""
        tool = MCPTool(
            name="test_tool",
            description="Test tool",
            allowed_paths=["CHAT", "WORK"]
        )
        
        assert tool.allowed_paths == ["CHAT", "WORK"]


class TestMCPServerPathFiltering:
    """測試 MCP Server 路徑過濾"""
    
    def test_register_tool_with_path_restriction(self, mcp_server):
        """測試註冊工具時指定路徑限制"""
        tool = MCPTool(
            name="memory_tool",
            description="記憶檢索工具",
            allowed_paths=["CHAT"]
        )
        
        mcp_server.register_tool(tool)
        
        # 驗證工具被正確註冊
        registered_tool = mcp_server.get_tool("memory_tool")
        assert registered_tool is not None
        assert registered_tool.allowed_paths == ["CHAT"]
    
    def test_register_tool_override_paths(self, mcp_server):
        """測試註冊時覆蓋工具的路徑限制"""
        tool = MCPTool(
            name="test_tool",
            description="Test tool",
            allowed_paths=["WORK"]
        )
        
        # 註冊時指定不同的路徑
        mcp_server.register_tool(tool, allowed_paths=["CHAT"])
        
        registered_tool = mcp_server.get_tool("test_tool")
        assert registered_tool.allowed_paths == ["CHAT"]
    
    def test_get_tools_for_chat_path(self, mcp_server):
        """測試取得 CHAT 路徑的工具"""
        # 註冊不同路徑的工具
        chat_tool = MCPTool(
            name="chat_tool",
            description="Chat tool",
            allowed_paths=["CHAT"]
        )
        work_tool = MCPTool(
            name="work_tool",
            description="Work tool",
            allowed_paths=["WORK"]
        )
        both_tool = MCPTool(
            name="both_tool",
            description="Both tool",
            allowed_paths=["CHAT", "WORK"]
        )
        
        mcp_server.register_tool(chat_tool)
        mcp_server.register_tool(work_tool)
        mcp_server.register_tool(both_tool)
        
        # 取得 CHAT 路徑的工具
        chat_tools = mcp_server.get_tools_for_path("CHAT")
        chat_tool_names = [t.name for t in chat_tools]
        
        assert "chat_tool" in chat_tool_names
        assert "both_tool" in chat_tool_names
        assert "work_tool" not in chat_tool_names
    
    def test_get_tools_for_work_path(self, mcp_server):
        """測試取得 WORK 路徑的工具"""
        # 註冊不同路徑的工具
        chat_tool = MCPTool(
            name="chat_tool",
            description="Chat tool",
            allowed_paths=["CHAT"]
        )
        work_tool = MCPTool(
            name="work_tool",
            description="Work tool",
            allowed_paths=["WORK"]
        )
        both_tool = MCPTool(
            name="both_tool",
            description="Both tool",
            allowed_paths=["CHAT", "WORK"]
        )
        
        mcp_server.register_tool(chat_tool)
        mcp_server.register_tool(work_tool)
        mcp_server.register_tool(both_tool)
        
        # 取得 WORK 路徑的工具
        work_tools = mcp_server.get_tools_for_path("WORK")
        work_tool_names = [t.name for t in work_tools]
        
        assert "work_tool" in work_tool_names
        assert "both_tool" in work_tool_names
        assert "chat_tool" not in work_tool_names
    
    def test_get_tools_spec_for_llm_with_path(self, mcp_server):
        """測試取得工具規範時應用路徑過濾"""
        chat_tool = MCPTool(
            name="memory_retrieve",
            description="Retrieve memory snapshots",
            allowed_paths=["CHAT"]
        )
        work_tool = MCPTool(
            name="play_media",
            description="Play media",
            allowed_paths=["WORK"]
        )
        
        mcp_server.register_tool(chat_tool)
        mcp_server.register_tool(work_tool)
        
        # 取得 CHAT 路徑的工具規範
        chat_specs = mcp_server.get_tools_spec_for_llm(path="CHAT")
        chat_names = [spec["name"] for spec in chat_specs]
        
        assert "memory_retrieve" in chat_names
        assert "play_media" not in chat_names
        
        # 取得 WORK 路徑的工具規範
        work_specs = mcp_server.get_tools_spec_for_llm(path="WORK")
        work_names = [spec["name"] for spec in work_specs]
        
        assert "play_media" in work_names
        assert "memory_retrieve" not in work_names


class TestMCPClientPathFiltering:
    """測試 MCP Client 路徑過濾"""
    
    def test_mcp_client_constants(self):
        """測試路徑常量定義"""
        assert PATH_CHAT == "CHAT"
        assert PATH_WORK == "WORK"
    
    def test_get_tools_for_llm_with_path(self, mcp_client, mcp_server):
        """測試 MCP Client 支援路徑參數"""
        chat_tool = MCPTool(
            name="memory_tool",
            description="Memory tool",
            allowed_paths=["CHAT"]
        )
        work_tool = MCPTool(
            name="workflow_tool",
            description="Workflow tool",
            allowed_paths=["WORK"]
        )
        
        mcp_server.register_tool(chat_tool)
        mcp_server.register_tool(work_tool)
        
        # 取得 CHAT 工具
        chat_tools = mcp_client.get_tools_for_llm(path=PATH_CHAT)
        chat_names = [t["name"] for t in chat_tools]
        
        assert "memory_tool" in chat_names
        assert "workflow_tool" not in chat_names
        
        # 取得 WORK 工具
        work_tools = mcp_client.get_tools_for_llm(path=PATH_WORK)
        work_names = [t["name"] for t in work_tools]
        
        assert "workflow_tool" in work_names
        assert "memory_tool" not in work_names
    
    def test_get_tools_as_gemini_format_with_path(self, mcp_client, mcp_server):
        """測試 get_tools_as_gemini_format 支援路徑參數"""
        # 先清空現有工具（移除核心工具）
        mcp_server.tools.clear()
        
        chat_tool = MCPTool(
            name="memory_tool",
            description="Memory tool",
            allowed_paths=["CHAT"]
        )
        
        mcp_server.register_tool(chat_tool)
        
        # CHAT 路徑應返回工具
        chat_format = mcp_client.get_tools_as_gemini_format(path=PATH_CHAT)
        assert chat_format is not None
        assert len(chat_format[0]["function_declarations"]) == 1
        assert chat_format[0]["function_declarations"][0]["name"] == "memory_tool"
        
        # WORK 路徑應返回空
        work_format = mcp_client.get_tools_as_gemini_format(path=PATH_WORK)
        assert work_format is None or len(work_format[0]["function_declarations"]) == 0
    
    def test_get_tools_default_path_is_chat(self, mcp_client, mcp_server):
        """測試工具獲取方法預設路徑為 CHAT"""
        tool = MCPTool(
            name="test_tool",
            description="Test tool",
            allowed_paths=["CHAT"]
        )
        
        mcp_server.register_tool(tool)
        
        # 不指定路徑時應預設使用 CHAT
        tools = mcp_client.get_tools_for_llm()  # 不指定 path 參數
        tool_names = [t["name"] for t in tools]
        
        assert "test_tool" in tool_names


class TestCoreToolsDefaultPaths:
    """測試核心工具的路徑設定"""
    
    def test_core_workflow_tools_only_in_work_path(self, mcp_server):
        """測試核心工作流控制工具只在 WORK 路徑可用"""
        # 所有核心工作流控制工具都應該限制在 WORK 路徑
        
        tools = mcp_server.list_tools()
        core_tool_names = [t.name for t in tools if t.name.startswith("review_") or 
                          t.name.startswith("approve_") or
                          t.name.startswith("modify_") or
                          t.name.startswith("cancel_") or
                          t.name.startswith("get_workflow_") or
                          t.name.startswith("provide_workflow_") or
                          t.name.startswith("resolve_")]
        
        # 驗證核心工作流工具只在 WORK 路徑
        for tool in tools:
            if tool.name in core_tool_names:
                # 核心工作流工具應該只允許 WORK 路徑
                assert tool.allowed_paths == ["WORK"], f"工具 {tool.name} 應該只在 WORK 路徑可用"
                assert "WORK" in tool.allowed_paths
                assert "CHAT" not in tool.allowed_paths
    
    def test_chat_path_has_no_tools_by_default(self, mcp_server):
        """測試 CHAT 路徑預設沒有工具（準備給記憶工具使用）"""
        chat_tools = mcp_server.get_tools_for_path("CHAT")
        
        # CHAT 路徑目前應該是空的（等待記憶工具註冊）
        assert len(chat_tools) == 0, f"CHAT 路徑應該沒有工具，但發現 {len(chat_tools)} 個"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
