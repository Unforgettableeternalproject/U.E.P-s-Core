from pydantic import BaseModel
from typing import Optional

class STTInput(BaseModel):
    trigger: str = "manual"  # or "realtime"

class STTOutput(BaseModel):
    text: str
    error: Optional[str] = None
