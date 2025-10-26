"""
測試 LLM-SYS 協作管道整合

驗證 SYS 模組正確註冊資料提供者後，LLM 能夠在 WORK 模式下獲取真實工作流狀態。
"""

import pytest
from unittest.mock import Mock, patch

from modules.sys_module.sys_module import SYSModule
from modules.llm_module.module_interfaces import state_aware_interface, CollaborationChannel
from core.states.state_manager import UEPState
from configs.config_loader import load_module_config


class TestLLMSYSCollaboration:
    """測試 LLM-SYS 協作管道"""
    
    def test_sys_module_registers_providers_on_init(self):
        """測試 SYS 模組初始化時註冊資料提供者"""
        # 清空現有提供者
        state_aware_interface._work_sys_providers.clear()
        
        # 初始化 SYS 模組
        config = load_module_config("sys_module")
        sys_module = SYSModule(config)
        sys_module.initialize()
        
        # 驗證提供者已註冊
        available_providers = state_aware_interface.get_available_data_types()
        assert "workflow_status" in available_providers["work_sys_providers"]
        assert "function_registry" in available_providers["work_sys_providers"]
        
        print("✅ SYS 模組成功註冊 workflow_status 和 function_registry 提供者")
    
    def test_workflow_status_provider_returns_data(self):
        """測試 workflow_status 提供者返回數據"""
        # 初始化 SYS 模組
        config = load_module_config("sys_module")
        sys_module = SYSModule(config)
        sys_module.initialize()
        
        # 啟用 WORK_SYS 管道
        state_aware_interface.set_channel_active(CollaborationChannel.WORK_SYS, True)
        
        # 測試無 workflow_id 的情況（返回所有活躍工作流）
        result = state_aware_interface.get_work_sys_data(
            data_type="workflow_status"
        )
        
        assert result is not None
        assert "active_workflows" in result
        assert "total_count" in result
        assert isinstance(result["active_workflows"], list)
        
        print(f"✅ workflow_status 提供者返回: {result}")
    
    def test_function_registry_provider_returns_functions(self):
        """測試 function_registry 提供者返回功能列表"""
        # 初始化 SYS 模組
        config = load_module_config("sys_module")
        sys_module = SYSModule(config)
        sys_module.initialize()
        
        # 啟用 WORK_SYS 管道
        state_aware_interface.set_channel_active(CollaborationChannel.WORK_SYS, True)
        
        # 獲取所有可用功能
        result = state_aware_interface.get_work_sys_data(
            data_type="function_registry",
            category="all"
        )
        
        assert result is not None
        assert isinstance(result, list)
        
        # 驗證返回的是功能對象而非字符串
        if len(result) > 0:
            assert isinstance(result[0], dict)
            assert "name" in result[0]
            assert "description" in result[0]
        
        print(f"✅ function_registry 提供者返回 {len(result)} 個功能")
    
    def test_channel_isolation(self):
        """測試管道隔離：未啟用時拒絕請求"""
        # 初始化 SYS 模組
        config = load_module_config("sys_module")
        sys_module = SYSModule(config)
        sys_module.initialize()
        
        # 確保 WORK_SYS 管道未啟用
        state_aware_interface.set_channel_active(CollaborationChannel.WORK_SYS, False)
        
        # 嘗試獲取數據應該返回 None
        result = state_aware_interface.get_work_sys_data(
            data_type="workflow_status"
        )
        
        assert result is None
        print("✅ 管道未啟用時正確拒絕請求")
        
        # 啟用管道後應該可以獲取數據
        state_aware_interface.set_channel_active(CollaborationChannel.WORK_SYS, True)
        
        result = state_aware_interface.get_work_sys_data(
            data_type="workflow_status"
        )
        
        assert result is not None
        print("✅ 管道啟用後成功獲取數據")
    
    def test_workflow_status_with_active_workflow(self):
        """測試查詢活躍工作流的狀態"""
        # 初始化 SYS 模組
        config = load_module_config("sys_module")
        sys_module = SYSModule(config)
        sys_module.initialize()
        
        # 創建測試工作流
        from modules.sys_module.workflows.test_workflows import create_test_workflow
        from core.sessions.session_manager import session_manager
        
        # 創建工作流會話
        session_result = session_manager.create_session(
            workflow_type="echo",
            command="test command",
            initial_data={}
        )
        
        if isinstance(session_result, str):
            session_id = session_result
            session = session_manager.get_workflow_session(session_id)
        else:
            session = session_result
            session_id = session.session_id
        
        # 創建工作流引擎
        engine = create_test_workflow("echo", session)
        sys_module.workflow_engines[session_id] = engine
        
        # 啟用管道
        state_aware_interface.set_channel_active(CollaborationChannel.WORK_SYS, True)
        
        # 查詢特定工作流狀態
        result = state_aware_interface.get_work_sys_data(
            data_type="workflow_status",
            workflow_id=session_id
        )
        
        assert result is not None
        assert result.get("workflow_id") == session_id
        assert result.get("workflow_type") == "echo"
        assert "status" in result
        assert "progress" in result
        
        print(f"✅ 成功查詢工作流 {session_id} 的狀態:")
        print(f"   - 類型: {result.get('workflow_type')}")
        print(f"   - 狀態: {result.get('status')}")
        print(f"   - 進度: {result.get('progress')}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
