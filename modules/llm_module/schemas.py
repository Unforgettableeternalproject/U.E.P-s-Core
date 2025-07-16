# modules/llm_module/schemas.py

from pydantic import BaseModel
from typing import Literal, Optional, Dict, Any

class LLMInput(BaseModel):
    text: str  # 要處理的文字內容
    intent: Literal["chat", "command", "direct"]  # 處理意圖
    memory: Optional[str] = None  # 相關記憶內容（可選）
    is_internal: Optional[bool] = False  # 是否為內部系統調用

class LLMOutput(BaseModel):
    text: str  # LLM 生成的回應文字
    mood: Optional[str] = "neutral"  # 情緒標記 (neutral, happy, sad, excited等)
    sys_action: Optional[Dict[str, Any]] = None  # 系統動作指令

