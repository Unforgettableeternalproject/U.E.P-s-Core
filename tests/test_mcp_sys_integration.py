"""
整合測試：MCP Server 與 SYS 模組

測試 MCP Server 與 SYS 模組的完整整合，驗證：
1. SYS 模組正確初始化 MCP Server
2. MCP Server 可以透過 async 方法呼叫 SYS 功能
3. 工作流啟動和控制流程正常運作
"""

import pytest
import asyncio

from modules.sys_module.sys_module import SYSModule
from modules.sys_module.mcp_server.protocol_handlers import MCPRequest
from configs.config_loader import load_module_config


@pytest.fixture
def sys_module():
    """創建 SYS 模組實例"""
    config = load_module_config("sys_module")
    module = SYSModule(config)
    module.initialize()
    return module


@pytest.fixture
def mcp_server(sys_module):
    """獲取 MCP Server 實例"""
    return sys_module.get_mcp_server()


class TestMCPSYSIntegration:
    """測試 MCP 與 SYS 模組整合"""
    
    def test_sys_module_has_mcp_server(self, sys_module):
        """測試 SYS 模組包含 MCP Server"""
        assert sys_module.mcp_server is not None
        assert sys_module.get_mcp_server() is not None
    
    def test_mcp_server_has_sys_reference(self, sys_module, mcp_server):
        """測試 MCP Server 持有 SYS 模組引用"""
        assert mcp_server.sys_module is sys_module
    
    def test_mcp_server_tools_registered(self, mcp_server):
        """測試 MCP Server 註冊了工具"""
        tools = mcp_server.list_tools()
        assert len(tools) == 6
        
        tool_names = {tool.name for tool in tools}
        expected = {
            "start_workflow", "review_step", "approve_step",
            "modify_step", "cancel_workflow", "get_workflow_status"
        }
        assert tool_names == expected
    
    @pytest.mark.asyncio
    async def test_sys_async_methods_exist(self, sys_module):
        """測試 SYS 模組包含 async 方法"""
        assert hasattr(sys_module, "start_workflow_async")
        assert hasattr(sys_module, "continue_workflow_async")
        assert hasattr(sys_module, "modify_and_reexecute_step_async")
        assert hasattr(sys_module, "cancel_workflow_async")
        
        # 驗證這些方法是協程函數
        assert asyncio.iscoroutinefunction(sys_module.start_workflow_async)
        assert asyncio.iscoroutinefunction(sys_module.continue_workflow_async)
        assert asyncio.iscoroutinefunction(sys_module.modify_and_reexecute_step_async)
        assert asyncio.iscoroutinefunction(sys_module.cancel_workflow_async)
    
    @pytest.mark.asyncio
    async def test_start_workflow_through_mcp(self, sys_module, mcp_server):
        """測試透過 MCP 啟動工作流"""
        # 創建 MCP 請求
        request = MCPRequest(
            jsonrpc="2.0",
            method="start_workflow",
            params={
                "workflow_type": "echo",
                "command": "測試 MCP 整合",
                "initial_data": {}
            },
            id=1
        )
        
        # 透過 MCP Server 處理請求
        response = await mcp_server.handle_request(request)
        
        # 驗證響應
        assert response.is_success()
        assert response.result["status"] == "success"
        assert "session_id" in response.result["data"]
        
        # 驗證工作流已在 SYS 模組中創建
        session_id = response.result["data"]["session_id"]
        assert session_id in sys_module.workflow_engines
    
    @pytest.mark.asyncio
    async def test_workflow_lifecycle_through_mcp(self, sys_module, mcp_server):
        """測試完整工作流生命週期"""
        # 1. 啟動工作流
        start_request = MCPRequest(
            jsonrpc="2.0",
            method="start_workflow",
            params={
                "workflow_type": "echo",
                "command": "測試生命週期",
                "initial_data": {}
            },
            id=1
        )
        start_response = await mcp_server.handle_request(start_request)
        assert start_response.is_success()
        
        session_id = start_response.result["data"]["session_id"]
        
        # 2. 查詢工作流狀態
        status_request = MCPRequest(
            jsonrpc="2.0",
            method="get_workflow_status",
            params={"session_id": session_id},
            id=2
        )
        status_response = await mcp_server.handle_request(status_request)
        assert status_response.is_success()
        assert status_response.result["data"]["session_id"] == session_id
        
        # 3. 繼續工作流（提供輸入）
        continue_request = MCPRequest(
            jsonrpc="2.0",
            method="approve_step",
            params={
                "session_id": session_id,
                "continue_data": {"message": "Hello from MCP"}
            },
            id=3
        )
        continue_response = await mcp_server.handle_request(continue_request)
        assert continue_response.is_success()
        
        # 4. 取消工作流
        cancel_request = MCPRequest(
            jsonrpc="2.0",
            method="cancel_workflow",
            params={
                "session_id": session_id,
                "reason": "測試完成"
            },
            id=4
        )
        cancel_response = await mcp_server.handle_request(cancel_request)
        assert cancel_response.is_success()
    
    @pytest.mark.asyncio
    async def test_step_notification(self, sys_module, mcp_server):
        """測試步驟完成通知"""
        # 啟動工作流
        session_id = "test_session_123"
        
        # 模擬步驟完成通知
        mcp_server.notify_step_completed(
            session_id=session_id,
            step_id="step_1",
            step_result={
                "status": "success",
                "message": "步驟完成",
                "data": {"output": "測試輸出"}
            }
        )
        
        # 驗證步驟結果已儲存
        step_result = mcp_server.resource_provider.get_step_result(session_id, "step_1")
        assert step_result is not None
        assert step_result.step_id == "step_1"
        assert step_result.status == "success"
    
    @pytest.mark.asyncio
    async def test_error_handling(self, sys_module, mcp_server):
        """測試錯誤處理"""
        # 測試不存在的工作流類型
        request = MCPRequest(
            jsonrpc="2.0",
            method="start_workflow",
            params={
                "workflow_type": "nonexistent_workflow",
                "command": "測試錯誤",
                "initial_data": {}
            },
            id=1
        )
        
        response = await mcp_server.handle_request(request)
        
        # 應該返回錯誤
        assert response.is_error()
        assert "未知的工作流程類型" in response.error.message or "無法為" in response.error.message


