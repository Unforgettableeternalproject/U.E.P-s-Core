"""
MCP 基礎設施測試

測試 MCP Server 和 MCP Client 的核心功能：
1. 協議處理 (JSON-RPC)
2. 工具定義與參數驗證
3. 資源提供者
4. MCP Server 請求處理
5. MCP Client 工具呼叫
6. LLM → MCP Server → SYS 整合流程
"""

import pytest
import asyncio
from typing import Dict, Any
from datetime import datetime

# MCP Server 組件
from modules.sys_module.mcp_server.protocol_handlers import (
    MCPRequest, MCPResponse, MCPError, MCPErrorCode,
    create_success_response, create_error_response, create_notification
)
from modules.sys_module.mcp_server.tool_definitions import (
    MCPTool, ToolParameter, ToolParameterType, ToolResult
)
from modules.sys_module.mcp_server.resource_providers import (
    WorkflowResourceProvider, WorkflowResource, StepResultResource
)
from modules.sys_module.mcp_server.mcp_server import MCPServer

# MCP Client
from modules.llm_module.mcp_client import MCPClient


# ========== 測試 Fixtures ==========

@pytest.fixture
def sample_tool_params():
    """範例工具參數定義"""
    return [
        ToolParameter(
            name="workflow_type",
            type=ToolParameterType.STRING,
            description="工作流類型",
            required=True
        ),
        ToolParameter(
            name="command",
            type=ToolParameterType.STRING,
            description="指令",
            required=True
        ),
        ToolParameter(
            name="initial_data",
            type=ToolParameterType.OBJECT,
            description="初始資料",
            required=False
        ),
    ]


@pytest.fixture
def mock_sys_module():
    """模擬 SYS 模組"""
    class MockSYSModule:
        def __init__(self):
            self.workflow_engines = {}
        
        async def start_workflow_async(self, workflow_type: str, command: str, initial_data: Dict):
            return {
                "status": "success",
                "session_id": "ws_test_123",
                "workflow_type": workflow_type,
                "message": f"工作流 {workflow_type} 已啟動"
            }
        
        async def continue_workflow_async(self, session_id: str, user_input: Any, additional_data: Dict):
            return {
                "status": "success",
                "session_id": session_id,
                "progress": 0.5,
                "current_step": "step_2",
                "message": "工作流繼續執行"
            }
        
        async def modify_and_reexecute_step_async(self, session_id: str, modifications: Dict):
            return {
                "status": "success",
                "session_id": session_id,
                "message": "步驟已修改並重新執行"
            }
        
        async def cancel_workflow_async(self, session_id: str, reason: str):
            return {
                "status": "success",
                "session_id": session_id,
                "message": f"工作流已取消: {reason}"
            }
        
        async def handle_llm_review_response_async(self, session_id: str, action: str, modified_params: Dict = None):
            """處理 LLM 審核響應 (階段 2 新增)"""
            return {
                "status": "success",
                "session_id": session_id,
                "message": f"LLM 審核響應已處理: {action}"
            }
    
    return MockSYSModule()


@pytest.fixture
def mcp_server(mock_sys_module):
    """MCP Server 實例"""
    return MCPServer(sys_module=mock_sys_module)


@pytest.fixture
def mcp_client(mcp_server):
    """MCP Client 實例"""
    client = MCPClient(mcp_server=mcp_server)
    return client


@pytest.fixture
def resource_provider():
    """資源提供者實例"""
    return WorkflowResourceProvider()


# ========== 協議處理測試 ==========

class TestProtocolHandlers:
    """測試 JSON-RPC 協議處理"""
    
    def test_mcp_request_creation(self):
        """測試 MCP 請求創建"""
        request = MCPRequest(
            jsonrpc="2.0",
            method="start_workflow",
            params={"workflow_type": "test", "command": "測試"},
            id=1
        )
        
        assert request.jsonrpc == "2.0"
        assert request.method == "start_workflow"
        assert request.params["workflow_type"] == "test"
        assert request.id == 1
    
    def test_mcp_success_response(self):
        """測試成功響應創建"""
        response = create_success_response(
            request_id=1,
            result={"status": "success", "data": "test"}
        )
        
        assert response.is_success()
        assert not response.is_error()
        assert response.id == 1
        assert response.result["status"] == "success"
    
    def test_mcp_error_response(self):
        """測試錯誤響應創建"""
        response = create_error_response(
            request_id=1,
            code=MCPErrorCode.METHOD_NOT_FOUND,
            message="工具不存在"
        )
        
        assert response.is_error()
        assert not response.is_success()
        assert response.error.code == MCPErrorCode.METHOD_NOT_FOUND
        assert response.error.message == "工具不存在"
    
    def test_mcp_notification(self):
        """測試通知創建"""
        notification = create_notification(
            method="workflow_progress",
            params={"session_id": "ws_123", "progress": 0.5}
        )
        
        assert notification.jsonrpc == "2.0"
        assert notification.method == "workflow_progress"
        assert notification.params["progress"] == 0.5


