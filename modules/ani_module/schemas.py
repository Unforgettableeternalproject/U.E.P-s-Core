from pydantic import BaseModel
from typing import Optional, Dict, Any

class ANIInput(BaseModel):
    action: str  # 動畫操作類型（待實作）
    params: Optional[Dict[str, Any]] = {}  # 動畫參數

class ANIOutput(BaseModel):
    status: str  # 執行狀態
    message: Optional[str] = None  # 回應訊息
    data: Optional[Dict[str, Any]] = None  # 動畫數據
