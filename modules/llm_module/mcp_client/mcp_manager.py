# modules/llm_module/mcp_client/mcp_manager.py
"""
MCP Manager - MCP 工具管理器

負責封裝 MCP 工具獲取、檢查和管理相關邏輯，
從 llm_module.py 中提取出來保持主文件的簡潔性。
"""

from typing import Optional, List, Dict, Any
from utils.debug_helper import debug_log, info_log, error_log


class MCPManager:
    """MCP 工具管理器 - 封裝 MCP 相關操作"""
    
    def __init__(self, mcp_client):
        """
        初始化 MCP 管理器
        
        Args:
            mcp_client: MCPClient 實例
        """
        self.mcp_client = mcp_client
        debug_log(2, "[MCPManager] MCP 管理器初始化完成")
    
    def get_tools_for_workflow(
        self,
        is_step_response: bool = False,
        suppress_tools: bool = False
    ) -> Optional[List[Dict[str, Any]]]:
        """
        獲取工作流可用的 MCP 工具
        
        根據當前工作流狀態決定是否提供 MCP 工具：
        - 步驟回應模式：不提供工具（避免 LLM 調用工具而不返回文本）
        - 抑制模式：不提供工具
        - 正常模式：提供所有可用工具
        
        Args:
            is_step_response: 是否為工作流步驟回應模式
            suppress_tools: 是否抑制工具提供
            
        Returns:
            Gemini 格式的工具列表，或 None
        """
        # 檢查是否應該抑制工具
        if suppress_tools:
            debug_log(2, "[MCPManager] 工具提供被抑制")
            return None
        
        # 步驟回應模式：不提供工具
        if is_step_response:
            debug_log(2, "[MCPManager] 步驟回應模式：不提供 MCP 工具（避免 LLM 調用工具）")
            return None
        
        # 檢查 MCP Client 是否可用
        if not self.mcp_client:
            debug_log(2, "[MCPManager] MCP Client 未初始化")
            return None
        
        if not hasattr(self.mcp_client, 'get_tools_as_gemini_format'):
            debug_log(2, "[MCPManager] MCP Client 不支持 get_tools_as_gemini_format 方法")
            return None
        
        # 獲取工具
        try:
            mcp_tools = self.mcp_client.get_tools_as_gemini_format()
            if mcp_tools:
                # 計算工具數量（從 function_declarations 中）
                tool_count = 0
                if isinstance(mcp_tools, list) and len(mcp_tools) > 0:
                    if isinstance(mcp_tools[0], dict) and 'function_declarations' in mcp_tools[0]:
                        tool_count = len(mcp_tools[0]['function_declarations'])
                
                debug_log(2, f"[MCPManager] MCP 工具已準備: {tool_count} 個")
                return mcp_tools
            else:
                debug_log(2, "[MCPManager] 無可用的 MCP 工具")
                return None
        except Exception as e:
            error_log(f"[MCPManager] 獲取 MCP 工具失敗: {e}")
            return None
    
    def determine_tool_choice(
        self,
        has_mcp_tools: bool,
        has_active_workflow: bool,
        is_reviewing_step: bool
    ) -> str:
        """
        動態決定 tool_choice 模式
        
        策略：
        - 沒有活躍工作流且有工具 → ANY（強制調用工具啟動工作流）
        - 有活躍工作流或正在審核 → AUTO（讓 LLM 自主決定）
        - 沒有工具 → AUTO（只能生成文本）
        
        Args:
            has_mcp_tools: 是否有可用的 MCP 工具
            has_active_workflow: 是否有活躍的工作流
            is_reviewing_step: 是否正在審核步驟
            
        Returns:
            "ANY" 或 "AUTO"
        """
        if has_mcp_tools and not has_active_workflow and not is_reviewing_step:
            # 沒有活躍工作流：強制 LLM 調用工具來啟動工作流
            debug_log(2, "[MCPManager] Function calling 模式: ANY (強制調用工具啟動工作流)")
            return "ANY"
        else:
            # 有活躍工作流或沒有工具：讓 LLM 自主決定
            debug_log(
                2,
                f"[MCPManager] Function calling 模式: AUTO "
                f"(has_active_workflow={has_active_workflow}, "
                f"is_reviewing_step={is_reviewing_step}, "
                f"has_tools={has_mcp_tools})"
            )
            return "AUTO"
    
    def is_mcp_available(self) -> bool:
        """
        檢查 MCP 是否可用
        
        Returns:
            True 如果 MCP Client 已初始化且可用
        """
        return (
            self.mcp_client is not None and
            hasattr(self.mcp_client, 'get_tools_as_gemini_format')
        )
    
    def set_mcp_server(self, mcp_server):
        """
        設置 MCP Server（委派給 MCPClient）
        
        Args:
            mcp_server: SYS 模組的 MCP Server 實例
        """
        if self.mcp_client:
            self.mcp_client.set_mcp_server(mcp_server)
            info_log("[MCPManager] MCP Server 已設置")
        else:
            error_log("[MCPManager] 無法設置 MCP Server - MCP Client 未初始化")