# ========== 工具定義測試 ==========

class TestToolDefinitions:
    """測試工具定義與參數驗證"""
    
    def test_tool_parameter_validation_success(self, sample_tool_params):
        """測試參數驗證成功"""
        param = sample_tool_params[0]  # workflow_type (required string)
        is_valid, error_msg = param.validate_value("test_workflow")
        
        assert is_valid
        assert error_msg is None
    
    def test_tool_parameter_validation_required_missing(self, sample_tool_params):
        """測試必填參數缺失"""
        param = sample_tool_params[0]  # workflow_type (required)
        is_valid, error_msg = param.validate_value(None)
        
        assert not is_valid
        assert "必填" in error_msg
    
    def test_tool_parameter_validation_wrong_type(self):
        """測試參數類型錯誤"""
        param = ToolParameter(
            name="count",
            type=ToolParameterType.INTEGER,
            description="數量",
            required=True
        )
        is_valid, error_msg = param.validate_value("not_an_integer")
        
        assert not is_valid
        assert "整數" in error_msg
    
    def test_tool_parameter_enum_validation(self):
        """測試枚舉值驗證"""
        param = ToolParameter(
            name="status",
            type=ToolParameterType.STRING,
            description="狀態",
            required=True,
            enum=["active", "paused", "cancelled"]
        )
        
        # 有效值
        is_valid, _ = param.validate_value("active")
        assert is_valid
        
        # 無效值
        is_valid, error_msg = param.validate_value("invalid")
        assert not is_valid
        assert "之一" in error_msg
    
    def test_tool_result_success(self):
        """測試成功結果"""
        result = ToolResult.success("操作成功", {"key": "value"})
        
        assert result.status == "success"
        assert result.message == "操作成功"
        assert result.data["key"] == "value"
        assert result.error_detail is None
    
    def test_tool_result_error(self):
        """測試錯誤結果"""
        result = ToolResult.error("操作失敗", error_detail="詳細錯誤訊息")
        
        assert result.status == "error"
        assert result.message == "操作失敗"
        assert result.error_detail == "詳細錯誤訊息"
    
    def test_tool_result_pending(self):
        """測試等待結果"""
        result = ToolResult.pending("等待使用者輸入", {"prompt": "請輸入檔案路徑"})
        
        assert result.status == "pending"
        assert result.message == "等待使用者輸入"
        assert result.data["prompt"] == "請輸入檔案路徑"
    
    def test_mcp_tool_validate_params_success(self, sample_tool_params):
        """測試工具參數驗證成功"""
        tool = MCPTool(
            name="start_workflow",
            description="啟動工作流",
            parameters=sample_tool_params
        )
        
        params = {
            "workflow_type": "test",
            "command": "測試指令",
            "initial_data": {"key": "value"}
        }
        
        is_valid, error_msg = tool.validate_params(params)
        assert is_valid
        assert error_msg is None
    
    def test_mcp_tool_validate_params_missing_required(self, sample_tool_params):
        """測試工具參數驗證失敗 - 缺少必填"""
        tool = MCPTool(
            name="start_workflow",
            description="啟動工作流",
            parameters=sample_tool_params
        )
        
        params = {
            "command": "測試指令"
            # 缺少 workflow_type
        }
        
        is_valid, error_msg = tool.validate_params(params)
        assert not is_valid
        assert "workflow_type" in error_msg
    
    def test_mcp_tool_to_llm_spec(self, sample_tool_params):
        """測試轉換為 LLM 工具規範"""
        tool = MCPTool(
            name="start_workflow",
            description="啟動工作流程",
            parameters=sample_tool_params
        )
        
        spec = tool.to_llm_spec()
        
        assert spec["name"] == "start_workflow"
        assert spec["description"] == "啟動工作流程"
        assert "parameters" in spec
        assert "workflow_type" in spec["parameters"]["properties"]
        assert "workflow_type" in spec["parameters"]["required"]
        assert "initial_data" not in spec["parameters"]["required"]


# ========== 資源提供者測試 ==========