class TestAsyncWorkflowMethods:
    """測試 SYS 模組的 async 方法"""
    
    @pytest.mark.asyncio
    async def test_start_workflow_async(self, sys_module):
        """測試 async 啟動工作流"""
        result = await sys_module.start_workflow_async(
            workflow_type="echo",
            command="測試 async",
            initial_data={}
        )
        
        assert result["status"] == "success"
        assert "session_id" in result
    
    @pytest.mark.asyncio
    async def test_continue_workflow_async(self, sys_module):
        """測試 async 繼續工作流"""
        # 先啟動工作流
        start_result = await sys_module.start_workflow_async(
            workflow_type="echo",
            command="測試",
            initial_data={}
        )
        
        session_id = start_result["session_id"]
        
        # 繼續工作流
        continue_result = await sys_module.continue_workflow_async(
            session_id=session_id,
            user_input="測試輸入"
        )
        
        assert continue_result["status"] in ["waiting", "completed", "cancelled"]
    
    @pytest.mark.asyncio
    async def test_cancel_workflow_async(self, sys_module):
        """測試 async 取消工作流"""
        # 先啟動工作流
        start_result = await sys_module.start_workflow_async(
            workflow_type="echo",
            command="測試",
            initial_data={}
        )
        
        session_id = start_result["session_id"]
        
        # 取消工作流
        cancel_result = await sys_module.cancel_workflow_async(
            session_id=session_id,
            reason="測試取消"
        )
        
        assert cancel_result["status"] == "cancelled"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
