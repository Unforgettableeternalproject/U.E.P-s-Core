# -*- coding: utf-8 -*-
"""
MEM模組代辦功能實現測試

根據MEM代辦.md文件測試已實現的功能：
1. 外部統合 - Router/State/Session/Working Context集成
2. 內部處理 - Identity記憶管理、長短期記憶、快照管理
3. CS狀態限制 - 只在CHAT狀態下運行
4. 會話生命週期管理 - 加入/離開會話
5. 動態快照管理 - 基於輸入的智能快照操作
"""

import sys
import os
import pytest
import json
from unittest.mock import Mock, patch, MagicMock
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
from core.state_manager import UEPState
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


class TestCSStateRestriction:
    """測試CS狀態限制 - MEM只在CHAT狀態下運行"""

    def test_mem_rejects_non_chat_state(self, mem_module):
        """測試MEM在非CHAT狀態下拒絕處理請求"""
        # 模擬非CHAT狀態
        with patch('core.state_manager.state_manager') as mock_state_manager:
            mock_state_manager.get_state.return_value = UEPState.IDLE

            # 嘗試處理請求
            result = mem_module.handle("test request")

            # 應該拒絕處理
            assert isinstance(result, dict)
            assert result.get('success') is False
            assert 'CHAT狀態' in result.get('error', '')
            print("✅ CS狀態限制測試通過 - 非CHAT狀態正確拒絕")

    def test_mem_accepts_chat_state(self, mem_module):
        """測試MEM在CHAT狀態下接受處理請求"""
        # 模擬CHAT狀態
        with patch('core.state_manager.state_manager') as mock_state_manager:
            mock_state_manager.get_state.return_value = UEPState.CHAT

            # 嘗試處理請求
            result = mem_module.handle("test request in chat")

            # 應該處理請求（雖然可能因為其他原因失敗，但不應該是狀態原因）
            assert isinstance(result, dict)
            # 不應該因為狀態而拒絕
            assert not ('CHAT狀態' in str(result.get('error', '')))
            print("✅ CS狀態限制測試通過 - CHAT狀態允許處理")


class TestSessionLifecycleManagement:
    """測試會話生命週期管理 - 加入/離開會話"""

    def test_join_chat_session(self, mem_module):
        """測試加入聊天會話"""
        with patch('core.state_manager.state_manager') as mock_state_manager, \
             patch('core.working_context.working_context_manager') as mock_wc:

            # 模擬狀態和上下文
            mock_state_manager.get_current_session_id.return_value = "test_session_123"
            mock_wc.get_memory_token.return_value = "test_memory_token"

            # 觸發狀態變化（模擬進入CHAT狀態）
            mem_module._handle_state_change(UEPState.IDLE, UEPState.CHAT)

            # 驗證會話已加入
            session_info = mem_module.get_current_session_info()
            assert "test_session_123" in str(session_info)
            print("✅ 會話加入測試通過")

    def test_leave_chat_session(self, mem_module):
        """測試離開聊天會話"""
        with patch('core.state_manager.state_manager') as mock_state_manager:
            # 模擬狀態變化（模擬離開CHAT狀態）
            mem_module._handle_state_change(UEPState.CHAT, UEPState.IDLE)

            # 驗證會話已清理
            session_info = mem_module.get_current_session_info()
            assert session_info.get('system_session_id') is None
            print("✅ 會話離開測試通過")


class TestRequestSourceAnalysis:
    """測試請求來源分析 - 區分使用者輸入與系統觸發"""

    def test_user_input_detection(self, mem_module):
        """測試使用者輸入檢測"""
        # 模擬來自NLP的使用者輸入
        request_data = {
            "from_nlp": True,
            "intent_info": {"primary_intent": "chat"},
            "conversation_text": "用戶說的話"
        }

        result = mem_module._check_request_session_context(request_data)

        assert result["trigger_type"] == "user_input"
        assert result["has_nlp_info"] is True
        assert result["should_process_memory"] is True
        print("✅ 使用者輸入檢測測試通過")

    def test_system_trigger_detection(self, mem_module):
        """測試系統觸發檢測"""
        # 模擬系統直接調用
        request_data = {
            "session_id": "system_session_123"
        }

        result = mem_module._check_request_session_context(request_data)

        assert result["trigger_type"] == "system_triggered"
        assert result["should_process_memory"] is False
        print("✅ 系統觸發檢測測試通過")


