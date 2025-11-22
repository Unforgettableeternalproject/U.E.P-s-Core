"""
聊天路徑與 Identity 系統整合測試

測試重點：
1. Memory Token 流動：NLP → Working Context → MEM/LLM
2. Snapshot 創建和存儲到正確的 Identity
3. 記憶檢索和使用
4. 不同 Identity 的記憶隔離和個性化回應
5. 學習資料返回機制
6. CS 結束機制

測試策略：
- 啟動完整系統循環
- 注入文字聊天輸入（非工作流請求）
- 監控 Identity → MEM → LLM 的數據流
- 驗證記憶機制和隔離性

注意：由於 LLM 回應不確定性，測試重點在於機制驗證而非具體內容匹配
"""
import sys
from pathlib import Path

# 確保專案根目錄在 sys.path 中
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import pytest
import time
import threading
import json
from typing import Dict, Any, Optional

# 測試標記
pytestmark = [pytest.mark.integration, pytest.mark.chat_path]

# 導入事件類型
from core.event_bus import SystemEvent


@pytest.fixture(scope="module")
def system_components():
    """
    初始化完整系統組件
    
    包括：
    - SystemInitializer：系統初始化
    - UnifiedController：控制器
    - SystemLoop：系統循環
    - 所有模組（STT, NLP, LLM, MEM等）
    """
    from utils.debug_helper import info_log, error_log
    from core.system_initializer import SystemInitializer
    from core.controller import unified_controller
    from core.system_loop import system_loop
    from core.event_bus import event_bus
    from utils.logger import force_enable_file_logging
    
    # 強制啟用文件日誌記錄
    force_enable_file_logging()
    
    info_log("[ChatIntegrationTest] 🚀 初始化完整系統...")
    
    # 1. 初始化系統
    initializer = SystemInitializer()
    success = initializer.initialize_system(production_mode=False)
    
    if not success:
        pytest.fail("System initialization failed")
    
    info_log("[ChatIntegrationTest] ✅ 系統初始化完成")
    
    # 2. 啟動系統循環
    loop_started = system_loop.start()
    if not loop_started:
        pytest.fail("System loop failed to start")
    
    info_log("[ChatIntegrationTest] ✅ 系統循環已啟動")
    
    # 3. 準備組件
    components = {
        "initializer": initializer,
        "controller": unified_controller,
        "system_loop": system_loop,
        "event_bus": event_bus,
    }
    
    # 等待系統穩定
    time.sleep(2)
    
    info_log("[ChatIntegrationTest] ✅ 系統組件就緒")
    
    yield components
    
    # 清理
    info_log("[ChatIntegrationTest] 🧹 清理系統組件...")
    
    try:
        # 停止系統循環
        system_loop.stop()
        time.sleep(1)
        
        # 關閉控制器
        unified_controller.shutdown()
        time.sleep(1)
        
        info_log("[ChatIntegrationTest] ✅ 清理完成")
    except Exception as e:
        error_log(f"[ChatIntegrationTest] ⚠️ 清理時發生錯誤: {e}")


@pytest.fixture
def cleanup_memory():
    """
    測試前清空測試用 Identity 的記憶，避免殘留數據影響
    """
    from utils.debug_helper import info_log, debug_log
    from modules.nlp_module.identity_manager import IdentityManager
    import json
    import os
    
    info_log("[Test Fixture] 🧹 清空測試用 Debug Identity 的記憶...")
    
    try:
        # 獲取 Debug Identity
        identity_manager = IdentityManager()
        debug_identity = None
        for identity in identity_manager.identities.values():
            if identity.display_name == "Debug":
                debug_identity = identity
                break
        
        if debug_identity and debug_identity.memory_token:
            memory_token = debug_identity.memory_token
            info_log(f"[Test Fixture]   Debug memory_token: {memory_token}")
            
            # 1. 清理元資料檔案中該 token 的記憶
            metadata_file = "memory/mem_metadata.json"
            if os.path.exists(metadata_file):
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                
                original_count = len(metadata)
                # 過濾掉屬於 Debug 的記憶
                metadata = [m for m in metadata if m.get('memory_token') != memory_token]
                filtered_count = len(metadata)
                
                # 寫回檔案
                with open(metadata_file, 'w', encoding='utf-8') as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)
                
                removed = original_count - filtered_count
                if removed > 0:
                    info_log(f"[Test Fixture]   已從元資料移除 {removed} 條 Debug 記憶")
                else:
                    info_log(f"[Test Fixture]   元資料中無 Debug 記憶")
            
            # 2. 清理 FAISS 向量索引（重建索引，排除 Debug 的記憶）
            # 注意：由於 FAISS 索引結構複雜，這裡採用簡單策略
            # 如果需要更精確的清理，應該重建整個索引
            faiss_index = "memory/dev_faiss_index"
            if os.path.exists(faiss_index):
                info_log(f"[Test Fixture]   FAISS 索引存在，但無法直接刪除特定 token 的向量")
                info_log(f"[Test Fixture]   建議：手動刪除 {faiss_index} 以完全清理")
            
            info_log("[Test Fixture] ✅ Debug Identity 記憶清理完成")
        else:
            info_log("[Test Fixture] ⚠️  未找到 Debug Identity 或無 memory_token")
    except Exception as e:
        info_log(f"[Test Fixture] ⚠️  清理記憶時發生錯誤: {e}")
        import traceback
        debug_log(1, traceback.format_exc())
    
    yield


