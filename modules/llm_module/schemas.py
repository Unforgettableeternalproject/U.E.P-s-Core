# modules/llm_module/schemas.py

from pydantic import BaseModel
from typing import Literal, Optional, Dict, Any

class LLMInput(BaseModel):
    text: str  # 要處理的文字內容
    intent: Literal["chat", "command", "direct"]  # 處理意圖
    memory: Optional[str] = None  # 相關記憶內容（可選）
    is_internal: Optional[bool] = False  # 是否為內部系統調用

class SystemAction(BaseModel):
    action: str  # 動作類型 (start_workflow, execute_function)
    workflow_type: Optional[str] = None  # 工作流程類型（當 action 為 start_workflow 時）
    function_name: Optional[str] = None  # 具體功能名稱（當 action 為 execute_function 時）
    params: Optional[Dict[str, Any]] = None  # 初始化參數
    reason: Optional[str] = None  # 選擇此動作的原因說明

class LLMOutput(BaseModel):
    text: str  # LLM 生成的回應文字
    mood: Optional[str] = "neutral"  # 情緒標記 (neutral, happy, sad, excited等)  
    emotion: Optional[str] = "neutral"  # 情緒標記的別名（向後兼容）
    sys_action: Optional[SystemAction] = None  # 系統動作建議（僅在 command intent 時）

