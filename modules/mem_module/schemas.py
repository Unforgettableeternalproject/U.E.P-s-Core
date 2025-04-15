from pydantic import BaseModel
from typing import Literal, Optional, List, Dict

class MEMInput(BaseModel):
    mode: Literal["fetch", "store"]
    text: Optional[str] = None  # for 'fetch'
    top_k: Optional[int] = 5
    entry: Optional[Dict[str, str]] = None  # for 'store'

class MEMOutput(BaseModel):
    results: List[Dict[str, str]]
