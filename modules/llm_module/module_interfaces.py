# modules/llm_module/module_interfaces.py
"""
模組接口協議 - 狀態感知雙管道系統

為模組間資料傳遞提供靈活的接口，支援 CHAT-MEM 和 WORK-SYS 兩個獨立協作管道。
根據系統狀態動態路由請求，確保協作管道完全隔離，避免狀態污染。
"""

from typing import Dict, Any, Optional, Callable, List, Union
from enum import Enum
from utils.debug_helper import debug_log, error_log


class CollaborationChannel(Enum):
    """協作管道類型"""
    CHAT_MEM = "chat_mem"    # CHAT 狀態下的 LLM-MEM 協作
    WORK_SYS = "work_sys"    # WORK 狀態下的 LLM-SYS 協作


class StateAwareModuleInterface:
    """狀態感知的模組接口 - 支援雙管道隔離"""
    
    def __init__(self):
        # 按協作管道分離的資料提供函數
        self._chat_mem_providers: Dict[str, Callable] = {}
        self._work_sys_providers: Dict[str, Callable] = {}
        
        # 管道狀態追蹤
        self._active_channels: Dict[CollaborationChannel, bool] = {
            CollaborationChannel.CHAT_MEM: False,
            CollaborationChannel.WORK_SYS: False
        }
        
        debug_log(2, "[StateAwareModuleInterface] 狀態感知模組接口初始化完成")
    
    def set_channel_active(self, channel: CollaborationChannel, active: bool):
        """
        設定協作管道的啟用狀態
        
        Args:
            channel: 協作管道類型
            active: 是否啟用
        """
        old_state = self._active_channels[channel]
        self._active_channels[channel] = active
        
        if old_state != active:
            state_text = "啟用" if active else "停用"
            debug_log(2, f"[StateAwareModuleInterface] {channel.value} 管道{state_text}")
    
    def register_chat_mem_provider(self, data_type: str, provider_func: Callable):
        """
        註冊 CHAT-MEM 協作管道的資料提供函數
        
        Args:
            data_type: 資料類型 (如 'memory_retrieval', 'conversation_storage')
            provider_func: 提供資料的函數
        """
        self._chat_mem_providers[data_type] = provider_func
        debug_log(3, f"[StateAwareModuleInterface] 註冊 CHAT-MEM 提供者: {data_type}")
    
    def register_work_sys_provider(self, data_type: str, provider_func: Callable):
        """
        註冊 WORK-SYS 協作管道的資料提供函數
        
        Args:
            data_type: 資料類型 (如 'workflow_status', 'function_registry')
            provider_func: 提供資料的函數
        """
        self._work_sys_providers[data_type] = provider_func
        debug_log(3, f"[StateAwareModuleInterface] 註冊 WORK-SYS 提供者: {data_type}")
    
    def get_chat_mem_data(self, data_type: str, **kwargs) -> Optional[Any]:
        """
        獲取 CHAT-MEM 協作管道的資料（僅在 CHAT 狀態下可用）
        
        Args:
            data_type: 資料類型
            **kwargs: 傳遞給提供函數的參數
            
        Returns:
            資料內容，失敗或管道未啟用時返回 None
        """
        if not self._active_channels[CollaborationChannel.CHAT_MEM]:
            debug_log(3, f"[StateAwareModuleInterface] CHAT-MEM 管道未啟用，拒絕請求: {data_type}")
            return None
            
        if data_type not in self._chat_mem_providers:
            debug_log(3, f"[StateAwareModuleInterface] CHAT-MEM 資料類型不存在: {data_type}")
            return None
        
        try:
            provider_func = self._chat_mem_providers[data_type]
            result = provider_func(**kwargs)
            debug_log(3, f"[StateAwareModuleInterface] 成功獲取 CHAT-MEM 資料: {data_type}")
            return result
        except Exception as e:
            error_log(f"[StateAwareModuleInterface] 獲取 CHAT-MEM 資料失敗 ({data_type}): {e}")
            return None
    
    def get_work_sys_data(self, data_type: str, **kwargs) -> Optional[Any]:
        """
        獲取 WORK-SYS 協作管道的資料（僅在 WORK 狀態下可用）
        
        Args:
            data_type: 資料類型
            **kwargs: 傳遞給提供函數的參數
            
        Returns:
            資料內容，失敗或管道未啟用時返回 None
        """
        if not self._active_channels[CollaborationChannel.WORK_SYS]:
            debug_log(3, f"[StateAwareModuleInterface] WORK-SYS 管道未啟用，拒絕請求: {data_type}")
            return None
            
        if data_type not in self._work_sys_providers:
            debug_log(3, f"[StateAwareModuleInterface] WORK-SYS 資料類型不存在: {data_type}")
            return None
        
        try:
            provider_func = self._work_sys_providers[data_type]
            result = provider_func(**kwargs)
            debug_log(3, f"[StateAwareModuleInterface] 成功獲取 WORK-SYS 資料: {data_type}")
            return result
        except Exception as e:
            error_log(f"[StateAwareModuleInterface] 獲取 WORK-SYS 資料失敗 ({data_type}): {e}")
            return None
    
    def get_available_data_types(self) -> Dict[str, Any]:
        """獲取可用的資料類型列表和管道狀態"""
        return {
            "chat_mem_providers": list(self._chat_mem_providers.keys()),
            "work_sys_providers": list(self._work_sys_providers.keys()),
            "active_channels": {channel.value: active for channel, active in self._active_channels.items()},
            "total_providers": len(self._chat_mem_providers) + len(self._work_sys_providers)
        }
    
    def get_channel_status(self) -> Dict[str, bool]:
        """獲取所有協作管道的狀態"""
        return {channel.value: active for channel, active in self._active_channels.items()}
    
    def is_channel_active(self, channel: CollaborationChannel) -> bool:
        """檢查指定協作管道是否啟用"""
        return self._active_channels.get(channel, False)


