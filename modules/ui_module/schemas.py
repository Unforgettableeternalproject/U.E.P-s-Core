from pydantic import BaseModel
from typing import Optional, Dict, Any

class UIInput(BaseModel):
    action: str  # UI操作類型（待實作）
    params: Optional[Dict[str, Any]] = {}  # UI操作參數

class UIOutput(BaseModel):
    status: str  # 執行狀態
    message: Optional[str] = None  # 回應訊息
    data: Optional[Dict[str, Any]] = None  # UI操作結果

# Example inputs (待實作時更新)
example_input_placeholder = {
    "action": "placeholder",
    "params": {}
}

# Example outputs (待實作時更新)
example_output_placeholder = {
    "status": "not_implemented",
    "message": "UI模組尚未實作",
    "data": None
}
