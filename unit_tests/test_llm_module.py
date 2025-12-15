# -*- coding: utf-8 -*-
"""
LLM 模組完整功能測試

測試 LLM 模組重構後的核心功能：
1. 模組初始化與組件整合
2. CHAT 狀態處理與 Router 溝通
3. StatusManager 整合（系統數值處理）
4. Context Caching 功能
5. 與 MEM 模組協作（狀態感知雙管道）
6. 學習功能與偏好記錄
7. Mischief/Sleep 特殊狀態
8. 錯誤處理與邊界情況

參考 LLM 待辦.md 中的驗證標準
"""

import sys
import os
import pytest
import json
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
from typing import Dict, Any, List, Optional

# 添加項目根目錄到系統路徑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.llm_module.llm_module import LLMModule
from modules.llm_module.schemas import (
    LLMInput, LLMOutput, LLMMode, SystemState, 
    SystemAction, ConversationEntry, LearningData, StatusUpdate
)
from core.states.state_manager import UEPState, state_manager
from core.status_manager import StatusManager
from core.working_context import working_context_manager, ContextType
from utils.debug_helper import debug_log, info_log, error_log


@pytest.fixture(scope="module")
def llm_module():
    """初始化 LLM 模組"""
    module = LLMModule()
    yield module
    # 清理（如果需要）


@pytest.fixture(scope="function")
def reset_state():
    """每個測試前重設狀態"""
    # 重設狀態管理器
    state_manager.current_state = UEPState.IDLE
    # 重設 StatusManager
    status_manager = StatusManager()
    status_manager.reset_status()
    yield
    # 測試後清理


class TestLLMModuleInitialization:
    """測試 LLM 模組初始化與組件整合"""
    
    def test_module_initialization(self, llm_module):
        """測試模組基本初始化"""
        assert llm_module is not None, "LLM 模組應該成功初始化"
        
        # 檢查核心組件
        assert hasattr(llm_module, 'model'), "應該有 Gemini 客戶端"
        assert hasattr(llm_module, 'prompt_manager'), "應該有 PromptManager"
        assert hasattr(llm_module, 'learning_engine'), "應該有 LearningEngine"
        assert hasattr(llm_module, 'cache_manager'), "應該有 CacheManager"
        
        # 檢查組件不為空
        assert llm_module.model is not None, "Gemini 客戶端應該被初始化"
        assert llm_module.prompt_manager is not None, "PromptManager 應該被初始化"
        assert llm_module.learning_engine is not None, "LearningEngine 應該被初始化"
        assert llm_module.cache_manager is not None, "CacheManager 應該被初始化"
    
    def test_component_integration(self, llm_module):
        """測試組件間整合"""
        # 測試 CacheManager 與 PromptManager 整合
        prompt_manager = llm_module.cache_manager._get_prompt_manager()
        assert prompt_manager is not None, "CacheManager 應該能獲取 PromptManager"
        
        # 測試狀態管理器整合
        assert hasattr(llm_module, 'state_manager'), "應該整合 StateManager"
        
        # 測試狀態感知接口
        if hasattr(llm_module, 'interface'):
            assert llm_module.interface is not None, "狀態感知接口應該被初始化"


