"""
MCP Tool Definitions - 工具定義與規範

定義 MCP 工具的結構、參數驗證、執行結果格式。
"""

from typing import Dict, Any, Optional, List, Callable
from pydantic import BaseModel, Field, validator
from enum import Enum


class ToolParameterType(str, Enum):
    """工具參數類型"""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"


class ToolParameter(BaseModel):
    """工具參數定義"""
    name: str = Field(..., description="參數名稱")
    type: ToolParameterType = Field(..., description="參數類型")
    description: str = Field(..., description="參數說明")
    required: bool = Field(default=True, description="是否必填")
    default: Optional[Any] = Field(default=None, description="預設值")
    enum: Optional[List[Any]] = Field(default=None, description="枚舉值")
    
    def validate_value(self, value: Any) -> tuple[bool, Optional[str]]:
        """
        驗證參數值
        
        Returns:
            (是否有效, 錯誤訊息)
        """
        # 檢查必填
        if self.required and value is None:
            return False, f"參數 '{self.name}' 為必填"
        
        if value is None:
            return True, None
        
        # 檢查類型
        if self.type == ToolParameterType.STRING and not isinstance(value, str):
            return False, f"參數 '{self.name}' 必須為字串"
        elif self.type == ToolParameterType.INTEGER and not isinstance(value, int):
            return False, f"參數 '{self.name}' 必須為整數"
        elif self.type == ToolParameterType.FLOAT and not isinstance(value, (int, float)):
            return False, f"參數 '{self.name}' 必須為數字"
        elif self.type == ToolParameterType.BOOLEAN and not isinstance(value, bool):
            return False, f"參數 '{self.name}' 必須為布林值"
        elif self.type == ToolParameterType.OBJECT and not isinstance(value, dict):
            return False, f"參數 '{self.name}' 必須為物件"
        elif self.type == ToolParameterType.ARRAY and not isinstance(value, list):
            return False, f"參數 '{self.name}' 必須為陣列"
        
        # 檢查枚舉值
        if self.enum and value not in self.enum:
            return False, f"參數 '{self.name}' 必須為 {self.enum} 之一"
        
        return True, None


class ToolResultStatus(str, Enum):
    """工具執行結果狀態"""
    SUCCESS = "success"
    ERROR = "error"
    PENDING = "pending"  # 需要等待 (例如: 等待使用者輸入)


class ToolResult(BaseModel):
    """工具執行結果"""
    status: ToolResultStatus = Field(..., description="執行狀態")
    message: str = Field(default="", description="結果訊息")
    data: Dict[str, Any] = Field(default_factory=dict, description="結果資料")
    error_detail: Optional[str] = Field(default=None, description="錯誤訊息")
    
    @classmethod
    def success(cls, message: str = "執行成功", data: Optional[Dict[str, Any]] = None):
        """創建成功結果"""
        return cls(
            status=ToolResultStatus.SUCCESS,
            message=message,
            data=data or {}
        )
    
    @classmethod
    def error(cls, message: str, error_detail: Optional[str] = None, data: Optional[Dict[str, Any]] = None):
        """創建錯誤結果"""
        return cls(
            status=ToolResultStatus.ERROR,
            message=message,
            error_detail=error_detail or message,
            data=data or {}
        )
    
    @classmethod
    def pending(cls, message: str, data: Optional[Dict[str, Any]] = None):
        """創建等待結果"""
        return cls(
            status=ToolResultStatus.PENDING,
            message=message,
            data=data or {}
        )


class MCPTool(BaseModel):
    """MCP 工具定義"""
    name: str = Field(..., description="工具名稱")
    description: str = Field(..., description="工具說明 (Traditional Chinese)")
    parameters: List[ToolParameter] = Field(default_factory=list, description="參數列表")
    handler: Optional[Callable] = Field(default=None, description="處理函數", exclude=True)
    allowed_paths: List[str] = Field(default_factory=lambda: ["CHAT", "WORK"], description="允許的路徑列表，預設為兩者均可")
    
    class Config:
        arbitrary_types_allowed = True
    
    def validate_params(self, params: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """
        驗證工具參數
        
        Returns:
            (是否有效, 錯誤訊息)
        """
        from devtools.debugger import debug_log
        debug_log(3, f"[MCPTool] 驗證參數: {params}")
        
        for param_def in self.parameters:
            param_value = params.get(param_def.name)
            debug_log(3, f"[MCPTool] 檢查參數 '{param_def.name}': value={repr(param_value)}, required={param_def.required}")
            is_valid, error_msg = param_def.validate_value(param_value)
            if not is_valid:
                debug_log(3, f"[MCPTool] 參數驗證失敗: {error_msg}")
                return False, error_msg
        
        return True, None
    
    def to_llm_spec(self) -> Dict[str, Any]:
        """
        轉換為 LLM 可理解的工具規範
        
        用於提供給 LLM 進行 function calling。
        """
        properties = {}
        required = []
        
        for param in self.parameters:
            # ✅ 將 "float" 映射為 "number"（Gemini API 要求）
            param_type = param.type.value
            if param_type == "float":
                param_type = "number"
            
            properties[param.name] = {
                "type": param_type,
                "description": param.description,
            }
            if param.enum:
                properties[param.name]["enum"] = param.enum
            if param.default is not None:
                properties[param.name]["default"] = param.default
            
            if param.required:
                required.append(param.name)
        
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            }
        }
    
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """
        執行工具
        
        Args:
            params: 工具參數
            
        Returns:
            工具執行結果
        """
        # 驗證參數
        is_valid, error_msg = self.validate_params(params)
        if not is_valid:
            return ToolResult.error(f"參數驗證失敗: {error_msg}")
        
        # 執行處理函數
        if self.handler is None:
            return ToolResult.error(f"工具 '{self.name}' 未註冊處理函數")
        
        try:
            result = await self.handler(params)
            return result
        except Exception as e:
            return ToolResult.error(f"工具執行失敗", error_detail=str(e))
