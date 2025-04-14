# modules/nlp_module/schemas.py

from pydantic import BaseModel
from typing import Literal

class NLPInput(BaseModel):
    text: str

class NLPOutput(BaseModel):
    text: str
    intent: Literal["command", "chat", "ignore"]
    label: Literal["command", "chat", "non-sense", "unknown"]