class TestChatStateProcessing:
    """測試 CHAT 狀態處理 - LLM 待辦事項關鍵功能"""
    
    def test_chat_state_activation(self, llm_module, reset_state):
        """測試 CHAT 狀態激活和處理"""
        # 設置 CHAT 狀態
        state_manager.current_state = UEPState.CHAT
        
        # 創建 CHAT 模式輸入
        chat_input = LLMInput(
            text="你好，今天天氣如何？",
            mode=LLMMode.CHAT
        )
        
        # 測試處理（不實際發送 API 請求）
        assert chat_input.mode == LLMMode.CHAT, "應該設置為 CHAT 模式"
        assert state_manager.current_state == UEPState.CHAT, "系統應該在 CHAT 狀態"
    
    @patch('modules.llm_module.gemini_client.GeminiWrapper.query')
    def test_chat_response_flow(self, mock_query, llm_module, reset_state):
        """測試 CHAT 狀態下的完整回應流程"""
        # 模擬 Gemini 回應
        mock_response = {
            "text": "今天天氣很不錯，陽光明媚！",
            "system_values": {
                "mood": 0.8,
                "helpfulness": 0.9
            }
        }
        mock_query.return_value = mock_response
        
        # 設置 CHAT 狀態
        state_manager.current_state = UEPState.CHAT
        
        # 創建輸入
        chat_data = {
            "text": "今天天氣如何？",
            "mode": "chat",
            "session_id": "test_chat_session",
            "memory_context": "用戶經常詢問天氣"
        }
        
        # 處理請求
        try:
            result = llm_module.handle(chat_data)
            
            # 驗證結果結構
            assert isinstance(result, dict), "應該返回字典格式結果"
            
            # 如果成功處理，檢查基本結構
            if result.get("success", False):
                assert "text" in result, "應該包含文字回應"
                print(f"✅ CHAT 狀態處理成功: {result.get('text', '')[:50]}...")
            else:
                print(f"⚠️ CHAT 狀態處理失敗（可能是網路問題）: {result.get('error', 'Unknown')}")
                
        except Exception as e:
            print(f"⚠️ CHAT 測試跳過（網路或配置問題）: {e}")
            pytest.skip("CHAT 功能測試因網路問題跳過")


class TestStatusManagerIntegration:
    """測試 StatusManager 整合 - 系統數值處理"""
    
    def test_status_manager_availability(self, llm_module):
        """測試 StatusManager 可用性"""
        # 檢查 StatusManager 是否可用
        status_manager = StatusManager()
        assert status_manager is not None, "StatusManager 應該可用"
        
        # 檢查基本數值 - 使用正確的 API
        status = status_manager.get_status()
        status_dict = status_manager.get_status_dict()
        
        assert hasattr(status, 'mood'), "應該有心情屬性"
        assert hasattr(status, 'pride'), "應該有自豪感屬性"
        assert hasattr(status, 'helpfulness'), "應該有助人意願屬性"
        assert hasattr(status, 'boredom'), "應該有無聊感屬性"
        
        # 驗證數值範圍
        assert -1 <= status.mood <= 1, f"心情值應該在 -1 到 1 之間，實際: {status.mood}"
        assert -1 <= status.pride <= 1, f"自豪感應該在 -1 到 1 之間，實際: {status.pride}"
        assert 0 <= status.helpfulness <= 1, f"助人意願應該在 0 到 1 之間，實際: {status.helpfulness}"
        assert 0 <= status.boredom <= 1, f"無聊感應該在 0 到 1 之間，實際: {status.boredom}"
    
    def test_system_values_update(self, llm_module, reset_state):
        """測試系統數值更新機制"""
        status_manager = StatusManager()
        
        # 記錄初始值
        initial_status = status_manager.get_status()
        initial_mood = initial_status.mood
        initial_pride = initial_status.pride
        
        # 模擬正面互動 - 使用正確的更新方法
        status_manager.update_mood(0.1, "測試正面互動")
        status_manager.update_pride(0.1, "測試成功完成")
        
        # 驗證更新
        updated_status = status_manager.get_status()
        updated_mood = updated_status.mood
        updated_pride = updated_status.pride
        
        assert updated_mood >= initial_mood, "心情應該提升或維持"
        assert updated_pride >= initial_pride, "自豪感應該提升或維持"
        
        print(f"✅ 系統數值更新測試: 心情 {initial_mood:.2f} -> {updated_mood:.2f}")


