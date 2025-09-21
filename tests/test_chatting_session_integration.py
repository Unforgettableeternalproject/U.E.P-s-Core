# -*- coding: utf-8 -*-
"""
Chatting Session (CS) 功能驗證測試

驗證MEM模組中的會話系統功能：
1. 會話創建與管理
2. 會話上下文保持
3. 多輪對話處理
4. 會話記憶整合
5. 會話結束與總結
"""

import sys
import os
import pytest
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List

# 添加項目根目錄到系統路徑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.mem_module.mem_module import MEMModule
from modules.mem_module.schemas import MEMInput, MEMOutput
from tests.llm_mock_service import create_mock_llm_service
from utils.debug_helper import debug_log, info_log, error_log


class TestChattingSessionFeatures:
    """Chatting Session功能測試"""
    
    @pytest.fixture(autouse=True)
    def setup_method(self):
        """測試設置"""
        self.mem_module = MEMModule()
        if not self.mem_module.initialize():
            pytest.fail("MEM模組初始化失敗")
        
        self.test_memory_token = f"cs_test_{int(datetime.now().timestamp())}"
        self.llm_mock = create_mock_llm_service()
        
    def teardown_method(self):
        """測試清理"""
        if hasattr(self, 'mem_module') and self.mem_module:
            self.mem_module.shutdown()


