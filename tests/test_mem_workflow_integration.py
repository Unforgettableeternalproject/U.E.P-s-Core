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


class TestMEMWorkflowIntegration:
    """MEM模組完整工作流程測試"""
    
    @pytest.fixture(autouse=True)
    def setup_method(self):
        """每個測試前的設置"""
        self.test_memory_token = f"integration_test_{int(datetime.now().timestamp())}"
        self.test_user_name = "IntegrationTestUser"
        
        # 初始化MEM模組
        self.mem_module = MEMModule()
        if not self.mem_module.initialize():
            pytest.fail("MEM模組初始化失敗")
        
        # 創建測試用的NLP輸出Mock
        self.mock_nlp_output = {
            "user_profile": {
                "user_name": self.test_user_name,
                "memory_token": self.test_memory_token,
                "emotional_state": "neutral",
                "conversation_style": "friendly",
                "voice_preferences": {"speed": 1.0, "tone": "warm"}
            },
            "intent_analysis": {
                "primary_intent": "information_seeking",
                "secondary_intents": ["learning", "casual_conversation"],
                "confidence": 0.85,
                "context_tags": ["learning", "programming", "questions"]
            },
            "conversation_context": {
                "topic": "程式學習",
                "emotional_context": "好奇且積極",
                "conversation_history_summary": "使用者正在學習新的編程技術",
                "key_points": ["想學習Python", "對AI感興趣", "希望實作專案"]
            }
        }
        
        # LLM回應Mock
        self.mock_llm_response = {
            "response_text": "根據您的學習興趣，我建議您從Python基礎開始...",
            "updated_memory_points": [
                {
                    "type": "user_preference",
                    "content": "偏好學習Python和AI相關技術",
                    "importance": "high"
                },
                {
                    "type": "interaction_pattern", 
                    "content": "喜歡透過實作專案來學習",
                    "importance": "medium"
                }
            ],
            "conversation_snapshot": {
                "summary": "討論程式學習計劃，用戶展現對Python和AI的興趣",
                "key_topics": ["Python學習", "AI技術", "專案實作"],
                "emotional_tone": "積極好學",
                "followup_suggestions": ["推薦學習資源", "制定學習計劃"]
            }
        }
    
    def teardown_method(self):
        """每個測試後的清理"""
        if hasattr(self, 'mem_module') and self.mem_module:
            self.mem_module.shutdown()


class TestStep1_IdentityAndMemoryToken:
    """測試步驟1：身分與記憶令牌獲取"""
    
    def test_memory_token_validation(self):
        """測試記憶令牌驗證功能"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            # 測試記憶令牌存取控制
            mem_input = MEMInput(
                operation_type="validate_token",
                memory_token=f"test_token_{int(datetime.now().timestamp())}"
            )
            
            result = mem_module.handle(mem_input)
            
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 記憶令牌驗證成功: {result.message}")
            
        finally:
            mem_module.shutdown()
    
    def test_identity_extraction_from_nlp(self):
        """測試從NLP輸出提取身分資訊"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
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
            
        finally:
            mem_module.shutdown()


class TestStep2_DatabaseOperations:
    """測試步驟2：長短期資料庫操作"""
    
    def test_short_term_memory_storage(self):
        """測試短期記憶（快照）存儲"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
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
            
        finally:
            mem_module.shutdown()
    
    def test_long_term_memory_storage(self):
        """測試長期記憶存儲"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
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
            
        finally:
            mem_module.shutdown()


class TestStep3_NLPIntegration:
    """測試步驟3：NLP整合與狀態內文處理"""
    
    def test_nlp_output_processing(self):
        """測試NLP輸出處理"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
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
            
        finally:
            mem_module.shutdown()
    
    def test_conversation_context_handling(self):
        """測試對話狀態內文處理"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"test_context_{int(datetime.now().timestamp())}"
            
            # 處理對話上下文
            conversation_context = {
                "current_topic": "AI學習路徑",
                "previous_topics": ["程式基礎", "Python語法"],
                "user_emotional_state": "興奮且專注",
                "conversation_depth": "深入討論",
                "session_duration": "25分鐘"
            }
            
            mem_input = MEMInput(
                operation_type="update_context",
                memory_token=memory_token,
                conversation_context=conversation_context,
                intent_info={"context_update": True}
            )
            
            result = mem_module.handle(mem_input)
            
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 對話上下文處理成功: {result.message}")
            
        finally:
            mem_module.shutdown()


