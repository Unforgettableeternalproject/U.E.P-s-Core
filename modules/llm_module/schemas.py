# modules/llm_module/schemas.py

from pydantic import BaseModel
from typing import Literal, Optional

class LLMInput(BaseModel):
    text: str
    intent: Literal["chat", "command"]
    memory: Optional[str] = None

class LLMOutput(BaseModel): # 之後會把指令的部份給加進去
    text: str
    mood: Optional[str] = "neutral"
    sys_action: Optional[dict] = None

