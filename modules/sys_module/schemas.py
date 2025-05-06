from pydantic import BaseModel
from typing import Optional, Any, Dict

class SYSInput(BaseModel):
    mode: str
    params: Optional[Dict[str, Any]] = {}

class SYSOutput(BaseModel):
    status: str
    data: Optional[Any] = None
    message: Optional[str] = None