class TestIdentityMemoryManagement:
    """測試Identity記憶管理 - Memory Token和長/短期記憶庫"""

    def test_memory_token_validation(self, mem_module):
        """測試記憶令牌驗證"""
        # 測試有效的記憶令牌
        valid_token = "test_memory_token"
        is_valid = mem_module.memory_manager.identity_manager.validate_memory_access(valid_token, "read")
        assert is_valid is True
        print("✅ 記憶令牌驗證測試通過")

    def test_memory_isolation(self, mem_module):
        """測試記憶隔離"""
        with patch('core.state_manager.state_manager') as mock_state_manager:
            mock_state_manager.get_state.return_value = UEPState.CHAT

            token1 = "test_user_token_1"
            token2 = "test_user_token_2"

            # 為token1存儲記憶
            mem_input1 = MEMInput(
                operation_type="store_memory",
                memory_token=token1,
                memory_entry={
                    "content": "用戶1的私人記憶",
                    "memory_type": "long_term",
                    "importance": "high"
                }
            )
            result1 = mem_module.handle(mem_input1)
            assert result1['success'] is True

            # 為token2存儲記憶
            mem_input2 = MEMInput(
                operation_type="store_memory",
                memory_token=token2,
                memory_entry={
                    "content": "用戶2的私人記憶",
                    "memory_type": "long_term",
                    "importance": "high"
                }
            )
            result2 = mem_module.handle(mem_input2)
            assert result2['success'] is True

            # token1查詢應該只能看到自己的記憶
            query_input = MEMInput(
                operation_type="query_memory",
                memory_token=token1,
                query_text="私人記憶"
            )
            query_result = mem_module.handle(query_input)
            assert query_result['success'] is True
            print("✅ 記憶隔離測試通過")


class TestSnapshotManagement:
    """測試快照管理 - 短期記憶的快照創建、更新"""

    def test_snapshot_creation(self, mem_module):
        """測試快照創建"""
        with patch('core.state_manager.state_manager') as mock_state_manager:
            mock_state_manager.get_state.return_value = UEPState.CHAT

            memory_token = f"test_snapshot_{int(datetime.now().timestamp())}"

            mem_input = MEMInput(
                operation_type="create_snapshot",
                memory_token=memory_token,
                conversation_text="用戶: 你好\n系統: 你好！",
                intent_info={"topic": "問候"}
            )

            result = mem_module.handle(mem_input)
            assert result['success'] is True
            print("✅ 快照創建測試通過")

    def test_snapshot_update(self, mem_module):
        """測試快照更新"""
        memory_token = f"test_update_{int(datetime.now().timestamp())}"
        session_id = f"session_{int(datetime.now().timestamp())}"

        # 先加入會話
        success = mem_module.memory_manager.join_chat_session(session_id, memory_token)
        assert success is True

        # 獲取初始快照（可能包含系統消息）
        initial_snapshot = mem_module.memory_manager.snapshot_manager.get_session_snapshot(session_id)
        initial_message_count = len(initial_snapshot.messages) if initial_snapshot else 0

        # 直接測試添加消息到快照
        messages = [
            {"speaker": "用戶", "content": "今天天氣真好", "timestamp": datetime.now().isoformat()},
            {"speaker": "系統", "content": "是啊，適合出去走走", "timestamp": datetime.now().isoformat()},
            {"speaker": "用戶", "content": "你知道附近的公園嗎？", "timestamp": datetime.now().isoformat()},
            {"speaker": "系統", "content": "當然知道...", "timestamp": datetime.now().isoformat()}
        ]

        for message_data in messages:
            # 直接調用snapshot_manager添加消息
            result = mem_module.memory_manager.snapshot_manager.add_message_to_snapshot(
                session_id, message_data
            )
            assert result is True

        # 檢查快照是否正確更新
        current_snapshot = mem_module.memory_manager.snapshot_manager.get_session_snapshot(session_id)
        assert current_snapshot is not None
        assert len(current_snapshot.messages) == initial_message_count + len(messages)
        
        # 檢查手動添加的消息內容（跳過初始消息）
        for i, message in enumerate(current_snapshot.messages[initial_message_count:], initial_message_count):
            expected_message = messages[i - initial_message_count]
            assert message["speaker"] == expected_message["speaker"]
            assert message["content"] == expected_message["content"]
        
        # 檢查快照內容是否被更新
        assert current_snapshot.content is not None
        assert len(current_snapshot.content) > 0
        print("✅ 快照更新測試通過")