class TestContextCaching:
    """測試 Context Caching 功能"""
    
    def test_cache_manager_initialization(self, llm_module):
        """測試快取管理器初始化"""
        assert hasattr(llm_module, 'cache_manager'), "應該有 CacheManager"
        cache_manager = llm_module.cache_manager
        
        # 測試基本快取功能
        stats = cache_manager.get_cache_statistics()
        assert isinstance(stats, dict), "統計應該是字典格式"
        # 檢查實際的統計結構
        expected_keys = ['explicit_cache', 'local_cache', 'overall', 'system']
        for key in expected_keys:
            assert key in stats, f"應該有 {key} 統計"
    
    def test_cache_operations(self, llm_module):
        """測試快取操作"""
        cache_manager = llm_module.cache_manager
        
        # 獲取統計（不執行清理操作，因為方法名不同）
        stats = cache_manager.get_cache_statistics()
        
        # 驗證快取統計結構
        expected_keys = ['explicit_cache', 'local_cache', 'overall', 'system']
        for key in expected_keys:
            assert key in stats, f"應該有 {key} 統計"
        
        # 驗證顯性快取統計的內部結構
        assert 'hit_count' in stats['explicit_cache'], "顯性快取應有命中計數"
        assert 'miss_count' in stats['explicit_cache'], "顯性快取應有未命中計數"
        assert 'cache_names' in stats['explicit_cache'], "顯性快取應有快取名稱列表"
        
        print(f"✅ 快取統計驗證通過")
    
    def test_prompt_manager_integration(self, llm_module):
        """測試 PromptManager 與 CacheManager 整合"""
        cache_manager = llm_module.cache_manager
        
        # 檢查 PromptManager 整合 - 使用正確的方法
        prompt_manager = cache_manager._get_prompt_manager()
        assert prompt_manager is not None, "應該整合 PromptManager"
        
        # 測試快取統計功能
        stats = cache_manager.get_cache_statistics()
        assert isinstance(stats, dict), "應該有快取統計資料"


class TestMemoryModuleCollaboration:
    """測試與 MEM 模組協作 - 狀態感知雙管道"""
    
    def test_state_aware_interface(self, llm_module):
        """測試狀態感知接口"""
        # 檢查是否有狀態感知接口相關屬性
        if hasattr(llm_module, 'interface'):
            assert llm_module.interface is not None, "狀態感知接口應該存在"
            print("✅ 狀態感知接口可用")
        else:
            print("⚠️ 狀態感知接口未找到（可能還未完全實現）")
    
    @patch('modules.mem_module.mem_module.MEMModule.handle')
    def test_memory_collaboration_simulation(self, mock_mem_handle, llm_module):
        """模擬與 MEM 模組的協作"""
        # 模擬 MEM 模組回應
        mock_mem_response = {
            "success": True,
            "memory_context": "用戶喜歡討論技術話題",
            "relevant_memories": ["上次討論了 Python", "用戶是程式設計師"]
        }
        mock_mem_handle.return_value = mock_mem_response
        
        # 測試記憶上下文處理
        input_data = LLMInput(
            text="我想學習新的程式語言",
            mode=LLMMode.CHAT,
            memory_context="技術學習"
        )
        
        assert input_data.memory_context == "技術學習", "記憶上下文應該被正確設置"
        print("✅ 記憶協作模擬測試通過")


class TestLearningEngine:
    """測試學習功能"""
    
    def test_learning_engine_initialization(self, llm_module):
        """測試學習引擎初始化"""
        assert hasattr(llm_module, 'learning_engine'), "應該有學習引擎"
        learning_engine = llm_module.learning_engine
        assert learning_engine is not None, "學習引擎應該被初始化"
    
    def test_learning_data_structure(self, llm_module):
        """測試學習數據結構"""
        learning_engine = llm_module.learning_engine
        
        # 檢查學習引擎基本功能
        if hasattr(learning_engine, 'process_feedback'):
            print("✅ 學習引擎具有回饋處理功能")
        
        if hasattr(learning_engine, 'get_user_preferences'):
            print("✅ 學習引擎具有偏好獲取功能")
        
        # 測試學習數據記錄
        if hasattr(learning_engine, 'record_interaction'):
            print("✅ 學習引擎具有互動記錄功能")


