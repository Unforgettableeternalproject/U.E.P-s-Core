from pydantic import BaseModel
from typing import Optional, Any, Dict, List

class SYSInput(BaseModel):
    mode: str  # 操作模式：list_functions, start_workflow, continue_workflow, cancel_workflow等
    params: Optional[Dict[str, Any]] = {}  # 模式專用參數
    session_id: Optional[str] = None  # 工作流程會話ID
    user_input: Optional[str] = None  # 使用者輸入內容
    session_data: Optional[Dict[str, Any]] = None  # 會話數據（測試工作流程用）

class SYSOutput(BaseModel):
    status: str  # 狀態：waiting, completed, cancelled, error等
    data: Optional[Any] = None  # 回傳數據
    message: Optional[str] = None  # 狀態訊息
    session_id: Optional[str] = None  # 工作流程會話ID
    requires_input: bool = False  # 是否需要使用者輸入
    prompt: Optional[str] = None  # 使用者輸入提示
    session_data: Optional[Dict[str, Any]] = None  # 會話數據（測試工作流程用）
    
class SessionInfo(BaseModel):
    """Session information for workflows"""
    session_id: str
    workflow_type: str
    command: str
    current_step: int
    status: str
    created_at: float
    last_active: float
    
class SessionHistory(BaseModel):
    """History entry for session events"""
    time: float
    step: int
    type: str
    description: str
    
class SessionDetail(SessionInfo):
    """Detailed session information including data and history"""
    data: Dict[str, Any]
    history: List[SessionHistory]
