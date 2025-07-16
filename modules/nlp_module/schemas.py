# modules/nlp_module/schemas.py

from pydantic import BaseModel
from typing import Literal

class NLPInput(BaseModel):
    text: str  # 要分析的文字內容

class NLPOutput(BaseModel):
    text: str  # 原始輸入文字
    intent: Literal["command", "chat", "ignore"]  # 意圖分類
    label: Literal["command", "chat", "non-sense", "unknown"]  # 詳細標籤
