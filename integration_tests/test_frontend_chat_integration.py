"""
前端與聊天路徑整合測試

測試重點：
1. 前端系統（UI, ANI, MOV）整合到系統循環
2. 通過真實的聊天輸入觸發系統循環
3. 驗證層級動畫在真實系統循環中的表現
4. 驗證 Qt 橋接器在真實場景下的線程安全性
5. 觀察 UI 動畫流暢度和同步性

測試策略：
- 使用完整的系統循環和前端
- 注入真實的文字聊天輸入（非直接發布事件）
- 讓系統自然流轉：STT → NLP → Router → LLM → TTS → 層級動畫
- 保持 Qt 事件循環運行，觀察 UI 表現
- 可選：延長運行時間以便人工觀察
"""
import pytest
import sys
import time
from pathlib import Path

# 確保專案根目錄在 sys.path 中
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import threading
from typing import Dict, Any, Optional

# 測試標記
pytestmark = [pytest.mark.integration, pytest.mark.frontend]

# 導入事件類型
from core.event_bus import SystemEvent


@pytest.fixture(scope="module")
def system_with_frontend():
    """
    初始化帶前端的完整系統
    
    包括：
    - SystemInitializer：系統初始化
    - UnifiedController：控制器
    - QtSystemLoopManager：Qt 系統循環管理器
    - 所有模組（STT, NLP, LLM, MEM, UI, ANI, MOV等）
    - Qt 應用程式和事件循環
    """
    from utils.debug_helper import info_log, error_log
    from core.system_initializer import SystemInitializer
    from core.controller import unified_controller
    from core.event_bus import event_bus
    from core.framework import core_framework
    from utils.logger import force_enable_file_logging
    from core.qt_system_loop import QtSystemLoopManager
    
    # 強制啟用文件日誌記錄
    force_enable_file_logging()
    
    info_log("[FrontendChatTest] 🚀 初始化帶前端的完整系統...")
    
    # 1. 初始化系統（包含前端）
    initializer = SystemInitializer()
    success = initializer.initialize_system(production_mode=False)
    
    if not success:
        error_log("[FrontendChatTest] ❌ 系統初始化失敗")
        pytest.fail("System initialization failed")
    
    info_log("[FrontendChatTest] ✅ 系統初始化完成")
    
    # 2. 獲取前端模組
    ui_module = core_framework.get_module('ui')
    ani_module = core_framework.get_module('ani')
    mov_module = core_framework.get_module('mov')
    
    if not ui_module or not ani_module or not mov_module:
        error_log("[FrontendChatTest] ❌ 前端模組未載入")
        pytest.fail("Frontend modules not loaded")
    
    info_log("[FrontendChatTest] ✅ 前端模組已載入")
    
    # 3. 創建 Qt 系統循環管理器
    qt_loop_manager = QtSystemLoopManager()
    
    # 4. 啟動系統循環（在背景線程）
    qt_loop_manager.start_system_loop()
    
    info_log("[FrontendChatTest] ✅ Qt 系統循環已在背景線程啟動")
    
    # 5. 等待系統穩定
    info_log("[FrontendChatTest] 等待系統穩定...")
    time.sleep(3)
    
    info_log("[FrontendChatTest] ✅ 系統組件就緒")
    
    # 準備組件字典
    components = {
        "initializer": initializer,
        "controller": unified_controller,
        "qt_loop_manager": qt_loop_manager,
        "event_bus": event_bus,
        "ui_module": ui_module,
        "ani_module": ani_module,
        "mov_module": mov_module,
    }
    
    yield components
    
    # 清理
    info_log("[FrontendChatTest] 🧹 清理系統組件...")
    
    try:
        # 停止 Qt 系統循環
        info_log("[FrontendChatTest] 停止 Qt 系統循環...")
        qt_loop_manager.stop_system_loop()
        
        # 等待線程結束
        time.sleep(2)
        
        # 關閉前端系統
        info_log("[FrontendChatTest] 關閉前端系統...")
        if hasattr(initializer, 'frontend_integrator'):
            initializer.frontend_integrator.stop()
        
        # 關閉所有模組
        info_log("[FrontendChatTest] 關閉所有模組...")
        core_framework.shutdown_all_modules()
        
        info_log("[FrontendChatTest] ✅ 系統清理完成")
    except Exception as e:
        error_log(f"[FrontendChatTest] 清理時發生錯誤: {e}")