class TestResourceProviders:
    """測試資源提供者"""
    
    def test_register_and_get_workflow(self, resource_provider):
        """測試註冊和取得工作流"""
        workflow = WorkflowResource(
            session_id="ws_123",
            workflow_type="test_workflow",
            status="running",
            progress=0.3
        )
        
        resource_provider.register_workflow(workflow)
        retrieved = resource_provider.get_workflow("ws_123")
        
        assert retrieved is not None
        assert retrieved.session_id == "ws_123"
        assert retrieved.workflow_type == "test_workflow"
        assert retrieved.progress == 0.3
    
    def test_update_workflow(self, resource_provider):
        """測試更新工作流"""
        workflow = WorkflowResource(
            session_id="ws_123",
            workflow_type="test_workflow",
            status="running",
            progress=0.3
        )
        resource_provider.register_workflow(workflow)
        
        resource_provider.update_workflow("ws_123", {
            "progress": 0.7,
            "current_step": "step_3"
        })
        
        updated = resource_provider.get_workflow("ws_123")
        assert updated.progress == 0.7
        assert updated.current_step == "step_3"
    
    def test_list_workflows(self, resource_provider):
        """測試列出所有工作流"""
        for i in range(3):
            workflow = WorkflowResource(
                session_id=f"ws_{i}",
                workflow_type="test_workflow",
                status="running",
                progress=0.0
            )
            resource_provider.register_workflow(workflow)
        
        workflows = resource_provider.list_workflows()
        assert len(workflows) == 3
    
    def test_add_and_get_step_result(self, resource_provider):
        """測試新增和取得步驟結果"""
        step_result = StepResultResource(
            session_id="ws_123",
            step_id="step_1",
            status="success",
            message="步驟完成",
            data={"output": "test"},
            timestamp=datetime.now().isoformat()
        )
        
        resource_provider.add_step_result("ws_123", step_result)
        retrieved = resource_provider.get_step_result("ws_123", "step_1")
        
        assert retrieved is not None
        assert retrieved.step_id == "step_1"
        assert retrieved.status == "success"
        assert retrieved.data["output"] == "test"
    
    def test_get_all_step_results(self, resource_provider):
        """測試取得所有步驟結果"""
        for i in range(3):
            step_result = StepResultResource(
                session_id="ws_123",
                step_id=f"step_{i}",
                status="success",
                message="步驟完成",
                data={},
                timestamp=datetime.now().isoformat()
            )
            resource_provider.add_step_result("ws_123", step_result)
        
        results = resource_provider.get_all_step_results("ws_123")
        assert len(results) == 3
    
    def test_remove_workflow(self, resource_provider):
        """測試移除工作流"""
        workflow = WorkflowResource(
            session_id="ws_123",
            workflow_type="test_workflow",
            status="completed",
            progress=1.0
        )
        resource_provider.register_workflow(workflow)
        
        # 新增步驟結果
        step_result = StepResultResource(
            session_id="ws_123",
            step_id="step_1",
            status="success",
            message="完成",
            data={},
            timestamp=datetime.now().isoformat()
        )
        resource_provider.add_step_result("ws_123", step_result)
        
        # 移除工作流
        resource_provider.remove_workflow("ws_123")
        
        # 驗證已移除
        assert resource_provider.get_workflow("ws_123") is None
        assert len(resource_provider.get_all_step_results("ws_123")) == 0


# ========== MCP Server 測試 ==========