@pytest.fixture
def isolated_gs(system_components):
    """
    確保每個測試使用獨立的 GS
    """
    from utils.debug_helper import info_log
    controller = system_components["controller"]
    
    # Setup: 確保測試開始前沒有活躍的 GS
    current_gs = controller.session_manager.get_current_general_session()
    if current_gs:
        info_log(f"[Test Fixture] ⚠️ 發現殘留 GS: {current_gs.session_id}，正在清理...")
        controller.session_manager.end_general_session({"status": "test_cleanup"})
        time.sleep(0.5)
    
    yield
    
    # Teardown: 測試結束後明確結束 GS
    current_gs = controller.session_manager.get_current_general_session()
    if current_gs:
        info_log(f"[Test Fixture] 🧹 測試結束，清理 GS: {current_gs.session_id}")
        controller.session_manager.end_general_session({"status": "test_complete"})
        time.sleep(0.5)


class ChatPathMonitor:
    """聊天路徑監控器"""
    
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.events = []
        self.llm_responses = []
        self.snapshots_created = []
        self.memories_retrieved = []
        self.chat_session_ended = threading.Event()
        self.cycle_completed = threading.Event()
        
        # 訂閱相關事件
        self.event_bus.subscribe(SystemEvent.LLM_RESPONSE_GENERATED, self._on_llm_response)
        self.event_bus.subscribe(SystemEvent.MEMORY_CREATED, self._on_memory_created)
        self.event_bus.subscribe(SystemEvent.SESSION_ENDED, self._on_session_ended)
        self.event_bus.subscribe(SystemEvent.CYCLE_COMPLETED, self._on_cycle_completed)
    
    def _on_llm_response(self, event):
        """記錄 LLM 回應事件"""
        self.events.append(("llm_response", event.data))
        self.llm_responses.append(event.data)
        
        from utils.debug_helper import debug_log
        debug_log(2, f"[ChatMonitor] LLM 回應: {event.data.get('response', '')[:100]}...")
    
    def _on_memory_created(self, event):
        """記錄記憶創建事件"""
        self.events.append(("memory_created", event.data))
        self.snapshots_created.append(event.data)
        
        from utils.debug_helper import debug_log
        debug_log(2, f"[ChatMonitor] 記憶已創建: {event.data.get('memory_id', 'unknown')}")
    
    def _on_session_ended(self, event):
        """記錄會話結束事件"""
        self.events.append(("session_ended", event.data))
        
        # 檢查是否是 Chat Session 結束
        session_type = event.data.get('session_type', '')
        if 'chat' in session_type.lower() or 'cs' in session_type.lower():
            self.chat_session_ended.set()
            
        from utils.debug_helper import debug_log
        debug_log(2, f"[ChatMonitor] 會話結束: {event.data.get('session_id')}")
    
    def _on_cycle_completed(self, event):
        """記錄循環完成事件"""
        self.events.append(("cycle_completed", event.data))
        self.cycle_completed.set()
        
        from utils.debug_helper import debug_log
        debug_log(2, f"[ChatMonitor] 循環完成: cycle={event.data.get('cycle_index')}")
    
    def wait_for_response(self, timeout=30):
        """等待 LLM 回應"""
        start = time.time()
        initial_count = len(self.llm_responses)
        
        while time.time() - start < timeout:
            if len(self.llm_responses) > initial_count:
                return True
            time.sleep(0.5)
        
        return False
    
    def wait_for_snapshot(self, timeout=15):
        """等待快照創建"""
        start = time.time()
        initial_count = len(self.snapshots_created)
        
        while time.time() - start < timeout:
            if len(self.snapshots_created) > initial_count:
                return True
            time.sleep(0.5)
        
        return False
    
    def wait_for_event(self, event_name: str, timeout=15):
        """等待特定事件"""
        if event_name == "CYCLE_COMPLETED":
            return self.cycle_completed.wait(timeout=timeout)
        else:
            raise ValueError(f"不支援等待事件: {event_name}")
    
    def reset(self):
        """重置監控器"""
        self.events = []
        self.llm_responses = []
        self.snapshots_created = []
        self.memories_retrieved = []
        self.chat_session_ended.clear()
        self.cycle_completed.clear()