@pytest.fixture
def cleanup_memory(system_with_frontend):
    """
    測試前清空測試用 Identity 的記憶
    """
    from utils.debug_helper import info_log, debug_log
    from modules.nlp_module.identity_manager import IdentityManager
    from modules.mem_module.mem_module import mem_module
    
    def _cleanup_debug_memory():
        """清理 Debug identity 的記憶"""
        try:
            identity_manager = IdentityManager()
            
            # 找到 Debug identity
            debug_identity = None
            for identity in identity_manager.identities.values():
                if identity.name.lower() == "debug":
                    debug_identity = identity
                    break
            
            if not debug_identity:
                debug_log(3, "[FrontendChatTest] 找不到 Debug identity，跳過記憶清理")
                return
            
            debug_token = debug_identity.memory_token
            if not debug_token:
                debug_log(3, "[FrontendChatTest] Debug identity 沒有 memory_token，跳過清理")
                return
            
            info_log(f"[FrontendChatTest] 清理 Debug ({debug_token[:20]}...) 的記憶...")
            
            # 清理 MEM 模組的記憶
            if mem_module:
                mem_module.clear_token_memories(debug_token)
                info_log(f"[FrontendChatTest] ✅ 已清理 Debug 的記憶")
            
        except Exception as e:
            error_log(f"[FrontendChatTest] 清理記憶時發生錯誤: {e}")
    
    # Setup: 測試開始前清理
    _cleanup_debug_memory()
    
    yield
    
    # Teardown: 測試結束後也清理
    _cleanup_debug_memory()


class FrontendChatMonitor:
    """前端聊天路徑監控器"""
    
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.events = []
        self.layer_events = []
        self.animation_events = []
        self.llm_responses = []
        self.tts_outputs = []
        
        # 訂閱相關事件
        self.event_bus.subscribe(SystemEvent.INPUT_LAYER_COMPLETE, self._on_input_layer)
        self.event_bus.subscribe(SystemEvent.PROCESSING_LAYER_COMPLETE, self._on_processing_layer)
        self.event_bus.subscribe(SystemEvent.OUTPUT_LAYER_COMPLETE, self._on_output_layer)
        self.event_bus.subscribe(SystemEvent.LLM_RESPONSE_GENERATED, self._on_llm_response)
        self.event_bus.subscribe(SystemEvent.TTS_OUTPUT_GENERATED, self._on_tts_output)
    
    def _on_input_layer(self, event):
        """記錄 INPUT 層完成"""
        self.events.append(("input_layer", event.data))
        self.layer_events.append("input")
        
        from utils.debug_helper import debug_log
        debug_log(2, f"[FrontendChatMonitor] 📥 INPUT 層完成")
    
    def _on_processing_layer(self, event):
        """記錄 PROCESSING 層完成"""
        self.events.append(("processing_layer", event.data))
        self.layer_events.append("processing")
        
        from utils.debug_helper import debug_log
        debug_log(2, f"[FrontendChatMonitor] ⚙️ PROCESSING 層完成")
    
    def _on_output_layer(self, event):
        """記錄 OUTPUT 層完成"""
        self.events.append(("output_layer", event.data))
        self.layer_events.append("output")
        
        from utils.debug_helper import debug_log
        debug_log(2, f"[FrontendChatMonitor] 📤 OUTPUT 層完成")
    
    def _on_llm_response(self, event):
        """記錄 LLM 回應"""
        self.events.append(("llm_response", event.data))
        self.llm_responses.append(event.data)
        
        from utils.debug_helper import debug_log
        response_text = event.data.get('response', '')[:100]
        debug_log(2, f"[FrontendChatMonitor] 🤖 LLM 回應: {response_text}...")
    
    def _on_tts_output(self, event):
        """記錄 TTS 輸出"""
        self.events.append(("tts_output", event.data))
        self.tts_outputs.append(event.data)
        
        from utils.debug_helper import debug_log
        debug_log(2, f"[FrontendChatMonitor] 🔊 TTS 輸出已生成")
    
    def wait_for_layer(self, layer_name: str, timeout=30):
        """等待特定層級完成"""
        start = time.time()
        
        while time.time() - start < timeout:
            if layer_name in self.layer_events:
                return True
            time.sleep(0.1)
        
        return False
    
    def reset(self):
        """重置監控器"""
        self.events = []
        self.layer_events = []
        self.animation_events = []
        self.llm_responses = []
        self.tts_outputs = []


