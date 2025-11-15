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
        
        # 1. start_workflow - Start a workflow (DEPRECATED - use direct workflow tools instead)
        self.register_tool(MCPTool(
            name="start_workflow",
            description="[DEPRECATED] Generic workflow starter. PREFER using direct workflow tools (e.g., intelligent_archive, drop_and_read) instead, as they provide better parameter extraction guidance. Only use this if the specific workflow tool is unavailable.",
            parameters=[
                ToolParameter(
                    name="workflow_type",
                    type=ToolParameterType.STRING,
                    description="Workflow type, e.g., drop_and_read, intelligent_archive, summarize_tag, translate_document",
                    required=True
                ),
                ToolParameter(
                    name="command",
                    type=ToolParameterType.STRING,
                    description="Original command that triggered this workflow",
                    required=True
                ),
                ToolParameter(
                    name="initial_data",
                    type=ToolParameterType.OBJECT,
                    description="Initial data for workflow initialization",
                    required=False
                ),
            ],
            handler=self._handle_start_workflow
        ))
        
        # 2. review_step - Review step execution result
        self.register_tool(MCPTool(
            name="review_step",
            description="Review workflow step execution result and decide whether to continue",
            parameters=[
                ToolParameter(
                    name="session_id",
                    type=ToolParameterType.STRING,
                    description="Workflow session ID",
                    required=True
                ),
                ToolParameter(
                    name="step_id",
                    type=ToolParameterType.STRING,
                    description="Step ID",
                    required=True
                ),
            ],
            handler=self._handle_review_step
        ))
        
        # 3. approve_step - Approve and continue to next step
        self.register_tool(MCPTool(
            name="approve_step",
            description="Approve current step result and continue to next step",
            parameters=[
                ToolParameter(
                    name="session_id",
                    type=ToolParameterType.STRING,
                    description="Workflow session ID",
                    required=True
                ),
                ToolParameter(
                    name="continue_data",
                    type=ToolParameterType.OBJECT,
                    description="Data to pass to the next step",
                    required=False
                ),
            ],
            handler=self._handle_approve_step
        ))
        
        # 4. modify_step - Modify step parameters and re-execute
        self.register_tool(MCPTool(
            name="modify_step",
            description="Modify current step parameters and re-execute",
            parameters=[
                ToolParameter(
                    name="session_id",
                    type=ToolParameterType.STRING,
                    description="Workflow session ID",
                    required=True
                ),
                ToolParameter(
                    name="modifications",
                    type=ToolParameterType.OBJECT,
                    description="Parameters to modify",
                    required=True
                ),
            ],
            handler=self._handle_modify_step
        ))
        
        # 5. cancel_workflow - Cancel workflow
        self.register_tool(MCPTool(
            name="cancel_workflow",
            description="Cancel an ongoing workflow",
            parameters=[
                ToolParameter(
                    name="session_id",
                    type=ToolParameterType.STRING,
                    description="Workflow session ID to cancel",
                    required=True
                ),
                ToolParameter(
                    name="reason",
                    type=ToolParameterType.STRING,
                    description="Reason for cancellation",
                    required=False
                ),
            ],
            handler=self._handle_cancel_workflow
        ))
        
        # 6. get_workflow_status - Query workflow status
        self.register_tool(MCPTool(
            name="get_workflow_status",
            description="Query current status and progress of a workflow",
            parameters=[
                ToolParameter(
                    name="session_id",
                    type=ToolParameterType.STRING,
                    description="Workflow session ID",
                    required=True
                ),
            ],
            handler=self._handle_get_workflow_status
        ))
        
        # 7. provide_workflow_input - Provide user input for workflow Input Step
        self.register_tool(MCPTool(
            name="provide_workflow_input",
            description="Provide user input for workflow Input Step. Can judge delegation intent and trigger fallback.",
            parameters=[
                ToolParameter(
                    name="session_id",
                    type=ToolParameterType.STRING,
                    description="Workflow session ID",
                    required=True
                ),
                ToolParameter(
                    name="user_input",
                    type=ToolParameterType.STRING,
                    description="User's input text for the Input Step",
                    required=True
                ),
                ToolParameter(
                    name="use_fallback",
                    type=ToolParameterType.BOOLEAN,
                    description="True if user delegated decision (e.g., 'you decide', '幫我選', '隨便'). False if user provided explicit value.",
                    required=False
                ),
            ],
            handler=self._handle_provide_workflow_input
        ))
        
        # 8. resolve_path - Resolve natural language path descriptions to actual system paths
        self.register_tool(MCPTool(
            name="resolve_path",
            description="Resolve natural language path descriptions (e.g., 'd drive root', 'documents folder', 'desktop') to actual system paths. Returns the resolved absolute path and whether it exists.",
            parameters=[
                ToolParameter(
                    name="path_description",
                    type=ToolParameterType.STRING,
                    description="Natural language path description or partial path (e.g., 'd drive', 'documents', 'C:\\temp', '~/downloads')",
                    required=True
                ),
                ToolParameter(
                    name="create_if_missing",
                    type=ToolParameterType.BOOLEAN,
                    description="If True, create the directory if it doesn't exist (default: False)",
                    required=False
                ),
            ],
            handler=self._handle_resolve_path
        ))
        
        debug_log(2, "[MCP] 已註冊 8 個核心工作流控制工具")
    
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
        debug_log(3, f"[MCP] 請求參數: {request.params}")
        
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
            result_status = result.get("status")
            
            # ✅ 工作流已啟動時註冊資源（completed 不需要註冊）
            if result_status in ["started", "success", "submitted", "pending"] and session_id:
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
                next_step = result.get("data", {}).get("next_step")
                self.resource_provider.update_workflow(session_id, {
                    "status": status,
                    "current_step": next_step
                })
                
                # ✅ 不在這裡執行下一步！
                # SystemLoop 會在 OUTPUT_LAYER_COMPLETE 事件中檢測處理步驟並觸發執行
                if next_step and result.get("data", {}).get("approved"):
                    debug_log(2, f"[MCP] 批准後移至下一步 {next_step}，等待 SystemLoop 觸發執行")
            
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
    
    async def _handle_provide_workflow_input(self, params: Dict[str, Any]) -> ToolResult:
        """處理 provide_workflow_input 工具 - 讓 LLM 提供並判斷用戶輸入"""
        session_id = params["session_id"]
        user_input = params["user_input"]
        use_fallback = params.get("use_fallback", False)
        
        if self.sys_module is None:
            return ToolResult.error("SYS 模組未初始化")
        
        try:
            # 如果 LLM 判斷用戶是委託意圖,使用空輸入觸發 fallback
            actual_input = "" if use_fallback else user_input
            
            debug_log(2, f"[MCP] provide_workflow_input: use_fallback={use_fallback}, input={'<empty>' if use_fallback else user_input[:50]}")
            
            # 調用 SYS 模組繼續工作流並傳入輸入
            result = await self.sys_module.continue_workflow_async(
                session_id=session_id,
                user_input=actual_input
            )
            
            if result.get("status") == "error":
                return ToolResult.error(result.get("message", "提供輸入失敗"))
            
            # 更新工作流資源
            workflow = self.resource_provider.get_workflow(session_id)
            if workflow:
                status = result.get("status", "running")
                if status == "completed":
                    self.resource_provider.update_workflow(session_id, {"status": "completed"})
                elif status == "cancelled":
                    self.resource_provider.update_workflow(session_id, {"status": "cancelled"})
            
            # ✅ 簡化返回格式，只保留 LLM 需要的關鍵信息
            simplified_result = {
                "status": result.get("status"),
                "session_id": session_id,
                "requires_input": result.get("requires_input", False),
                "prompt": result.get("prompt", ""),
                "message": result.get("message", "")
            }
            
            # 只在需要下一步輸入時提供步驟信息
            if result.get("requires_input") and "step_info" in result:
                step_info = result["step_info"]
                simplified_result["step_info"] = {
                    "current_step": {
                        "step_id": step_info.get("current_step", {}).get("step_id"),
                        "step_type": step_info.get("current_step", {}).get("step_type"),
                        "prompt": step_info.get("current_step", {}).get("prompt"),
                        "description": step_info.get("current_step", {}).get("description")
                    },
                    "previous_step_result": {
                        "success": step_info.get("previous_step_result", {}).get("success"),
                        "message": step_info.get("previous_step_result", {}).get("message")
                    }
                }
            
            return ToolResult.success(
                message=f"輸入已處理 (fallback={use_fallback})",
                data=simplified_result
            )
        
        except Exception as e:
            error_log(f"[MCP] provide_workflow_input 失敗: {e}")
            return ToolResult.error(f"提供工作流輸入失敗: {str(e)}")
    
    async def _handle_resolve_path(self, params: Dict[str, Any]) -> ToolResult:
        """處理 resolve_path 工具 - 解析自然語言路徑描述為實際系統路徑"""
        import os
        from pathlib import Path
        
        path_description = params["path_description"].strip()
        create_if_missing = params.get("create_if_missing", False)
        
        try:
            debug_log(2, f"[MCP] resolve_path: 解析路徑描述 '{path_description}'")
            
            # 解析常見的自然語言路徑描述
            resolved_path = None
            description_lower = path_description.lower()
            
            # 1. 檢查是否已經是完整路徑
            if os.path.isabs(path_description):
                resolved_path = path_description
                debug_log(2, f"[MCP] 檢測到絕對路徑: {resolved_path}")
            
            # 2. Windows 驅動器簡稱 (d drive, c drive, etc.)
            elif 'd drive' in description_lower or 'd:' in description_lower or description_lower == 'd\\':
                resolved_path = "D:\\"
            elif 'c drive' in description_lower or 'c:' in description_lower or description_lower == 'c\\':
                resolved_path = "C:\\"
            elif 'e drive' in description_lower:
                resolved_path = "E:\\"
            
            # 3. 常見系統文件夾
            elif 'desktop' in description_lower or '桌面' in description_lower:
                resolved_path = str(Path.home() / "Desktop")
            elif 'document' in description_lower or '文件' in description_lower or '文檔' in description_lower:
                resolved_path = str(Path.home() / "Documents")
            elif 'download' in description_lower or '下載' in description_lower:
                resolved_path = str(Path.home() / "Downloads")
            elif 'picture' in description_lower or 'photo' in description_lower or '圖片' in description_lower:
                resolved_path = str(Path.home() / "Pictures")
            elif 'music' in description_lower or '音樂' in description_lower:
                resolved_path = str(Path.home() / "Music")
            elif 'video' in description_lower or '影片' in description_lower:
                resolved_path = str(Path.home() / "Videos")
            elif 'home' in description_lower or '主目錄' in description_lower or description_lower.startswith('~'):
                resolved_path = str(Path.home())
            
            # 4. 相對路徑（從用戶主目錄）
            elif description_lower.startswith('./') or description_lower.startswith('.\\'):
                resolved_path = str(Path.home() / path_description[2:])
            
            # 5. 如果無法解析，嘗試作為相對於主目錄的路徑
            else:
                # 移除常見的介詞和冠詞
                cleaned = path_description.replace('my ', '').replace('the ', '').strip()
                resolved_path = str(Path.home() / cleaned)
                debug_log(2, f"[MCP] 嘗試作為相對路徑: {resolved_path}")
            
            if resolved_path is None:
                return ToolResult.error(f"無法解析路徑描述: {path_description}")
            
            # 標準化路徑
            resolved_path = os.path.normpath(resolved_path)
            
            # 檢查路徑是否存在
            path_exists = os.path.exists(resolved_path)
            
            # 如果需要，創建目錄
            if create_if_missing and not path_exists:
                try:
                    os.makedirs(resolved_path, exist_ok=True)
                    path_exists = True
                    debug_log(2, f"[MCP] 已創建目錄: {resolved_path}")
                except Exception as e:
                    return ToolResult.error(f"無法創建目錄 {resolved_path}: {str(e)}")
            
            result_data = {
                "original_description": path_description,
                "resolved_path": resolved_path,
                "exists": path_exists,
                "is_directory": os.path.isdir(resolved_path) if path_exists else None,
                "is_file": os.path.isfile(resolved_path) if path_exists else None,
                "absolute_path": os.path.abspath(resolved_path)
            }
            
            status_msg = "存在" if path_exists else "不存在"
            debug_log(2, f"[MCP] 路徑解析成功: {resolved_path} ({status_msg})")
            
            return ToolResult.success(
                message=f"路徑已解析: {resolved_path} ({status_msg})",
                data=result_data
            )
        
        except Exception as e:
            error_log(f"[MCP] resolve_path 失敗: {e}")
            return ToolResult.error(f"路徑解析失敗: {str(e)}")
    
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