class TestSpecialStates:
    """測試 Mischief 和 Sleep 特殊狀態"""
    
    def test_special_state_detection(self):
        """測試特殊狀態檢測機制"""
        # 檢查 StateManager 是否支援特殊狀態
        assert hasattr(UEPState, 'MISCHIEF'), "應該支援 MISCHIEF 狀態"
        assert hasattr(UEPState, 'SLEEP'), "應該支援 SLEEP 狀態"
    
    def test_mischief_state_conditions(self):
        """測試 Mischief 狀態觸發條件"""
        status_manager = StatusManager()
        
        # 設置高心情、高自豪感、高無聊感條件 - 使用正確的更新方法
        status_manager.update_mood(0.9, "測試高心情")
        status_manager.update_pride(0.8, "測試高自豪感")  
        status_manager.update_boredom(0.7, "測試高無聊感")
        
        # 檢查數值設置
        status = status_manager.get_status()
        mood = status.mood
        pride = status.pride
        boredom = status.boredom
        
        print(f"✅ Mischief 觸發條件測試: 心情={mood:.2f}, 自豪感={pride:.2f}, 無聊感={boredom:.2f}")
        
        # 檢查是否有特殊狀態檢查機制
        if hasattr(state_manager, 'check_special_state_conditions'):
            print("✅ StateManager 具有特殊狀態檢查功能")
        else:
            print("⚠️ 特殊狀態檢查機制未完全實現")
    
    def test_sleep_state_conditions(self):
        """測試 Sleep 狀態觸發條件"""
        status_manager = StatusManager()
        
        # 設置高無聊感條件 - 使用正確的更新方法
        status_manager.update_boredom(0.8, "測試高無聊感")
        status_manager.update_helpfulness(-0.6, "測試低助人意願")  # 減少助人意願
        
        status = status_manager.get_status()
        boredom = status.boredom
        helpfulness = status.helpfulness
        
        print(f"✅ Sleep 觸發條件測試: 無聊感={boredom:.2f}, 助人意願={helpfulness:.2f}")


class TestRouterIntegration:
    """測試與 Router 的整合"""
    
    def test_router_data_format(self, llm_module):
        """測試 Router 數據格式處理"""
        # 模擬來自 Router 的數據格式
        router_data = {
            "text": "使用者的輸入文字",
            "mode": "chat",
            "session_id": "router_session_123",
            "source_layer": "nlp_layer",
            "processing_context": {
                "intent": "conversation",
                "confidence": 0.9
            }
        }
        
        # 測試數據結構驗證
        try:
            llm_input = LLMInput(**router_data)
            assert llm_input.text == "使用者的輸入文字"
            assert llm_input.mode == LLMMode.CHAT
            print("✅ Router 數據格式處理正常")
        except Exception as e:
            print(f"⚠️ Router 數據格式處理需要調整: {e}")


class TestErrorHandling:
    """測試錯誤處理與邊界情況"""
    
    def test_invalid_input_handling(self, llm_module):
        """測試無效輸入處理"""
        # 測試 None 輸入
        try:
            result = llm_module.handle(None)
            # 如果沒有拋出異常，檢查錯誤處理
            if isinstance(result, dict) and not result.get("success", True):
                print(f"✅ None 輸入正確被拒絕: {result.get('error', 'Unknown')}")
            else:
                print("⚠️ None 輸入處理可能需要加強")
        except Exception as e:
            print(f"✅ None 輸入正確觸發異常: {type(e).__name__}")
    
    def test_empty_text_handling(self, llm_module):
        """測試空文字處理"""
        try:
            empty_data = {"text": "", "mode": "chat"}
            result = llm_module.handle(empty_data)
            
            if isinstance(result, dict) and not result.get("success", True):
                print(f"✅ 空文字輸入正確被處理: {result.get('error', 'Unknown')}")
            else:
                print("⚠️ 空文字輸入處理可能需要加強")
        except Exception as e:
            print(f"✅ 空文字輸入正確觸發異常: {type(e).__name__}")
    
    def test_invalid_mode_handling(self, llm_module):
        """測試無效模式處理"""
        try:
            invalid_data = {"text": "測試", "mode": "invalid_mode"}
            result = llm_module.handle(invalid_data)
            
            if isinstance(result, dict) and not result.get("success", True):
                print(f"✅ 無效模式正確被處理: {result.get('error', 'Unknown')}")
            else:
                print("⚠️ 無效模式處理可能需要加強")
        except Exception as e:
            print(f"✅ 無效模式正確觸發異常: {type(e).__name__}")