class TestMCPServer:
    """測試 MCP Server"""
    
    def test_mcp_server_initialization(self, mcp_server):
        """測試 MCP Server 初始化"""
        assert mcp_server is not None
        assert mcp_server.sys_module is not None
        assert len(mcp_server.tools) == 6  # 6 個核心工具
    
    def test_list_tools(self, mcp_server):
        """測試列出所有工具"""
        tools = mcp_server.list_tools()
        assert len(tools) == 6
        
        tool_names = {tool.name for tool in tools}
        expected_tools = {
            "start_workflow", "review_step", "approve_step",
            "modify_step", "cancel_workflow", "get_workflow_status"
        }
        assert tool_names == expected_tools
    
    def test_get_tools_spec_for_llm(self, mcp_server):
        """測試取得 LLM 工具規範"""
        specs = mcp_server.get_tools_spec_for_llm()
        assert len(specs) == 6
        
        # 檢查規範格式
        for spec in specs:
            assert "name" in spec
            assert "description" in spec
            assert "parameters" in spec
            assert spec["parameters"]["type"] == "object"
    
    @pytest.mark.asyncio
    async def test_handle_start_workflow_success(self, mcp_server):
        """測試處理 start_workflow 請求成功"""
        request = MCPRequest(
            jsonrpc="2.0",
            method="start_workflow",
            params={
                "workflow_type": "test_workflow",
                "command": "測試指令",
                "initial_data": {}
            },
            id=1
        )
        
        response = await mcp_server.handle_request(request)
        
        assert response.is_success()
        assert response.result["status"] == "success"
        assert "session_id" in response.result["data"]
    
    @pytest.mark.asyncio
    async def test_handle_method_not_found(self, mcp_server):
        """測試處理不存在的方法"""
        request = MCPRequest(
            jsonrpc="2.0",
            method="nonexistent_tool",
            params={},
            id=1
        )
        
        response = await mcp_server.handle_request(request)
        
        assert response.is_error()
        assert response.error.code == MCPErrorCode.METHOD_NOT_FOUND
    
    @pytest.mark.asyncio
    async def test_handle_invalid_params(self, mcp_server):
        """測試處理無效參數"""
        request = MCPRequest(
            jsonrpc="2.0",
            method="start_workflow",
            params={
                "command": "測試指令"
                # 缺少必填的 workflow_type
            },
            id=1
        )
        
        response = await mcp_server.handle_request(request)
        
        assert response.is_error()
        assert "參數驗證失敗" in response.error.message
    
    @pytest.mark.asyncio
    async def test_workflow_lifecycle(self, mcp_server):
        """測試完整工作流生命週期"""
        # 1. 啟動工作流
        start_request = MCPRequest(
            jsonrpc="2.0",
            method="start_workflow",
            params={
                "workflow_type": "test_workflow",
                "command": "測試",
                "initial_data": {}
            },
            id=1
        )
        start_response = await mcp_server.handle_request(start_request)
        assert start_response.is_success()
        
        session_id = start_response.result["data"]["session_id"]
        
        # 2. 查詢狀態
        status_request = MCPRequest(
            jsonrpc="2.0",
            method="get_workflow_status",
            params={"session_id": session_id},
            id=2
        )
        status_response = await mcp_server.handle_request(status_request)
        assert status_response.is_success()
        assert status_response.result["data"]["session_id"] == session_id
        
        # 3. 批准步驟
        approve_request = MCPRequest(
            jsonrpc="2.0",
            method="approve_step",
            params={"session_id": session_id},
            id=3
        )
        approve_response = await mcp_server.handle_request(approve_request)
        assert approve_response.is_success()
        
        # 4. 取消工作流
        cancel_request = MCPRequest(
            jsonrpc="2.0",
            method="cancel_workflow",
            params={"session_id": session_id, "reason": "測試完成"},
            id=4
        )
        cancel_response = await mcp_server.handle_request(cancel_request)
        assert cancel_response.is_success()


# ========== MCP Client 測試 ==========

class TestMCPClient:
    """測試 MCP Client"""
    
    def test_mcp_client_initialization(self, mcp_client):
        """測試 MCP Client 初始化"""
        assert mcp_client is not None
        assert mcp_client.mcp_server is not None
    
    @pytest.mark.asyncio
    async def test_call_tool_success(self, mcp_client):
        """測試呼叫工具成功"""
        result = await mcp_client.call_tool(
            tool_name="start_workflow",
            params={
                "workflow_type": "test_workflow",
                "command": "測試",
                "initial_data": {}
            }
        )
        
        assert result["status"] == "success"
        assert "data" in result
    
    @pytest.mark.asyncio
    async def test_call_tool_error(self, mcp_client):
        """測試呼叫工具錯誤"""
        result = await mcp_client.call_tool(
            tool_name="start_workflow",
            params={
                "command": "測試"
                # 缺少 workflow_type
            }
        )
        
        assert result["status"] == "error"
        assert "message" in result
    
    def test_get_tools_for_llm(self, mcp_client):
        """測試取得 LLM 工具規範"""
        tools = mcp_client.get_tools_for_llm()
        assert len(tools) == 6
        
        # 驗證格式適合 LLM
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool
    
    def test_parse_llm_tool_call_dict(self, mcp_client):
        """測試解析 LLM 工具呼叫 (dict 參數)"""
        tool_call = {
            "name": "start_workflow",
            "arguments": {
                "workflow_type": "test",
                "command": "測試"
            }
        }
        
        tool_name, params = mcp_client.parse_llm_tool_call(tool_call)
        
        assert tool_name == "start_workflow"
        assert params["workflow_type"] == "test"
    
    def test_parse_llm_tool_call_json_string(self, mcp_client):
        """測試解析 LLM 工具呼叫 (JSON string 參數)"""
        tool_call = {
            "name": "start_workflow",
            "arguments": '{"workflow_type": "test", "command": "測試"}'
        }
        
        tool_name, params = mcp_client.parse_llm_tool_call(tool_call)
        
        assert tool_name == "start_workflow"
        assert params["workflow_type"] == "test"
    
    @pytest.mark.asyncio
    async def test_handle_llm_function_call_success(self, mcp_client):
        """測試處理 LLM function call 成功"""
        function_call = {
            "name": "start_workflow",
            "arguments": {
                "workflow_type": "test_workflow",
                "command": "測試指令",
                "initial_data": {}
            }
        }
        
        result = await mcp_client.handle_llm_function_call(function_call)
        
        assert result["tool_name"] == "start_workflow"
        assert result["status"] == "success"
        assert "formatted_message" in result
    
    @pytest.mark.asyncio
    async def test_handle_llm_function_call_error(self, mcp_client):
        """測試處理 LLM function call 錯誤"""
        function_call = {
            "name": "start_workflow",
            "arguments": {
                "command": "測試"
                # 缺少 workflow_type
            }
        }
        
        result = await mcp_client.handle_llm_function_call(function_call)
        
        assert result["tool_name"] == "start_workflow"
        assert result["status"] == "error"
        assert "error" in result
    
    def test_is_workflow_tool(self, mcp_client):
        """測試判斷是否為工作流工具"""
        assert mcp_client.is_workflow_tool("start_workflow")
        assert mcp_client.is_workflow_tool("approve_step")
        assert not mcp_client.is_workflow_tool("some_other_tool")


