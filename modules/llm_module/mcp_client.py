"""
MCP Client for LLM Module

LLM 模組的 MCP 客戶端，負責:
1. 呼叫 MCP Server 提供的工具
2. 解析 LLM 的工具呼叫請求
3. 將 MCP 工具整合到 LLM function calling
4. 處理 MCP 響應並轉換為 LLM 可理解的格式
"""

from typing import Dict, Any, Optional, List
import json

from utils.debug_helper import debug_log, info_log, error_log
from modules.sys_module.mcp_server.protocol_handlers import (
    MCPRequest, MCPResponse, MCPErrorCode
)


class MCPClient:
    """
    MCP 客戶端
    
    用於 LLM 模組與 SYS 模組的 MCP Server 通訊。
    """
    
    def __init__(self, mcp_server=None, llm_module=None):
        """
        初始化 MCP 客戶端
        
        Args:
            mcp_server: MCP Server 實例
            llm_module: LLM 模組實例（用於獲取當前會話信息）
        """
        self.mcp_server = mcp_server
        self.llm_module = llm_module  # ✅ 保存 LLM 模組引用以獲取會話信息
        self._request_id_counter = 0
        
        debug_log(2, "[MCP Client] MCP 客戶端初始化完成")
    
    def set_mcp_server(self, mcp_server):
        """設置 MCP Server 實例"""
        self.mcp_server = mcp_server
        debug_log(2, "[MCP Client] MCP Server 已設置")
    
    def _next_request_id(self) -> int:
        """生成下一個請求 ID"""
        self._request_id_counter += 1
        return self._request_id_counter
    
    async def call_tool(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        呼叫 MCP 工具
        
        Args:
            tool_name: 工具名稱
            params: 工具參數
            
        Returns:
            工具執行結果
        """
        if self.mcp_server is None:
            error_log("[MCP Client] MCP Server 未設置")
            return {
                "status": "error",
                "message": "MCP Server 未初始化",
                "error": "MCP_SERVER_NOT_SET"
            }
        
        # 創建 MCP 請求
        request = MCPRequest(
            jsonrpc="2.0",
            method=tool_name,
            params=params,
            id=self._next_request_id()
        )
        
        debug_log(3, f"[MCP Client] 呼叫工具: {tool_name}, 請求ID: {request.id}")
        
        try:
            # 呼叫 MCP Server
            response = await self.mcp_server.handle_request(request)
            
            # 解析響應
            if response.is_success():
                debug_log(3, f"[MCP Client] 工具呼叫成功: {tool_name}")
                return {
                    "status": "success",
                    "data": response.result,
                    "request_id": response.id
                }
            else:
                error_log(f"[MCP Client] 工具呼叫失敗: {response.error.message}")
                return {
                    "status": "error",
                    "message": response.error.message,
                    "error_code": response.error.code,
                    "error_data": response.error.data,
                    "request_id": response.id
                }
        
        except Exception as e:
            error_log(f"[MCP Client] 工具呼叫異常: {e}")
            return {
                "status": "error",
                "message": f"工具呼叫異常: {str(e)}",
                "error": "EXCEPTION"
            }
    
    def get_tools_for_llm(self) -> List[Dict[str, Any]]:
        """
        取得工具規範供 LLM 使用
        
        Returns:
            適合 LLM function calling 的工具規範列表
        """
        if self.mcp_server is None:
            error_log("[MCP Client] MCP Server 未設置，無法取得工具規範")
            return []
        
        try:
            tools_spec = self.mcp_server.get_tools_spec_for_llm()
            debug_log(3, f"[MCP Client] 取得 {len(tools_spec)} 個工具規範")
            return tools_spec
        
        except Exception as e:
            error_log(f"[MCP Client] 取得工具規範失敗: {e}")
            return []
    
    def parse_llm_tool_call(self, tool_call: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """
        解析 LLM 的工具呼叫請求
        
        Args:
            tool_call: LLM 的工具呼叫物件，格式:
                {
                    "name": "tool_name",
                    "arguments": {...}  # 或 "args": {...} (Gemini 格式)
                }
        
        Returns:
            (工具名稱, 工具參數)
        """
        tool_name = tool_call.get("name", "")
        
        # ✅ 支持兩種參數格式: "arguments" (標準) 或 "args" (Gemini)
        arguments = tool_call.get("arguments") or tool_call.get("args", {})
        
        # 如果 arguments 是字串，嘗試解析為 JSON
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as e:
                error_log(f"[MCP Client] 解析工具參數失敗: {e}")
                arguments = {}
        
        debug_log(3, f"[MCP Client] 解析工具調用: tool={tool_name}, args={arguments}")
        return tool_name, arguments
    
    async def handle_llm_function_call(self, function_call: Dict[str, Any]) -> Dict[str, Any]:
        """
        處理 LLM 的 function calling
        
        完整流程:
        1. 解析 LLM 的工具呼叫
        2. 自動注入系統級參數（如 session_id）
        3. 呼叫 MCP 工具
        4. 將結果格式化為 LLM 可理解的格式
        
        Args:
            function_call: LLM 的 function call 物件
            
        Returns:
            格式化後的結果
        """
        tool_name, params = self.parse_llm_tool_call(function_call)
        
        info_log(f"[MCP Client] 處理 LLM function call: {tool_name}")
        
        # ✅ 自動注入 session_id（如果工具需要且 LLM 未提供）
        params = self._inject_system_params(tool_name, params)
        
        # 呼叫 MCP 工具
        result = await self.call_tool(tool_name, params)
        
        # 格式化結果供 LLM 理解
        if result["status"] == "success":
            return {
                "tool_name": tool_name,
                "status": "success",
                "content": result["data"],
                "formatted_message": self._format_success_message(tool_name, result["data"])
            }
        else:
            return {
                "tool_name": tool_name,
                "status": "error",
                "error": result.get("message", "未知錯誤"),
                "formatted_message": self._format_error_message(tool_name, result.get("message", "未知錯誤"))
            }
    
    def _format_success_message(self, tool_name: str, data: Dict[str, Any]) -> str:
        """
        格式化成功訊息供 LLM 理解
        
        Args:
            tool_name: 工具名稱
            data: 工具執行結果資料
            
        Returns:
            格式化後的訊息
        """
        # 根據工具類型格式化不同的訊息
        if tool_name == "start_workflow":
            session_id = data.get("data", {}).get("session_id", "unknown")
            workflow_type = data.get("data", {}).get("workflow_type", "unknown")
            return f"工作流 '{workflow_type}' 已成功啟動 (會話ID: {session_id})"
        
        elif tool_name == "get_workflow_status":
            status_data = data.get("data", {})
            workflow_type = status_data.get("workflow_type", "unknown")
            status = status_data.get("status", "unknown")
            progress = status_data.get("progress", 0.0)
            return f"工作流 '{workflow_type}' 狀態: {status}, 進度: {progress*100:.1f}%"
        
        elif tool_name == "review_step":
            step_data = data.get("data", {})
            step_id = step_data.get("step_id", "unknown")
            status = step_data.get("status", "unknown")
            message = step_data.get("message", "")
            return f"步驟 '{step_id}' 執行結果: {status}\n{message}"
        
        elif tool_name == "approve_step":
            return data.get("message", "步驟已批准，工作流繼續執行")
        
        elif tool_name == "cancel_workflow":
            return data.get("message", "工作流已取消")
        
        else:
            # 通用格式
            message = data.get("message", "")
            if message:
                return message
            return json.dumps(data, ensure_ascii=False, indent=2)
    
    def _format_error_message(self, tool_name: str, error: str) -> str:
        """
        格式化錯誤訊息供 LLM 理解
        
        Args:
            tool_name: 工具名稱
            error: 錯誤訊息
            
        Returns:
            格式化後的錯誤訊息
        """
        return f"執行工具 '{tool_name}' 時發生錯誤: {error}"
    
    def _inject_system_params(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        自動注入系統級參數
        
        某些參數（如 session_id）是系統級的全局狀態，不應該讓 LLM 提供。
        這個方法會自動注入這些參數，如果 LLM 已經提供則保留 LLM 的值。
        
        Args:
            tool_name: 工具名稱
            params: LLM 提供的參數
            
        Returns:
            注入系統參數後的參數字典
        """
        # 需要 session_id 的工具列表
        tools_requiring_session = {
            'review_step', 'approve_step', 'modify_step', 
            'cancel_workflow', 'get_workflow_status',
            'provide_workflow_input'  # ✅ 工作流輸入也需要自動注入 session_id
        }
        
        # 如果工具需要 session_id 且 LLM 未提供（或提供了錯誤的）
        if tool_name in tools_requiring_session:
            # 從 LLM 模組獲取當前會話 ID
            if self.llm_module and hasattr(self.llm_module, 'session_info'):
                session_info = self.llm_module.session_info
                current_session_id = session_info.get('session_id') if session_info else None
                
                if current_session_id:
                    # ✅ 自動注入或覆蓋 session_id
                    if 'session_id' not in params or not params['session_id']:
                        debug_log(2, f"[MCP Client] 自動注入 session_id: {current_session_id}")
                        params['session_id'] = current_session_id
                    elif params['session_id'] != current_session_id:
                        # LLM 提供了錯誤的 session_id，覆蓋它
                        debug_log(2, f"[MCP Client] 覆蓋錯誤的 session_id: {params['session_id']} -> {current_session_id}")
                        params['session_id'] = current_session_id
        
        return params
    
    def is_workflow_tool(self, tool_name: str) -> bool:
        """
        判斷是否為工作流控制工具
        
        Returns:
            True 如果是工作流控制工具
        """
        workflow_tools = {
            "start_workflow",
            "review_step",
            "approve_step",
            "modify_step",
            "cancel_workflow",
            "get_workflow_status"
        }
        return tool_name in workflow_tools
    
    def get_tools_as_gemini_format(self) -> Optional[List[Dict[str, Any]]]:
        """
        取得 Gemini Function Calling 格式的工具規範
        
        將 MCP 工具規範轉換為 Gemini API 所需的格式
        
        Returns:
            Gemini tools 格式的列表，或 None 如果無工具可用
        """
        if self.mcp_server is None:
            return None
        
        try:
            mcp_tools = self.mcp_server.get_tools_spec_for_llm()
            if not mcp_tools:
                return None
            
            # 轉換為 Gemini Function Calling 格式
            # ✅ Gemini 要求單一 dict 包含所有 function_declarations
            function_declarations = []
            for tool in mcp_tools:
                function_declarations.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {
                        "type": "object",
                        "properties": {}
                    })
                })
            
            gemini_tools = [{"function_declarations": function_declarations}]
            debug_log(2, f"[MCP Client] 轉換了 {len(function_declarations)} 個工具為 Gemini 格式")
            
            # 🔍 DEBUG: 顯示完整的工具格式
            import json
            debug_log(3, f"[MCP Client] Gemini 工具格式:\n{json.dumps(gemini_tools, indent=2, ensure_ascii=False)}")
            
            return gemini_tools
        
        except Exception as e:
            error_log(f"[MCP Client] 轉換工具規範為 Gemini 格式失敗: {e}")
            return None
