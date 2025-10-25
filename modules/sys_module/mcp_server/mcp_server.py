"""
MCP Server - 核心伺服器實現

基於 JSON-RPC 2.0 的輕量級 MCP 伺服器，負責:
1. 工具註冊與管理
2. 請求處理與路由
3. 工作流控制工具實現
4. 資源提供
"""

from typing import Dict, Any, Optional, Callable
import asyncio
from datetime import datetime

from utils.debug_helper import debug_log, info_log, error_log
from .protocol_handlers import (
    MCPRequest, MCPResponse, MCPError, MCPErrorCode,
    create_success_response, create_error_response, create_notification
)
from .tool_definitions import MCPTool, ToolParameter, ToolParameterType, ToolResult
from .resource_providers import WorkflowResourceProvider, WorkflowResource, StepResultResource


class MCPServer:
    """
    MCP Server 核心實現
    
    提供工具註冊、請求處理、工作流控制等功能。
    """
    
    def __init__(self, sys_module=None):
        """
        初始化 MCP Server
        
        Args:
            sys_module: SYS 模組實例，用於存取工作流引擎
        """
        self.sys_module = sys_module
        self.tools: Dict[str, MCPTool] = {}
        self.resource_provider = WorkflowResourceProvider()
        
        # 註冊核心工作流控制工具
        self._register_core_tools()
        
        debug_log(2, "[MCP] MCP Server 初始化完成")
    
    def _register_core_tools(self):
        """註冊核心工作流控制工具"""
        
        # 1. start_workflow - 啟動工作流
        self.register_tool(MCPTool(
            name="start_workflow",
            description="啟動新的工作流程（用於複雜指令處理）",
            parameters=[
                ToolParameter(
                    name="workflow_type",
                    type=ToolParameterType.STRING,
                    description="工作流程類型，例如 drop_and_read, intelligent_archive, file_processing 等",
                    required=True
                ),
                ToolParameter(
                    name="command",
                    type=ToolParameterType.STRING,
                    description="觸發工作流程的原始指令",
                    required=True
                ),
                ToolParameter(
                    name="initial_data",
                    type=ToolParameterType.OBJECT,
                    description="初始化工作流程的資料",
                    required=False
                ),
            ],
            handler=self._handle_start_workflow
        ))
        
        # 2. review_step - 審核步驟執行結果
        self.register_tool(MCPTool(
            name="review_step",
            description="審核工作流程步驟的執行結果，決定是否繼續",
            parameters=[
                ToolParameter(
                    name="session_id",
                    type=ToolParameterType.STRING,
                    description="工作流程會話 ID",
                    required=True
                ),
                ToolParameter(
                    name="step_id",
                    type=ToolParameterType.STRING,
                    description="步驟 ID",
                    required=True
                ),
            ],
            handler=self._handle_review_step
        ))
        
        # 3. approve_step - 批准並繼續下一步驟
        self.register_tool(MCPTool(
            name="approve_step",
            description="批准當前步驟結果並繼續執行下一步驟",
            parameters=[
                ToolParameter(
                    name="session_id",
                    type=ToolParameterType.STRING,
                    description="工作流程會話 ID",
                    required=True
                ),
                ToolParameter(
                    name="continue_data",
                    type=ToolParameterType.OBJECT,
                    description="傳遞給下一步驟的資料",
                    required=False
                ),
            ],
            handler=self._handle_approve_step
        ))
        
        # 4. modify_step - 修改步驟參數後重新執行
        self.register_tool(MCPTool(
            name="modify_step",
            description="修改當前步驟的參數並重新執行",
            parameters=[
                ToolParameter(
                    name="session_id",
                    type=ToolParameterType.STRING,
                    description="工作流程會話 ID",
                    required=True
                ),
                ToolParameter(
                    name="modifications",
                    type=ToolParameterType.OBJECT,
                    description="要修改的參數",
                    required=True
                ),
            ],
            handler=self._handle_modify_step
        ))
        
        # 5. cancel_workflow - 取消工作流
        self.register_tool(MCPTool(
            name="cancel_workflow",
            description="取消進行中的工作流程",
            parameters=[
                ToolParameter(
                    name="session_id",
                    type=ToolParameterType.STRING,
                    description="要取消的工作流程會話 ID",
                    required=True
                ),
                ToolParameter(
                    name="reason",
                    type=ToolParameterType.STRING,
                    description="取消原因",
                    required=False
                ),
            ],
            handler=self._handle_cancel_workflow
        ))
        
        # 6. get_workflow_status - 查詢工作流狀態
        self.register_tool(MCPTool(
            name="get_workflow_status",
            description="查詢工作流程的當前狀態與進度",
            parameters=[
                ToolParameter(
                    name="session_id",
                    type=ToolParameterType.STRING,
                    description="工作流程會話 ID",
                    required=True
                ),
            ],
            handler=self._handle_get_workflow_status
        ))
        
        debug_log(2, "[MCP] 已註冊 6 個核心工作流控制工具")
    
    def register_tool(self, tool: MCPTool):
        """
        註冊工具
        
        Args:
            tool: 工具定義
        """
        self.tools[tool.name] = tool
        debug_log(3, f"[MCP] 註冊工具: {tool.name}")
    
    def unregister_tool(self, tool_name: str):
        """
        取消註冊工具
        
        Args:
            tool_name: 工具名稱
        """
        if tool_name in self.tools:
            del self.tools[tool_name]
            debug_log(3, f"[MCP] 取消註冊工具: {tool_name}")
    
    def get_tool(self, tool_name: str) -> Optional[MCPTool]:
        """取得工具定義"""
        return self.tools.get(tool_name)
    
    def list_tools(self) -> list[MCPTool]:
        """列出所有工具"""
        return list(self.tools.values())
    
    def get_tools_spec_for_llm(self) -> list[Dict[str, Any]]:
        """
        取得工具規範供 LLM 使用
        
        Returns:
            適合 LLM function calling 的工具規範列表
        """
        return [tool.to_llm_spec() for tool in self.tools.values()]
    
    async def handle_request(self, request: MCPRequest) -> MCPResponse:
        """
        處理 MCP 請求
        
        Args:
            request: MCP 請求物件
            
        Returns:
            MCP 響應物件
        """
        debug_log(3, f"[MCP] 收到請求: {request.method}")
        
        # 檢查工具是否存在
        tool = self.get_tool(request.method)
        if tool is None:
            error_log(f"[MCP] 工具不存在: {request.method}")
            return create_error_response(
                request.id,
                MCPErrorCode.METHOD_NOT_FOUND,
                f"工具 '{request.method}' 不存在"
            )
        
        # 執行工具
        try:
            result = await tool.execute(request.params)
            
            if result.status == "success":
                return create_success_response(request.id, {
                    "status": result.status,
                    "message": result.message,
                    "data": result.data
                })
            elif result.status == "pending":
                return create_success_response(request.id, {
                    "status": result.status,
                    "message": result.message,
                    "data": result.data
                })
            else:  # error
                return create_error_response(
                    request.id,
                    MCPErrorCode.INTERNAL_ERROR,
                    result.message,
                    {"error_detail": result.error_detail, "data": result.data}
                )
        
        except Exception as e:
            error_log(f"[MCP] 工具執行異常: {e}")
            return create_error_response(
                request.id,
                MCPErrorCode.INTERNAL_ERROR,
                f"工具執行失敗: {str(e)}"
            )
    
    # ========== 核心工具處理函數 ==========
    
    async def _handle_start_workflow(self, params: Dict[str, Any]) -> ToolResult:
        """處理 start_workflow 工具"""
        workflow_type = params["workflow_type"]
        command = params["command"]
        initial_data = params.get("initial_data", {})
        
        if self.sys_module is None:
            return ToolResult.error("SYS 模組未初始化")
        
        try:
            # 呼叫 SYS 模組啟動工作流
            result = await self.sys_module.start_workflow_async(
                workflow_type=workflow_type,
                command=command,
                initial_data=initial_data
            )
            
            if result.get("status") == "error":
                return ToolResult.error(result.get("message", "工作流啟動失敗"))
            
            session_id = result.get("session_id")
            
            # 註冊工作流資源
            self.resource_provider.register_workflow(WorkflowResource(
                session_id=session_id,
                workflow_type=workflow_type,
                status="running",
                progress=0.0,
                metadata={"command": command}
            ))
            
            return ToolResult.success(
                message=f"工作流 '{workflow_type}' 已啟動",
                data=result
            )
        
        except Exception as e:
            error_log(f"[MCP] start_workflow 失敗: {e}")
            return ToolResult.error(f"啟動工作流失敗: {str(e)}")
    
    async def _handle_review_step(self, params: Dict[str, Any]) -> ToolResult:
        """處理 review_step 工具"""
        session_id = params["session_id"]
        step_id = params["step_id"]
        
        # 從資源提供者取得步驟結果
        step_result = self.resource_provider.get_step_result(session_id, step_id)
        
        if step_result is None:
            return ToolResult.error(f"找不到步驟結果: {session_id}/{step_id}")
        
        return ToolResult.success(
            message="步驟結果已取得",
            data={
                "session_id": session_id,
                "step_id": step_id,
                "status": step_result.status,
                "message": step_result.message,
                "data": step_result.data,
                "timestamp": step_result.timestamp
            }
        )
    
    async def _handle_approve_step(self, params: Dict[str, Any]) -> ToolResult:
        """處理 approve_step 工具"""
        session_id = params["session_id"]
        continue_data = params.get("continue_data", {})
        
        if self.sys_module is None:
            return ToolResult.error("SYS 模組未初始化")
        
        try:
            # 調用 SYS 模組處理 LLM 審核響應（批准）
            result = await self.sys_module.handle_llm_review_response_async(
                session_id=session_id,
                action="approve",
                modified_params=continue_data if continue_data else None
            )
            
            # 更新工作流資源
            workflow = self.resource_provider.get_workflow(session_id)
            if workflow and result.get("status") != "error":
                status = result.get("status", "running")
                self.resource_provider.update_workflow(session_id, {
                    "status": status,
                    "current_step": result.get("data", {}).get("current_step")
                })
            
            return ToolResult.success(
                message=result.get("message", "步驟已批准，工作流繼續執行"),
                data=result
            )
        
        except Exception as e:
            error_log(f"[MCP] approve_step 失敗: {e}")
            return ToolResult.error(f"批准步驟失敗: {str(e)}")
    
    async def _handle_modify_step(self, params: Dict[str, Any]) -> ToolResult:
        """處理 modify_step 工具"""
        session_id = params["session_id"]
        modifications = params["modifications"]
        
        if self.sys_module is None:
            return ToolResult.error("SYS 模組未初始化")
        
        try:
            # 調用 SYS 模組處理 LLM 審核響應（修改）
            result = await self.sys_module.handle_llm_review_response_async(
                session_id=session_id,
                action="modify",
                modified_params=modifications
            )
            
            return ToolResult.success(
                message=result.get("message", "步驟已修改並重新執行"),
                data=result
            )
        
        except Exception as e:
            error_log(f"[MCP] modify_step 失敗: {e}")
            return ToolResult.error(f"修改步驟失敗: {str(e)}")
    
    async def _handle_cancel_workflow(self, params: Dict[str, Any]) -> ToolResult:
        """處理 cancel_workflow 工具"""
        session_id = params["session_id"]
        reason = params.get("reason", "LLM 取消")
        
        if self.sys_module is None:
            return ToolResult.error("SYS 模組未初始化")
        
        try:
            # 檢查工作流是否正在等待 LLM 審核
            engine = self.sys_module.workflow_engines.get(session_id)
            
            if engine and engine.is_awaiting_llm_review():
                # 如果正在等待審核，通過審核響應方法取消
                result = await self.sys_module.handle_llm_review_response_async(
                    session_id=session_id,
                    action="cancel"
                )
            else:
                # 否則直接取消工作流
                result = await self.sys_module.cancel_workflow_async(
                    session_id=session_id,
                    reason=reason
                )
            
            # 更新工作流資源
            self.resource_provider.update_workflow(session_id, {
                "status": "cancelled"
            })
            
            return ToolResult.success(
                message=f"工作流已取消: {reason}",
                data=result
            )
        
        except Exception as e:
            error_log(f"[MCP] cancel_workflow 失敗: {e}")
            return ToolResult.error(f"取消工作流失敗: {str(e)}")
    
    async def _handle_get_workflow_status(self, params: Dict[str, Any]) -> ToolResult:
        """處理 get_workflow_status 工具"""
        session_id = params["session_id"]
        
        # 從資源提供者取得工作流狀態
        workflow = self.resource_provider.get_workflow(session_id)
        
        if workflow is None:
            return ToolResult.error(f"找不到工作流: {session_id}")
        
        # 取得所有步驟結果
        step_results = self.resource_provider.get_all_step_results(session_id)
        
        return ToolResult.success(
            message="工作流狀態已取得",
            data={
                "session_id": workflow.session_id,
                "workflow_type": workflow.workflow_type,
                "current_step": workflow.current_step,
                "status": workflow.status,
                "progress": workflow.progress,
                "metadata": workflow.metadata,
                "step_count": len(step_results)
            }
        )
    
    def notify_step_completed(self, session_id: str, step_id: str, step_result: Dict[str, Any]):
        """
        通知步驟完成 (供 SYS 模組呼叫)
        
        將步驟結果儲存到資源提供者，供 LLM 審核使用。
        
        Args:
            session_id: 工作流會話 ID
            step_id: 步驟 ID
            step_result: 步驟結果
        """
        result_resource = StepResultResource(
            session_id=session_id,
            step_id=step_id,
            status=step_result.get("status", "unknown"),
            message=step_result.get("message", ""),
            data=step_result.get("data", {}),
            timestamp=datetime.now().isoformat()
        )
        
        self.resource_provider.add_step_result(session_id, result_resource)
        debug_log(3, f"[MCP] 步驟完成通知: {session_id}/{step_id}")
