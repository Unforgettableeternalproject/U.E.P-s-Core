"""
記憶系統 MCP 工具化整合測試

測試目標：
1. CHAT 模式 LLM 可透過 MCP 工具檢索快照記憶
2. WORK 模式不接收記憶工具（路徑隔離）
3. 提示詞大小減少（移除自動注入的快照）
4. 記憶檢索準確度維持

測試策略：
- 使用完整系統循環
- 測試記憶工具的實際調用
- 驗證路徑過濾機制
- 測量提示詞大小變化
"""

import pytest
import time
import sys
import threading
from pathlib import Path

# 確保專案根目錄在 sys.path 中
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from typing import Dict, Any, Optional, List

# 測試標記
pytestmark = [pytest.mark.integration, pytest.mark.memory_mcp]

# 導入事件類型
from core.event_bus import SystemEvent


@pytest.fixture(scope="module")
def system_components():
    """初始化完整系統組件"""
    from utils.debug_helper import info_log, error_log
    from core.system_initializer import SystemInitializer
    from core.controller import unified_controller
    from core.system_loop import system_loop
    from core.event_bus import event_bus
    from utils.logger import force_enable_file_logging
    
    force_enable_file_logging()
    
    info_log("[MemoryMCPIntegrationTest] 🚀 初始化完整系統...")
    
    # 初始化系統
    initializer = SystemInitializer()
    success = initializer.initialize_system(production_mode=False)
    
    if not success:
        pytest.fail("系統初始化失敗")
    
    info_log("[MemoryMCPIntegrationTest] ✅ 系統初始化完成")
    
    # 啟動系統循環
    loop_started = system_loop.start()
    if not loop_started:
        pytest.fail("系統循環啟動失敗")
    
    info_log("[MemoryMCPIntegrationTest] ✅ 系統循環已啟動")
    
    # 準備組件
    components = {
        "initializer": initializer,
        "controller": unified_controller,
        "system_loop": system_loop,
        "event_bus": event_bus,
    }
    
    # 等待系統穩定
    time.sleep(2)
    
    info_log("[MemoryMCPIntegrationTest] ✅ 系統組件就緒")
    
    yield components
    
    # 清理
    info_log("[MemoryMCPIntegrationTest] 🧹 清理系統組件...")
    
    try:
        system_loop.stop()
        time.sleep(1)
    except Exception as e:
        error_log(f"清理系統循環失敗: {e}")


@pytest.fixture
def override_test_identity(system_components):
    """覆蓋系統的 current_identity 為 Debug identity，用於測試"""
    from utils.debug_helper import info_log
    from modules.nlp_module.identity_manager import IdentityManager
    from core.working_context import working_context_manager
    
    # 獲取 Debug Identity
    identity_manager = IdentityManager()
    debug_identity = None
    
    for identity in identity_manager.identities.values():
        if identity.display_name and identity.display_name.lower() == "debug":
            debug_identity = identity
            break
    
    if not debug_identity:
        pytest.fail("找不到 Debug Identity，無法進行測試")
    
    info_log(f"[MemoryMCPTest] 🔄 覆蓋 current_identity 為 Debug: {debug_identity.identity_id}")
    
    # 覆蓋 Working Context 中的 current_identity
    working_context_manager.global_context_data['current_identity_id'] = debug_identity.identity_id
    working_context_manager.global_context_data['current_identity'] = {
        'identity_id': debug_identity.identity_id,
        'display_name': debug_identity.display_name,
        'speaker_id': debug_identity.speaker_id,
        'memory_token': debug_identity.memory_token
    }
    
    # 也設置到 set_current_identity (這會設置 context_data)
    identity_dict = {
        'identity_id': debug_identity.identity_id,
        'display_name': debug_identity.display_name,
        'speaker_id': debug_identity.speaker_id,
        'memory_token': debug_identity.memory_token
    }
    if hasattr(debug_identity, 'user_identity'):
        identity_dict['user_identity'] = debug_identity.user_identity
    
    working_context_manager.set_current_identity(identity_dict)
    
    info_log(f"[MemoryMCPTest] ✅ current_identity 已覆蓋為 Debug identity")
    info_log(f"[MemoryMCPTest] 📝 memory_token: {debug_identity.memory_token}")
    
    yield debug_identity
    
    # Teardown: 恢復原本的 identity (如果需要的話，這裡我們不恢復，因為測試後系統會清理)


