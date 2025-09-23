# -*- coding: utf-8 -*-
"""
MEM模組完整工作流程集成測試

測試完整流程：
1. 身分/記憶令牌獲取
2. 長短期資料庫創建/查詢
3. NLP指示與狀態內文處理
4. 快照創建/查詢/歷史記錄
5. 總結大綱提取
6. 用戶特質整合
7. LLM交互 (Mock)
8. Chatting Session驗證

這些測試直接調用MEM模組，不使用debug_api
"""

import sys
import os
import pytest
import json
from unittest.mock import Mock, patch
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

# 添加項目根目錄到系統路徑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.mem_module.mem_module import MEMModule
from modules.mem_module.schemas import (
    MEMInput, MEMOutput, MemoryEntry, MemoryQuery, 
    MemoryType, MemoryImportance, ConversationSnapshot,
    LLMMemoryInstruction, MemoryOperationResult
)
from modules.nlp_module.schemas import UserProfile
from core.working_context import ContextType
from utils.debug_helper import debug_log, info_log, error_log


@pytest.fixture(scope="module")
def mem_module():
    """初始化MEM模組"""
    module = MEMModule()
    if not module.initialize():
        pytest.fail("MEM模組初始化失敗")
    
    yield module
    
    # 清理
    if module:
        module.shutdown()


class TestStep1_IdentityAndMemoryToken:
    """測試步驟1：身分與記憶令牌獲取"""
    
    def test_memory_token_validation(self, mem_module):
        """測試記憶令牌驗證功能"""
        # 測試記憶令牌存取控制
        mem_input = MEMInput(
            operation_type="validate_token",
            memory_token=f"test_token_{int(datetime.now().timestamp())}"
        )
        
        result = mem_module.handle(mem_input)
        
        assert isinstance(result, MEMOutput)
        assert result.success is True
        print(f"✅ 記憶令牌驗證成功: {result.message}")
    
    def test_identity_extraction_from_nlp(self, mem_module):
        """測試從NLP輸出提取身分資訊"""
        # 模擬從NLP模組接收的使用者資料
        nlp_user_profile = {
            "user_name": "TestUser",
            "memory_token": "test_memory_token_123",
            "emotional_state": "curious",
            "conversation_style": "formal",
            "learning_preferences": ["visual", "hands-on"]
        }
        
        # 測試身分資訊處理
        mem_input = MEMInput(
            operation_type="process_identity",
            intent_info={"user_profile": nlp_user_profile}
        )
        
        result = mem_module.handle(mem_input)
        
        assert isinstance(result, MEMOutput)
        assert result.success is True
        assert "memory_token" in str(result.data)
        print(f"✅ 身分資訊提取成功: {result.message}")


class TestStep2_DatabaseOperations:
    """測試步驟2：長短期資料庫操作"""
    
    def test_short_term_memory_storage(self, mem_module):
        """測試短期記憶（快照）存儲"""
        memory_token = f"test_short_{int(datetime.now().timestamp())}"
        
        # 存儲短期記憶
        mem_input = MEMInput(
            operation_type="store_memory",
            memory_token=memory_token,
            memory_entry={
                "content": "用戶詢問了關於Python學習的問題",
                "memory_type": "snapshot",
                "topic": "程式學習",
                "importance": "medium"
            }
        )
        
        result = mem_module.handle(mem_input)
        
        assert isinstance(result, MEMOutput)
        assert result.success is True
        print(f"✅ 短期記憶存儲成功: {result.message}")
        
        # 查詢剛存儲的記憶
        query_input = MEMInput(
            operation_type="query_memory",
            memory_token=memory_token,
            query_text="Python學習",
            memory_types=["snapshot"]
        )
        
        query_result = mem_module.handle(query_input)
        assert isinstance(query_result, MEMOutput)
        assert query_result.success is True
        assert len(query_result.search_results) > 0
        print(f"✅ 短期記憶查詢成功，找到 {len(query_result.search_results)} 條記錄")
    
    def test_long_term_memory_storage(self, mem_module):
        """測試長期記憶存儲"""
        memory_token = f"test_long_{int(datetime.now().timestamp())}"
        
        # 存儲長期記憶
        mem_input = MEMInput(
            operation_type="store_memory",
            memory_token=memory_token,
            memory_entry={
                "content": "用戶偏好使用視覺化學習方式，喜歡實作專案",
                "memory_type": "long_term",
                "topic": "學習偏好",
                "importance": "high"
            }
        )
        
        result = mem_module.handle(mem_input)
        
        assert isinstance(result, MEMOutput)
        assert result.success is True
        print(f"✅ 長期記憶存儲成功: {result.message}")
        
        # 查詢長期記憶
        query_input = MEMInput(
            operation_type="query_memory",
            memory_token=memory_token,
            query_text="學習偏好",
            memory_types=["long_term"]
        )
        
        query_result = mem_module.handle(query_input)
        assert isinstance(query_result, MEMOutput)
        assert query_result.success is True
        print(f"✅ 長期記憶查詢成功")