# 全域實例，供其他模組使用
state_aware_interface = StateAwareModuleInterface()


# 模擬資料提供函數（開發期間使用）
def _mock_memory_retrieval(**kwargs) -> List[Dict[str, Any]]:
    """模擬 CHAT-MEM 記憶檢索"""
    query = kwargs.get('query', '')
    max_results = kwargs.get('max_results', 3)
    return [
        {
            "content": f"Mock memory about {query}",
            "type": "conversation",
            "timestamp": "2025-01-15T10:30:00",
            "relevance": 0.85
        }
    ][:max_results]


def _mock_conversation_storage(**kwargs) -> bool:
    """模擬 CHAT-MEM 對話儲存"""
    conversation_data = kwargs.get('conversation_data', {})
    debug_log(3, f"[Mock] 模擬儲存對話: {len(str(conversation_data))} 字符")
    return True


def _mock_workflow_status(**kwargs) -> Dict[str, Any]:
    """模擬 WORK-SYS 工作流狀態"""
    workflow_id = kwargs.get('workflow_id', 'default')
    return {
        "workflow_id": workflow_id,
        "current_step": "data_analysis",
        "progress": 0.65,
        "remaining_steps": ["validation", "output_generation"],
        "estimated_completion": "2 minutes",
        "available_functions": ["file_ops", "data_analysis", "web_search"]
    }


def _mock_function_registry(**kwargs) -> List[str]:
    """模擬 WORK-SYS 功能註冊表"""
    category = kwargs.get('category', 'all')
    functions = {
        'file': ['read_file', 'write_file', 'list_directory'],
        'analysis': ['analyze_data', 'generate_report', 'visualize'],
        'web': ['search_web', 'fetch_url', 'extract_text'],
        'all': ['read_file', 'write_file', 'analyze_data', 'search_web']
    }
    return functions.get(category, functions['all'])


# 狀態管理輔助函數
def set_collaboration_state(system_state: Union[str, Any]):
    """根據系統狀態設定協作管道"""
    # 重設所有管道
    state_aware_interface.set_channel_active(CollaborationChannel.CHAT_MEM, False)
    state_aware_interface.set_channel_active(CollaborationChannel.WORK_SYS, False)
    
    # 根據系統狀態啟用對應管道
    state_str = str(system_state).upper()
    if 'CHAT' in state_str:
        state_aware_interface.set_channel_active(CollaborationChannel.CHAT_MEM, True)
        debug_log(2, "[StateAwareInterface] 系統進入 CHAT 狀態，啟用 CHAT-MEM 協作管道")
    elif 'WORK' in state_str:
        state_aware_interface.set_channel_active(CollaborationChannel.WORK_SYS, True)
        debug_log(2, "[StateAwareInterface] 系統進入 WORK 狀態，啟用 WORK-SYS 協作管道")


# 自動註冊模擬提供者（開發期間）
def register_mock_providers():
    """註冊模擬資料提供者，供開發測試使用"""
    # CHAT-MEM 協作管道
    state_aware_interface.register_chat_mem_provider("memory_retrieval", _mock_memory_retrieval)
    state_aware_interface.register_chat_mem_provider("conversation_storage", _mock_conversation_storage)
    
    # WORK-SYS 協作管道  
    state_aware_interface.register_work_sys_provider("workflow_status", _mock_workflow_status)
    state_aware_interface.register_work_sys_provider("function_registry", _mock_function_registry)
    
    debug_log(2, "[StateAwareInterface] 已註冊雙管道模擬資料提供者")


# 向後相容性支援（暫時保留舊接口）
class LegacyModuleDataProvider:
    """向後相容的舊接口包裝器"""
    
    def get_mem_data(self, data_type: str, **kwargs):
        return state_aware_interface.get_chat_mem_data(data_type, **kwargs)
    
    def get_sys_data(self, data_type: str, **kwargs):
        return state_aware_interface.get_work_sys_data(data_type, **kwargs)


# 提供向後相容實例
module_data_provider = LegacyModuleDataProvider()

# ⚠️ 模擬提供者已停用，真實提供者由各模組在 initialize() 時註冊
# 如需測試，手動調用: register_mock_providers()
# register_mock_providers()  # ← 已註釋，避免覆蓋真實提供者