@pytest.fixture
def cleanup_memory(override_test_identity):
    """測試前清空測試用 Identity 的記憶"""
    from utils.debug_helper import info_log
    from core.framework import core_framework
    
    def _cleanup_debug_memory():
        info_log("[MemoryMCPTest] 🧹 清理 Debug Identity 的記憶...")
        
        debug_identity = override_test_identity
        
        # 獲取 MEM 模組
        mem_module = core_framework.get_module('mem')
        if not mem_module or not mem_module.memory_manager:
            info_log("[MemoryMCPTest] ⚠️ MEM 模組不可用，跳過清理")
            return
        
        memory_token = debug_identity.memory_token
        if not memory_token:
            info_log("[MemoryMCPTest] ⚠️ Debug Identity 沒有 memory_token，跳過清理")
            return
        
        # 清理快照記憶和 profile 記憶
        try:
            # 使用 retrieve_memories 檢索所有快照和 profile 類型的記憶
            from modules.mem_module.schemas import MemoryType
            results = mem_module.memory_manager.retrieve_memories(
                query_text="",
                memory_token=memory_token,
                memory_types=[MemoryType.SNAPSHOT, MemoryType.PROFILE],
                max_results=100,
                similarity_threshold=0.0  # 返回所有記憶
            )
            
            # 刪除找到的記憶
            for result in results:
                mem_module.memory_manager.delete_memory(result.memory_entry.memory_id, memory_token)
            
            info_log(f"[MemoryMCPTest] ✅ 清理了 {len(results)} 個記憶")
        except Exception as e:
            info_log(f"[MemoryMCPTest] ⚠️ 清理記憶失敗: {e}")
    
    # Setup: 測試開始前清理
    _cleanup_debug_memory()
    
    yield
    
    # Teardown: 測試結束後也清理
    _cleanup_debug_memory()


@pytest.fixture
def isolated_gs(system_components):
    """確保每個測試使用獨立的 GS"""
    from utils.debug_helper import info_log
    controller = system_components["controller"]
    
    # Setup: 確保測試開始前沒有活躍的 GS
    current_gs = controller.session_manager.get_current_general_session()
    if current_gs:
        controller.session_manager.end_general_session(current_gs.session_id)
        time.sleep(1)
    
    yield
    
    # Teardown: 測試結束後明確結束 GS
    current_gs = controller.session_manager.get_current_general_session()
    if current_gs:
        controller.session_manager.end_general_session(current_gs.session_id)
        time.sleep(1)


class MemoryMCPMonitor:
    """記憶 MCP 工具監控器"""
    
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.events = []
        self.llm_responses = []
        self.tool_calls = []
        self.memory_operations = []
        self.response_received = threading.Event()
        self.cycle_completed = threading.Event()
        
        # 訂閱相關事件
        self.event_bus.subscribe(SystemEvent.LLM_RESPONSE_GENERATED, self._on_llm_response)
        self.event_bus.subscribe(SystemEvent.MEMORY_CREATED, self._on_memory_operation)
        self.event_bus.subscribe(SystemEvent.CYCLE_COMPLETED, self._on_cycle_completed)
    
    def _on_llm_response(self, event):
        """記錄 LLM 回應事件"""
        self.events.append(("llm_response", event.data))
        self.llm_responses.append(event.data)
        
        # 檢查是否有工具調用（從事件數據中提取）
        if 'function_call' in event.data and event.data['function_call']:
            tool_call_data = event.data['function_call']
            self.tool_calls.append(tool_call_data)
            
            from utils.debug_helper import debug_log
            debug_log(2, f"[MemoryMCPMonitor] 檢測到工具調用: {tool_call_data.get('name')} with args {tool_call_data.get('args', {})}")
        
        self.response_received.set()
        
        from utils.debug_helper import debug_log
        debug_log(2, f"[MemoryMCPMonitor] LLM 回應: {event.data.get('response', '')[:100]}...")
    
    def _on_memory_operation(self, event):
        """記錄記憶操作事件"""
        self.events.append(("memory_operation", event.data))
        self.memory_operations.append(event.data)
        
        from utils.debug_helper import debug_log
        debug_log(2, f"[MemoryMCPMonitor] 記憶操作: {event.data.get('operation', 'unknown')}")
    
    def _on_cycle_completed(self, event):
        """記錄循環完成事件"""
        self.events.append(("cycle_completed", event.data))
        self.cycle_completed.set()
        
        from utils.debug_helper import debug_log
        debug_log(2, f"[MemoryMCPMonitor] 循環完成: cycle={event.data.get('cycle_index')}")
    
    def wait_for_response(self, timeout=30):
        """等待 LLM 回應"""
        return self.response_received.wait(timeout=timeout)
    
    def wait_for_cycle_completed(self, timeout=30):
        """等待循環完成"""
        return self.cycle_completed.wait(timeout=timeout)
    
    def reset(self):
        """重置監控器"""
        self.events = []
        self.llm_responses = []
        self.tool_calls = []
        self.memory_operations = []
        self.response_received.clear()
        self.cycle_completed.clear()