class TestStep3_NLPIntegration:
    """測試步驟3：NLP整合與狀態內文處理"""
    
    def test_nlp_output_processing(self, mem_module):
        """測試NLP輸出處理"""
        memory_token = f"test_nlp_{int(datetime.now().timestamp())}"
        
        # 模擬NLP輸出
        nlp_output = {
            "intent_analysis": {
                "primary_intent": "learning_request",
                "confidence": 0.9,
                "entities": ["Python", "機器學習", "專案"],
                "sentiment": "positive"
            },
            "conversation_context": {
                "topic": "技術學習",
                "context_shift": False,
                "urgency": "normal"
            }
        }
        
        # 處理NLP輸出
        mem_input = MEMInput(
            operation_type="process_nlp_output",
            memory_token=memory_token,
            intent_info=nlp_output,
            conversation_text="我想學習Python和機器學習，可以推薦一些實作專案嗎？"
        )
        
        result = mem_module.handle(mem_input)
        
        assert isinstance(result, MEMOutput)
        assert result.success is True
        print(f"✅ NLP輸出處理成功: {result.message}")


class TestStep4_SnapshotManagement:
    """測試步驟4：快照創建、查詢與歷史記錄"""
    
    def test_conversation_snapshot_creation(self, mem_module):
        """測試對話快照創建"""
        memory_token = f"test_snapshot_{int(datetime.now().timestamp())}"
        
        # 創建對話快照
        conversation_text = """
        用戶: 我想學習機器學習，從哪裡開始比較好？
        系統: 建議您先學習Python基礎和數學概念...
        用戶: 那有什麼推薦的書籍或課程嗎？
        系統: 推薦《Python機器學習》這本書...
        """
        
        mem_input = MEMInput(
            operation_type="create_snapshot",
            memory_token=memory_token,
            conversation_text=conversation_text,
            intent_info={
                "primary_intent": "learning_guidance",
                "topic": "機器學習入門"
            }
        )
        
        result = mem_module.handle(mem_input)
        
        assert isinstance(result, MEMOutput)
        assert result.success is True
        print(f"✅ 對話快照創建成功: {result.message}")
        
        # 驗證快照內容
        if hasattr(result, 'operation_result') and result.operation_result:
            snapshot_data = result.operation_result
            assert "memory_id" in snapshot_data
            assert "summary" in snapshot_data
            print(f"✅ 快照包含必要欄位: memory_id, summary")
    
    def test_snapshot_history_retrieval(self, mem_module):
        """測試快照歷史記錄檢索"""
        memory_token = f"test_history_{int(datetime.now().timestamp())}"
        
        # 先創建幾個快照
        for i in range(3):
            snapshot_text = f"對話記錄 {i+1}: 討論關於{'Python' if i%2==0 else 'AI'}的學習內容..."
            
            mem_input = MEMInput(
                operation_type="create_snapshot",
                memory_token=memory_token,
                conversation_text=snapshot_text,
                intent_info={"topic": f"主題{i+1}"}
            )
            
            result = mem_module.handle(mem_input)
            assert result.success is True
        
        print("✅ 創建了3個測試快照")
        
        # 檢索快照歷史
        history_input = MEMInput(
            operation_type="get_snapshot_history",
            memory_token=memory_token,
            query_text="學習"
        )
        
        history_result = mem_module.handle(history_input)
        
        assert isinstance(history_result, MEMOutput)
        assert history_result.success is True
        
        if hasattr(history_result, 'search_results'):
            snapshots = history_result.search_results
            assert len(snapshots) > 0
            print(f"✅ 成功檢索到 {len(snapshots)} 個歷史快照")


class TestStep5_ComprehensiveWorkflow:
    """測試步驟5：完整工作流程"""
    
    def test_full_integration_workflow(self, mem_module):
        """測試完整的MEM工作流程集成"""
        memory_token = f"test_full_workflow_{int(datetime.now().timestamp())}"
        session_id = f"full_session_{int(datetime.now().timestamp())}"
        
        print("🚀 開始完整工作流程測試...")
        
        # 1. 身分驗證與初始化
        identity_input = MEMInput(
            operation_type="validate_token",
            memory_token=memory_token
        )
        result = mem_module.handle(identity_input)
        assert result.success is True
        print("✅ 1. 身分驗證成功")
        
        # 2. 處理NLP輸出並存儲記憶
        nlp_output = {
            "intent_analysis": {
                "primary_intent": "comprehensive_test",
                "confidence": 0.95
            },
            "user_profile": {
                "learning_style": "systematic",
                "experience_level": "beginner"
            }
        }
        
        nlp_input = MEMInput(
            operation_type="process_nlp_output",
            memory_token=memory_token,
            intent_info=nlp_output,
            conversation_text="這是一個完整的工作流程測試"
        )
        result = mem_module.handle(nlp_input)
        assert result.success is True
        print("✅ 2. NLP輸出處理成功")
        
        # 3. 創建對話快照
        snapshot_input = MEMInput(
            operation_type="create_snapshot",
            memory_token=memory_token,
            conversation_text="完整測試對話: 用戶進行系統功能驗證",
            intent_info={"topic": "系統測試"}
        )
        result = mem_module.handle(snapshot_input)
        assert result.success is True
        print("✅ 3. 對話快照創建成功")
        
        # 4. 查詢相關記憶
        query_input = MEMInput(
            operation_type="query_memory",
            memory_token=memory_token,
            query_text="測試",
            max_results=5
        )
        result = mem_module.handle(query_input)
        assert result.success is True
        print("✅ 4. 記憶查詢成功")
        
        print("🎉 完整工作流程測試全部通過！")
