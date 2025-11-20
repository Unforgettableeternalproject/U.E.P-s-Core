# modules/llm_module/workflow/workflow_controller.py
"""
工作流控制器

處理工作流的批准、修改、取消等控制操作
"""

import asyncio
from typing import Dict, Any, Optional

from utils.debug_helper import debug_log, error_log


class WorkflowController:
    """工作流控制操作"""
    
    def __init__(self, llm_module):
        """
        初始化工作流控制器
        
        Args:
            llm_module: LLM 模組實例
        """
        self.llm_module = llm_module
    
    def approve_workflow_step(self, session_id: str, modifications: Optional[Dict] = None):
        """
        批准工作流步驟並繼續
        
        Args:
            session_id: 工作流會話 ID
            modifications: 可選的修改參數
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        loop.run_until_complete(
            self.llm_module.mcp_client.call_tool("approve_step", {
                "session_id": session_id,
                "modifications": modifications or {}
            })
        )
        debug_log(2, f"[LLM.WorkflowController] 已批准工作流步驟: {session_id}")
    
    def modify_workflow_step(self, session_id: str, modifications: Dict[str, Any]):
        """
        修改工作流步驟並重試
        
        Args:
            session_id: 工作流會話 ID
            modifications: 修改參數
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        loop.run_until_complete(
            self.llm_module.mcp_client.call_tool("modify_step", {
                "session_id": session_id,
                "modifications": modifications
            })
        )
        debug_log(2, f"[LLM.WorkflowController] 已修改工作流步驟: {session_id}")
    
    def cancel_workflow(self, session_id: str, reason: str):
        """
        取消工作流
        
        Args:
            session_id: 工作流會話 ID
            reason: 取消原因
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        loop.run_until_complete(
            self.llm_module.mcp_client.call_tool("cancel_workflow", {
                "session_id": session_id,
                "reason": reason
            })
        )
        debug_log(2, f"[LLM.WorkflowController] 已取消工作流: {session_id}")
