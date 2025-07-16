from pydantic import BaseModel
from typing import Optional

class STTInput(BaseModel):
    trigger: str = "manual"  # or "realtime" - 預設為manual模式

class STTOutput(BaseModel):
    text: str  # 辨識出的文字內容
    error: Optional[str] = None  # 錯誤訊息（如果有的話）
