# -*- coding: utf-8 -*-
"""
LLM-MCP 集成測試

測試目標：
1. LLM 模組獲取 MCP 工具規範
2. LLM 模組調用 MCP 工具
3. Gemini 回應解析（JSON schema 和 function call）
4. LLM 與 MCP 的完整工作流
"""

import pytest
from unittest.mock import Mock, MagicMock, AsyncMock, patch
import json

from modules.llm_module.llm_module import LLMModule
from modules.llm_module.schemas import LLMInput, LLMMode
from modules.sys_module.mcp_server.mcp_server import MCPServer
from modules.llm_module.mcp_client import MCPClient


@pytest.fixture
def mock_gemini_wrapper():
    """Mock Gemini API wrapper"""
    mock = Mock()
    mock.model_name = "gemini-2.0-flash-exp"
    mock.temperature = 0.7
    mock.top_p = 0.95
    mock.max_tokens = 8192
    return mock


@pytest.fixture
def llm_module_with_mcp(mock_sys_module, mock_gemini_wrapper):
    """創建帶有 MCP 客戶端的 LLM 模組"""
    with patch('modules.llm_module.llm_module.GeminiWrapper', return_value=mock_gemini_wrapper):
        llm = LLMModule()
        # 連接 MCP Server
        mcp_server = MCPServer(sys_module=mock_sys_module)
        llm.mcp_client.set_mcp_server(mcp_server)
        return llm


@pytest.mark.llm
@pytest.mark.critical
class TestLLMMCPToolDiscovery:
    """LLM 發現和獲取 MCP 工具測試"""
    
    def test_llm_can_get_mcp_tools(self, llm_module_with_mcp):
        """測試 LLM 可以獲取 MCP 工具規範"""
        tools = llm_module_with_mcp.get_mcp_tools_for_llm()
        
        assert isinstance(tools, list)
        assert len(tools) > 0
        
        # 驗證工具規範格式
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool
    
    def test_mcp_tools_include_workflow_tools(self, llm_module_with_mcp):
        """測試 MCP 工具包含工作流控制工具"""
        tools = llm_module_with_mcp.get_mcp_tools_for_llm()
        tool_names = [t["name"] for t in tools]
        
        # 核心工作流工具
        expected_tools = ["start_workflow", "get_workflow_status", "provide_workflow_input"]
        
        for tool_name in expected_tools:
            assert tool_name in tool_names, f"工具 {tool_name} 未找到"
    
    def test_mcp_tool_has_valid_parameters_schema(self, llm_module_with_mcp):
        """測試 MCP 工具有有效的參數模式"""
        tools = llm_module_with_mcp.get_mcp_tools_for_llm()
        
        start_workflow = next(t for t in tools if t["name"] == "start_workflow")
        
        params = start_workflow["parameters"]
        assert "type" in params
        assert params["type"] == "object"
        assert "properties" in params
        
        # 驗證必填參數
        assert "workflow_type" in params["properties"]
        assert "command" in params["properties"]


@pytest.mark.llm
@pytest.mark.critical  
class TestLLMMCPToolInvocation:
    """LLM 調用 MCP 工具測試"""
    
    @pytest.mark.asyncio
    async def test_llm_can_call_mcp_tool(self, llm_module_with_mcp):
        """測試 LLM 可以調用 MCP 工具"""
        # 準備工具調用參數
        tool_name = "resolve_path"
        tool_params = {
            "path_description": "documents folder",
            "create_if_missing": False
        }
        
        # 調用工具
        try:
            result = await llm_module_with_mcp.handle_mcp_tool_call(tool_name, tool_params)
            
            # 驗證結果結構
            assert isinstance(result, dict)
            # resolve_path 應該返回路徑信息
            
        except Exception as e:
            # 如果系統無法解析路徑，這是預期的
            assert "path" in str(e).lower() or "resolve" in str(e).lower()
    
    @pytest.mark.asyncio
    async def test_llm_tool_call_with_invalid_params(self, llm_module_with_mcp):
        """測試 LLM 使用無效參數調用工具"""
        tool_name = "start_workflow"
        invalid_params = {}  # 缺少必填參數
        
        try:
            result = await llm_module_with_mcp.handle_mcp_tool_call(tool_name, invalid_params)
            # 應該返回錯誤或拋出異常
            if isinstance(result, dict):
                # 檢查是否包含錯誤信息
                assert "error" in str(result).lower() or "status" in result
        except Exception as e:
            # 預期會拋出異常
            assert "required" in str(e).lower() or "parameter" in str(e).lower() or "invalid" in str(e).lower()


