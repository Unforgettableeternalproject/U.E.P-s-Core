"""
測試 LLM 模組與 MCP 的整合

驗證：
1. LLM 模組能正確初始化 MCP Client
2. MCP Server 可以正確設置到 LLM 模組
3. LLM 可以取得 MCP 工具規範
4. LLM 可以呼叫 MCP 工具
"""

import pytest
import asyncio
from modules.llm_module.llm_module import LLMModule
from modules.sys_module.sys_module import SYSModule
from configs.config_loader import load_module_config


@pytest.fixture
def llm_module():
    """創建 LLM 模組實例"""
    config = load_module_config("llm_module")
    module = LLMModule(config)
    module.initialize()
    return module


@pytest.fixture
def sys_module():
    """創建 SYS 模組實例"""
    config = load_module_config("sys_module")
    module = SYSModule(config)
    module.initialize()
    return module


class TestLLMMCPIntegration:
    """測試 LLM-MCP 整合"""
    
    def test_llm_has_mcp_client(self, llm_module):
        """測試 LLM 模組是否有 MCP Client"""
        assert hasattr(llm_module, 'mcp_client')
        assert llm_module.mcp_client is not None
    
    def test_set_mcp_server(self, llm_module, sys_module):
        """測試設置 MCP Server"""
        # 獲取 SYS 模組的 MCP Server
        mcp_server = sys_module.get_mcp_server()
        assert mcp_server is not None
        
        # 設置到 LLM 模組
        llm_module.set_mcp_server(mcp_server)
        
        # 驗證 MCP Client 已連接
        assert llm_module.mcp_client.mcp_server is not None
        assert llm_module.mcp_client.mcp_server == mcp_server
    
    def test_get_mcp_tools_for_llm(self, llm_module, sys_module):
        """測試獲取 MCP 工具規範"""
        # 設置 MCP Server
        mcp_server = sys_module.get_mcp_server()
        llm_module.set_mcp_server(mcp_server)
        
        # 獲取工具規範
        tools = llm_module.get_mcp_tools_for_llm()
        
        # 驗證工具列表
        assert isinstance(tools, list)
        assert len(tools) > 0
        
        # 驗證工具格式
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool
            
        # 驗證核心工具存在
        tool_names = [tool["name"] for tool in tools]
        assert "start_workflow" in tool_names
        assert "get_workflow_status" in tool_names
        assert "cancel_workflow" in tool_names
    
    @pytest.mark.asyncio
    async def test_handle_mcp_tool_call(self, llm_module, sys_module):
        """測試 MCP 工具呼叫"""
        # 設置 MCP Server
        mcp_server = sys_module.get_mcp_server()
        llm_module.set_mcp_server(mcp_server)
        
        # 測試啟動工作流
        result = await llm_module.handle_mcp_tool_call(
            tool_name="start_workflow",
            tool_params={
                "workflow_type": "echo",
                "command": "測試 LLM-MCP 整合",
                "initial_data": {}
            }
        )
        
        # 驗證結果
        assert result["status"] == "success"
        assert "data" in result
        
        # MCP Server 返回的結果在 data.data 中
        workflow_result = result["data"]["data"]
        assert "session_id" in workflow_result
        
        session_id = workflow_result["session_id"]
        
        # 測試查詢工作流狀態
        status_result = await llm_module.handle_mcp_tool_call(
            tool_name="get_workflow_status",
            tool_params={"session_id": session_id}
        )
        
        assert status_result["status"] == "success"
        assert "data" in status_result
        
        # 測試取消工作流
        cancel_result = await llm_module.handle_mcp_tool_call(
            tool_name="cancel_workflow",
            tool_params={
                "session_id": session_id,
                "reason": "測試完成"
            }
        )
        
        assert cancel_result["status"] == "success"


class TestLLMMCPToolParsing:
    """測試 LLM 工具解析"""
    
    def test_mcp_client_parse_tool_call(self, llm_module):
        """測試解析 LLM 工具呼叫"""
        # 模擬 LLM 的工具呼叫格式
        tool_call = {
            "name": "start_workflow",
            "arguments": {
                "workflow_type": "echo",
                "command": "測試指令",
                "initial_data": {}
            }
        }
        
        tool_name, tool_params = llm_module.mcp_client.parse_llm_tool_call(tool_call)
        
        assert tool_name == "start_workflow"
        assert tool_params["workflow_type"] == "echo"
        assert tool_params["command"] == "測試指令"
    
    def test_mcp_client_parse_tool_call_with_json_string(self, llm_module):
        """測試解析 JSON 字串格式的參數"""
        import json
        
        tool_call = {
            "name": "get_workflow_status",
            "arguments": json.dumps({"session_id": "test_123"})
        }
        
        tool_name, tool_params = llm_module.mcp_client.parse_llm_tool_call(tool_call)
        
        assert tool_name == "get_workflow_status"
        assert tool_params["session_id"] == "test_123"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