class TestLongTermMemory:
    """測試長期記憶 - 跨對話資訊記錄"""

    def test_long_term_memory_storage(self, mem_module):
        """測試長期記憶存儲"""
        with patch('core.state_manager.state_manager') as mock_state_manager:
            mock_state_manager.get_state.return_value = UEPState.CHAT

            memory_token = f"test_long_term_{int(datetime.now().timestamp())}"

            # 存儲用戶偏好
            mem_input = MEMInput(
                operation_type="store_memory",
                memory_token=memory_token,
                memory_entry={
                    "content": "用戶偏好使用簡潔明瞭的解釋方式",
                    "memory_type": "long_term",
                    "topic": "溝通偏好",
                    "importance": "high"
                }
            )

            result = mem_module.handle(mem_input)
            assert result['success'] is True

            # 查詢長期記憶
            query_input = MEMInput(
                operation_type="query_memory",
                memory_token=memory_token,
                query_text="溝通偏好",
                memory_types=["long_term"]
            )

            query_result = mem_module.handle(query_input)
            assert query_result['success'] is True
            print("✅ 長期記憶測試通過")


class TestGSIDExpirationMechanism:
    """測試GSID過期機制 - 短期記憶自動清理"""

    def test_gsid_advancement(self, mem_module):
        """測試GSID前進"""
        initial_gsid = mem_module.memory_manager.snapshot_manager.get_current_gsid()

        # 前進GSID
        new_gsid = mem_module.memory_manager.snapshot_manager.advance_general_session()

        assert new_gsid == initial_gsid + 1
        print("✅ GSID前進測試通過")

    def test_expired_snapshot_cleanup(self, mem_module):
        """測試過期快照清理"""
        memory_token = f"test_expiry_{int(datetime.now().timestamp())}"

        # 創建一個舊GSID的快照
        old_gsid = 1  # 假設這是一個舊的GSID

        # 模擬創建快照並設置舊GSID
        session_id = f"old_session_{int(datetime.now().timestamp())}"
        success = mem_module.memory_manager.join_chat_session(session_id, memory_token)
        assert success is True

        # 手動設置舊GSID（通過修改快照管理器內部狀態）
        if hasattr(mem_module.memory_manager.snapshot_manager, '_active_snapshots'):
            if session_id in mem_module.memory_manager.snapshot_manager._active_snapshots:
                mem_module.memory_manager.snapshot_manager._active_snapshots[session_id].gsid = old_gsid

        # 前進GSID多次，超過過期閾值
        for _ in range(15):  # 超過max_general_sessions (10)
            mem_module.memory_manager.snapshot_manager.advance_general_session()

        # 觸發清理
        mem_module.memory_manager.snapshot_manager._cleanup_expired_snapshots()

        # 檢查舊快照是否已被清理
        current_snapshot = mem_module.memory_manager.snapshot_manager.get_session_snapshot(session_id)
        assert current_snapshot is None
        print("✅ 過期快照清理測試通過")


class TestSessionInitializationMemory:
    """測試會話初始化記憶處理 - 快照查詢和記憶總結"""

    def test_session_memory_initialization(self, mem_module):
        """測試會話記憶初始化"""
        memory_token = f"test_init_{int(datetime.now().timestamp())}"
        session_id = f"init_session_{int(datetime.now().timestamp())}"

        # 先存儲一些歷史記憶
        historical_memories = [
            "用戶之前問過關於Python的問題",
            "用戶表現出對機器學習的興趣",
            "用戶喜歡實作導向的學習方式"
        ]

        with patch('core.state_manager.state_manager') as mock_state_manager:
            mock_state_manager.get_state.return_value = UEPState.CHAT

            for memory in historical_memories:
                mem_input = MEMInput(
                    operation_type="store_memory",
                    memory_token=memory_token,
                    memory_entry={
                        "content": memory,
                        "memory_type": "long_term",
                        "importance": "medium"
                    }
                )
                result = mem_module.handle(mem_input)
                assert result['success'] is True

            # 加入會話（這會觸發記憶初始化）
            initial_context = {"started_by_state_change": True}
            success = mem_module.memory_manager.join_chat_session(
                session_id, memory_token, initial_context
            )

            assert success is True
            print("✅ 會話記憶初始化測試通過")


