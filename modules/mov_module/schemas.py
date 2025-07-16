from pydantic import BaseModel
from typing import Optional, Dict, Any

class MOVInput(BaseModel):
    action: str  # 移動/控制操作類型（待實作）
    params: Optional[Dict[str, Any]] = {}  # 操作參數

class MOVOutput(BaseModel):
    status: str  # 執行狀態  
    message: Optional[str] = None  # 回應訊息
    data: Optional[Dict[str, Any]] = None  # 操作結果

# Example inputs (待實作時更新)
example_input_placeholder = {
    "action": "placeholder",
    "params": {}
}

# Example outputs (待實作時更新)
example_output_placeholder = {
    "status": "not_implemented",
    "message": "MOV模組尚未實作",
    "data": None
}
