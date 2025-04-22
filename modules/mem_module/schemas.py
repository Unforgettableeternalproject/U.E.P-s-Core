from pydantic import BaseModel
from typing import Literal, Optional, List, Dict

class MEMInput(BaseModel):
    mode: Literal["fetch", "store", "clear_all", "clear_by_text", "list_all"]
    text: Optional[str] = None  # for 'fetch'
    top_k: Optional[int] = 5
    entry: Optional[Dict[str, str]] = None  # for 'store'
    page: Optional[int] = 1  # for 'list_all'

class MEMOutput(BaseModel):
    results: List[Dict[str, str]]
    message: Optional[str] = None
    status: Optional[str] = "ok"