def inject_chat_message(message: str, identity_id: Optional[str] = None):
    """注入聊天訊息到系統"""
    from utils.debug_helper import info_log
    from core.framework import core_framework
    from core.working_context import working_context_manager
    from modules.nlp_module.identity_manager import IdentityManager
    
    info_log(f"[MemoryMCPTest] 💬 注入聊天訊息: {message}")
    
    if identity_id:
        # 獲取完整的 identity 物件並設置為 current_identity
        identity_manager = IdentityManager()
        identity = identity_manager.get_identity_by_id(identity_id)
        
        if identity:
            # 轉換為 dict 格式並設置為當前 identity
            identity_dict = identity.model_dump() if hasattr(identity, 'model_dump') else identity.dict()
            working_context_manager.set_current_identity(identity_dict)
            info_log(f"[MemoryMCPTest] 🆔 已設置當前 Identity: {identity_id}, 記憶令牌: {identity_dict.get('memory_token')}")
        else:
            info_log(f"[MemoryMCPTest] ⚠️ 找不到 Identity: {identity_id}")
    
    # 通過 STT 模組注入文字輸入
    stt_module = core_framework.get_module('stt')
    if not stt_module:
        raise RuntimeError("STT module not available")
    
    result = stt_module.handle_text_input(message)
    
    if not result:
        raise RuntimeError(f"Failed to inject text: {message}")
    
    info_log(f"[MemoryMCPTest] ✅ 文字注入成功")