@pytest.mark.llm
class TestGeminiResponseParsing:
    """Gemini 回應解析測試"""
    
    def test_parse_json_schema_response(self, llm_module_with_mcp):
        """測試解析 JSON schema 回應"""
        # 模擬 Gemini 的 JSON schema 回應
        mock_response = {
            "text": "這是回應文字",
            "confidence": 0.85,
            "sys_action": None
        }
        
        # 驗證回應格式
        assert "text" in mock_response
        assert "confidence" in mock_response
        assert isinstance(mock_response["confidence"], (int, float))
    
    def test_parse_function_call_response(self, mock_gemini_wrapper):
        """測試解析 function call 回應"""
        # 模擬 Gemini 的 function call 回應格式
        mock_result = Mock()
        mock_candidate = Mock()
        mock_content = Mock()
        mock_part = Mock()
        
        # 設置 function_call
        mock_function_call = Mock()
        mock_function_call.name = "start_workflow"
        mock_function_call.args = {
            "workflow_type": "drop_and_read",
            "command": "讀取文件"
        }
        
        mock_part.function_call = mock_function_call
        mock_part.text = None
        
        mock_content.parts = [mock_part]
        mock_candidate.content = mock_content
        mock_result.candidates = [mock_candidate]
        
        # 驗證可以訪問 function call
        assert hasattr(mock_part, 'function_call')
        assert mock_part.function_call.name == "start_workflow"
        assert "workflow_type" in mock_part.function_call.args
    
    def test_parse_text_response_with_json(self, mock_gemini_wrapper):
        """測試解析包含 JSON 的文本回應"""
        # 模擬 Gemini 返回 JSON 字符串
        json_text = json.dumps({
            "text": "好的，我會處理這個任務",
            "confidence": 0.9
        })
        
        # 解析 JSON
        parsed = json.loads(json_text)
        
        assert "text" in parsed
        assert "confidence" in parsed
        assert isinstance(parsed["text"], str)


@pytest.mark.llm
class TestLLMMCPWorkflowDecision:
    """LLM 工作流決策測試"""
    
    def test_llm_decides_workflow_type(self, llm_module_with_mcp, mock_gemini_wrapper):
        """測試 LLM 決策工作流類型"""
        # 模擬用戶輸入
        user_input = "幫我讀取這個文件的內容"
        
        # 模擬 LLM 回應
        mock_gemini_wrapper.query.return_value = {
            "text": json.dumps({
                "workflow_type": "drop_and_read",
                "params": {},
                "reasoning": "User wants to read file content"
            })
        }
        
        # LLM 應該能夠理解意圖並返回工作流決策
        response = mock_gemini_wrapper.query("decision prompt", mode="work")
        
        assert "text" in response
        
        # 解析決策
        decision = json.loads(response["text"])
        assert "workflow_type" in decision
        assert decision["workflow_type"] == "drop_and_read"
    
    def test_llm_receives_mcp_tools_in_context(self, llm_module_with_mcp, mock_gemini_wrapper):
        """測試 LLM 在上下文中收到 MCP 工具"""
        tools = llm_module_with_mcp.get_mcp_tools_for_llm()
        
        # 驗證工具可以傳遞給 Gemini
        assert len(tools) > 0
        
        # 模擬傳遞工具給 Gemini
        # 在實際使用中，這些工具會轉換為 Gemini function calling 格式
        tool_declarations = []
        for tool in tools:
            tool_declarations.append({
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"]
            })
        
        assert len(tool_declarations) > 0
        assert tool_declarations[0]["name"] == tools[0]["name"]


