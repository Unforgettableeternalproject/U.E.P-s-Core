# modules/tts_module/schemas.py

from pydantic import BaseModel
from typing import Optional

class TTSInput(BaseModel):
    text: str
    mood: Optional[str] = "neutral"
    save: Optional[bool] = False

class TTSOutput(BaseModel):
    status: str
    output_path: Optional[str] = None
    message: Optional[str] = None
