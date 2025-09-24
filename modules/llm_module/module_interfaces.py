# modules/llm_module/module_interfaces.py
"""
模組接口協議 - 簡化版

為模組間資料傳遞提供靈活的接口，避免硬編碼。
會話中模組是緊密相連的，只需預留接口讓資料可以流通。
"""

from typing import Dict, Any, Optional, Callable, List
from utils.debug_helper import debug_log, error_log


class ModuleDataProvider:
    """模組資料提供者 - 簡化的模組間接口"""
    
    def __init__(self):
        # 儲存各模組的資料提供函數
        self._mem_providers: Dict[str, Callable] = {}
        self._sys_providers: Dict[str, Callable] = {}
        
        debug_log(2, "[ModuleDataProvider] 模組資料提供者初始化")
    
    def register_mem_provider(self, data_type: str, provider_func: Callable):
        """
        註冊 MEM 模組的資料提供函數
        
        Args:
            data_type: 資料類型 (如 'memory_summary', 'identity_context')
            provider_func: 提供資料的函數
        """
        self._mem_providers[data_type] = provider_func
        debug_log(3, f"[ModuleDataProvider] 註冊 MEM 資料提供者: {data_type}")
    
    def register_sys_provider(self, data_type: str, provider_func: Callable):
        """
        註冊 SYS 模組的資料提供函數
        
        Args:
            data_type: 資料類型 (如 'workflow_status', 'available_functions')
            provider_func: 提供資料的函數
        """
        self._sys_providers[data_type] = provider_func
        debug_log(3, f"[ModuleDataProvider] 註冊 SYS 資料提供者: {data_type}")
    
    def get_mem_data(self, data_type: str, **kwargs) -> Optional[Any]:
        """
        獲取 MEM 模組資料
        
        Args:
            data_type: 資料類型
            **kwargs: 傳遞給提供函數的參數
            
        Returns:
            資料內容，失敗時返回 None
        """
        if data_type not in self._mem_providers:
            debug_log(3, f"[ModuleDataProvider] MEM 資料類型不存在: {data_type}")
            return None
        
        try:
            provider_func = self._mem_providers[data_type]
            result = provider_func(**kwargs)
            debug_log(3, f"[ModuleDataProvider] 成功獲取 MEM 資料: {data_type}")
            return result
        except Exception as e:
            error_log(f"[ModuleDataProvider] 獲取 MEM 資料失敗 ({data_type}): {e}")
            return None
    
    def get_sys_data(self, data_type: str, **kwargs) -> Optional[Any]:
        """
        獲取 SYS 模組資料
        
        Args:
            data_type: 資料類型
            **kwargs: 傳遞給提供函數的參數
            
        Returns:
            資料內容，失敗時返回 None
        """
        if data_type not in self._sys_providers:
            debug_log(3, f"[ModuleDataProvider] SYS 資料類型不存在: {data_type}")
            return None
        
        try:
            provider_func = self._sys_providers[data_type]
            result = provider_func(**kwargs)
            debug_log(3, f"[ModuleDataProvider] 成功獲取 SYS 資料: {data_type}")
            return result
        except Exception as e:
            error_log(f"[ModuleDataProvider] 獲取 SYS 資料失敗 ({data_type}): {e}")
            return None
    
    def get_available_data_types(self) -> Dict[str, Any]:
        """獲取可用的資料類型列表"""
        return {
            "mem_data_types": list(self._mem_providers.keys()),
            "sys_data_types": list(self._sys_providers.keys()),
            "total_providers": len(self._mem_providers) + len(self._sys_providers)
        }


# 全域實例，供其他模組使用
module_data_provider = ModuleDataProvider()


# 模擬資料提供函數（開發期間使用）
def _mock_memory_summary(**kwargs) -> str:
    """模擬記憶摘要"""
    user_id = kwargs.get('user_id', 'unknown')
    return f"Mock memory summary for user {user_id}: Recent interactions show interest in technical topics."


def _mock_identity_context(**kwargs) -> Dict[str, Any]:
    """模擬身份上下文"""
    identity_id = kwargs.get('identity_id', 'default')
    return {
        "identity": {"name": "Test User", "id": identity_id},
        "preferences": {
            "formality": "casual",
            "detail": "moderate",
            "technical": "intermediate"
        },
        "learning_profile": {
            "total_interactions": 15,
            "preferred_topics": ["technology", "programming"]
        }
    }


def _mock_workflow_status(**kwargs) -> Dict[str, Any]:
    """模擬工作流狀態"""
    workflow_id = kwargs.get('workflow_id', 'default')
    return {
        "workflow_id": workflow_id,
        "current_step": "data_analysis",
        "progress": 0.65,
        "remaining_steps": ["validation", "output_generation"],
        "estimated_completion": "2 minutes"
    }


def _mock_available_functions(**kwargs) -> List[str]:
    """模擬可用功能列表"""
    return [
        "file_operations",
        "data_analysis", 
        "web_search",
        "text_processing",
        "system_monitoring"
    ]


# 自動註冊模擬提供者（開發期間）
def register_mock_providers():
    """註冊模擬資料提供者，供開發測試使用"""
    module_data_provider.register_mem_provider("memory_summary", _mock_memory_summary)
    module_data_provider.register_mem_provider("identity_context", _mock_identity_context)
    module_data_provider.register_sys_provider("workflow_status", _mock_workflow_status)
    module_data_provider.register_sys_provider("available_functions", _mock_available_functions)
    
    debug_log(2, "[ModuleDataProvider] 已註冊模擬資料提供者")


# 開發期間自動載入模擬提供者
register_mock_providers()