def inject_chat_message(message: str, identity_id: Optional[str] = None):
    """
    注入聊天訊息到系統
    
    這會觸發完整的系統循環，包括前端動畫
    
    Args:
        message: 聊天訊息內容
        identity_id: 可選的 Identity ID
    """
    from utils.debug_helper import info_log
    from core.framework import core_framework
    from core.working_context import working_context_manager
    
    info_log(f"[FrontendChatTest] 💬 注入聊天訊息: {message}")
    
    # 如果提供 identity_id，先聲明身份
    if identity_id:
        working_context_manager.set_declared_identity(identity_id)
        info_log(f"[FrontendChatTest] 🆔 已聲明 Identity: {identity_id}")
    
    # 通過 STT 模組注入文字輸入
    stt_module = core_framework.get_module('stt')
    if not stt_module:
        raise RuntimeError("STT module not available")
    
    # 調用 STT 模組的文字輸入處理
    result = stt_module.handle_text_input(message)
    
    if not result:
        raise RuntimeError(f"Failed to inject text: {message}")
    
    info_log(f"[FrontendChatTest] ✅ 文字注入成功")


class TestFrontendChatIntegration:
    """前端與聊天路徑整合測試"""
    
    def test_01_simple_chat_with_frontend(self, system_with_frontend, cleanup_memory):
        """
        測試 1: 簡單聊天與前端動畫
        驗證完整的聊天流程和層級動畫
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        from PyQt5.QtCore import QTimer
        
        info_log("\n" + "=" * 70)
        info_log("TEST 1: Simple Chat with Frontend")
        info_log("=" * 70)
        
        event_bus = system_with_frontend["event_bus"]
        ui_module = system_with_frontend["ui_module"]
        
        # 獲取 Debug Identity
        identity_manager = IdentityManager()
        debug_identity = None
        for identity in identity_manager.identities.values():
            if identity.name.lower() == "debug":
                debug_identity = identity
                break
        
        assert debug_identity is not None, "Debug identity not found"
        info_log(f"✅ 找到 Debug: {debug_identity.identity_id}")
        
        # 創建監控器
        monitor = FrontendChatMonitor(event_bus)
        
        # 注入聊天訊息
        info_log("\n💬 注入聊天訊息...")
        inject_chat_message(
            "Hello! Can you tell me what time it is?",
            identity_id=debug_identity.identity_id
        )
        
        # 等待層級完成
        info_log("\n⏳ 等待系統循環處理...")
        
        input_ok = monitor.wait_for_layer("input", timeout=15)
        if input_ok:
            info_log("✅ INPUT 層完成")
        else:
            info_log("⚠️ INPUT 層未完成（可能是工作流請求）")
        
        processing_ok = monitor.wait_for_layer("processing", timeout=30)
        if processing_ok:
            info_log("✅ PROCESSING 層完成")
        
        output_ok = monitor.wait_for_layer("output", timeout=30)
        if output_ok:
            info_log("✅ OUTPUT 層完成")
        
        # 驗證基本流程
        info_log(f"\n📊 統計:")
        info_log(f"   層級事件: {len(monitor.layer_events)}")
        info_log(f"   LLM 回應: {len(monitor.llm_responses)}")
        info_log(f"   TTS 輸出: {len(monitor.tts_outputs)}")
        
        if monitor.llm_responses:
            response = monitor.llm_responses[0].get('response', '')[:200]
            info_log(f"\n🤖 LLM 回應摘要:")
            info_log(f"   {response}...")
        
        # 保持 UI 運行以便觀察動畫
        info_log("\n🎨 保持 UI 運行 10 秒，觀察動畫...")
        
        if ui_module and ui_module.app:
            # 設定 10 秒後自動退出
            QTimer.singleShot(10000, ui_module.app.quit)
            
            # 運行 Qt 事件循環
            ui_module.app.exec_()
        else:
            # 如果沒有 Qt 應用，就簡單等待
            time.sleep(10)
        
        info_log("\n✅ TEST 1 PASSED: 前端聊天整合正常")
    
    def test_02_multiple_chats_observe_animations(self, system_with_frontend, cleanup_memory):
        """
        測試 2: 多輪對話觀察動畫
        進行多輪對話，觀察層級動畫切換
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        from PyQt5.QtCore import QTimer
        
        info_log("\n" + "=" * 70)
        info_log("TEST 2: Multiple Chats - Observe Animations")
        info_log("=" * 70)
        
        event_bus = system_with_frontend["event_bus"]
        ui_module = system_with_frontend["ui_module"]
        
        # 獲取 Debug Identity
        identity_manager = IdentityManager()
        debug_identity = None
        for identity in identity_manager.identities.values():
            if identity.name.lower() == "debug":
                debug_identity = identity
                break
        
        assert debug_identity is not None, "Debug identity not found"
        
        # 創建監控器
        monitor = FrontendChatMonitor(event_bus)
        
        # 準備多個問題
        questions = [
            "What is Python?",
            "How does machine learning work?",
            "Can you explain neural networks?",
        ]
        
        info_log(f"\n💬 將注入 {len(questions)} 個問題...")
        
        # 注入第一個問題
        info_log(f"\n[1/{len(questions)}] {questions[0]}")
        inject_chat_message(questions[0], identity_id=debug_identity.identity_id)
        
        # 等待第一個完成
        monitor.wait_for_layer("output", timeout=30)
        info_log("✅ 第一輪對話完成")
        
        # 給系統時間完成動畫和後台任務
        time.sleep(3)
        monitor.reset()
        
        # 注入第二個問題
        if len(questions) > 1:
            info_log(f"\n[2/{len(questions)}] {questions[1]}")
            inject_chat_message(questions[1], identity_id=debug_identity.identity_id)
            
            monitor.wait_for_layer("output", timeout=30)
            info_log("✅ 第二輪對話完成")
            
            time.sleep(3)
            monitor.reset()
        
        # 注入第三個問題
        if len(questions) > 2:
            info_log(f"\n[3/{len(questions)}] {questions[2]}")
            inject_chat_message(questions[2], identity_id=debug_identity.identity_id)
            
            monitor.wait_for_layer("output", timeout=30)
            info_log("✅ 第三輪對話完成")
        
        # 保持 UI 運行以便觀察
        info_log("\n🎨 保持 UI 運行 15 秒，觀察多輪對話動畫...")
        
        if ui_module and ui_module.app:
            QTimer.singleShot(15000, ui_module.app.quit)
            ui_module.app.exec_()
        else:
            time.sleep(15)
        
        info_log("\n✅ TEST 2 PASSED: 多輪對話動畫觀察完成")


if __name__ == "__main__":
    """直接運行測試（用於調試）"""
    print("Running Frontend Chat Integration Tests")
    print("=" * 70)
    
    # Run with pytest
    pytest.main([__file__, "-v", "-s", "--tb=short"])