class TestStep4_SnapshotManagement:
    """測試步驟4：快照創建、查詢與歷史記錄"""
    
    def test_conversation_snapshot_creation(self):
        """測試對話快照創建"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
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
            
        finally:
            mem_module.shutdown()
    
    def test_snapshot_history_retrieval(self):
        """測試快照歷史記錄檢索"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
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
            
        finally:
            mem_module.shutdown()


class TestStep5_SummaryExtraction:
    """測試步驟5：總結與大綱提取"""
    
    def test_conversation_summary_generation(self):
        """測試對話總結生成"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"test_summary_{int(datetime.now().timestamp())}"
            
            # 創建包含詳細對話的快照
            detailed_conversation = """
            用戶: 我是程式新手，想學習Python
            系統: 很好的選擇！Python是很適合初學者的語言
            用戶: 需要什麼基礎知識嗎？
            系統: 基本的邏輯思維即可，不需要太多數學背景
            用戶: 那學習順序是什麼？
            系統: 建議從語法基礎開始，然後是數據結構，最後是實際專案
            用戶: 大概需要多長時間？
            系統: 如果每天學習2小時，大約3-6個月可以掌握基礎
            """
            
            mem_input = MEMInput(
                operation_type="generate_summary",
                memory_token=memory_token,
                conversation_text=detailed_conversation,
                intent_info={
                    "extract_key_points": True,
                    "generate_outline": True
                }
            )
            
            result = mem_module.handle(mem_input)
            
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 對話總結生成成功: {result.message}")
            
            # 驗證總結內容
            if hasattr(result, 'operation_result'):
                summary_data = result.operation_result
                assert isinstance(summary_data, dict)
                print(f"✅ 總結數據格式正確")
            
        finally:
            mem_module.shutdown()
    
    def test_key_points_extraction(self):
        """測試關鍵要點提取"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"test_keypoints_{int(datetime.now().timestamp())}"
            
            # 模擬包含多個要點的對話
            conversation_with_keypoints = """
            這次對話的重要要點：
            1. 用戶是程式新手
            2. 選擇Python作為第一語言
            3. 學習時間安排：每天2小時
            4. 預期學習週期：3-6個月
            5. 學習順序：語法→數據結構→專案實作
            6. 用戶偏好：實作導向學習
            """
            
            mem_input = MEMInput(
                operation_type="extract_key_points",
                memory_token=memory_token,
                conversation_text=conversation_with_keypoints
            )
            
            result = mem_module.handle(mem_input)
            
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 關鍵要點提取成功: {result.message}")
            
        finally:
            mem_module.shutdown()