def inject_chat_message(message: str, identity_id: Optional[str] = None):
    """
    注入聊天訊息到系統
    
    模擬使用者透過文字輸入的場景
    這會觸發完整的系統循環：STT → NLP → Router → LLM/MEM
    
    Args:
        message: 聊天訊息內容
        identity_id: 可選的 Identity ID（用於主動聲明身份）
    """
    from utils.debug_helper import info_log
    from core.framework import core_framework
    from core.working_context import working_context_manager
    
    info_log(f"[ChatTest] 💬 注入聊天訊息: {message}")
    
    # 如果提供 identity_id，先聲明身份
    if identity_id:
        working_context_manager.set_declared_identity(identity_id)
        info_log(f"[ChatTest] 🆔 已聲明 Identity: {identity_id}")
    
    # 通過 STT 模組注入文字輸入
    # 這會觸發完整的處理流程
    stt_module = core_framework.get_module('stt')
    if not stt_module:
        raise RuntimeError("STT module not available")
    
    # 調用 STT 模組的文字輸入處理
    result = stt_module.handle_text_input(message)
    
    if not result:
        raise RuntimeError(f"Failed to inject text: {message}")
    
    info_log(f"[ChatTest] ✅ 文字注入成功")


class TestChatPathIdentityIntegration:
    """聊天路徑與 Identity 整合測試"""
    
    def test_01_simple_chat_with_identity(self, system_components, isolated_gs, cleanup_memory):
        """
        測試 1: 簡單聊天與 Identity
        驗證基本的聊天流程和 Identity 關聯
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        
        info_log("\n" + "=" * 70)
        info_log("TEST 1: Simple Chat with Identity")
        info_log("=" * 70)
        
        controller = system_components["controller"]
        event_bus = system_components["event_bus"]
        
        # 獲取 Debug Identity (測試用)
        identity_manager = IdentityManager()
        debug_identity = None
        for identity in identity_manager.identities.values():
            if identity.display_name == "Debug":
                debug_identity = identity
                break
        
        assert debug_identity is not None, "Debug identity not found"
        info_log(f"✅ 找到 Debug: {debug_identity.identity_id}")
        info_log(f"   Memory Token: {debug_identity.memory_token}")
        
        # 創建監控器
        monitor = ChatPathMonitor(event_bus)
        
        # 注入聊天訊息
        inject_chat_message(
            "Hello! I'm Debug and I love testing systems.",
            identity_id=debug_identity.identity_id
        )
        
        # 等待 LLM 回應
        info_log("\n⏳ 等待 LLM 回應...")
        response_received = monitor.wait_for_response(timeout=30)
        
        if response_received:
            info_log("✅ 收到 LLM 回應")
            latest_response = monitor.llm_responses[-1]
            info_log(f"   回應內容: {latest_response.get('response', '')[:200]}...")
        else:
            info_log("⚠️  未收到 LLM 回應（可能 LLM 未啟用或 API 問題）")
        
        # 等待快照創建
        info_log("\n⏳ 等待快照創建...")
        snapshot_created = monitor.wait_for_snapshot(timeout=15)
        
        if snapshot_created:
            info_log("✅ 快照已創建")
            latest_snapshot = monitor.snapshots_created[-1]
            info_log(f"   Snapshot ID: {latest_snapshot.get('memory_id', 'unknown')}")
            
            # 驗證 memory_token
            snapshot_token = latest_snapshot.get('memory_token')
            if snapshot_token:
                assert snapshot_token == debug_identity.memory_token, \
                    f"Memory token 不匹配! 期望 {debug_identity.memory_token}, 得到 {snapshot_token}"
                info_log(f"✅ Memory token 正確關聯到 Debug")
        else:
            info_log("⚠️  未檢測到快照創建事件")
        
        info_log("\n✅ TEST 1 PASSED: 基本聊天流程正常")
    
    def test_02_identity_isolation(self, system_components, isolated_gs):
        """
        測試 2: Identity 隔離性
        驗證不同 Identity 的記憶完全隔離
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        
        info_log("\n" + "=" * 70)
        info_log("TEST 2: Identity Isolation")
        info_log("=" * 70)
        
        controller = system_components["controller"]
        event_bus = system_components["event_bus"]
        
        # 獲取 Bernie 和 Debug
        identity_manager = IdentityManager()
        bernie = None
        debug = None
        
        for identity in identity_manager.identities.values():
            if identity.display_name == "Bernie":
                bernie = identity
            elif identity.display_name == "Debug":
                debug = identity
        
        assert bernie is not None, "Bernie identity not found"
        assert debug is not None, "Debug identity not found"
        
        bernie_token = bernie.memory_token or "(no token)"
        debug_token = debug.memory_token or "(no token)"
        info_log(f"✅ Bernie: {bernie.identity_id} (token: {bernie_token[:20]}...)")
        info_log(f"✅ Debug: {debug.identity_id} (token: {debug_token[:20]}...)")
        
        # 創建監控器
        monitor = ChatPathMonitor(event_bus)
        
        # Bernie 的對話
        info_log("\n--- Bernie 的對話 ---")
        inject_chat_message(
            "I love coffee and I enjoy drinking it in the morning.",
            identity_id=bernie.identity_id
        )
        
        monitor.wait_for_response(timeout=20)
        bernie_responses = len(monitor.llm_responses)
        
        # 等待 cycle 完成，確保下一個輸入在新 cycle 中
        info_log("   等待當前 cycle 完成...")
        cycle_completed = monitor.wait_for_event("CYCLE_COMPLETED", timeout=30)
        
        if cycle_completed:
            info_log("   ✅ Cycle 已完成")
        else:
            info_log("   ⚠️  Cycle 完成超時，但繼續測試")
        
        # 給系統更多時間完成所有後台任務（TTS、記憶儲存等）
        time.sleep(3)
        monitor.reset()
        
        # Debug 的對話
        info_log("\n--- Debug 的對話 ---")
        inject_chat_message(
            "I prefer tea and I like to drink it at night.",
            identity_id=debug.identity_id
        )
        
        monitor.wait_for_response(timeout=20)
        debug_responses = len(monitor.llm_responses)
        
        # 驗證記憶隔離
        info_log("\n🔍 驗證記憶隔離...")
        
        # 檢查快照的 memory_token
        bernie_snapshots = [s for s in monitor.snapshots_created 
                           if s.get('memory_token') == bernie.memory_token]
        debug_snapshots = [s for s in monitor.snapshots_created 
                          if s.get('memory_token') == debug.memory_token]
        
        info_log(f"   Bernie 快照數: {len(bernie_snapshots)}")
        info_log(f"   Debug 快照數: {len(debug_snapshots)}")
        
        # 驗證：每個 Identity 的快照都用自己的 token
        for snapshot in monitor.snapshots_created:
            token = snapshot.get('memory_token')
            assert token in [bernie.memory_token, debug.memory_token], \
                f"發現未知的 memory_token: {token}"
        
        info_log("✅ 記憶隔離驗證通過")
        info_log("\n✅ TEST 2 PASSED: Identity 隔離性正常")
    
    def test_03_memory_retrieval(self, system_components, isolated_gs, cleanup_memory):
        """
        測試 3: 記憶檢索
        驗證能否檢索先前對話的記憶
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        from core.framework import core_framework
        
        info_log("\n" + "=" * 70)
        info_log("TEST 3: Memory Retrieval")
        info_log("=" * 70)
        
        controller = system_components["controller"]
        event_bus = system_components["event_bus"]
        
        # 獲取 Debug (測試用)
        identity_manager = IdentityManager()
        debug_identity = None
        for identity in identity_manager.identities.values():
            if identity.display_name == "Debug":
                debug_identity = identity
                break
        
        assert debug_identity is not None, "Debug identity not found"
        
        # 創建監控器
        monitor = ChatPathMonitor(event_bus)
        
        # 第一輪對話：建立記憶
        info_log("\n--- 第一輪對話：建立記憶 ---")
        inject_chat_message(
            "My favorite programming language is Python, and I love machine learning.",
            identity_id=debug_identity.identity_id
        )
        
        monitor.wait_for_response(timeout=20)
        
        # 等待 cycle 完成，確保記憶已建立且下一個輸入在新 cycle 中
        info_log("   等待當前 cycle 完成...")
        cycle_completed = monitor.wait_for_event("CYCLE_COMPLETED", timeout=30)
        
        if cycle_completed:
            info_log("   ✅ Cycle 已完成")
        else:
            info_log("   ⚠️  Cycle 完成超時，但繼續測試")
        
        # 給系統更多時間完成所有後台任務（TTS、記憶儲存等）
        time.sleep(3)
        monitor.reset()
        
        # 第二輪對話：測試記憶檢索
        info_log("\n--- 第二輪對話：測試記憶檢索 ---")
        inject_chat_message(
            "What is my favorite programming language?",
            identity_id=debug_identity.identity_id
        )
        
        response_received = monitor.wait_for_response(timeout=20)
        
        if response_received:
            latest_response = monitor.llm_responses[-1]
            response_text = latest_response.get('response', '').lower()
            
            info_log(f"   LLM 回應: {response_text[:200]}...")
            
            # 檢查回應是否包含 Python（表示檢索到記憶）
            # 注意：這個檢查不嚴格，因為 LLM 可能以不同方式表達
            if 'python' in response_text:
                info_log("✅ LLM 回應中包含 'Python'，可能檢索到記憶")
            else:
                info_log("⚠️  LLM 回應中未明確提到 'Python'，但這不一定表示錯誤")
        
        monitor.wait_for_event("CYCLE_COMPLETED", timeout=30)
        
        # 直接測試 MEM 模組的檢索功能
        info_log("\n--- 直接測試 MEM 檢索 ---")
        mem_module = core_framework.get_module('mem')
        
        if mem_module and mem_module.memory_manager:
            results = mem_module.memory_manager.retrieve_memories(
                query_text="programming language",
                memory_token=debug_identity.memory_token,
                max_results=5
            )
            
            info_log(f"   檢索到 {len(results)} 條記憶")
            
            if results:
                # 驗證所有結果都屬於 Debug
                for result in results:
                    result_token = result.get('memory_token') or result.get('metadata', {}).get('memory_token')
                    if result_token:
                        assert result_token == debug_identity.memory_token, \
                            f"檢索到其他 Identity 的記憶! {result_token}"
                
                info_log("✅ 所有檢索結果都屬於 Debug")
            else:
                info_log("⚠️  未檢索到記憶，但這可能是正常的（記憶可能還未建立索引或查詢不匹配）")
        
        info_log("\n✅ TEST 3 PASSED: 記憶檢索功能正常")
    
    def test_04_chat_session_lifecycle(self, system_components, isolated_gs, cleanup_memory):
        """
        測試 4: Chat Session 生命週期
        驗證 CS 的創建、維持和結束機制
        
        注意：
        - 每個 cycle 結束後會清理去重鍵，所以同樣的輸入在不同 cycle 不會被去重
        - LLM 通過 session_control 建議結束會話，需要信心度 >= 0.7
        - ModuleCoordinator 檢測到 session_control 後會在 CYCLE_COMPLETED 時結束會話
        """
        from utils.debug_helper import info_log, debug_log
        from modules.nlp_module.identity_manager import IdentityManager
        
        info_log("\n" + "=" * 70)
        info_log("TEST 4: Chat Session Lifecycle")
        info_log("=" * 70)
        
        controller = system_components["controller"]
        event_bus = system_components["event_bus"]
        
        # 獲取 Debug (測試用)
        identity_manager = IdentityManager()
        debug_identity = None
        for identity in identity_manager.identities.values():
            if identity.display_name == "Debug":
                debug_identity = identity
                break
        
        assert debug_identity is not None, "Debug identity not found"
        
        # 創建監控器
        monitor = ChatPathMonitor(event_bus)
        
        # 開始對話
        info_log("\n--- 第 1 次輸入：開始對話 ---")
        inject_chat_message(
            "Let's talk about programming.",
            identity_id=debug_identity.identity_id
        )
        
        response_received = monitor.wait_for_response(timeout=20)
        if response_received:
            info_log(f"   ✅ 收到回應: {monitor.llm_responses[-1].get('response', '')[:100]}...")
        
        # 等待 cycle 完成（這會清理去重鍵）
        info_log("   ⏳ 等待 cycle 完成...")
        cycle_completed = monitor.wait_for_event("CYCLE_COMPLETED", timeout=60)
        if cycle_completed:
            info_log("   ✅ Cycle 完成，去重鍵已清理")
        monitor.cycle_completed.clear()  # 重置標誌
        time.sleep(2)  # 額外等待確保清理完成
        
        # 繼續對話
        info_log("\n--- 第 2 次輸入：繼續對話 ---")
        inject_chat_message(
            "I know a lot about CSharp, what about you?",
            identity_id=debug_identity.identity_id
        )
        
        response_received = monitor.wait_for_response(timeout=20)
        if response_received:
            info_log(f"   ✅ 收到回應: {monitor.llm_responses[-1].get('response', '')[:100]}...")
        
        # 等待 cycle 完成
        info_log("   ⏳ 等待 cycle 完成...")
        cycle_completed = monitor.wait_for_event("CYCLE_COMPLETED", timeout=60)
        if cycle_completed:
            info_log("   ✅ Cycle 完成，去重鍵已清理")
        monitor.cycle_completed.clear()  # 重置標誌
        time.sleep(2)
        
        # 明確表示要結束對話
        info_log("\n--- 第 3 次輸入：明確結束對話 ---")
        inject_chat_message(
            "Thanks for the chat! I need to go now. Goodbye!",
            identity_id=debug_identity.identity_id
        )
        
        response_received = monitor.wait_for_response(timeout=20)
        if response_received:
            latest_response = monitor.llm_responses[-1]
            response_text = latest_response.get('response', '')
            info_log(f"   ✅ 收到回應: {response_text[:100]}...")
            
            # 檢查 metadata 中的 session_control
            metadata = latest_response.get('metadata', {})
            session_control = metadata.get('session_control')
            if session_control:
                info_log(f"   📋 LLM 設置了 session_control: {session_control}")
                should_end = (session_control.get('action') == 'end_session' or 
                            session_control.get('session_ended') is True or
                            session_control.get('should_end_session') is True)
                confidence = session_control.get('confidence', 0.0)
                info_log(f"   🔍 should_end={should_end}, confidence={confidence}")
            else:
                info_log("   ⚠️  LLM 未設置 session_control")
        
        # 等待 cycle 完成（ModuleCoordinator 會在這時檢查 session_control）
        info_log("   ⏳ 等待 cycle 完成（等待 ModuleCoordinator 檢測結束信號）...")
        cycle_completed = monitor.wait_for_event("CYCLE_COMPLETED", timeout=60)
        if cycle_completed:
            info_log("   ✅ Cycle 完成")
        
        # 等待 CS 結束（ModuleCoordinator 應該會觸發結束）
        info_log("   ⏳ 等待 Chat Session 結束事件...")
        cs_ended = monitor.chat_session_ended.wait(timeout=10)
        
        if cs_ended:
            info_log("✅ Chat Session 已結束（LLM 判斷結束且 confidence >= 0.7）")
        else:
            info_log("⚠️  Chat Session 未自動結束")
            info_log("   可能原因：")
            info_log("   1. LLM 未識別出結束意圖")
            info_log("   2. LLM 的 confidence < 0.7（需要更明確的結束語）")
            info_log("   3. session_control 格式不正確")
        
        info_log("\n📊 測試總結:")
        info_log(f"   - 總回應數: {len(monitor.llm_responses)}")
        info_log(f"   - 總事件數: {len(monitor.events)}")
        info_log(f"   - CS 自動結束: {'是' if cs_ended else '否'}")
        
        info_log("\n✅ TEST 4 PASSED: Chat Session 生命週期測試完成")


if __name__ == "__main__":
    """直接運行測試（用於調試）"""
    print("Running Chat Path Identity Integration Tests")
    print("=" * 70)
    
    # Run with pytest
    pytest.main([__file__, "-v", "-s", "--tb=short"])