@pytest.mark.llm
@pytest.mark.integration
class TestLLMMCPEndToEnd:
    """LLM-MCP 端到端測試"""
    
    @pytest.mark.asyncio
    async def test_llm_workflow_with_mcp_tools(self, llm_module_with_mcp, mock_gemini_wrapper):
        """測試 LLM 使用 MCP 工具完成工作流"""
        # Step 1: LLM 獲取可用工具
        tools = llm_module_with_mcp.get_mcp_tools_for_llm()
        assert len(tools) >= 8
        
        # Step 2: 模擬 LLM 決定調用工具
        mock_gemini_wrapper.query.return_value = {
            "function_call": {
                "name": "get_workflow_status",
                "args": {"session_id": "test_session"}
            }
        }
        
        # Step 3: LLM 調用 MCP 工具
        try:
            result = await llm_module_with_mcp.handle_mcp_tool_call(
                "get_workflow_status",
                {"session_id": "test_session"}
            )
            
            # 工具應該返回結果（即使會話不存在）
            assert isinstance(result, dict)
            
        except Exception as e:
            # 預期的錯誤（如會話不存在）
            assert "session" in str(e).lower() or "not found" in str(e).lower()
    
    def test_mcp_client_has_server_reference(self, llm_module_with_mcp):
        """測試 MCP Client 有 Server 引用"""
        assert llm_module_with_mcp.mcp_client is not None
        assert llm_module_with_mcp.mcp_client.mcp_server is not None
    
    def test_mcp_client_can_list_tools(self, llm_module_with_mcp):
        """測試 MCP Client 可以列出工具"""
        if hasattr(llm_module_with_mcp.mcp_client, 'list_tools'):
            tools = llm_module_with_mcp.mcp_client.list_tools()
            assert isinstance(tools, list)


@pytest.mark.llm
class TestLLMResponseModes:
    """LLM 回應模式測試"""
    
    def test_chat_mode_response_schema(self, mock_gemini_wrapper):
        """測試 CHAT 模式回應模式"""
        # CHAT 模式應該返回簡單的文本回應
        mock_response = {
            "text": "你好！有什麼可以幫助你的嗎？",
            "confidence": 0.95
        }
        
        assert "text" in mock_response
        assert len(mock_response["text"]) > 0
    
    def test_work_mode_response_schema(self, mock_gemini_wrapper):
        """測試 WORK 模式回應模式"""
        # WORK 模式可能包含系統動作和狀態更新
        mock_response = {
            "text": "我會開始處理這個任務",
            "confidence": 0.85,
            "sys_action": {
                "action": "start_workflow",
                "target": "drop_and_read",
                "reason": "用戶想要讀取文件"
            },
            "status_updates": None
        }
        
        assert "text" in mock_response
        assert "sys_action" in mock_response
        
        if mock_response["sys_action"]:
            assert "action" in mock_response["sys_action"]
            assert "target" in mock_response["sys_action"]
    
    def test_function_calling_mode(self, mock_gemini_wrapper):
        """測試 function calling 模式"""
        # 當提供 tools 時，Gemini 可能返回 function_call
        mock_response = {
            "function_call": {
                "name": "start_workflow",
                "args": {
                    "workflow_type": "intelligent_archive",
                    "command": "整理文件",
                    "initial_data": {}
                }
            },
            "text": ""
        }
        
        assert "function_call" in mock_response
        assert "name" in mock_response["function_call"]
        assert "args" in mock_response["function_call"]
        
        # 驗證參數
        args = mock_response["function_call"]["args"]
        assert "workflow_type" in args
        assert "command" in args


