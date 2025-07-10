from pydantic import BaseModel
from typing import Optional, Any, Dict, List

class SYSInput(BaseModel):
    mode: str
    params: Optional[Dict[str, Any]] = {}
    session_id: Optional[str] = None
    user_input: Optional[str] = None
    session_data: Optional[Dict[str, Any]] = None  # 用於測試工作流程傳遞會話數據

class SYSOutput(BaseModel):
    status: str
    data: Optional[Any] = None
    message: Optional[str] = None
    session_id: Optional[str] = None
    requires_input: bool = False
    prompt: Optional[str] = None
    session_data: Optional[Dict[str, Any]] = None  # 用於測試工作流程返回會話數據
    
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
