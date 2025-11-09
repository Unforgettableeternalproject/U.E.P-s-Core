# -*- coding: utf-8 -*-
"""
MCP 集成測試 - 重構版

測試目標：
1. MCPServer 工具註冊
2. MCPClient 工具調用
3. 8 個核心 MCP 工具
4. 錯誤處理機制
"""

import pytest
from unittest.mock import Mock, MagicMock, AsyncMock
import json

from modules.sys_module.mcp_server.mcp_server import MCPServer
from modules.llm_module.mcp_client import MCPClient


@pytest.mark.mcp
@pytest.mark.critical
class TestMCPServerInitialization:
    """MCP Server 初始化測試"""
    
    def test_server_initialization(self, mcp_server):
        """測試服務器初始化"""
        assert mcp_server is not None
        assert hasattr(mcp_server, 'tools')
    
    def test_sys_module_reference(self, mcp_server):
        """測試 sys_module 引用"""
        assert mcp_server.sys_module is not None


@pytest.mark.mcp
@pytest.mark.critical
class TestMCPCoreTools:
    """MCP 核心工具測試"""
    
    def test_core_tools_registered(self, mcp_server):
        """測試8個核心工具已註冊"""
        expected_tools = [
            "start_workflow",
            "review_step",
            "approve_step",
            "modify_step",
            "cancel_workflow",
            "get_workflow_status",
            "provide_workflow_input",
            "resolve_path"
        ]
        
        available_tools = mcp_server.list_tools()
        tool_names = [tool.name for tool in available_tools]
        
        for tool_name in expected_tools:
            assert tool_name in tool_names, f"工具 {tool_name} 未註冊"
    
    def test_tool_count(self, mcp_server):
        """測試工具數量"""
        tools = mcp_server.list_tools()
        # 至少有8個核心工具
        assert len(tools) >= 8
    
    def test_tools_have_names(self, mcp_server):
        """測試所有工具都有名稱"""
        tools = mcp_server.list_tools()
        
        for tool in tools:
            assert hasattr(tool, 'name')
            assert tool.name is not None
            assert len(tool.name) > 0
    
    def test_tools_have_descriptions(self, mcp_server):
        """測試所有工具都有描述"""
        tools = mcp_server.list_tools()
        
        for tool in tools:
            assert hasattr(tool, 'description')
            assert tool.description is not None
    
    def test_tools_have_input_schema(self, mcp_server):
        """測試所有工具都有輸入模式"""
        tools = mcp_server.list_tools()
        
        for tool in tools:
            assert hasattr(tool, 'parameters')
            assert isinstance(tool.parameters, list)


@pytest.mark.mcp
class TestStartWorkflowTool:
    """start_workflow 工具測試"""
    
    def test_start_workflow_tool_exists(self, mcp_server):
        """測試工具存在"""
        tools = mcp_server.list_tools()
        tool_names = [t.name for t in tools]
        assert "start_workflow" in tool_names
    
    def test_start_workflow_schema(self, mcp_server):
        """測試工具輸入模式"""
        tools = mcp_server.list_tools()
        start_workflow = next(t for t in tools if t.name == "start_workflow")
        
        # MCPTool 使用 parameters 列表
        params = start_workflow.parameters
        param_names = [p.name for p in params]
        assert "workflow_type" in param_names
        assert "command" in param_names


@pytest.mark.mcp
class TestReviewStepTool:
    """review_step 工具測試"""
    
    def test_review_step_tool_exists(self, mcp_server):
        """測試工具存在"""
        tools = mcp_server.list_tools()
        tool_names = [t.name for t in tools]
        assert "review_step" in tool_names
    
    def test_review_step_schema(self, mcp_server):
        """測試工具輸入模式"""
        tools = mcp_server.list_tools()
        review_step = next(t for t in tools if t.name == "review_step")
        
        # MCPTool 使用 parameters 列表
        assert len(review_step.parameters) > 0


@pytest.mark.mcp
class TestApproveStepTool:
    """approve_step 工具測試"""
    
    def test_approve_step_tool_exists(self, mcp_server):
        """測試工具存在"""
        tools = mcp_server.list_tools()
        tool_names = [t.name for t in tools]
        assert "approve_step" in tool_names


@pytest.mark.mcp
class TestModifyStepTool:
    """modify_step 工具測試"""
    
    def test_modify_step_tool_exists(self, mcp_server):
        """測試工具存在"""
        tools = mcp_server.list_tools()
        tool_names = [t.name for t in tools]
        assert "modify_step" in tool_names


