# unit_tests/test_mem_mcp_tools.py
"""
單元測試：MEM 模組的 MCP 工具註冊和執行
測試記憶檢索工具的基本功能
"""

import pytest
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any
from unittest.mock import MagicMock, Mock

# 添加專案根目錄到 Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from modules.mem_module.schemas import MemoryType, MemoryEntry, ConversationSnapshot, MemoryImportance
from modules.sys_module.mcp_server.tool_definitions import MCPTool, ToolResult, ToolResultStatus


@pytest.fixture
def mock_memory_manager():
    """Mock MemoryManager 用於測試"""
    mock_mgr = MagicMock()
    
    # Mock identity_manager
    mock_identity = MagicMock()
    mock_identity.get_current_memory_token.return_value = "test_token_123"
    mock_mgr.identity_manager = mock_identity
    
    # Mock storage_manager
    mock_storage = MagicMock()
    mock_mgr.storage_manager = mock_storage
    
    return mock_mgr


@pytest.fixture
def mem_module_with_mcp(mock_memory_manager):
    """建立帶有 MCP 工具註冊的 MEMModule"""
    from modules.mem_module.mem_module import MEMModule
    
    # Mock dependencies
    mem_module = MEMModule()
    mem_module.memory_manager = mock_memory_manager
    
    return mem_module


@pytest.fixture
def mcp_server():
    """建立測試用 MCP Server"""
    from modules.sys_module.mcp_server.mcp_server import MCPServer
    
    server = MCPServer()
    # 清空預設工具，只測試記憶工具
    server.tools.clear()
    
    return server


class TestMemoryToolsRegistration:
    """測試記憶工具註冊"""
    
    def test_register_memory_tools_success(self, mem_module_with_mcp, mcp_server):
        """測試成功註冊記憶工具（3 檢索 + 7 寫入/更新，含 create_snapshot）"""
        result = mem_module_with_mcp.register_memory_tools_to_mcp(mcp_server)
        
        assert result is True
        assert len(mcp_server.tools) == 10
        
        # 檢查工具名稱
        tool_names = [tool.name for tool in mcp_server.tools.values()]
        # 檢索工具
        assert "memory_retrieve_snapshots" in tool_names
        assert "memory_get_snapshot" in tool_names
        assert "memory_search_timeline" in tool_names
        # 寫入工具
        assert "memory_update_profile" in tool_names
        assert "memory_store_observation" in tool_names
        assert "memory_add_to_snapshot" in tool_names
        assert "memory_update_snapshot_summary" in tool_names
    
    def test_memory_tools_restricted_to_chat_path(self, mem_module_with_mcp, mcp_server):
        """測試記憶工具限制於 CHAT 路徑"""
        mem_module_with_mcp.register_memory_tools_to_mcp(mcp_server)
        
        # 檢查每個工具的 allowed_paths
        for tool in mcp_server.tools.values():
            assert tool.allowed_paths == ["CHAT"]
    
    def test_get_tools_for_chat_path(self, mem_module_with_mcp, mcp_server):
        """測試 CHAT 路徑可以獲取所有記憶工具"""
        mem_module_with_mcp.register_memory_tools_to_mcp(mcp_server)
        
        chat_tools = mcp_server.get_tools_for_path("CHAT")
        assert len(chat_tools) == 10
    
    def test_get_tools_for_work_path(self, mem_module_with_mcp, mcp_server):
        """測試 WORK 路徑無法獲取記憶工具"""
        mem_module_with_mcp.register_memory_tools_to_mcp(mcp_server)
        
        work_tools = mcp_server.get_tools_for_path("WORK")
        assert len(work_tools) == 0


