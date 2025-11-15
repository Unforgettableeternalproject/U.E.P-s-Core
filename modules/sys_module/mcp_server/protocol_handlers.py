"""
MCP Protocol Handlers - JSON-RPC 請求/響應處理

實現基於 JSON-RPC 2.0 的輕量級協議處理。
"""

from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field
from enum import Enum


class MCPErrorCode(int, Enum):
    """MCP 錯誤代碼 (基於 JSON-RPC 2.0)"""
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    
    # 自定義錯誤碼
    WORKFLOW_NOT_FOUND = -32001
    WORKFLOW_ALREADY_EXISTS = -32002
    STEP_EXECUTION_FAILED = -32003
    UNAUTHORIZED = -32004
    TIMEOUT = -32005


class MCPError(BaseModel):
    """MCP 錯誤物件"""
    code: int
    message: str
    data: Optional[Dict[str, Any]] = None


class MCPRequest(BaseModel):
    """
    MCP 請求 (JSON-RPC 2.0 格式)
    
    Examples:
        {
            "jsonrpc": "2.0",
            "method": "start_workflow",
            "params": {"workflow_type": "drop_and_read", "command": "讀取檔案內容"},
            "id": 1
        }
    """
    jsonrpc: str = Field(default="2.0", description="JSON-RPC 版本")
    method: str = Field(..., description="工具名稱")
    params: Dict[str, Any] = Field(default_factory=dict, description="工具參數")
    id: Optional[int] = Field(default=None, description="請求 ID")


class MCPResponse(BaseModel):
    """
    MCP 響應 (JSON-RPC 2.0 格式)
    
    Examples:
        # 成功響應
        {
            "jsonrpc": "2.0",
            "result": {"status": "success", "session_id": "ws_123"},
            "id": 1
        }
        
        # 錯誤響應
        {
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": "Method not found"},
            "id": 1
        }
    """
    jsonrpc: str = Field(default="2.0", description="JSON-RPC 版本")
    result: Optional[Dict[str, Any]] = Field(default=None, description="成功結果")
    error: Optional[MCPError] = Field(default=None, description="錯誤資訊")
    id: Optional[int] = Field(default=None, description="請求 ID")
    
    def is_success(self) -> bool:
        """是否為成功響應"""
        return self.error is None and self.result is not None
    
    def is_error(self) -> bool:
        """是否為錯誤響應"""
        return self.error is not None


class MCPNotification(BaseModel):
    """
    MCP 通知 (無需響應的單向訊息)
    
    Examples:
        {
            "jsonrpc": "2.0",
            "method": "workflow_progress",
            "params": {"session_id": "ws_123", "progress": 0.5}
        }
    """
    jsonrpc: str = Field(default="2.0", description="JSON-RPC 版本")
    method: str = Field(..., description="通知類型")
    params: Dict[str, Any] = Field(default_factory=dict, description="通知參數")


class MCPBatchRequest(BaseModel):
    """MCP 批次請求 (多個請求一次發送)"""
    requests: List[MCPRequest]


class MCPBatchResponse(BaseModel):
    """MCP 批次響應"""
    responses: List[MCPResponse]


def create_success_response(request_id: Optional[int], result: Dict[str, Any]) -> MCPResponse:
    """創建成功響應"""
    return MCPResponse(
        jsonrpc="2.0",
        result=result,
        id=request_id
    )


def create_error_response(
    request_id: Optional[int],
    code: MCPErrorCode,
    message: str,
    data: Optional[Dict[str, Any]] = None
) -> MCPResponse:
    """創建錯誤響應"""
    return MCPResponse(
        jsonrpc="2.0",
        error=MCPError(code=code.value, message=message, data=data),
        id=request_id
    )


def create_notification(method: str, params: Dict[str, Any]) -> MCPNotification:
    """創建通知"""
    return MCPNotification(
        jsonrpc="2.0",
        method=method,
        params=params
    )