@pytest.mark.mcp
class TestCancelWorkflowTool:
    """cancel_workflow 工具測試"""
    
    def test_cancel_workflow_tool_exists(self, mcp_server):
        """測試工具存在"""
        tools = mcp_server.list_tools()
        tool_names = [t.name for t in tools]
        assert "cancel_workflow" in tool_names


@pytest.mark.mcp
class TestGetWorkflowStatusTool:
    """get_workflow_status 工具測試"""
    
    def test_get_workflow_status_tool_exists(self, mcp_server):
        """測試工具存在"""
        tools = mcp_server.list_tools()
        tool_names = [t.name for t in tools]
        assert "get_workflow_status" in tool_names


@pytest.mark.mcp
class TestProvideWorkflowInputTool:
    """provide_workflow_input 工具測試"""
    
    def test_provide_workflow_input_tool_exists(self, mcp_server):
        """測試工具存在"""
        tools = mcp_server.list_tools()
        tool_names = [t.name for t in tools]
        assert "provide_workflow_input" in tool_names


@pytest.mark.mcp
class TestResolvePathTool:
    """resolve_path 工具測試"""
    
    def test_resolve_path_tool_exists(self, mcp_server):
        """測試工具存在"""
        tools = mcp_server.list_tools()
        tool_names = [t.name for t in tools]
        assert "resolve_path" in tool_names
    
    def test_resolve_path_schema(self, mcp_server):
        """測試工具輸入模式"""
        tools = mcp_server.list_tools()
        resolve_path = next(t for t in tools if t.name == "resolve_path")
        
        # MCPTool 使用 parameters 列表
        params = resolve_path.parameters
        param_names = [p.name for p in params]
        assert "path_description" in param_names


@pytest.mark.mcp
@pytest.mark.critical
class TestMCPClient:
    """MCPClient 測試"""
    
    def test_client_initialization(self, mcp_client):
        """測試客戶端初始化"""
        assert mcp_client is not None
        assert mcp_client.mcp_server is not None
        # llm_module 可以是 None（在 fixture 中沒有實際使用）
        assert hasattr(mcp_client, 'llm_module')
    
    @pytest.mark.asyncio
    async def test_call_tool_method_exists(self, mcp_client):
        """測試 call_tool 方法存在"""
        assert hasattr(mcp_client, 'call_tool')
        assert callable(mcp_client.call_tool)
    
    @pytest.mark.asyncio
    async def test_list_tools_method(self, mcp_client):
        """測試獲取工具列表"""
        if hasattr(mcp_client, 'list_tools'):
            tools = mcp_client.list_tools()
            assert isinstance(tools, list)
            assert len(tools) > 0


@pytest.mark.mcp
class TestMCPRequestHandling:
    """MCP 請求處理測試"""
    
    def test_list_tools_request(self, mcp_server):
        """測試列出工具請求"""
        tools = mcp_server.list_tools()
        
        assert isinstance(tools, list)
        assert len(tools) > 0
        
        # 驗證每個工具的結構
        for tool in tools:
            assert hasattr(tool, 'name')
            assert hasattr(tool, 'description')
            assert hasattr(tool, 'parameters')
    
    @pytest.mark.skip(reason="MCPServer 使用 handle_request() 而非 call_tool()")
    def test_call_tool_request_structure(self, mcp_server):
        """測試調用工具請求結構"""
        # MCPServer 使用 async handle_request(MCPRequest) 來處理工具調用
        assert hasattr(mcp_server, 'handle_request')
        assert callable(mcp_server.handle_request)


@pytest.mark.mcp
class TestMCPErrorHandling:
    """MCP 錯誤處理測試"""
    
    @pytest.mark.skip(reason="MCPServer 使用 async handle_request() 且需要 MCPRequest 對象")
    def test_invalid_tool_name(self, mcp_server):
        """測試調用不存在的工具"""
        # 需要使用 MCPRequest 來測試
        tool = mcp_server.get_tool("nonexistent_tool")
        assert tool is None
    
    def test_missing_required_parameters(self, mcp_server):
        """測試缺少必要參數"""
        # 直接測試 validate_params
        start_workflow_tool = mcp_server.get_tool("start_workflow")
        is_valid, error_msg = start_workflow_tool.validate_params({})
        assert not is_valid
        assert error_msg is not None


