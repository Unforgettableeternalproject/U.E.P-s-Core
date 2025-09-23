"""
測試狀態上下文與NLP上下文的區分實現
驗證MEM代辦.md要求的"第一次進入會話時，MEM以狀態的上下文為主去進行快照查詢以及記憶總結"
"""

import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock
from core.schemas import MemInitRequest, SessionInfo, MemInitResult, SystemState
from modules.mem_module.memory_manager import MemoryManager
from modules.mem_module.mem_module import MEMModule


class TestStateContextImplementation:
    """測試狀態上下文與NLP上下文的區分實現"""
    
    @pytest.fixture
    def mock_memory_manager(self):
        """創建模擬的MemoryManager"""
        with patch.multiple(
            MemoryManager,
            _save_memory=AsyncMock(),
            _search_snapshots=AsyncMock(return_value=[]),
            _search_long_term=AsyncMock(return_value=[]),
            _faiss_search=AsyncMock(return_value=[]),
            _save_faiss_data=AsyncMock(),
        ):
            manager = MemoryManager()
            manager._initialized = True
            manager._ensure_faiss_index = Mock()
            return manager
    
    @pytest.fixture
    def mock_session_manager(self):
        """創建模擬的Session Manager"""
        mock_sm = Mock()
        mock_sm.get_current_session = Mock(return_value=SessionInfo(
            session_id="test_session_001",
            session_type="chatting",
            status="active",
            conversation_turns=0,
            created_at="2024-01-01T00:00:00Z"
        ))
        return mock_sm
    
    @pytest.fixture
    def mock_working_context(self):
        """創建模擬的WorkingContext"""
        mock_wc = Mock()
        mock_wc.set_context = Mock()
        mock_wc.get_context = Mock(return_value={})
        return mock_wc
    
    @pytest.fixture
    def mem_module(self, mock_memory_manager, mock_session_manager, mock_working_context):
        """創建MEMModule實例"""
        with patch('modules.mem_module.mem_module.MemoryManager', return_value=mock_memory_manager):
            module = MEMModule()
            module._manager = mock_memory_manager
            module._session_manager = mock_session_manager
            module._working_context = mock_working_context
            return module
    
    @pytest.mark.asyncio
    async def test_state_context_priority_over_nlp_context(self, mem_module, mock_memory_manager):
        """測試狀態上下文優先於NLP上下文（代辦.md核心要求）"""
        # 準備測試數據：同時包含狀態上下文和NLP上下文
        request = MemInitRequest(
            current_context={
                "state_context": "用戶說：'我想聽音樂'",  # 來自NLP狀態創建的識別文字
                "nlp_context": "音樂播放意圖檢測完成",     # 來自NLP模組輸出
                "session_id": "test_session_001",
                "started_by_state_change": True
            }
        )
        
        # 模擬記憶搜索結果
        mock_memory_manager._search_snapshots.return_value = [
            {"memory_entry": {"content": "上次聊天記錄：討論了音樂偏好"}}
        ]
        mock_memory_manager._search_long_term.return_value = [
            {"memory_entry": {"content": "長期記憶：用戶喜歡古典音樂"}}
        ]
        
        # 執行MEM初始化
        result = await mem_module.init_memory(request)
        
        # 驗證狀態上下文被正確使用
        assert result.success is True
        
        # 檢查_initialize_session_memories被調用時的參數
        memory_manager_calls = mock_memory_manager._initialize_session_memories.call_args_list
        assert len(memory_manager_calls) > 0
        
        # 驗證傳遞的上下文內容
        call_args = memory_manager_calls[0][1]  # 獲取關鍵字參數
        assert "用戶說：'我想聽音樂'" in str(call_args.get('context', {}))
        
        # 驗證記憶總結包含狀態上下文標識
        assert "狀態上下文" in result.summary or "state_context" in result.summary
    
    @pytest.mark.asyncio
    async def test_nlp_context_fallback_when_no_state_context(self, mem_module, mock_memory_manager):
        """測試當沒有狀態上下文時使用NLP上下文作為備選"""
        # 準備測試數據：只有NLP上下文
        request = MemInitRequest(
            current_context={
                "nlp_context": "音樂播放意圖檢測完成",
                "session_id": "test_session_001"
            }
        )
        
        # 模擬記憶搜索結果
        mock_memory_manager._search_snapshots.return_value = []
        mock_memory_manager._search_long_term.return_value = []
        
        # 執行MEM初始化
        result = await mem_module.init_memory(request)
        
        # 驗證NLP上下文被使用
        assert result.success is True
        
        # 檢查_initialize_session_memories被調用時的參數
        memory_manager_calls = mock_memory_manager._initialize_session_memories.call_args_list
        assert len(memory_manager_calls) > 0
        
        # 驗證傳遞的上下文內容
        call_args = memory_manager_calls[0][1]
        assert "音樂播放意圖檢測完成" in str(call_args.get('context', {}))
    
    @pytest.mark.asyncio
    async def test_context_source_tracking_in_memory_summary(self, mock_memory_manager):
        """測試記憶總結中的上下文來源追蹤"""
        # 測試狀態上下文來源
        summary_with_state = mock_memory_manager._generate_memory_summary(
            snapshots=[],
            long_term=[],
            context={"started_by_state_change": True},
            session_context={"session_type": "chatting", "conversation_turns": 0},
            context_source="state_context"
        )
        
        assert "狀態上下文" in summary_with_state
        assert "狀態變化觸發" in summary_with_state or "state_context" in summary_with_state
        
        # 測試NLP上下文來源
        summary_with_nlp = mock_memory_manager._generate_memory_summary(
            snapshots=[],
            long_term=[],
            context={},
            session_context={"session_type": "chatting", "conversation_turns": 0},
            context_source="nlp_context"
        )
        
        assert "NLP輸出觸發" in summary_with_nlp or "nlp_context" in summary_with_nlp
    
    @pytest.mark.asyncio
    async def test_mem_requirement_compliance(self, mem_module, mock_memory_manager):
        """測試MEM代辦.md要求的合規性"""
        # 代辦.md要求：第一次進入會話時，MEM以狀態的上下文為主去進行快照查詢以及記憶總結
        
        # 模擬第一次進入會話的情況
        request = MemInitRequest(
            current_context={
                "state_context": "系統檢測到用戶語音輸入：'播放音樂'",
                "nlp_context": "NLP處理完成，檢測到音樂播放意圖",
                "session_id": "new_session_001",
                "started_by_state_change": True,
                "is_first_entry": True
            }
        )
        
        # 模擬Session Manager返回新會話
        mem_module._session_manager.get_current_session.return_value = SessionInfo(
            session_id="new_session_001",
            session_type="chatting",
            status="active",
            conversation_turns=0,  # 第一次進入
            created_at="2024-01-01T00:00:00Z"
        )
        
        # 執行MEM初始化
        result = await mem_module.init_memory(request)
        
        # 驗證結果符合代辦.md要求
        assert result.success is True
        
        # 驗證使用狀態上下文進行快照查詢
        snapshot_calls = mock_memory_manager._search_snapshots.call_args_list
        assert len(snapshot_calls) > 0
        
        # 驗證快照查詢使用的是狀態上下文內容
        search_query = str(snapshot_calls[0])
        assert "播放音樂" in search_query or "系統檢測" in search_query
        
        # 驗證記憶總結反映狀態上下文優先
        assert "狀態" in result.summary or "第一次進入會話" in result.summary


if __name__ == "__main__":
    pytest.main([__file__, "-v"])