class TestSessionLifecycle:
    """會話生命週期測試"""
    
    def test_session_creation(self):
        """測試會話創建"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"session_create_{int(datetime.now().timestamp())}"
            session_id = f"session_{int(datetime.now().timestamp())}"
            
            # 創建新會話
            create_input = MEMInput(
                operation_type="create_session",
                identity_token=memory_token,
                session_id=session_id,
                session_metadata={
                    "topic": "Python學習諮詢",
                    "expected_duration": "45分鐘",
                    "session_type": "教學指導",
                    "participant_count": 2
                }
            )
            
            result = mem_module.handle(create_input)
            
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 會話創建成功: {session_id}")
            
            # 驗證會話存在
            check_input = MEMInput(
                operation_type="get_session_info",
                identity_token=memory_token,
                session_id=session_id
            )
            
            check_result = mem_module.handle(check_input)
            assert isinstance(check_result, MEMOutput)
            assert check_result.success is True
            print(f"✅ 會話信息查詢成功")
            
        finally:
            mem_module.shutdown()
    
    def test_session_interaction_management(self):
        """測試會話互動管理"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"session_interaction_{int(datetime.now().timestamp())}"
            session_id = f"session_{int(datetime.now().timestamp())}"
            
            # 創建會話
            create_input = MEMInput(
                operation_type="create_session",
                identity_token=memory_token,
                session_id=session_id,
                session_metadata={"topic": "技術問答"}
            )
            result = mem_module.handle(create_input)
            assert result.success is True
            
            # 添加多個互動
            interactions = [
                {
                    "speaker": "user",
                    "content": "我想學習機器學習，需要什麼基礎？",
                    "timestamp": datetime.now().isoformat(),
                    "intent": "learning_inquiry"
                },
                {
                    "speaker": "system", 
                    "content": "機器學習需要一些數學基礎，特別是線性代數和統計學...",
                    "timestamp": (datetime.now() + timedelta(seconds=30)).isoformat(),
                    "intent": "educational_response"
                },
                {
                    "speaker": "user",
                    "content": "那Python程度需要到什麼水準？",
                    "timestamp": (datetime.now() + timedelta(minutes=1)).isoformat(),
                    "intent": "clarification_request"
                },
                {
                    "speaker": "system",
                    "content": "Python基礎語法掌握即可，重點是numpy和pandas庫...",
                    "timestamp": (datetime.now() + timedelta(minutes=2)).isoformat(),
                    "intent": "detailed_guidance"
                }
            ]
            
            for i, interaction in enumerate(interactions):
                add_input = MEMInput(
                    operation_type="add_session_interaction",
                    identity_token=memory_token,
                    session_id=session_id,
                    interaction_data=interaction
                )
                
                result = mem_module.handle(add_input)
                assert result.success is True
                print(f"✅ 添加互動 {i+1}/{len(interactions)}")
            
            # 獲取會話歷史
            history_input = MEMInput(
                operation_type="get_session_history",
                identity_token=memory_token,
                session_id=session_id
            )
            
            history_result = mem_module.handle(history_input)
            assert isinstance(history_result, MEMOutput)
            assert history_result.success is True
            print(f"✅ 會話歷史檢索成功")
            
        finally:
            mem_module.shutdown()
    
    def test_session_context_preservation(self):
        """測試會話上下文保持"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"session_context_{int(datetime.now().timestamp())}"
            session_id = f"session_{int(datetime.now().timestamp())}"
            
            # 創建會話
            create_input = MEMInput(
                operation_type="create_session",
                identity_token=memory_token,
                session_id=session_id
            )
            result = mem_module.handle(create_input)
            assert result.success is True
            
            # 設置會話上下文
            context_data = {
                "current_topic": "機器學習入門",
                "user_background": "Python初學者",
                "learning_goals": ["理解基本概念", "學會使用工具", "完成小專案"],
                "session_progress": {
                    "topics_covered": ["什麼是機器學習", "需要的背景知識"],
                    "current_focus": "Python程度要求",
                    "next_topics": ["推薦學習資源", "實作練習"]
                },
                "emotional_context": {
                    "user_engagement": "high",
                    "confidence_level": "moderate",
                    "curiosity_indicators": ["多個follow-up問題", "詳細要求"]
                }
            }
            
            context_input = MEMInput(
                operation_type="update_session_context",
                identity_token=memory_token,
                session_id=session_id,
                session_context=context_data
            )
            
            result = mem_module.handle(context_input)
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 會話上下文更新成功")
            
            # 檢索上下文
            retrieve_input = MEMInput(
                operation_type="get_session_context",
                identity_token=memory_token,
                session_id=session_id
            )
            
            retrieve_result = mem_module.handle(retrieve_input)
            assert isinstance(retrieve_result, MEMOutput)
            assert retrieve_result.success is True
            print(f"✅ 會話上下文檢索成功")
            
        finally:
            mem_module.shutdown()


class TestMultiTurnConversation:
    """多輪對話處理測試"""
    
    def test_context_continuity(self):
        """測試上下文連續性"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"multi_turn_{int(datetime.now().timestamp())}"
            session_id = f"session_{int(datetime.now().timestamp())}"
            
            # 創建會話
            create_input = MEMInput(
                operation_type="create_session",
                identity_token=memory_token,
                session_id=session_id
            )
            result = mem_module.handle(create_input)
            assert result.success is True
            
            # 模擬多輪對話
            conversation_turns = [
                {
                    "turn": 1,
                    "user_input": "我想學習深度學習",
                    "context_expectation": "建立基礎學習目標"
                },
                {
                    "turn": 2,
                    "user_input": "我已經會Python了",
                    "context_expectation": "更新用戶技能背景，調整建議"
                },
                {
                    "turn": 3,
                    "user_input": "那我需要學什麼數學？",
                    "context_expectation": "基於Python背景提供數學建議"
                },
                {
                    "turn": 4,
                    "user_input": "線性代數我學過，統計學沒有",
                    "context_expectation": "個性化統計學習建議"
                },
                {
                    "turn": 5,
                    "user_input": "有推薦的統計學資源嗎？",
                    "context_expectation": "提供具體資源，考慮技術背景"
                }
            ]
            
            accumulated_context = {}
            
            for turn_data in conversation_turns:
                # 處理用戶輸入
                turn_input = MEMInput(
                    operation_type="process_conversation_turn",
                    identity_token=memory_token,
                    session_id=session_id,
                    conversation_text=turn_data["user_input"],
                    turn_number=turn_data["turn"],
                    previous_context=accumulated_context
                )
                
                result = mem_module.handle(turn_input)
                assert isinstance(result, MEMOutput)
                assert result.success is True
                
                # 更新累積上下文
                if hasattr(result, 'session_context'):
                    accumulated_context.update(result.session_context or {})
                
                print(f"✅ 處理第 {turn_data['turn']} 輪對話: {turn_data['user_input'][:30]}...")
            
            print(f"✅ 多輪對話上下文連續性測試完成")
            
        finally:
            mem_module.shutdown()
    
    def test_topic_transition_handling(self):
        """測試話題轉換處理"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"topic_transition_{int(datetime.now().timestamp())}"
            session_id = f"session_{int(datetime.now().timestamp())}"
            
            # 創建會話
            create_input = MEMInput(
                operation_type="create_session",
                identity_token=memory_token,
                session_id=session_id
            )
            result = mem_module.handle(create_input)
            assert result.success is True
            
            # 模擬話題轉換
            topic_transitions = [
                {
                    "phase": "initial",
                    "topic": "Python學習",
                    "user_input": "我想學Python網頁開發",
                    "expected_context": "web_development_focus"
                },
                {
                    "phase": "development",
                    "topic": "Python學習-深入",
                    "user_input": "Django和Flask哪個比較適合初學者？",
                    "expected_context": "framework_comparison"
                },
                {
                    "phase": "transition",
                    "topic": "部署相關",
                    "user_input": "學會後要怎麼部署到雲端？",
                    "expected_context": "deployment_inquiry"
                },
                {
                    "phase": "new_topic",
                    "topic": "雲端技術",
                    "user_input": "AWS和Azure哪個比較好？",
                    "expected_context": "cloud_platform_comparison"
                }
            ]
            
            for transition in topic_transitions:
                transition_input = MEMInput(
                    operation_type="handle_topic_transition",
                    identity_token=memory_token,
                    session_id=session_id,
                    conversation_text=transition["user_input"],
                    current_topic=transition["topic"],
                    transition_phase=transition["phase"]
                )
                
                result = mem_module.handle(transition_input)
                assert isinstance(result, MEMOutput)
                assert result.success is True
                
                print(f"✅ 處理話題轉換: {transition['phase']} -> {transition['topic']}")
            
            print(f"✅ 話題轉換處理測試完成")
            
        finally:
            mem_module.shutdown()


class TestSessionMemoryIntegration:
    """會話記憶整合測試"""
    
    def test_session_to_memory_conversion(self):
        """測試會話轉記憶功能"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"session_memory_{int(datetime.now().timestamp())}"
            session_id = f"session_{int(datetime.now().timestamp())}"
            
            # 創建並運行完整會話
            create_input = MEMInput(
                operation_type="create_session",
                identity_token=memory_token,
                session_id=session_id
            )
            result = mem_module.handle(create_input)
            assert result.success is True
            
            # 添加有意義的互動
            meaningful_interactions = [
                "用戶表達學習Python的強烈意願",
                "用戶透露已有Java基礎，學習能力強",
                "用戶偏好實作導向的學習方式",
                "用戶目標是在3個月內完成第一個網站專案",
                "用戶詢問了許多深入的技術問題，顯示高度興趣"
            ]
            
            for interaction in meaningful_interactions:
                add_input = MEMInput(
                    operation_type="add_session_interaction",
                    identity_token=memory_token,
                    session_id=session_id,
                    interaction_content=interaction
                )
                result = mem_module.handle(add_input)
                assert result.success is True
            
            # 轉換會話為記憶
            convert_input = MEMInput(
                operation_type="convert_session_to_memory",
                identity_token=memory_token,
                session_id=session_id,
                memory_extraction_options={
                    "extract_user_preferences": True,
                    "extract_learning_patterns": True,
                    "extract_goals": True,
                    "create_summary": True
                }
            )
            
            result = mem_module.handle(convert_input)
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 會話轉記憶成功")
            
            # 驗證生成的記憶
            query_input = MEMInput(
                operation_type="query_memory",
                identity_token=memory_token,
                query_text="用戶學習偏好",
                memory_types=["long_term"]
            )
            
            query_result = mem_module.handle(query_input)
            assert isinstance(query_result, MEMOutput)
            assert query_result.success is True
            print(f"✅ 記憶查詢驗證成功")
            
        finally:
            mem_module.shutdown()
    
    def test_cross_session_memory_continuity(self):
        """測試跨會話記憶連續性"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"cross_session_{int(datetime.now().timestamp())}"
            
            # 第一個會話
            session1_id = f"session1_{int(datetime.now().timestamp())}"
            create_input1 = MEMInput(
                operation_type="create_session",
                identity_token=memory_token,
                session_id=session1_id,
                session_metadata={"topic": "Python基礎學習"}
            )
            result = mem_module.handle(create_input1)
            assert result.success is True
            
            # 在第一個會話中建立記憶
            memory_input1 = MEMInput(
                operation_type="store_session_memory",
                identity_token=memory_token,
                session_id=session1_id,
                memory_entry={
                    "content": "用戶偏好視覺化學習，對Python語法已有基本了解",
                    "memory_type": "long_term",
                    "importance": "high"
                }
            )
            result = mem_module.handle(memory_input1)
            assert result.success is True
            
            # 結束第一個會話
            end_input1 = MEMInput(
                operation_type="end_session",
                identity_token=memory_token,
                session_id=session1_id,
                create_summary=True
            )
            result = mem_module.handle(end_input1)
            assert result.success is True
            print(f"✅ 第一個會話完成")
            
            # 第二個會話（應該能訪問之前的記憶）
            session2_id = f"session2_{int(datetime.now().timestamp())}"
            create_input2 = MEMInput(
                operation_type="create_session",
                identity_token=memory_token,
                session_id=session2_id,
                session_metadata={"topic": "Python進階應用"},
                load_previous_context=True
            )
            result = mem_module.handle(create_input2)
            assert result.success is True
            
            # 查詢之前會話的記憶
            context_query = MEMInput(
                operation_type="load_user_context",
                identity_token=memory_token,
                session_id=session2_id,
                context_scope="all_previous_sessions"
            )
            
            context_result = mem_module.handle(context_query)
            assert isinstance(context_result, MEMOutput)
            assert context_result.success is True
            print(f"✅ 跨會話上下文載入成功")
            
            # 在第二個會話中使用之前的記憶
            guided_input = MEMInput(
                operation_type="provide_contextual_guidance",
                identity_token=memory_token,
                session_id=session2_id,
                query_text="我想學習更進階的Python功能",
                use_previous_context=True
            )
            
            guided_result = mem_module.handle(guided_input)
            assert isinstance(guided_result, MEMOutput)
            assert guided_result.success is True
            print(f"✅ 基於歷史記憶的指導成功")
            
        finally:
            mem_module.shutdown()


class TestSessionEndAndSummary:
    """會話結束與總結測試"""
    
    def test_session_summary_generation(self):
        """測試會話總結生成"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"session_summary_{int(datetime.now().timestamp())}"
            session_id = f"session_{int(datetime.now().timestamp())}"
            
            # 創建完整會話
            create_input = MEMInput(
                operation_type="create_session",
                identity_token=memory_token,
                session_id=session_id
            )
            result = mem_module.handle(create_input)
            assert result.success is True
            
            # 添加豐富的會話內容
            session_content = {
                "main_interactions": [
                    "用戶詢問機器學習入門路徑",
                    "討論所需的數學基礎",
                    "推薦具體的學習資源",
                    "制定3個月學習計劃",
                    "確定第一個實作專案"
                ],
                "key_decisions": [
                    "選擇Python作為主要程式語言",
                    "決定先學統計學再學線性代數",
                    "選擇Coursera的機器學習課程",
                    "訂定每週學習時數目標"
                ],
                "user_insights": [
                    "學習動機強烈，目標明確",
                    "有程式基礎，學習能力佳",
                    "偏好結構化學習路徑",
                    "重視實作應用"
                ]
            }
            
            # 記錄會話內容
            content_input = MEMInput(
                operation_type="record_session_content",
                identity_token=memory_token,
                session_id=session_id,
                session_content=session_content
            )
            result = mem_module.handle(content_input)
            assert result.success is True
            
            # 生成會話總結
            summary_input = MEMInput(
                operation_type="generate_session_summary",
                identity_token=memory_token,
                session_id=session_id,
                summary_options={
                    "include_key_outcomes": True,
                    "include_user_insights": True,
                    "include_follow_up_actions": True,
                    "include_learning_progress": True
                }
            )
            
            result = mem_module.handle(summary_input)
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 會話總結生成成功")
            
            # 驗證總結內容
            if hasattr(result, 'session_summary'):
                summary = result.session_summary
                assert isinstance(summary, dict)
                print(f"✅ 總結包含完整結構")
            
        finally:
            mem_module.shutdown()
    
    def test_session_cleanup_and_archival(self):
        """測試會話清理與歸檔"""
        mem_module = MEMModule()
        mem_module.initialize()
        
        try:
            memory_token = f"session_cleanup_{int(datetime.now().timestamp())}"
            session_id = f"session_{int(datetime.now().timestamp())}"
            
            # 創建並完成會話
            create_input = MEMInput(
                operation_type="create_session",
                identity_token=memory_token,
                session_id=session_id
            )
            result = mem_module.handle(create_input)
            assert result.success is True
            
            # 歸檔會話
            archive_input = MEMInput(
                operation_type="archive_session",
                identity_token=memory_token,
                session_id=session_id,
                archive_options={
                    "preserve_full_content": True,
                    "extract_key_learnings": True,
                    "create_searchable_index": True
                }
            )
            
            result = mem_module.handle(archive_input)
            assert isinstance(result, MEMOutput)
            assert result.success is True
            print(f"✅ 會話歸檔成功")
            
            # 驗證歸檔的會話可以被搜尋
            search_input = MEMInput(
                operation_type="search_archived_sessions",
                identity_token=memory_token,
                query_text="學習計劃",
                search_scope="archived_sessions"
            )
            
            search_result = mem_module.handle(search_input)
            assert isinstance(search_result, MEMOutput)
            assert search_result.success is True
            print(f"✅ 歸檔會話搜尋成功")
            
        finally:
            mem_module.shutdown()


if __name__ == "__main__":
    # 執行測試
    print("運行Chatting Session集成測試...")
    pytest.main([__file__, "-v"])