@pytest.mark.mcp
class TestMCPToolExecution:
    """MCP 工具執行測試"""
    
    @pytest.mark.skip(reason="需要完整的工作流環境")
    def test_start_workflow_execution(self, mcp_server):
        """測試實際啟動工作流"""
        pass
    
    @pytest.mark.skip(reason="需要活動的工作流會話")
    def test_provide_input_execution(self, mcp_server):
        """測試提供工作流輸入"""
        pass
    
    @pytest.mark.skip(reason="需要活動的工作流會話")
    def test_get_status_execution(self, mcp_server):
        """測試獲取工作流狀態"""
        pass


@pytest.mark.mcp
class TestMCPIntegration:
    """MCP 集成測試"""
    
    def test_server_client_integration(self, mcp_server, mcp_client):
        """測試服務器與客戶端集成"""
        # 驗證客戶端可以訪問服務器
        assert mcp_client.mcp_server is not None
        
        # 驗證客戶端可以列出服務器工具
        if hasattr(mcp_client, 'list_tools'):
            client_tools = mcp_client.list_tools()
            server_tools = mcp_server.list_tools()
            
            # 工具數量應該一致
            assert len(client_tools) == len(server_tools)
    
    def test_tool_name_consistency(self, mcp_server, mcp_client):
        """測試工具名稱一致性"""
        server_tools = mcp_server.list_tools()
        server_tool_names = {t.name for t in server_tools}
        
        # 驗證核心工具都在
        core_tools = {
            "start_workflow", "review_step", "approve_step",
            "modify_step", "cancel_workflow", "get_workflow_status",
            "provide_workflow_input", "resolve_path"
        }
        
        assert core_tools.issubset(server_tool_names)


@pytest.mark.mcp
class TestMCPToolSchemas:
    """MCP 工具模式測試"""
    
    def test_all_tools_have_valid_schemas(self, mcp_server):
        """測試所有工具有有效的模式"""
        tools = mcp_server.list_tools()
        
        for tool in tools:
            # 使用 to_llm_spec() 轉換為 LLM 規範
            schema = tool.to_llm_spec()
            
            # to_llm_spec() 返回格式: {name, description, parameters}
            assert "name" in schema
            assert "description" in schema
            assert "parameters" in schema
            
            # parameters 是字典且包含 type
            params = schema["parameters"]
            assert isinstance(params, dict)
            assert "type" in params
            assert params["type"] == "object"
    
    def test_required_fields_in_schemas(self, mcp_server):
        """測試模式中的必填字段"""
        tools = mcp_server.list_tools()
        
        for tool in tools:
            schema = tool.to_llm_spec()
            params = schema["parameters"]
            
            # 如果有 required 字段，應該是列表
            if "required" in params:
                assert isinstance(params["required"], list)
    
    def test_property_types_defined(self, mcp_server):
        """測試屬性類型已定義"""
        tools = mcp_server.list_tools()
        
        for tool in tools:
            schema = tool.to_llm_spec()
            params = schema["parameters"]
            
            if "properties" in params:
                for prop_name, prop_def in params["properties"].items():
                    # 每個屬性應該有類型定義
                    assert "type" in prop_def or "anyOf" in prop_def or "$ref" in prop_def


@pytest.mark.mcp
class TestMCPServerMethods:
    """MCP Server 方法測試"""
    
    def test_has_list_tools_method(self, mcp_server):
        """測試有 list_tools 方法"""
        assert hasattr(mcp_server, 'list_tools')
        assert callable(mcp_server.list_tools)
    
    @pytest.mark.skip(reason="MCPServer 使用 handle_request() 而非 call_tool()")
    def test_has_call_tool_method(self, mcp_server):
        """測試有 handle_request 方法"""
        assert hasattr(mcp_server, 'handle_request')
        assert callable(mcp_server.handle_request)
    
    def test_list_tools_returns_list(self, mcp_server):
        """測試 list_tools 返回列表"""
        result = mcp_server.list_tools()
        assert isinstance(result, list)
    
    def test_tool_metadata_complete(self, mcp_server):
        """測試工具元數據完整"""
        tools = mcp_server.list_tools()
        
        for tool in tools:
            # 必需的元數據字段
            assert hasattr(tool, 'name')
            assert hasattr(tool, 'description')
            assert hasattr(tool, 'parameters')
            
            # 名稱和描述不為空
            assert tool.name
            assert tool.description