# ========== 整合測試 ==========

class TestMCPIntegration:
    """測試 LLM → MCP Server → SYS 整合"""
    
    @pytest.mark.asyncio
    async def test_full_workflow_flow(self, mcp_client, mcp_server):
        """測試完整工作流流程"""
        # 模擬 LLM 發起工作流
        function_call = {
            "name": "start_workflow",
            "arguments": {
                "workflow_type": "file_processing",
                "command": "處理檔案",
                "initial_data": {"file_path": "/test/file.txt"}
            }
        }
        
        # LLM Client 處理 function call
        result = await mcp_client.handle_llm_function_call(function_call)
        assert result["status"] == "success"
        
        # 取得 session_id
        session_id = result["content"]["data"]["session_id"]
        
        # 模擬步驟完成通知
        mcp_server.notify_step_completed(
            session_id=session_id,
            step_id="step_1",
            step_result={
                "status": "success",
                "message": "步驟完成",
                "data": {"output": "處理完成"}
            }
        )
        
        # 模擬 LLM 審核步驟
        review_call = {
            "name": "review_step",
            "arguments": {
                "session_id": session_id,
                "step_id": "step_1"
            }
        }
        review_result = await mcp_client.handle_llm_function_call(review_call)
        assert review_result["status"] == "success"
        assert "步驟完成" in review_result["formatted_message"]
        
        # 模擬 LLM 批准繼續
        approve_call = {
            "name": "approve_step",
            "arguments": {
                "session_id": session_id
            }
        }
        approve_result = await mcp_client.handle_llm_function_call(approve_call)
        assert approve_result["status"] == "success"


# ========== 效能測試 ==========

class TestMCPPerformance:
    """測試 MCP 效能"""
    
    @pytest.mark.asyncio
    async def test_concurrent_requests(self, mcp_server):
        """測試並發請求"""
        import time
        
        async def make_request(request_id):
            request = MCPRequest(
                jsonrpc="2.0",
                method="start_workflow",
                params={
                    "workflow_type": "test",
                    "command": f"測試 {request_id}",
                    "initial_data": {}
                },
                id=request_id
            )
            return await mcp_server.handle_request(request)
        
        start_time = time.time()
        
        # 並發 10 個請求
        tasks = [make_request(i) for i in range(10)]
        results = await asyncio.gather(*tasks)
        
        elapsed = time.time() - start_time
        
        # 驗證所有請求都成功
        assert all(r.is_success() for r in results)
        
        # 驗證效能 (應在 2 秒內完成)
        assert elapsed < 2.0
        
        print(f"\n並發 10 個請求耗時: {elapsed:.3f} 秒")
    
    @pytest.mark.asyncio
    async def test_request_response_latency(self, mcp_server):
        """測試單次請求響應延遲"""
        import time
        
        request = MCPRequest(
            jsonrpc="2.0",
            method="get_workflow_status",
            params={"session_id": "ws_test"},
            id=1
        )
        
        start_time = time.time()
        response = await mcp_server.handle_request(request)
        elapsed = time.time() - start_time
        
        # 延遲應小於 100ms
        assert elapsed < 0.1
        
        print(f"\n單次請求延遲: {elapsed*1000:.2f} ms")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