class TestStep6_UserCharacteristicsIntegration:
    """測試步驟6：用戶特質整合到長期記憶"""
    
    def test_user_preferences_integration(self):
        """測試用戶偏好整合"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"test_preferences_{int(datetime.now().timestamp())}"
            
            # 整合用戶學習偏好
            user_characteristics = {
                "learning_style": "visual_and_practical",
                "preferred_pace": "steady_progress",
                "interaction_style": "question_driven",
                "technical_background": "beginner",
                "interests": ["web_development", "data_science", "automation"],
                "time_availability": "2_hours_daily",
                "goals": ["build_portfolio", "career_change", "personal_projects"]
            }
            
            mem_input = MEMInput(
                operation_type="integrate_user_characteristics",
                memory_token=memory_token,
                user_profile=user_characteristics
            )
            
            result = mem_module.handle(mem_input)
            
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 用戶特質整合成功: {result.message}")
            
            # 驗證特質是否正確存儲到長期記憶
            query_input = MEMInput(
                operation_type="query_memory",
                memory_token=memory_token,
                query_text="用戶偏好",
                memory_types=["long_term"]
            )
            
            query_result = mem_module.handle(query_input)
            assert isinstance(query_result, MEMOutput)
            assert query_result.success is True
            print(f"✅ 用戶特質查詢驗證成功")
            
        finally:
            mem_module.shutdown()


class TestStep7_LLMInteractionMock:
    """測試步驟7：LLM交互Mock測試"""
    
    def test_llm_memory_instruction_generation(self):
        """測試LLM記憶指令生成"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"test_llm_{int(datetime.now().timestamp())}"
            
            # 先存儲一些相關記憶
            memories_to_store = [
                {
                    "content": "用戶偏好視覺化學習",
                    "memory_type": "long_term",
                    "topic": "學習偏好",
                    "importance": "high"
                },
                {
                    "content": "用戶詢問Python入門問題",
                    "memory_type": "snapshot", 
                    "topic": "程式學習",
                    "importance": "medium"
                },
                {
                    "content": "用戶表現出對AI的興趣",
                    "memory_type": "long_term",
                    "topic": "興趣領域",
                    "importance": "medium"
                }
            ]
            
            for memory in memories_to_store:
                store_input = MEMInput(
                    operation_type="store_memory",
                    memory_token=memory_token,
                    memory_entry=memory
                )
                result = mem_module.handle(store_input)
                assert result.success is True
            
            print("✅ 存儲了測試記憶數據")
            
            # 生成LLM記憶指令
            llm_instruction_input = MEMInput(
                operation_type="generate_llm_instruction",
                memory_token=memory_token,
                query_text="如何幫助用戶學習程式設計",
                conversation_context="用戶正在尋求學習建議"
            )
            
            result = mem_module.handle(llm_instruction_input)
            
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ LLM記憶指令生成成功: {result.message}")
            
            # 驗證指令內容
            if hasattr(result, 'llm_instruction') and result.llm_instruction:
                instruction = result.llm_instruction
                assert isinstance(instruction, dict)
                print(f"✅ LLM指令格式正確")
            
        finally:
            mem_module.shutdown()
    
    def test_mock_llm_response_processing(self):
        """測試模擬LLM回應處理"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"test_llm_response_{int(datetime.now().timestamp())}"
            
            # 模擬LLM回應
            mock_llm_response = {
                "response_text": "根據您的學習偏好，我建議從Python基礎語法開始...",
                "confidence": 0.9,
                "updated_user_model": {
                    "learning_progress": "beginner_python_syntax",
                    "next_recommended_topics": ["variables", "data_types", "control_structures"],
                    "estimated_completion_time": "2_weeks"
                },
                "memory_updates": [
                    {
                        "type": "conversation_outcome",
                        "content": "提供了Python學習路徑建議",
                        "importance": "medium"
                    }
                ]
            }
            
            # 處理LLM回應
            process_input = MEMInput(
                operation_type="process_llm_response",
                memory_token=memory_token,
                llm_response=mock_llm_response
            )
            
            result = mem_module.handle(process_input)
            
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ LLM回應處理成功: {result.message}")
            
        finally:
            mem_module.shutdown()


class TestStep8_ChattingSessionValidation:
    """測試步驟8：Chatting Session功能驗證"""
    
    def test_session_creation_and_management(self):
        """測試會話創建與管理"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"test_session_{int(datetime.now().timestamp())}"
            session_id = f"session_{int(datetime.now().timestamp())}"
            
            # 創建新會話
            session_input = MEMInput(
                operation_type="create_session",
                memory_token=memory_token,
                session_id=session_id,
                session_metadata={
                    "topic": "Python學習諮詢",
                    "expected_duration": "60_minutes",
                    "session_type": "learning_guidance"
                }
            )
            
            result = mem_module.handle(session_input)
            
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 會話創建成功: {result.message}")
            
            # 會話中添加多個互動
            interactions = [
                "用戶: 我想學習Python",
                "系統: 很好的選擇！我來為您制定學習計劃",
                "用戶: 我每天有2小時時間",
                "系統: 那麼我們可以安排一個循序漸進的學習計劃"
            ]
            
            for interaction in interactions:
                interaction_input = MEMInput(
                    operation_type="add_session_interaction",
                    memory_token=memory_token,
                    session_id=session_id,
                    interaction_content=interaction
                )
                
                result = mem_module.handle(interaction_input)
                assert result.success is True
            
            print(f"✅ 添加了 {len(interactions)} 個會話互動")
            
            # 結束會話並創建總結
            end_session_input = MEMInput(
                operation_type="end_session",
                memory_token=memory_token,
                session_id=session_id,
                create_summary=True
            )
            
            result = mem_module.handle(end_session_input)
            
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 會話結束並創建總結成功: {result.message}")
            
        finally:
            mem_module.shutdown()
    
    def test_session_context_preservation(self):
        """測試會話上下文保存"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"test_context_preservation_{int(datetime.now().timestamp())}"
            session_id = f"session_context_{int(datetime.now().timestamp())}"
            
            # 測試長會話的上下文保存
            session_data = {
                "start_time": datetime.now().isoformat(),
                "participant_count": 2,
                "context_transitions": [
                    {"from": "greeting", "to": "learning_inquiry", "timestamp": "00:02:00"},
                    {"from": "learning_inquiry", "to": "detailed_planning", "timestamp": "00:15:00"},
                    {"from": "detailed_planning", "to": "resource_recommendation", "timestamp": "00:35:00"}
                ],
                "emotional_journey": [
                    {"emotion": "curious", "timestamp": "00:00:00"},
                    {"emotion": "excited", "timestamp": "00:15:00"},
                    {"emotion": "focused", "timestamp": "00:30:00"}
                ]
            }
            
            context_input = MEMInput(
                operation_type="preserve_session_context",
                memory_token=memory_token,
                session_id=session_id,
                session_context=session_data
            )
            
            result = mem_module.handle(context_input)
            
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 會話上下文保存成功: {result.message}")
            
            # 驗證上下文可以被檢索
            retrieve_input = MEMInput(
                operation_type="retrieve_session_context",
                memory_token=memory_token,
                session_id=session_id
            )
            
            retrieve_result = mem_module.handle(retrieve_input)
            assert isinstance(retrieve_result, MEMOutput)
            assert retrieve_result.success is True
            print(f"✅ 會話上下文檢索成功")
            
        finally:
            mem_module.shutdown()


class TestCompleteWorkflow:
    """完整工作流程集成測試"""
    
    def test_full_integration_workflow(self):
        """測試完整的MEM工作流程集成"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
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
            
            # 2. 創建會話
            session_input = MEMInput(
                operation_type="create_session",
                memory_token=memory_token,
                session_id=session_id,
                session_metadata={"topic": "完整流程測試"}
            )
            result = mem_module.handle(session_input)
            assert result.success is True
            print("✅ 2. 會話創建成功")
            
            # 3. 處理NLP輸出並存儲記憶
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
            print("✅ 3. NLP輸出處理成功")
            
            # 4. 創建對話快照
            snapshot_input = MEMInput(
                operation_type="create_snapshot",
                memory_token=memory_token,
                conversation_text="完整測試對話: 用戶進行系統功能驗證",
                intent_info={"topic": "系統測試"}
            )
            result = mem_module.handle(snapshot_input)
            assert result.success is True
            print("✅ 4. 對話快照創建成功")
            
            # 5. 查詢相關記憶
            query_input = MEMInput(
                operation_type="query_memory",
                memory_token=memory_token,
                query_text="測試",
                max_results=5
            )
            result = mem_module.handle(query_input)
            assert result.success is True
            print("✅ 5. 記憶查詢成功")
            
            # 6. 生成LLM指令
            llm_instruction_input = MEMInput(
                operation_type="generate_llm_instruction",
                memory_token=memory_token,
                query_text="系統測試指導"
            )
            result = mem_module.handle(llm_instruction_input)
            assert result.success is True
            print("✅ 6. LLM指令生成成功")
            
            # 7. 模擬LLM回應處理
            mock_response = {
                "response_text": "測試完成，所有功能正常運作",
                "memory_updates": [
                    {
                        "type": "test_completion",
                        "content": "完整工作流程測試成功",
                        "importance": "high"
                    }
                ]
            }
            
            llm_response_input = MEMInput(
                operation_type="process_llm_response",
                memory_token=memory_token,
                llm_response=mock_response
            )
            result = mem_module.handle(llm_response_input)
            assert result.success is True
            print("✅ 7. LLM回應處理成功")
            
            # 8. 結束會話
            end_input = MEMInput(
                operation_type="end_session",
                memory_token=memory_token,
                session_id=session_id,
                create_summary=True
            )
            result = mem_module.handle(end_input)
            assert result.success is True
            print("✅ 8. 會話結束成功")
            
            print("🎉 完整工作流程測試全部通過！")
            
        finally:
            mem_module.shutdown()


if __name__ == "__main__":
    # 可以直接運行這個檔案進行測試
    print("運行MEM模組完整工作流程集成測試...")
    pytest.main([__file__, "-v"])