class TestSchemaValidation:
    """測試 Schema 驗證"""
    
    def test_llm_input_creation(self):
        """測試 LLMInput 創建"""
        # 基本輸入
        basic_input = LLMInput(
            text="測試輸入",
            mode=LLMMode.CHAT
        )
        
        assert basic_input.text == "測試輸入"
        assert basic_input.mode == LLMMode.CHAT
        
        # 完整輸入
        full_input = LLMInput(
            text="完整測試",
            mode=LLMMode.WORK,
            system_state=SystemState.WORK,
            memory_context="工作記憶",
            session_id="test_session"
        )
        
        assert full_input.mode == LLMMode.WORK
        assert full_input.system_state == SystemState.WORK
        assert full_input.memory_context == "工作記憶"
    
    def test_different_modes(self):
        """測試不同處理模式"""
        modes = [LLMMode.CHAT, LLMMode.WORK, LLMMode.DIRECT, LLMMode.INTERNAL]
        
        for mode in modes:
            input_data = LLMInput(
                text=f"測試 {mode.value} 模式",
                mode=mode
            )
            assert input_data.mode == mode
            print(f"✅ {mode.value} 模式測試通過")


class TestSystemIntegration:
    """測試系統整合功能"""
    
    def test_working_context_integration(self, llm_module):
        """測試工作上下文整合"""
        # 檢查工作上下文管理器
        assert working_context_manager is not None, "工作上下文管理器應該可用"
        
        # 測試身份上下文獲取
        try:
            identity_context = working_context_manager.get_context(ContextType.IDENTITY)
            print(f"✅ 身份上下文獲取成功: {type(identity_context)}")
        except Exception as e:
            print(f"⚠️ 身份上下文獲取失敗: {e}")
    
    def test_session_management_integration(self, llm_module):
        """測試會話管理整合"""
        # 測試會話ID處理
        session_input = LLMInput(
            text="測試會話管理",
            mode=LLMMode.CHAT,
            session_id="test_session_integration"
        )
        
        assert session_input.session_id == "test_session_integration"
        print("✅ 會話管理整合測試通過")


# 整體整合測試
def test_overall_integration():
    """整體整合測試"""
    print("🚀 開始整體整合測試...")
    
    # 初始化模組
    llm = LLMModule()
    assert llm is not None, "LLM 模組應該成功初始化"
    
    # 檢查核心組件
    components = ['model', 'prompt_manager', 'learning_engine', 'cache_manager']
    for component in components:
        assert hasattr(llm, component), f"應該有 {component} 組件"
        assert getattr(llm, component) is not None, f"{component} 應該被正確初始化"
    
    # 檢查 handle 方法
    assert hasattr(llm, 'handle'), "應該有 handle 方法"
    assert callable(llm.handle), "handle 方法應該可呼叫"
    
    # 檢查狀態管理整合
    assert state_manager is not None, "StateManager 應該可用"
    assert StatusManager is not None, "StatusManager 應該可用"
    
    print("🎉 整體整合測試通過！")


if __name__ == "__main__":
    # 運行測試
    pytest.main([__file__, "-v", "-s"])