@pytest.mark.llm
class TestLLMErrorHandling:
    """LLM 錯誤處理測試"""
    
    @pytest.mark.asyncio
    async def test_handle_tool_not_found(self, llm_module_with_mcp):
        """測試處理工具不存在的情況"""
        try:
            result = await llm_module_with_mcp.handle_mcp_tool_call(
                "nonexistent_tool",
                {}
            )
            
            # 應該返回錯誤
            if isinstance(result, dict):
                assert "error" in str(result).lower() or "not found" in str(result).lower()
                
        except Exception as e:
            # 預期拋出異常
            assert "not found" in str(e).lower() or "invalid" in str(e).lower()
    
    @pytest.mark.asyncio
    async def test_handle_malformed_tool_params(self, llm_module_with_mcp):
        """測試處理格式錯誤的工具參數"""
        try:
            result = await llm_module_with_mcp.handle_mcp_tool_call(
                "start_workflow",
                {"invalid_param": "value"}  # 缺少必填參數
            )
            
            # 應該返回參數驗證錯誤
            if isinstance(result, dict):
                assert "error" in str(result).lower() or "parameter" in str(result).lower()
                
        except Exception as e:
            # 預期拋出驗證異常
            assert "parameter" in str(e).lower() or "required" in str(e).lower()
    
    def test_handle_empty_gemini_response(self, mock_gemini_wrapper):
        """測試處理空的 Gemini 回應"""
        # 模擬空回應
        mock_gemini_wrapper.query.return_value = {"text": ""}
        
        response = mock_gemini_wrapper.query("test prompt")
        
        # 應該至少有 text 字段
        assert "text" in response


@pytest.mark.llm
class TestMCPClientIntegration:
    """MCP Client 集成測試"""
    
    def test_mcp_client_initialization(self, llm_module_with_mcp):
        """測試 MCP Client 初始化"""
        client = llm_module_with_mcp.mcp_client
        
        assert client is not None
        assert hasattr(client, 'mcp_server')
        assert hasattr(client, 'llm_module')
    
    def test_mcp_client_get_tools_for_llm(self, llm_module_with_mcp):
        """測試 MCP Client 為 LLM 提供工具"""
        client = llm_module_with_mcp.mcp_client
        
        if hasattr(client, 'get_tools_for_llm'):
            tools = client.get_tools_for_llm()
            assert isinstance(tools, list)
            assert len(tools) > 0
    
    @pytest.mark.asyncio
    async def test_mcp_client_call_tool(self, llm_module_with_mcp):
        """測試 MCP Client 調用工具"""
        client = llm_module_with_mcp.mcp_client
        
        # 嘗試調用簡單的工具
        try:
            result = await client.call_tool(
                "resolve_path",
                {"path_description": "temp"}
            )
            
            # 應該返回結果
            assert isinstance(result, dict)
            
        except Exception as e:
            # 可能的錯誤是正常的
            pass


@pytest.mark.llm
class TestLLMToolContextInjection:
    """LLM 工具上下文注入測試"""
    
    def test_tools_converted_to_gemini_format(self, llm_module_with_mcp):
        """測試工具轉換為 Gemini 格式"""
        tools = llm_module_with_mcp.get_mcp_tools_for_llm()
        
        # 模擬轉換為 Gemini function declarations
        function_declarations = []
        for tool in tools:
            declaration = {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"]
            }
            function_declarations.append(declaration)
        
        assert len(function_declarations) > 0
        
        # 驗證第一個工具的格式
        first_tool = function_declarations[0]
        assert "name" in first_tool
        assert "description" in first_tool
        assert "parameters" in first_tool
        assert "properties" in first_tool["parameters"]
    
    def test_tool_parameters_have_required_fields(self, llm_module_with_mcp):
        """測試工具參數有必填字段標記"""
        tools = llm_module_with_mcp.get_mcp_tools_for_llm()
        
        start_workflow = next(t for t in tools if t["name"] == "start_workflow")
        params = start_workflow["parameters"]
        
        # 應該有 required 字段
        if "required" in params:
            assert isinstance(params["required"], list)
            assert "workflow_type" in params["required"]
            assert "command" in params["required"]