class TestMemoryRetrieveSnapshots:
    """測試 memory_retrieve_snapshots 工具"""
    
    @pytest.mark.asyncio
    async def test_retrieve_snapshots_with_results(self, mem_module_with_mcp):
        """測試語義檢索返回快照摘要 (PROFILE 分支無結果，SNAPSHOT 有結果)"""
        # Mock retrieve_memories 返回結果
        mock_result = MagicMock()
        mock_result.memory_entry = {
            "memory_id": "snap_001",
            "summary": "Test conversation about Python",
            "key_topics": ["Python", "testing"],
            "created_at": datetime.now(),
            "message_count": 5
        }
        mock_result.similarity_score = 0.85
        mock_result.retrieval_reason = "High relevance"
        
        # PROFILE 分支空，SNAPSHOT 分支有結果
        mem_module_with_mcp.memory_manager.retrieve_memories.side_effect = [
            [],              # PROFILE branch
            [mock_result],   # SNAPSHOT branch
        ]
        
        # 執行工具
        params = {
            "query": "Python testing discussion",
            "max_results": 5,
            "similarity_threshold": 0.6
        }
        
        result = await mem_module_with_mcp._handle_memory_retrieve_snapshots(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["count"] == 1
        assert len(result.data["snapshots"]) == 1
        
        snapshot = result.data["snapshots"][0]
        assert snapshot["snapshot_id"] == "snap_001"
        assert snapshot["summary"] == "Test conversation about Python"
        assert snapshot["topics"] == ["Python", "testing"]
        assert snapshot["similarity_score"] == 0.85
    
    @pytest.mark.asyncio
    async def test_retrieve_snapshots_no_results(self, mem_module_with_mcp):
        """測試檢索無結果的情況"""
        mem_module_with_mcp.memory_manager.retrieve_memories.side_effect = [[], []]
        
        params = {"query": "nonexistent topic"}
        result = await mem_module_with_mcp._handle_memory_retrieve_snapshots(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["count"] == 0
        assert result.data["snapshots"] == []
    
    @pytest.mark.asyncio
    async def test_retrieve_snapshots_missing_query(self, mem_module_with_mcp):
        """測試缺少 query 仍可回傳 PROFILE 記憶"""
        profile_result = MagicMock()
        profile_result.memory_entry = {
            "memory_id": "profile_001",
            "summary": "User likes ML",
            "key_topics": [],
            "created_at": datetime.now(),
            "message_count": 0
        }
        profile_result.similarity_score = 0.0
        profile_result.retrieval_reason = "profile"
        
        mem_module_with_mcp.memory_manager.retrieve_memories.side_effect = [
            [profile_result],  # PROFILE branch
            []                 # SNAPSHOT branch
        ]
        
        params = {}
        result = await mem_module_with_mcp._handle_memory_retrieve_snapshots(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["count"] == 1
        snapshots = result.data["snapshots"]
        assert snapshots[0]["snapshot_id"] == "profile_001"


class TestMemoryUpdateSnapshotSummary:
    """測試 memory_update_snapshot_summary 工具"""

    @pytest.mark.asyncio
    async def test_update_snapshot_summary_calls_manager(self, mem_module_with_mcp):
        """確認 update_snapshot_content 被呼叫且保留原內容"""
        # 準備 active snapshot
        snapshot = MagicMock()
        snapshot.content = "original content"
        snapshot.metadata = {"foo": "bar"}
        mem_module_with_mcp.memory_manager.snapshot_manager._active_snapshots = {
            "session123": snapshot
        }
        # 模擬 current_context
        class DummyCtx:
            current_session_id = "session123"
        mem_module_with_mcp.memory_manager.current_context = DummyCtx()

        # Spy update_snapshot_content
        mem_module_with_mcp.memory_manager.snapshot_manager.update_snapshot_content = MagicMock(return_value=True)

        params = {
            "summary": "new summary",
            "key_topics": "alpha, beta",
            "notes": "llm note"
        }
        result = await mem_module_with_mcp._handle_memory_update_snapshot_summary(params)

        assert result.status == ToolResultStatus.SUCCESS
        mem_module_with_mcp.memory_manager.snapshot_manager.update_snapshot_content.assert_called_once()
        call_kwargs = mem_module_with_mcp.memory_manager.snapshot_manager.update_snapshot_content.call_args.kwargs
        assert call_kwargs["snapshot_id"] == "session123"
        assert call_kwargs["new_content"] == "original content"
        assert call_kwargs["new_summary"] == "new summary"
        assert call_kwargs["key_topics"] == ["alpha", "beta"]
        # metadata merged presence
        assert "llm_notes" in call_kwargs["additional_metadata"]
    
    @pytest.mark.asyncio
    async def test_retrieve_snapshots_no_memory_token(self, mem_module_with_mcp):
        """測試沒有 memory_token 的情況"""
        mem_module_with_mcp.memory_manager.identity_manager.get_current_memory_token.return_value = None
        
        params = {"query": "test query"}
        result = await mem_module_with_mcp._handle_memory_retrieve_snapshots(params)
        
        assert result.status == ToolResultStatus.ERROR
        assert "memory token" in result.message.lower()


class TestMemoryGetSnapshot:
    """測試 memory_get_snapshot 工具"""
    
    @pytest.mark.asyncio
    async def test_get_snapshot_success(self, mem_module_with_mcp):
        """測試成功獲取快照詳細內容"""
        mock_snapshot = MagicMock()
        mock_snapshot.memory_type = MemoryType.SNAPSHOT
        mock_snapshot.model_dump.return_value = {
            "memory_id": "snap_001",
            "summary": "Detailed conversation",
            "content": "Full conversation content here",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"}
            ],
            "key_topics": ["greeting"],
            "created_at": datetime.now(),
            "message_count": 2,
            "stage_number": 1
        }
        
        mem_module_with_mcp.memory_manager.storage_manager.get_memory_by_id.return_value = mock_snapshot
        
        params = {"snapshot_id": "snap_001"}
        result = await mem_module_with_mcp._handle_memory_get_snapshot(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["snapshot_id"] == "snap_001"
        assert result.data["message_count"] == 2
        assert len(result.data["messages"]) == 2
    
    @pytest.mark.asyncio
    async def test_get_snapshot_not_found(self, mem_module_with_mcp):
        """測試快照不存在"""
        mem_module_with_mcp.memory_manager.storage_manager.get_memory_by_id.return_value = None
        
        params = {"snapshot_id": "nonexistent"}
        result = await mem_module_with_mcp._handle_memory_get_snapshot(params)
        
        assert result.status == ToolResultStatus.ERROR
        assert "not found" in result.message.lower()
    
    @pytest.mark.asyncio
    async def test_get_snapshot_wrong_type(self, mem_module_with_mcp):
        """測試獲取的不是快照類型"""
        mock_memory = MagicMock()
        mock_memory.memory_type = MemoryType.PROFILE
        
        mem_module_with_mcp.memory_manager.storage_manager.get_memory_by_id.return_value = mock_memory
        
        params = {"snapshot_id": "profile_001"}
        result = await mem_module_with_mcp._handle_memory_get_snapshot(params)
        
        assert result.status == ToolResultStatus.ERROR
        assert "not a snapshot" in result.message.lower()


class TestMemorySearchTimeline:
    """測試 memory_search_timeline 工具"""
    
    @pytest.mark.asyncio
    async def test_search_timeline_success(self, mem_module_with_mcp):
        """測試時間範圍檢索成功"""
        now = datetime.now()
        
        # Mock 返回 3 個快照
        mock_snapshots = []
        for i in range(3):
            mock_snap = MagicMock()
            mock_snap.created_at = now - timedelta(days=i)
            mock_snap.model_dump.return_value = {
                "memory_id": f"snap_00{i}",
                "summary": f"Conversation {i}",
                "key_topics": ["topic1", "topic2"],
                "created_at": now - timedelta(days=i),
                "message_count": 10 + i
            }
            mock_snapshots.append(mock_snap)
        
        mem_module_with_mcp.memory_manager.storage_manager.get_memories_by_type.return_value = mock_snapshots
        
        params = {
            "start_time": (now - timedelta(days=7)).isoformat(),
            "end_time": now.isoformat()
        }
        
        result = await mem_module_with_mcp._handle_memory_search_timeline(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["count"] == 3
        assert len(result.data["snapshots"]) == 3
    
    @pytest.mark.asyncio
    async def test_search_timeline_with_topic_filter(self, mem_module_with_mcp):
        """測試帶主題過濾的時間檢索"""
        now = datetime.now()
        
        # Mock 2 個快照，只有 1 個包含 "Python" 主題
        mock_snap1 = MagicMock()
        mock_snap1.created_at = now
        mock_snap1.key_topics = ["Python", "testing"]
        mock_snap1.model_dump.return_value = {
            "memory_id": "snap_001",
            "summary": "Python discussion",
            "key_topics": ["Python", "testing"],
            "created_at": now,
            "message_count": 5
        }
        
        mock_snap2 = MagicMock()
        mock_snap2.created_at = now - timedelta(days=1)
        mock_snap2.key_topics = ["JavaScript", "web"]
        mock_snap2.model_dump.return_value = {
            "memory_id": "snap_002",
            "summary": "JavaScript discussion",
            "key_topics": ["JavaScript", "web"],
            "created_at": now - timedelta(days=1),
            "message_count": 3
        }
        
        mem_module_with_mcp.memory_manager.storage_manager.get_memories_by_type.return_value = [
            mock_snap1, mock_snap2
        ]
        
        params = {
            "start_time": (now - timedelta(days=7)).isoformat(),
            "end_time": now.isoformat(),
            "topic": "Python"
        }
        
        result = await mem_module_with_mcp._handle_memory_search_timeline(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["count"] == 1
        assert result.data["snapshots"][0]["snapshot_id"] == "snap_001"
    
    @pytest.mark.asyncio
    async def test_search_timeline_invalid_time_format(self, mem_module_with_mcp):
        """測試無效的時間格式"""
        params = {
            "start_time": "invalid-time",
            "end_time": "also-invalid"
        }
        
        result = await mem_module_with_mcp._handle_memory_search_timeline(params)
        
        assert result.status == ToolResultStatus.ERROR
        assert "invalid time format" in result.message.lower()


class TestMemoryUpdateProfile:
    """測試 memory_update_profile 工具"""
    
    @pytest.mark.asyncio
    async def test_update_profile_success(self, mem_module_with_mcp):
        """測試成功更新用戶檔案"""
        # Mock store_memory 返回成功
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.memory_id = "profile_new_001"
        
        mem_module_with_mcp.memory_manager.store_memory.return_value = mock_result
        mem_module_with_mcp.memory_manager.current_context = MagicMock()
        mem_module_with_mcp.memory_manager.current_context.current_session_id = "session_123"
        
        params = {
            "observation": "User prefers detailed explanations with examples",
            "category": "communication_style",
            "importance": "high"
        }
        
        result = await mem_module_with_mcp._handle_memory_update_profile(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["memory_id"] == "profile_new_001"
        assert result.data["category"] == "communication_style"
        assert result.data["importance"] == "high"
        
        # 驗證 store_memory 被正確調用
        mem_module_with_mcp.memory_manager.store_memory.assert_called_once()
        call_args = mem_module_with_mcp.memory_manager.store_memory.call_args
        assert call_args[1]["memory_type"] == MemoryType.PROFILE
    
    @pytest.mark.asyncio
    async def test_update_profile_missing_observation(self, mem_module_with_mcp):
        """測試缺少 observation 參數"""
        params = {}
        result = await mem_module_with_mcp._handle_memory_update_profile(params)
        
        assert result.status == ToolResultStatus.ERROR
        assert "required" in result.message.lower()
    
    @pytest.mark.asyncio
    async def test_update_profile_default_values(self, mem_module_with_mcp):
        """測試預設值處理"""
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.memory_id = "profile_002"
        
        mem_module_with_mcp.memory_manager.store_memory.return_value = mock_result
        mem_module_with_mcp.memory_manager.current_context = None
        
        params = {
            "observation": "User likes Python programming"
        }
        
        result = await mem_module_with_mcp._handle_memory_update_profile(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        
        # 驗證使用了預設值
        call_args = mem_module_with_mcp.memory_manager.store_memory.call_args
        assert call_args[1]["topic"] == "general"
        assert call_args[1]["importance"] == MemoryImportance.MEDIUM


class TestMemoryStoreObservation:
    """測試 memory_store_observation 工具"""
    
    @pytest.mark.asyncio
    async def test_store_observation_success(self, mem_module_with_mcp):
        """測試成功儲存觀察"""
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.memory_id = "obs_001"
        
        mem_module_with_mcp.memory_manager.store_memory.return_value = mock_result
        mem_module_with_mcp.memory_manager.current_context = MagicMock()
        mem_module_with_mcp.memory_manager.current_context.current_session_id = "session_456"
        
        params = {
            "content": "The project uses microservices architecture with Docker",
            "memory_type": "long_term",
            "topic": "technical_context",
            "importance": "high"
        }
        
        result = await mem_module_with_mcp._handle_memory_store_observation(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["memory_id"] == "obs_001"
        assert result.data["memory_type"] == "long_term"
        assert result.data["topic"] == "technical_context"
    
    @pytest.mark.asyncio
    async def test_store_observation_as_profile(self, mem_module_with_mcp):
        """測試儲存為用戶檔案類型"""
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.memory_id = "obs_profile_001"
        
        mem_module_with_mcp.memory_manager.store_memory.return_value = mock_result
        mem_module_with_mcp.memory_manager.current_context = None
        
        params = {
            "content": "User is a software engineer specializing in backend development",
            "memory_type": "profile",
            "topic": "occupation"
        }
        
        result = await mem_module_with_mcp._handle_memory_store_observation(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        
        # 驗證使用了 PROFILE 類型
        call_args = mem_module_with_mcp.memory_manager.store_memory.call_args
        assert call_args[1]["memory_type"] == MemoryType.PROFILE
    
    @pytest.mark.asyncio
    async def test_store_observation_missing_content(self, mem_module_with_mcp):
        """測試缺少 content 參數"""
        params = {}
        result = await mem_module_with_mcp._handle_memory_store_observation(params)
        
        assert result.status == ToolResultStatus.ERROR
        assert "required" in result.message.lower()


class TestMemoryAddToSnapshot:
    """測試 memory_add_to_snapshot 工具"""
    
    @pytest.mark.asyncio
    async def test_add_message_to_snapshot_success(self, mem_module_with_mcp):
        """測試成功添加消息到快照"""
        # Mock current_context
        mem_module_with_mcp.memory_manager.current_context = MagicMock()
        mem_module_with_mcp.memory_manager.current_context.current_session_id = "session_123"
        
        # Mock active snapshot
        mock_snapshot = MagicMock()
        mock_snapshot.messages = [{"content": "previous message"}]
        
        mem_module_with_mcp.memory_manager.snapshot_manager._active_snapshots = {
            "session_123": mock_snapshot
        }
        mem_module_with_mcp.memory_manager.snapshot_manager.add_message_to_snapshot.return_value = True
        
        params = {
            "speaker": "user",
            "content": "Hello, how are you?",
            "intent": "greeting"
        }
        
        result = await mem_module_with_mcp._handle_memory_add_to_snapshot(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["session_id"] == "session_123"
        assert result.data["speaker"] == "user"
        
        # 驗證 add_message_to_snapshot 被調用
        mem_module_with_mcp.memory_manager.snapshot_manager.add_message_to_snapshot.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_add_message_missing_parameters(self, mem_module_with_mcp):
        """測試缺少必要參數"""
        params = {"speaker": "user"}  # 缺少 content
        result = await mem_module_with_mcp._handle_memory_add_to_snapshot(params)
        
        assert result.status == ToolResultStatus.ERROR
        assert "required" in result.message.lower()
    
    @pytest.mark.asyncio
    async def test_add_message_no_active_session(self, mem_module_with_mcp):
        """測試沒有活躍會話"""
        mem_module_with_mcp.memory_manager.current_context = None
        
        params = {
            "speaker": "user",
            "content": "test message"
        }
        
        result = await mem_module_with_mcp._handle_memory_add_to_snapshot(params)
        
        assert result.status == ToolResultStatus.ERROR
        assert "no active conversation" in result.message.lower()


class TestMemoryUpdateSnapshotSummary:
    """測試 memory_update_snapshot_summary 工具"""
    
    @pytest.mark.asyncio
    async def test_update_snapshot_summary_success(self, mem_module_with_mcp):
        """測試成功更新快照摘要"""
        # Mock current_context
        mem_module_with_mcp.memory_manager.current_context = MagicMock()
        mem_module_with_mcp.memory_manager.current_context.current_session_id = "session_456"
        
        # Mock active snapshot
        mock_snapshot = MagicMock()
        mock_snapshot.content = "Original content"
        mock_snapshot.metadata = {}
        
        mem_module_with_mcp.memory_manager.snapshot_manager._active_snapshots = {
            "session_456": mock_snapshot
        }
        mem_module_with_mcp.memory_manager.snapshot_manager.update_snapshot_content.return_value = True
        
        params = {
            "summary": "Discussion about Python programming and best practices",
            "key_topics": "Python, programming, best practices",
            "notes": "User showed interest in async programming"
        }
        
        result = await mem_module_with_mcp._handle_memory_update_snapshot_summary(params)
        
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["session_id"] == "session_456"
        assert "summary" in result.data["updated_fields"]
    
    @pytest.mark.asyncio
    async def test_update_snapshot_no_parameters(self, mem_module_with_mcp):
        """測試沒有提供任何更新參數"""
        params = {}
        result = await mem_module_with_mcp._handle_memory_update_snapshot_summary(params)
        
        assert result.status == ToolResultStatus.ERROR
        assert "at least one" in result.message.lower()
    
    @pytest.mark.asyncio
    async def test_update_snapshot_topics_parsing(self, mem_module_with_mcp):
        """測試主題列表解析"""
        mem_module_with_mcp.memory_manager.current_context = MagicMock()
        mem_module_with_mcp.memory_manager.current_context.current_session_id = "session_789"
        
        mock_snapshot = MagicMock()
        mock_snapshot.content = "test"
        mock_snapshot.metadata = {}
        
        mem_module_with_mcp.memory_manager.snapshot_manager._active_snapshots = {
            "session_789": mock_snapshot
        }
        mem_module_with_mcp.memory_manager.snapshot_manager.update_snapshot_content.return_value = True
        
        params = {
            "key_topics": "AI, machine learning, neural networks, deep learning"
        }
        
        result = await mem_module_with_mcp._handle_memory_update_snapshot_summary(params)
        
        assert result.status == ToolResultStatus.SUCCESS


class TestMemoryToolsIntegration:
    """測試記憶工具的整合場景"""
    
    @pytest.mark.asyncio
    async def test_full_retrieval_workflow(self, mem_module_with_mcp, mcp_server):
        """測試完整的檢索工作流：檢索摘要 -> 獲取詳細內容"""
        # 註冊工具
        mem_module_with_mcp.register_memory_tools_to_mcp(mcp_server)
        
        # Step 1: 檢索快照摘要
        mock_search_result = MagicMock()
        mock_search_result.memory_entry = {
            "memory_id": "snap_python_001",
            "summary": "Python async programming discussion",
            "key_topics": ["Python", "async", "asyncio"],
            "created_at": datetime.now(),
            "message_count": 15
        }
        mock_search_result.similarity_score = 0.92
        mock_search_result.retrieval_reason = "Highly relevant"
        
        mem_module_with_mcp.memory_manager.retrieve_memories.return_value = [mock_search_result]
        
        search_result = await mem_module_with_mcp._handle_memory_retrieve_snapshots({
            "query": "Python async programming"
        })
        
        assert search_result.status == ToolResultStatus.SUCCESS
        snapshot_id = search_result.data["snapshots"][0]["snapshot_id"]
        
        # Step 2: 獲取詳細內容
        mock_full_snapshot = MagicMock()
        mock_full_snapshot.memory_type = MemoryType.SNAPSHOT
        mock_full_snapshot.model_dump.return_value = {
            "memory_id": snapshot_id,
            "summary": "Python async programming discussion",
            "content": "Full conversation about async/await...",
            "messages": [
                {"role": "user", "content": "How does async work?"},
                {"role": "assistant", "content": "Async in Python..."}
            ],
            "key_topics": ["Python", "async", "asyncio"],
            "created_at": datetime.now(),
            "message_count": 15,
            "stage_number": 3
        }
        
        mem_module_with_mcp.memory_manager.storage_manager.get_memory_by_id.return_value = mock_full_snapshot
        
        detail_result = await mem_module_with_mcp._handle_memory_get_snapshot({
            "snapshot_id": snapshot_id
        })
        
        assert detail_result.status == ToolResultStatus.SUCCESS
        assert detail_result.data["message_count"] == 15
        assert len(detail_result.data["messages"]) == 2
    
    @pytest.mark.asyncio
    async def test_write_and_read_profile(self, mem_module_with_mcp, mcp_server):
        """測試寫入後讀取用戶檔案的完整流程"""
        # 註冊工具
        mem_module_with_mcp.register_memory_tools_to_mcp(mcp_server)
        
        # Step 1: 寫入用戶檔案
        mock_store_result = MagicMock()
        mock_store_result.success = True
        mock_store_result.memory_id = "profile_test_001"
        
        mem_module_with_mcp.memory_manager.store_memory.return_value = mock_store_result
        mem_module_with_mcp.memory_manager.current_context = None
        
        write_result = await mem_module_with_mcp._handle_memory_update_profile({
            "observation": "User is interested in machine learning and AI",
            "category": "interests",
            "importance": "high"
        })
        
        assert write_result.status == ToolResultStatus.SUCCESS
        profile_id = write_result.data["memory_id"]
        
        # Step 2: 讀取用戶檔案
        mock_profile = MagicMock()
        mock_profile.memory_type = MemoryType.PROFILE
        mock_profile.model_dump.return_value = {
            "memory_id": profile_id,
            "content": "User is interested in machine learning and AI",
            "summary": "User interests in AI/ML",
            "topic": "interests",
            "created_at": datetime.now(),
            "importance_score": 0.8,
            "metadata": {"category": "interests"}
        }
        
        mem_module_with_mcp.memory_manager.storage_manager.get_memory_by_id.return_value = mock_profile
        
        read_result = await mem_module_with_mcp._handle_memory_get_snapshot({
            "snapshot_id": profile_id
        })
        
        # 注意：get_snapshot 會檢查類型，PROFILE 不是 SNAPSHOT 會失敗
        # 這是預期行為，因為這兩個是不同的檢索路徑
        assert read_result.status == ToolResultStatus.ERROR
        assert "not a snapshot" in read_result.message.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
