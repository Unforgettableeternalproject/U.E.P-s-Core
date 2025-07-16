"""
Schema Converter Utility
模組間資料格式轉換工具

此模組提供各種模組間的資料格式轉換功能，確保不同模組能夠正確地進行資料交換。
"""

from utils.debug_helper import debug_log, error_log
from typing import Dict, Any, Optional


class SchemaConverter:
    """模組間資料格式轉換器"""
    
    @staticmethod
    def llm_sys_action_to_sys_input(sys_action: Dict[str, Any]) -> Dict[str, Any]:
        """
        將 LLM 生成的 sys_action 格式轉換為 SYS 模組期望的 SYSInput 格式
        
        Args:
            sys_action: LLM 生成的系統動作，格式如 {'action': 'start_workflow', 'workflow_type': '...', ...}
            
        Returns:
            SYS 模組期望的輸入格式，包含 'mode' 字段
        """
        if not isinstance(sys_action, dict):
            error_log("[SchemaConverter] sys_action 必須是字典格式")
            return {}
        
        action = sys_action.get("action")
        debug_log(2, f"[SchemaConverter] 轉換動作類型: {action}")
        
        if action == "start_workflow":
            params = sys_action.get("params") or {}
            result = {
                "mode": "start_workflow",
                "params": {
                    "workflow_type": sys_action.get("workflow_type"),
                    "command": sys_action.get("reason", ""),
                    **params
                }
            }
            debug_log(2, f"[SchemaConverter] 工作流轉換結果: {result}")
            return result
            
        elif action == "execute_function":
            params = sys_action.get("params") or {}
            result = {
                "mode": "execute_function", 
                "params": {
                    "function_name": sys_action.get("function_name"),
                    **params
                }
            }
            debug_log(2, f"[SchemaConverter] 功能執行轉換結果: {result}")
            return result
            
        elif action == "continue_workflow":
            params = sys_action.get("params") or {}
            result = {
                "mode": "continue_workflow",
                "session_id": sys_action.get("session_id"),
                "params": params
            }
            debug_log(2, f"[SchemaConverter] 工作流繼續轉換結果: {result}")
            return result
            
        elif action == "cancel_workflow":
            result = {
                "mode": "cancel_workflow", 
                "session_id": sys_action.get("session_id"),
                "params": {"reason": sys_action.get("reason", "用戶取消")}
            }
            debug_log(2, f"[SchemaConverter] 工作流取消轉換結果: {result}")
            return result
            
        else:
            error_log(f"[SchemaConverter] 未知的動作類型: {action}")
            return {}

    @staticmethod
    def sys_action_to_workflow_input(sys_action: Dict[str, Any]) -> Dict[str, Any]:
        """
        將系統動作轉換為工作流輸入格式（當直接功能執行不可用時）
        
        Args:
            sys_action: 系統動作字典
            
        Returns:
            工作流輸入格式
        """
        if not isinstance(sys_action, dict):
            error_log("[SchemaConverter] sys_action 必須是字典格式")
            return {}
        
        params = sys_action.get("params") or {}
        return {
            "mode": "start_workflow",
            "params": {
                "workflow_type": "single_function",
                "function_name": sys_action.get("function_name"),
                "function_params": params,
                "command": sys_action.get("reason", "執行單一系統功能")
            }
        }

    @staticmethod
    def validate_sys_input(sys_input: Dict[str, Any]) -> bool:
        """
        驗證 SYS 模組輸入格式是否正確
        
        Args:
            sys_input: 要驗證的輸入字典
            
        Returns:
            是否為有效格式
        """
        if not isinstance(sys_input, dict):
            return False
            
        # 檢查必要的 mode 字段
        if "mode" not in sys_input:
            error_log("[SchemaConverter] SYS 輸入缺少必要的 'mode' 字段")
            return False
            
        mode = sys_input["mode"]
        valid_modes = ["start_workflow", "execute_function", "continue_workflow", "cancel_workflow", "list_functions"]
        
        if mode not in valid_modes:
            error_log(f"[SchemaConverter] 無效的 mode 值: {mode}")
            return False
            
        debug_log(2, f"[SchemaConverter] SYS 輸入格式驗證通過: {mode}")
        return True

    @staticmethod
    def convert_and_validate(sys_action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        轉換並驗證 LLM sys_action 到 SYS 輸入格式的完整流程
        
        Args:
            sys_action: LLM 生成的系統動作
            
        Returns:
            驗證通過的 SYS 輸入格式，失敗時返回 None
        """
        # 轉換格式
        sys_input = SchemaConverter.llm_sys_action_to_sys_input(sys_action)
        
        if not sys_input:
            error_log("[SchemaConverter] 轉換失敗")
            return None
            
        # 驗證格式
        if not SchemaConverter.validate_sys_input(sys_input):
            error_log("[SchemaConverter] 驗證失敗")
            return None
            
        debug_log(1, f"[SchemaConverter] 轉換和驗證成功: {sys_input}")
        return sys_input


# 向後兼容的函數（保持原有的函數名稱）
def convert_sys_action_to_sys_input(sys_action: Dict[str, Any]) -> Dict[str, Any]:
    """
    向後兼容函數：將 LLM sys_action 轉換為 SYS 輸入格式
    
    Args:
        sys_action: LLM 生成的系統動作
        
    Returns:
        SYS 模組期望的輸入格式
    """
    return SchemaConverter.llm_sys_action_to_sys_input(sys_action)