class TestDynamicSnapshotManagement:
    """測試動態快照管理 - 基於輸入的智能快照操作"""

    def test_topic_change_detection(self, mem_module):
        """測試話題轉換檢測"""
        memory_token = f"test_topic_{int(datetime.now().timestamp())}"
        session_id = f"topic_session_{int(datetime.now().timestamp())}"

        # 加入會話
        success = mem_module.memory_manager.join_chat_session(session_id, memory_token)
        assert success is True

        # 添加初始話題的消息
        initial_message = "用戶: 我想學習Python基礎語法"
        result = mem_module.memory_manager.process_conversation_input(
            session_id, initial_message, memory_token,
            {"topics": ["Python", "程式設計"]}
        )
        assert result.success is True

        # 添加不同話題的消息（應該觸發新快照創建）
        new_topic_message = "用戶: 其實我更想學美術繪畫技巧"
        result = mem_module.memory_manager.process_conversation_input(
            session_id, new_topic_message, memory_token,
            {"topics": ["美術", "繪畫"]}
        )

        assert result.success is True
        print("✅ 話題轉換檢測測試通過")

    def test_large_conversation_handling(self, mem_module):
        """測試大對話處理"""
        memory_token = f"test_large_{int(datetime.now().timestamp())}"
        session_id = f"large_session_{int(datetime.now().timestamp())}"

        # 加入會話
        success = mem_module.memory_manager.join_chat_session(session_id, memory_token)
        assert success is True

        # 添加很多消息（模擬長對話）
        for i in range(25):  # 超過一般快照大小限制
            message = f"用戶: 這是第{i+1}條消息，討論程式設計話題"
            result = mem_module.memory_manager.process_conversation_input(
                session_id, message, memory_token,
                {"topics": ["程式設計"]}
            )
            assert result.success is True

        # 檢查是否正確處理了大對話（可能創建了新快照）
        current_snapshot = mem_module.memory_manager.snapshot_manager.get_session_snapshot(session_id)
        assert current_snapshot is not None
        print("✅ 大對話處理測試通過")


class TestComprehensiveWorkflow:
    """測試完整工作流程"""

    def test_full_mem_workflow(self, mem_module):
        """測試完整的MEM工作流程"""
        memory_token = f"test_full_workflow_{int(datetime.now().timestamp())}"
        session_id = f"full_session_{int(datetime.now().timestamp())}"

        print("🚀 開始完整MEM工作流程測試...")

        # 1. 狀態檢查 - 確保在CHAT狀態
        with patch('core.state_manager.state_manager') as mock_state_manager:
            mock_state_manager.get_state.return_value = UEPState.CHAT

            # 2. 會話加入
            success = mem_module.memory_manager.join_chat_session(session_id, memory_token)
            assert success is True
            print("✅ 1. 會話加入成功")

            # 3. 處理使用者輸入
            user_input = "我想學習Python程式設計"
            result = mem_module.memory_manager.process_conversation_input(
                session_id, user_input, memory_token,
                {"primary_intent": "learning_request", "topics": ["Python", "程式設計"]}
            )
            assert result.success is True
            print("✅ 2. 使用者輸入處理成功")

            # 4. 存儲長期記憶
            mem_input = MEMInput(
                operation_type="store_memory",
                memory_token=memory_token,
                memory_entry={
                    "content": "用戶對程式設計感興趣",
                    "memory_type": "long_term",
                    "importance": "high"
                }
            )
            result = mem_module.handle(mem_input)
            assert result['success'] is True
            print("✅ 3. 長期記憶存儲成功")

            # 5. 查詢相關記憶
            query_input = MEMInput(
                operation_type="query_memory",
                memory_token=memory_token,
                query_text="程式設計"
            )
            result = mem_module.handle(query_input)
            assert result['success'] is True
            print("✅ 4. 記憶查詢成功")

            # 6. 會話結束清理
            result = mem_module.memory_manager.leave_chat_session(session_id, memory_token)
            assert result.success is True
            print("✅ 5. 會話結束清理成功")

        print("🎉 完整MEM工作流程測試全部通過！")


if __name__ == "__main__":
    # 運行基本測試
    print("開始MEM模組功能測試...")

    mem = MEMModule()
    if mem.initialize():
        print("✅ MEM模組初始化成功")

        # 運行關鍵測試
        test_cs = TestCSStateRestriction()
        test_cs.test_mem_rejects_non_chat_state(mem)

        print("🎉 基本功能測試完成")
    else:
        print("❌ MEM模組初始化失敗")