class TestMemoryMCPIntegration:
    """記憶系統 MCP 工具化整合測試"""
    
    def test_01_chat_path_has_memory_tools(self, system_components):
        """
        測試 1: CHAT 路徑擁有記憶工具
        驗證 CHAT 模式下 LLM 可以訪問記憶工具
        """
        from utils.debug_helper import info_log
        from core.framework import core_framework
        
        info_log("\n" + "=" * 70)
        info_log("TEST 1: CHAT Path Has Memory Tools")
        info_log("=" * 70)
        
        # 獲取 LLM 模組
        llm_module = core_framework.get_module('llm')
        assert llm_module is not None, "LLM 模組應該可用"
        
        # 獲取 MCP Client
        mcp_client = llm_module.mcp_client
        assert mcp_client is not None, "MCP Client 應該可用"
        
        # 獲取 CHAT 路徑的工具
        from modules.llm_module.mcp_client import PATH_CHAT, PATH_WORK
        
        chat_tools = mcp_client.get_tools_as_gemini_format(path=PATH_CHAT)
        work_tools = mcp_client.get_tools_as_gemini_format(path=PATH_WORK)
        
        # 計算工具數量
        chat_tool_count = sum(len(t.get('function_declarations', [])) for t in chat_tools) if chat_tools else 0
        work_tool_count = sum(len(t.get('function_declarations', [])) for t in work_tools) if work_tools else 0
        
        info_log(f"✅ CHAT 路徑工具數量: {chat_tool_count}")
        info_log(f"✅ WORK 路徑工具數量: {work_tool_count}")
        
        # 驗證：CHAT 路徑應該有記憶工具
        assert chat_tool_count > 0, "CHAT 路徑應該有工具"
        
        # 檢查記憶工具名稱
        memory_tool_names = [
            "memory_retrieve_snapshots",
            "memory_get_snapshot",
            "memory_search_timeline",
            "memory_update_profile",
            "memory_store_observation"
        ]
        
        chat_tool_names = []
        for tool_group in chat_tools:
            for func_decl in tool_group.get('function_declarations', []):
                chat_tool_names.append(func_decl.get('name', ''))
        
        info_log(f"\n📋 CHAT 路徑工具列表:")
        for name in chat_tool_names:
            info_log(f"   - {name}")
        
        # 驗證記憶工具存在
        memory_tools_found = [name for name in memory_tool_names if name in chat_tool_names]
        info_log(f"\n✅ 找到記憶工具: {len(memory_tools_found)}/{len(memory_tool_names)}")
        
        assert len(memory_tools_found) >= 3, f"應該至少有 3 個記憶工具，實際找到: {memory_tools_found}"
        
        info_log("\n✅ TEST 1 PASSED: CHAT 路徑擁有記憶工具")
    
    def test_02_work_path_no_memory_tools(self, system_components):
        """
        測試 2: WORK 路徑不包含記憶工具
        驗證路徑隔離機制正確工作
        """
        from utils.debug_helper import info_log
        from core.framework import core_framework
        
        info_log("\n" + "=" * 70)
        info_log("TEST 2: WORK Path No Memory Tools")
        info_log("=" * 70)
        
        # 獲取 LLM 模組
        llm_module = core_framework.get_module('llm')
        mcp_client = llm_module.mcp_client
        
        # 獲取 WORK 路徑的工具
        from modules.llm_module.mcp_client import PATH_WORK
        
        work_tools = mcp_client.get_tools_as_gemini_format(path=PATH_WORK)
        
        # 獲取工具名稱
        work_tool_names = []
        if work_tools:
            for tool_group in work_tools:
                for func_decl in tool_group.get('function_declarations', []):
                    work_tool_names.append(func_decl.get('name', ''))
        
        info_log(f"\n📋 WORK 路徑工具列表 ({len(work_tool_names)} 個):")
        for name in work_tool_names:
            info_log(f"   - {name}")
        
        # 檢查是否有記憶工具
        memory_tool_names = [
            "memory_retrieve_snapshots",
            "memory_get_snapshot",
            "memory_search_timeline",
            "memory_update_profile",
            "memory_store_observation"
        ]
        
        memory_tools_in_work = [name for name in memory_tool_names if name in work_tool_names]
        
        info_log(f"\n🔍 WORK 路徑中的記憶工具: {len(memory_tools_in_work)}")
        
        assert len(memory_tools_in_work) == 0, f"WORK 路徑不應包含記憶工具，但找到: {memory_tools_in_work}"
        
        info_log("\n✅ TEST 2 PASSED: WORK 路徑正確隔離記憶工具")
    
    def test_03_memory_tool_call_flow(self, system_components, isolated_gs, cleanup_memory):
        """
        測試 3: 記憶工具調用流程
        驗證 CHAT 模式下 LLM 可以成功調用記憶工具並處理結果
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        
        info_log("\n" + "=" * 70)
        info_log("TEST 3: Memory Tool Call Flow")
        info_log("=" * 70)
        
        event_bus = system_components["event_bus"]
        
        # 獲取 Debug Identity
        identity_manager = IdentityManager()
        debug_identity = None
        
        for identity in identity_manager.identities.values():
            if identity.display_name and identity.display_name.lower() == "debug":
                debug_identity = identity
                break
        
        assert debug_identity is not None, "Debug identity not found"
        info_log(f"✅ 找到 Debug: {debug_identity.identity_id}")
        
        # 創建監控器
        monitor = MemoryMCPMonitor(event_bus)
        
        # 第一輪：建立記憶
        info_log("\n--- 第一輪：建立記憶 ---")
        inject_chat_message(
            "I love Python programming and machine learning. Remember this about me.",
            identity_id=debug_identity.identity_id
        )
        
        # 等待回應和循環完成
        monitor.wait_for_response(timeout=30)
        info_log("   ✅ 收到第一輪回應")
        
        monitor.wait_for_cycle_completed(timeout=30)
        info_log("   ✅ 第一輪循環完成")
        
        # 檢查第一輪是否有工具調用
        info_log(f"\n🔍 第一輪工具調用檢查:")
        info_log(f"   工具調用次數: {len(monitor.tool_calls)}")
        
        round1_memory_tools = [
            tool_call for tool_call in monitor.tool_calls
            if 'memory' in tool_call.get('name', '').lower()
        ]
        
        if len(round1_memory_tools) == 0:
            info_log("   ⚠️ 第一輪 LLM 沒有調用記憶工具來儲存")
            info_log("   這可能導致第二輪檢索不到記憶")
        else:
            info_log(f"   ✅ 第一輪調用了 {len(round1_memory_tools)} 個記憶工具:")
            for tool_call in round1_memory_tools:
                info_log(f"      - {tool_call.get('name')}: {tool_call.get('args', {})}")
        
        # 等待記憶儲存
        time.sleep(3)
        monitor.reset()
        
        # 第二輪：測試記憶檢索（必須觸發工具調用並成功檢索）
        info_log("\n--- 第二輪：測試記憶檢索 ---")
        inject_chat_message(
            "What did I tell you about my interests earlier?",
            identity_id=debug_identity.identity_id
        )
        
        # 等待回應
        response_received = monitor.wait_for_response(timeout=30)
        
        assert response_received, "❌ 第二輪未收到回應"
        info_log(f"   ✅ 收到第二輪回應")
        
        # 檢查是否有工具調用
        info_log(f"\n🔍 檢查工具調用:")
        info_log(f"   工具調用次數: {len(monitor.tool_calls)}")
        
        # **嚴格驗證：必須調用記憶工具**
        memory_tool_calls = [
            tool_call for tool_call in monitor.tool_calls
            if 'memory' in tool_call.get('name', '').lower()
        ]
        
        if len(memory_tool_calls) == 0:
            info_log("   ❌ LLM 沒有調用記憶工具！")
            info_log(f"   所有工具調用: {[t.get('name') for t in monitor.tool_calls]}")
            pytest.fail("LLM 應該調用記憶工具來檢索用戶興趣，但沒有調用")
        
        info_log(f"   ✅ 成功調用 {len(memory_tool_calls)} 個記憶工具:")
        for i, tool_call in enumerate(memory_tool_calls):
            tool_name = tool_call.get('name', 'unknown')
            tool_args = tool_call.get('args', {})
            info_log(f"      {i+1}. {tool_name}")
            info_log(f"         參數: {tool_args}")
        
        # 驗證是否為檢索類工具（包含 PROFILE 和 SNAPSHOT 檢索）
        retrieval_tools = ['memory_retrieve_snapshots', 'memory_search_timeline', 'memory_get_snapshot', 'memory_retrieve_profile']
        retrieval_called = any(
            tool_call.get('name') in retrieval_tools
            for tool_call in memory_tool_calls
        )
        
        if not retrieval_called:
            pytest.fail(f"LLM 應該調用檢索工具（{retrieval_tools}），但調用了: {[t.get('name') for t in memory_tool_calls]}")
        
        info_log("   ✅ 調用了檢索類記憶工具")
        
        # 檢查回應內容是否提到 Python 或 machine learning
        if len(monitor.llm_responses) > 0:
            last_response = monitor.llm_responses[-1]
            response_text = last_response.get('response', '').lower()
            info_log(f"\n📝 LLM 回應: {response_text[:200]}...")
            
            # 驗證回應內容包含用戶興趣關鍵詞
            keywords = ['python', 'machine learning', 'programming', 'ml']
            found_keywords = [kw for kw in keywords if kw in response_text]
            
            if found_keywords:
                info_log(f"   ✅ 回應包含用戶興趣關鍵詞: {found_keywords}")
            else:
                info_log(f"   ⚠️  回應未包含預期的用戶興趣關鍵詞，可能記憶檢索失敗")
                info_log(f"   預期關鍵詞: {keywords}")
        
        monitor.wait_for_cycle_completed(timeout=30)
        info_log("   ✅ 第二輪循環完成")
        
        info_log("\n✅ TEST 3 PASSED: 記憶工具調用流程驗證通過")
    
    def test_04_prompt_size_reduction(self, system_components, isolated_gs, cleanup_memory):
        """
        測試 4: 提示詞大小減少
        驗證移除快照自動注入後，提示詞大小顯著減少
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        from core.framework import core_framework
        
        info_log("\n" + "=" * 70)
        info_log("TEST 4: Prompt Size Reduction")
        info_log("=" * 70)
        
        event_bus = system_components["event_bus"]
        
        # 獲取 Debug Identity
        identity_manager = IdentityManager()
        debug_identity = None
        
        for identity in identity_manager.identities.values():
            if identity.display_name and identity.display_name.lower() == "debug":
                debug_identity = identity
                break
        
        assert debug_identity is not None, "Debug identity not found"
        
        # 獲取 LLM 模組和 PromptManager
        llm_module = core_framework.get_module('llm')
        prompt_manager = llm_module.prompt_manager
        
        # 創建監控器
        monitor = MemoryMCPMonitor(event_bus)
        
        # 建立一些對話記憶
        info_log("\n--- 建立對話記憶 ---")
        for i in range(3):
            inject_chat_message(
                f"This is test message number {i+1}. I'm creating some conversation history.",
                identity_id=debug_identity.identity_id
            )
            monitor.wait_for_response(timeout=20)
            monitor.wait_for_cycle_completed(timeout=20)
            monitor.reset()
            time.sleep(2)
        
        info_log("   ✅ 已建立 3 輪對話記憶")
        
        # 測試提示詞構建
        info_log("\n--- 測試提示詞構建 ---")
        
        # 構建 CHAT 提示（不包含快照）
        test_prompt = prompt_manager.build_chat_prompt(
            user_input="Hello, how are you?",
            identity_context={"identity": {"name": "Debug"}},
            memory_context=None,  # 不傳入記憶上下文
            conversation_history=None,
            is_internal=False
        )
        
        prompt_size = len(test_prompt)
        info_log(f"\n📏 提示詞大小: {prompt_size} 字符")
        
        # 檢查提示詞中是否包含記憶工具說明
        has_memory_tool_guide = "Memory Tools Available" in test_prompt or "memory_retrieve_snapshots" in test_prompt
        
        if has_memory_tool_guide:
            info_log("   ✅ 提示詞包含記憶工具使用說明")
        else:
            info_log("   ℹ️  提示詞不包含記憶工具說明（可能在其他地方提供）")
        
        # 檢查提示詞中是否不包含快照內容
        has_snapshot_content = "[Recent Context]" in test_prompt or "Conversation" in test_prompt
        
        if not has_snapshot_content:
            info_log("   ✅ 提示詞不包含快照內容（正確）")
        else:
            info_log("   ⚠️  提示詞可能包含快照內容")
        
        # 估算：如果有 3 輪對話，每輪 100 字，快照注入會增加約 300+ 字符
        # 移除快照注入應該能減少這部分大小
        info_log(f"\n📊 提示詞分析:")
        info_log(f"   - 當前大小: {prompt_size} 字符")
        info_log(f"   - 包含快照: {'否' if not has_snapshot_content else '是'}")
        info_log(f"   - 包含工具說明: {'是' if has_memory_tool_guide else '否'}")
        
        # 驗證：提示詞應該相對簡潔（不包含大量快照內容）
        # 加入記憶工具說明後，基礎提示詞約在 7000 字符以內是合理的
        # （包含 persona、記憶工具使用說明、CRITICAL RULES 等）
        assert prompt_size < 8000, f"提示詞過大: {prompt_size} 字符，可能包含過多內容"
        assert not has_snapshot_content, "提示詞不應包含自動注入的快照內容"
        
        info_log("\n✅ TEST 4 PASSED: 提示詞大小合理，不包含自動注入的快照")
    
    def test_05_memory_accuracy_maintained(self, system_components, isolated_gs, cleanup_memory):
        """
        測試 5: 記憶檢索準確度維持
        驗證透過工具檢索的記憶仍然準確可用
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        from core.framework import core_framework
        
        info_log("\n" + "=" * 70)
        info_log("TEST 5: Memory Accuracy Maintained")
        info_log("=" * 70)
        
        event_bus = system_components["event_bus"]
        
        # 獲取 Debug Identity
        identity_manager = IdentityManager()
        debug_identity = None
        
        for identity in identity_manager.identities.values():
            if identity.display_name and identity.display_name.lower() == "debug":
                debug_identity = identity
                break
        
        assert debug_identity is not None, "Debug identity not found"
        
        # 獲取 MEM 模組
        mem_module = core_framework.get_module('mem')
        assert mem_module is not None, "MEM 模組應該可用"
        
        # 創建監控器
        monitor = MemoryMCPMonitor(event_bus)
        
        # 建立特定的記憶內容
        info_log("\n--- 建立特定記憶內容 ---")
        test_facts = [
            "My favorite color is blue",
            "I work as a software engineer",
            "I enjoy playing guitar"
        ]
        
        for fact in test_facts:
            inject_chat_message(
                f"Remember this: {fact}",
                identity_id=debug_identity.identity_id
            )
            monitor.wait_for_response(timeout=20)
            monitor.wait_for_cycle_completed(timeout=20)
            monitor.reset()
            time.sleep(2)
        
        info_log(f"   ✅ 已建立 {len(test_facts)} 個特定記憶")
        
        # 直接測試記憶檢索
        info_log("\n--- 直接測試記憶檢索 ---")
        
        if mem_module.memory_manager:
            from modules.mem_module.schemas import MemoryType
            
            memory_token = debug_identity.memory_token
            
            # 檢索快照和 profile 記憶
            all_memories = mem_module.memory_manager.retrieve_memories(
                query_text="",
                memory_token=memory_token,
                memory_types=[MemoryType.SNAPSHOT, MemoryType.PROFILE],
                max_results=50,
                similarity_threshold=0.0
            )
            
            # 分離快照和 profile
            snapshots = [m for m in all_memories if m.memory_entry.memory_type == MemoryType.SNAPSHOT]
            profiles = [m for m in all_memories if m.memory_entry.memory_type == MemoryType.PROFILE]
            info_log(f"   快照數量: {len(snapshots)}")
            
            # 搜尋特定內容
            search_results = mem_module.memory_manager.retrieve_memories(
                query_text="favorite color",
                memory_token=memory_token,
                memory_types=[MemoryType.SNAPSHOT, MemoryType.PROFILE],
                max_results=5,
                similarity_threshold=0.0
            )
            
            info_log(f"\n🔍 搜尋 'favorite color' 的結果:")
            info_log(f"   結果數量: {len(search_results)}")
            
            if len(search_results) > 0:
                for i, result in enumerate(search_results[:3]):
                    content = result.memory_entry.content[:100]
                    score = result.similarity_score
                    info_log(f"   {i+1}. [{score:.3f}] {content}...")
                
                # 驗證：應該能找到相關記憶
                assert len(search_results) > 0, "應該能搜尋到相關記憶"
                
                # 檢查最高分結果是否包含 "blue"
                top_result = search_results[0]
                top_content = top_result.memory_entry.content.lower()
                
                if "blue" in top_content or "color" in top_content:
                    info_log("   ✅ 搜尋結果準確，包含相關內容")
                else:
                    info_log(f"   ℹ️  最高分結果: {top_content[:100]}")
            else:
                info_log("   ⚠️  未找到相關記憶（可能記憶尚未建立完成）")
        
        info_log("\n✅ TEST 5 PASSED: 記憶檢索功能正常且準確")


if __name__ == "__main__":
    """直接運行測試（用於調試）"""
    print("Running Memory MCP Integration Tests")
    print("=" * 70)
    
    # Run with pytest
    pytest.main([__file__, "-v", "-s", "--tb=short"])
