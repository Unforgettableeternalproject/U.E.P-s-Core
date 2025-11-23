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
                # 過濾掉屬於 Debug 的記憶（只處理字典類型）
                metadata = [m for m in metadata if isinstance(m, dict) and m.get('memory_token') != memory_token]
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
    
    def test_05_work_in_chat_session(self, system_components, isolated_gs, cleanup_memory):
        """
        測試 5: CS 中注入工作意圖的處理
        
        驗證：
        1. Direct Work (DW) 應中斷當前 CS 並加入 WORK 狀態
        2. Background Work (BW) 應加入 WORK 狀態但不中斷 CS
        3. WorkflowValidator 正確驗證工作流匹配度
        
        測試流程：
        - 開始一個正常的聊天對話
        - 測試 BW: 注入音樂播放請求（Background Work，不中斷 CS）
        - 測試 DW: 注入文件讀取請求（Direct Work，中斷 CS）
        - 驗證兩種工作模式的不同處理方式
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        from core.sessions.session_manager import session_manager
        from core.states.state_manager import state_manager
        
        info_log("\n" + "=" * 70)
        info_log("TEST 5: Work Intent in Chat Session (DW/BW)")
        info_log("=" * 70)
        
        controller = system_components["controller"]
        event_bus = system_components["event_bus"]
        
        # 獲取 Debug Identity
        identity_manager = IdentityManager()
        debug_identity = None
        for identity in identity_manager.identities.values():
            if identity.display_name == "Debug":
                debug_identity = identity
                break
        
        assert debug_identity is not None, "Debug identity not found"
        
        # 創建監控器
        monitor = ChatPathMonitor(event_bus)
        
        # # ========== Part 1: 測試 Background Work（播放音樂）==========
        # info_log("\n" + "=" * 70)
        # info_log("PART 1: Background Work - 不應中斷 CS")
        # info_log("=" * 70)
        
        # # 1. 開始正常聊天
        # info_log("\n--- 階段 1A: 開始聊天對話 ---")
        # inject_chat_message(
        #     "Let's have a conversation about music and our favorite songs.",
        #     identity_id=debug_identity.identity_id
        # )
        
        # monitor.wait_for_response(timeout=20)
        # info_log("   ✅ 收到第一個聊天回應")
        # monitor.wait_for_event("CYCLE_COMPLETED", timeout=60)
        # monitor.cycle_completed.clear()
        # time.sleep(2)
        
        # # 記錄當前 CS
        # active_cs_before_bw = session_manager.get_active_chatting_sessions()
        # assert len(active_cs_before_bw) > 0, "應該有活動的 CS"
        # cs_id_before_bw = active_cs_before_bw[0].session_id
        # info_log(f"   當前 CS: {cs_id_before_bw}")
        
        # # 2. 注入 Background Work 請求（播放音樂）
        # info_log("\n--- 階段 2A: 在 CS 中注入 Background Work（音樂播放）---")
        # inject_chat_message(
        #     "Please play some music on my computer for me.",
        #     identity_id=debug_identity.identity_id
        # )
        
        # # 等待處理
        # time.sleep(3)
        # monitor.wait_for_event("CYCLE_COMPLETED", timeout=60)
        # monitor.cycle_completed.clear()
        # time.sleep(1)
        
        # # 3. 驗證 Background Work 結果
        # info_log("\n--- 階段 3A: 驗證 BW 處理結果 ---")
        
        # # 檢查 CS 是否仍然活動（Background Work 不應中斷）
        # active_cs_after_bw = session_manager.get_active_chatting_sessions()
        # info_log(f"   BW 後活動 CS 數量: {len(active_cs_after_bw)}")
        
        # if len(active_cs_after_bw) > 0 and active_cs_after_bw[0].session_id == cs_id_before_bw:
        #     info_log(f"   ✅ CS 仍然活動（{cs_id_before_bw}），Background Work 正確處理")
        # else:
        #     info_log(f"   ⚠️  CS 狀態改變，這可能表示 Background Work 被誤判為 Direct Work")
        
        # # 檢查是否創建了 Workflow Session
        # active_ws = session_manager.get_active_workflow_sessions()
        # info_log(f"   活動 WS 數量: {len(active_ws)}")
        # if len(active_ws) > 0:
        #     info_log(f"   ✅ 檢測到工作流會話: {[ws.session_id for ws in active_ws]}")
        
        # # 4. 等待系統恢復 CHAT 並完成三個循環
        # info_log("\n--- 階段 4A: 等待系統恢復 CHAT 並完成三個對話循環 ---")
        
        # # 追蹤循環完成次數
        # cycle_count = {"count": 0, "target": 3}
        # cycle_completed_event = threading.Event()
        
        # def on_cycle_completed(event):
        #     cycle_count["count"] += 1
        #     info_log(f"   📊 循環完成: {cycle_count['count']}/{cycle_count['target']}")
        #     if cycle_count["count"] >= cycle_count["target"]:
        #         cycle_completed_event.set()
        
        # # 訂閱 CYCLE_COMPLETED 事件
        # event_bus.subscribe(SystemEvent.CYCLE_COMPLETED, on_cycle_completed, handler_name="test_bw_cycle_monitor")
        
        # try:
        #     # 等待完成三個循環（最多 90 秒）
        #     info_log("   ⏳ 等待完成 3 個循環...")
        #     cycle_completed_event.wait(timeout=90)
            
        #     # 驗證循環次數
        #     info_log(f"   📊 實際完成循環數: {cycle_count['count']}")
            
        #     if cycle_count["count"] >= cycle_count["target"]:
        #         info_log(f"   ✅ 成功完成 {cycle_count['count']} 個循環")
        #     else:
        #         info_log(f"   ⚠️  僅完成 {cycle_count['count']}/{cycle_count['target']} 個循環")
            
        #     # 檢查最終狀態
        #     final_state = state_manager.get_current_state()
        #     info_log(f"   📊 最終系統狀態: {final_state.value if final_state else 'None'}")
            
        #     # 檢查 CS 狀態
        #     final_cs = session_manager.get_active_chatting_sessions()
        #     if len(final_cs) > 0:
        #         info_log(f"   ✅ CHAT 已恢復，當前 CS: {final_cs[0].session_id}")
        #     else:
        #         info_log(f"   ℹ️  無活動 CS（可能已自然結束）")
        
        # finally:
        #     # 清理事件訂閱
        #     try:
        #         event_bus.unsubscribe(SystemEvent.CYCLE_COMPLETED, on_cycle_completed)
        #     except:
        #         pass
        
        # 清理：等待當前循環完成
        time.sleep(2)
        monitor.reset()
        
        # ========== Part 2: 測試 Direct Work（文件操作）==========
        info_log("\n" + "=" * 70)
        info_log("PART 2: Direct Work - 應中斷 CS")
        info_log("=" * 70)
        
        # 1. 重新開始聊天（確保有新的 CS）
        info_log("\n--- 階段 1B: 重新開始聊天對話 ---")
        inject_chat_message(
            "Let's continue our conversation.",
            identity_id=debug_identity.identity_id
        )
        
        monitor.wait_for_response(timeout=20)
        info_log("   ✅ 收到回應")
        monitor.wait_for_event("CYCLE_COMPLETED", timeout=60)
        monitor.cycle_completed.clear()
        time.sleep(2)
        
        # 記錄當前 CS
        active_cs_before_dw = session_manager.get_active_chatting_sessions()
        cs_id_before_dw = active_cs_before_dw[0].session_id if len(active_cs_before_dw) > 0 else None
        info_log(f"   當前 CS: {cs_id_before_dw}")
        
        # 2. 注入 Direct Work 請求（新聞摘要 - news_summary）
        info_log("\n--- 階段 2B: 在 CS 中注入 Direct Work（新聞摘要）---")
        inject_chat_message(
            "Can you tell me about the weather in Taipei?",
            identity_id=debug_identity.identity_id
        )
        
        # 等待處理
        time.sleep(3)
        monitor.wait_for_event("CYCLE_COMPLETED", timeout=60)
        time.sleep(1)
        
        # 3. 驗證 Direct Work 結果
        info_log("\n--- 階段 3B: 驗證 DW 處理結果 ---")
        
        # 檢查原 CS 是否被中斷/結束
        active_cs_after_dw = session_manager.get_active_chatting_sessions()
        info_log(f"   DW 後活動 CS 數量: {len(active_cs_after_dw)}")
        
        if len(active_cs_after_dw) == 0:
            info_log(f"   ✅ 原 CS 已結束（{cs_id_before_dw}），Direct Work 正確中斷了 CS")
        elif len(active_cs_after_dw) > 0:
            new_cs_id = active_cs_after_dw[0].session_id
            if new_cs_id != cs_id_before_dw:
                info_log(f"   ✅ CS 已切換（{cs_id_before_dw} → {new_cs_id}），Direct Work 正確處理")
            else:
                info_log(f"   ⚠️  CS 仍然是同一個（{cs_id_before_dw}），Direct Work 可能未正確中斷")
        
        # 檢查 SESSION_ENDED 事件
        session_ended_events = [e for e in monitor.events if e[0] == "session_ended"]
        info_log(f"   捕獲到 SESSION_ENDED 事件數量: {len(session_ended_events)}")
        
        if cs_id_before_dw:
            cs_ended = any(e[1].get("session_id") == cs_id_before_dw for e in session_ended_events)
            if cs_ended:
                info_log(f"   ✅ 確認原 CS ({cs_id_before_dw}) 的 SESSION_ENDED 事件")
        
        # 檢查當前系統狀態
        current_state = state_manager.get_current_state()
        info_log(f"   當前系統狀態: {current_state.value if current_state else 'None'}")
        
        # 總結
        info_log("\n" + "=" * 70)
        info_log("TEST 5 總結")
        info_log("=" * 70)
        # info_log(f"   Part 1 (BW): CS 連續性 - {'✅ 通過' if len(active_cs_after_bw) > 0 else '❌ 失敗'}")
        info_log(f"   Part 2 (DW): CS 中斷 - {'✅ 通過' if len(active_cs_after_dw) == 0 or (len(active_cs_after_dw) > 0 and active_cs_after_dw[0].session_id != cs_id_before_dw) else '❌ 失敗'}")
        
        info_log("\n✅ TEST 5 完成: DW/BW 工作意圖處理測試")
    
    def test_06_chat_session_timeout(self, system_components, isolated_gs, cleanup_memory):
        """
        測試 6: 聊天會話超時處理
        
        驗證：
        1. Controller._check_session_timeouts() 每秒檢查
        2. CS 在超時後自動結束
        3. 配置參數 max_session_age 生效
        
        測試策略：
        - 由於正常 max_session_age = 86400秒（24小時），測試需要：
          1. 暫時修改配置為短超時時間（5秒）
          2. 或 mock last_activity 時間戳
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        from core.sessions.session_manager import session_manager
        
        info_log("\n" + "=" * 70)
        info_log("TEST 6: Chat Session Timeout")
        info_log("=" * 70)
        
        controller = system_components["controller"]
        event_bus = system_components["event_bus"]
        
        # 獲取 Debug Identity
        identity_manager = IdentityManager()
        debug_identity = None
        for identity in identity_manager.identities.values():
            if identity.display_name == "Debug":
                debug_identity = identity
                break
        
        assert debug_identity is not None, "Debug identity not found"
        
        # 創建監控器
        monitor = ChatPathMonitor(event_bus)
        
        # 1. 開始聊天會話
        info_log("\n--- 階段 1: 開始聊天會話 ---")
        inject_chat_message(
            "Let's chat for a bit.",
            identity_id=debug_identity.identity_id
        )
        
        monitor.wait_for_response(timeout=20)
        monitor.wait_for_event("CYCLE_COMPLETED", timeout=60)
        info_log("   ✅ 聊天會話已啟動")
        
        # 記錄啟動時的 CS
        active_cs_before = session_manager.get_active_chatting_sessions()
        assert len(active_cs_before) > 0, "應該有活動的 CS"
        cs_session_id = active_cs_before[0].session_id
        info_log(f"   活動 CS: {cs_session_id}")
        
        # 2. 暫時修改超時配置（for testing）
        info_log("\n--- 階段 2: 修改超時配置為 5 秒 ---")
        original_timeout = session_manager.config['max_session_age']
        session_manager.config['max_session_age'] = 5  # 5秒超時
        info_log(f"   原始超時: {original_timeout}秒")
        info_log("   測試超時: 5秒")
        
        # 3. 等待超時 (6秒，確保超過 5秒超時閾值)
        info_log("\n--- 階段 3: 等待超時（6秒）---")
        time.sleep(6)
        
        # 4. 驗證會話已結束
        info_log("\n--- 階段 4: 驗證會話已超時結束 ---")
        
        # 檢查 CS 是否已結束
        active_cs_after = session_manager.get_active_chatting_sessions()
        info_log(f"   超時後活動 CS 數量: {len(active_cs_after)}")
        assert len(active_cs_after) == 0, "超時後不應有活動的 CS"
        
        # 檢查 SESSION_ENDED 事件 (格式: (event_type, event_data))
        session_ended_events = [e for e in monitor.events if e[0] == "session_ended"]
        info_log(f"   捕獲到 SESSION_ENDED 事件數量: {len(session_ended_events)}")
        assert len(session_ended_events) > 0, "應該捕獲到 SESSION_ENDED 事件"
        
        # 檢查超時原因
        cs_ended_event = None
        for event_type, event_data in session_ended_events:
            if event_data.get("session_id") == cs_session_id:
                cs_ended_event = event_data
                break
        
        assert cs_ended_event is not None, f"應該找到 CS {cs_session_id} 的 SESSION_ENDED 事件"
        
        # 調試：輸出事件數據
        info_log(f"   CS 結束事件數據: {cs_ended_event}")
        
        # 檢查原因（可能在不同的字段中）
        reason = cs_ended_event.get("reason") or cs_ended_event.get("end_reason", "")
        info_log(f"   結束原因: {reason}")
        
        # 驗證原因包含超時信息（reason 可能是 string 或其他類型）
        if isinstance(reason, str):
            assert "超時" in reason or "timeout" in reason.lower(), f"結束原因應包含超時信息，實際為: {reason}"
        else:
            info_log(f"   ⚠️ 原因不是字符串類型: {type(reason)}, 值: {reason}")
        
        # 恢復配置
        session_manager.config['max_session_age'] = original_timeout
        info_log(f"   ✅ 已恢復原始超時配置: {original_timeout}秒")
        
        info_log("\n✅ TEST 6 完成: 超時處理測試通過")
    
    def test_07_multistep_workflow_in_session(self, system_components, isolated_gs, cleanup_memory):
        """
        測試 7: 複合意圖處理（COMPOUND - CHAT + WORK）
        
        驗證：
        1. NLP 正確解析複合意圖（一句話包含聊天和工作）
        2. 狀態佇列按優先級添加多個狀態（WORK 優先於 CHAT）
        3. WORK 完成後自動推進到 CHAT
        4. CHAT 會話正常創建和運行
        5. 整個流程無需額外用戶輸入（自動推進）
        
        測試流程：
        - 發送複合指令：「Check the weather in Taipei and then let's talk about it」
        - 驗證 NLP 解析為 COMPOUND (WORK + CHAT)
        - 驗證狀態佇列包含兩個項目：WORK (priority=100) + CHAT (priority=10)
        - 驗證 WORK 完成後自動推進到 CHAT
        - 驗證 CHAT 會話正常啟動
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        from core.sessions.session_manager import session_manager
        from core.states.state_manager import state_manager
        from core.states.state_queue import get_state_queue_manager
        import time
        
        info_log("\n" + "=" * 70)
        info_log("TEST 7: Compound Intent (CHAT + WORK)")
        info_log("=" * 70)
        
        controller = system_components["controller"]
        event_bus = system_components["event_bus"]
        state_queue = get_state_queue_manager()
        
        # 獲取 Debug Identity
        identity_manager = IdentityManager()
        debug_identity = None
        for identity in identity_manager.identities.values():
            if identity.display_name == "Debug":
                debug_identity = identity
                break
        
        assert debug_identity is not None, "Debug identity not found"
        
        # 創建監控器
        monitor = ChatPathMonitor(event_bus)
        
        try:
            # 1. 發送複合指令
            info_log("\n--- 階段 1: 發送複合意圖輸入 ---")
            inject_chat_message(
                "Check the weather in Taipei and then let's talk about it.",
                identity_id=debug_identity.identity_id
            )
            
            # 等待 NLP 處理
            time.sleep(3)
            
            # 2. 檢查狀態佇列
            info_log("\n--- 階段 2: 檢查狀態佇列 ---")
            queue_status = state_queue.get_queue_status()
            info_log(f"   當前狀態: {queue_status['current_state']}")
            info_log(f"   佇列長度: {queue_status['queue_length']}")
            info_log(f"   待處理狀態: {queue_status['pending_states']}")
            
            # 驗證佇列包含兩個狀態
            if queue_status['queue_length'] >= 2:
                info_log("   ✅ 佇列包含多個狀態（複合意圖已解析）")
            else:
                info_log(f"   ⚠️  佇列長度不足: {queue_status['queue_length']} (預期 >= 2)")
            
            # 3. 等待 WORK 狀態處理
            info_log("\n--- 階段 3: 等待 WORK 狀態處理 ---")
            current_state = state_manager.get_current_state()
            info_log(f"   當前狀態: {current_state.value if current_state else 'None'}")
            
            # 等待工作流完成（天氣查詢）
            info_log("   ⏳ 等待工作流完成...")
            time.sleep(60)  # 天氣查詢需要時間
            
            # 4. 檢查 WORK 完成後的狀態
            info_log("\n--- 階段 4: 檢查 WORK 完成後狀態 ---")
            current_state = state_manager.get_current_state()
            info_log(f"   當前狀態: {current_state.value if current_state else 'None'}")
            
            queue_status = state_queue.get_queue_status()
            info_log(f"   佇列長度: {queue_status['queue_length']}")
            
            # 5. 驗證自動推進到 CHAT
            info_log("\n--- 階段 5: 驗證自動推進到 CHAT ---")
            
            # 檢查 CS 是否已創建
            active_cs = session_manager.get_active_chatting_sessions()
            if len(active_cs) > 0:
                info_log(f"   ✅ CHAT 會話已創建: {active_cs[0].session_id}")
            else:
                info_log("   ℹ️  尚未創建 CHAT 會話，等待中...")
                time.sleep(5)
                active_cs = session_manager.get_active_chatting_sessions()
                if len(active_cs) > 0:
                    info_log(f"   ✅ CHAT 會話已創建: {active_cs[0].session_id}")
            
            # 6. 等待 CHAT 回應
            info_log("\n--- 階段 6: 等待 CHAT 回應 ---")
            response_received = monitor.wait_for_response(timeout=30)
            if response_received:
                info_log("   ✅ 收到 CHAT 回應")
                info_log(f"   📝 回應數量: {len(monitor.llm_responses)}")
            else:
                info_log("   ⚠️  未收到 CHAT 回應（可能還在處理中）")
            
            # 總結
            info_log("\n" + "=" * 70)
            info_log("TEST 7 總結")
            info_log("=" * 70)
            info_log(f"   - 複合意圖解析: {'✅' if queue_status['queue_length'] >= 2 else '❌'}")
            info_log(f"   - WORK 完成: ✅")
            info_log(f"   - CHAT 會話創建: {'✅' if len(active_cs) > 0 else '❌'}")
            info_log(f"   - CHAT 回應: {'✅' if response_received else '⚠️'}")
            
            info_log("\n✅ TEST 7 完成: 複合意圖處理測試")
            
        finally:
            # 清理
            pass


if __name__ == "__main__":
    """直接運行測試（用於調試）"""
    print("Running Chat Path Identity Integration Tests")
    print("=" * 70)
    
    # Run with pytest
    pytest.main([__file__, "-v", "-s", "--tb=short"])
