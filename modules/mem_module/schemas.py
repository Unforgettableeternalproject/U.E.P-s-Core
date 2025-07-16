from pydantic import BaseModel
from typing import Literal, Optional, List, Dict, Union

class MEMInput(BaseModel):
    mode: Literal["fetch", "store", "clear_all", "clear_by_text", "list_all"]
    text: Optional[str] = None  # for 'fetch' and 'clear_by_text'
    top_k: Optional[int] = 5  # for 'fetch' and 'clear_by_text'
    entry: Optional[Dict[str, str]] = None  # for 'store' (包含user和response)
    page: Optional[int] = 1  # for 'list_all'

class MEMOutput(BaseModel):
    # 通用欄位
    status: Optional[str] = None  # "empty", "stored", "cleared", "failed", "ok"
    message: Optional[str] = None  # 狀態訊息
    error: Optional[str] = None  # 錯誤訊息
    
    # fetch/list_all 專用
    results: Optional[List[Dict[str, Union[str, int]]]] = []  # 記憶結果列表
    
    # list_all 專用
    records: Optional[List[Dict[str, str]]] = None  # 分頁記錄
    page: Optional[int] = None  # 當前頁碼
    total_pages: Optional[int] = None  # 總頁數
    total_records: Optional[int] = None  # 總記錄數
    
    # clear_by_text 專用  
    deleted: Optional[int] = None  # 刪除的記錄數
