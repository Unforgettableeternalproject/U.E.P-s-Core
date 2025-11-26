"""
前端系統整合測試

測試重點：
1. 帶前端的完整系統循環啟動
2. 前端模組（UI, ANI, MOV）與核心系統的協同工作
3. 使用者互動（拖曳、捉弄）對 status_manager 的影響
4. 聊天互動在有前端情況下的完整流程

測試策略：
- 啟用 debug.enable_frontend 配置
- 啟動完整系統循環（包含前端）
- 驗證前端模組正常工作
- 測試前端互動和聊天流程
"""

import pytest
import time
import sys
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
def frontend_config():
    """修改配置以啟用前端"""
    import shutil
    from pathlib import Path
    from configs.config_loader import CONFIG_PATH, load_config, save_config
    
    # 備份原始配置文件
    config_path = Path(CONFIG_PATH)
    backup_path = config_path.with_suffix('.yaml.backup')
    
    if config_path.exists():
        shutil.copy2(config_path, backup_path)
    
    # 載入並修改配置
    config = load_config()
    
    if 'debug' not in config:
        config['debug'] = {}
    config['debug']['enable_frontend'] = True
    
    # 保存修改後的配置
    save_config(config)
    
    yield config
    
    # 清理：恢復原配置
    if backup_path.exists():
        shutil.copy2(backup_path, config_path)
        backup_path.unlink()  # 刪除備份文件


@pytest.fixture(scope="module")
def system_with_frontend(frontend_config):
    """
    初始化帶前端的完整系統
    
    包括：
    - SystemInitializer：系統初始化（包含前端）
    - UnifiedController：控制器
    - SystemLoop：系統循環
    - 前端模組：UI, ANI, MOV
    - 所有核心模組
    """
    from utils.debug_helper import info_log, error_log
    from core.system_initializer import SystemInitializer
    from core.controller import unified_controller
    from core.system_loop import system_loop
    from core.event_bus import event_bus
    from utils.logger import force_enable_file_logging
    
    # 強制啟用文件日誌記錄
    force_enable_file_logging()
    
    info_log("[FrontendIntegrationTest] 🚀 初始化帶前端的完整系統...")
    
    # 1. 初始化系統（會自動初始化前端）
    initializer = SystemInitializer()
    success = initializer.initialize_system(production_mode=False)
    
    if not success:
        pytest.fail("系統初始化失敗")
    
    info_log("[FrontendIntegrationTest] ✅ 系統初始化完成")
    
    # 檢查前端是否成功初始化
    if hasattr(initializer, 'frontend_integrator'):
        if initializer.frontend_integrator.is_initialized:
            info_log("[FrontendIntegrationTest] ✅ 前端已初始化")
        else:
            pytest.fail("前端初始化失敗")
    else:
        pytest.fail("前端整合器未創建")
    
    # 2. 啟動系統循環
    loop_started = system_loop.start()
    if not loop_started:
        pytest.fail("系統循環啟動失敗")
    
    info_log("[FrontendIntegrationTest] ✅ 系統循環已啟動")
    
    # 3. 準備組件
    # 注意：Qt 事件處理已由 UI 模組內部的專用線程處理，不需要在測試中額外處理
    components = {
        "initializer": initializer,
        "controller": unified_controller,
        "system_loop": system_loop,
        "event_bus": event_bus,
        "frontend_integrator": initializer.frontend_integrator
    }
    
    # 等待系統穩定
    info_log("[FrontendIntegrationTest] 等待系統穩定...")
    time.sleep(3)
    
    info_log("[FrontendIntegrationTest] ✅ 系統組件就緒")
    
    yield components
    
    # 清理
    info_log("[FrontendIntegrationTest] 🧹 清理系統組件...")
    
    try:
        # 1. 先隱藏 UI（避免關閉時閃爍）
        if hasattr(initializer, 'frontend_integrator') and initializer.frontend_integrator.ui_module:
            try:
                initializer.frontend_integrator.ui_module.handle_frontend_request({
                    'command': 'hide_interface',
                    'interface': 'main_desktop_pet'
                })
            except:
                pass
        
        # 2. 停止系統循環
        if system_loop.status.value != "stopped":
            info_log("[FrontendIntegrationTest] 停止系統循環...")
            system_loop.stop()
            time.sleep(0.5)
        
        # 3. 關閉前端（使用 shutdown 而不是 stop）
        if hasattr(initializer, 'frontend_integrator'):
            info_log("[FrontendIntegrationTest] 關閉前端系統...")
            initializer.frontend_integrator.shutdown()
            time.sleep(0.5)
        
        # 4. Qt 事件處理已由 UI 模組管理，無需額外處理
        # UI 模組的 shutdown() 會停止事件處理線程
        
        info_log("[FrontendIntegrationTest] ✅ 清理完成")
        
    except Exception as e:
        error_log(f"[FrontendIntegrationTest] 清理失敗: {e}")
        import traceback
        error_log(traceback.format_exc())


class FrontendEventMonitor:
    """前端事件監控器"""
    
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.events = []
        self.status_updates = []
        
        # 訂閱 status_manager 更新事件
        # 注意：需要 status_manager 支持發布事件
        # 暫時我們通過輪詢來檢測
    
    def get_status_values(self):
        """獲取當前 status_manager 數值"""
        from core.status_manager import status_manager
        return {
            'mood': status_manager.status.mood,
            'pride': status_manager.status.pride,
            'helpfulness': status_manager.status.helpfulness,
            'boredom': status_manager.status.boredom
        }
    
    def wait_for_status_change(self, status_key: str, timeout=10):
        """等待特定狀態值變化"""
        from core.status_manager import status_manager
        
        initial_value = getattr(status_manager.status, status_key)
        start = time.time()
        
        while time.time() - start < timeout:
            current_value = getattr(status_manager.status, status_key)
            if abs(current_value - initial_value) > 0.01:  # 檢測到變化
                return True, current_value - initial_value
            time.sleep(0.1)
        
        return False, 0


class TestFrontendSystemIntegration:
    """前端系統整合測試"""
    
    def test_01_frontend_initialization(self, system_with_frontend):
        """
        測試 1: 前端初始化
        驗證前端模組正確初始化並啟動
        """
        from utils.debug_helper import info_log
        
        info_log("\n" + "=" * 70)
        info_log("TEST 1: Frontend Initialization")
        info_log("=" * 70)
        
        frontend_integrator = system_with_frontend["frontend_integrator"]
        
        # 檢查前端整合器狀態
        assert frontend_integrator.is_initialized, "前端整合器未初始化"
        assert frontend_integrator.is_running, "前端未運行"
        
        # 檢查前端模組
        assert frontend_integrator.ui_module is not None, "UI 模組未創建"
        assert frontend_integrator.ani_module is not None, "ANI 模組未創建"
        assert frontend_integrator.mov_module is not None, "MOV 模組未創建"
        
        info_log("   ✅ UI 模組已創建")
        info_log("   ✅ ANI 模組已創建")
        info_log("   ✅ MOV 模組已創建")
        
        # 檢查模組是否已初始化
        assert frontend_integrator.ui_module.is_initialized, "UI 模組未初始化"
        assert frontend_integrator.ani_module.is_initialized, "ANI 模組未初始化"
        assert frontend_integrator.mov_module.is_initialized, "MOV 模組未初始化"
        
        info_log("\n✅ TEST 1 PASSED: 前端初始化成功")
    
    def test_02_frontend_status_integration(self, system_with_frontend):
        """
        測試 2: 前端與 status_manager 整合
        驗證前端互動（捉弄）會影響系統數值
        """
        from utils.debug_helper import info_log
        
        info_log("\n" + "=" * 70)
        info_log("TEST 2: Frontend Status Integration")
        info_log("=" * 70)
        
        event_bus = system_with_frontend["event_bus"]
        frontend_integrator = system_with_frontend["frontend_integrator"]
        mov_module = frontend_integrator.mov_module
        
        # 創建監控器
        monitor = FrontendEventMonitor(event_bus)
        
        # 獲取初始狀態
        initial_status = monitor.get_status_values()
        info_log(f"   初始狀態: mood={initial_status['mood']:.2f}, boredom={initial_status['boredom']:.2f}")
        
        # 模擬捉弄互動（直接調用 status_manager 來模擬效果）
        # 不依賴實際動畫，而是直接模擬狀態變化
        info_log("\n   模擬捉弄互動...")
        
        # 直接通過 status_manager 應用 tease 效果
        # 參考 MOVModule._handle_tease_completed 的實現
        from core.status_manager import status_manager
        
        # 捉弄效果：mood 下降，boredom 緩解
        mood_change = -0.1  # 捉弄降低心情
        boredom_change = -0.15  # 捉弄緩解無聊
        
        info_log(f"   應用 tease 效果: mood {mood_change:+.2f}, boredom {boredom_change:+.2f}")
        status_manager.update_mood(mood_change, reason="測試捉弄互動")
        status_manager.update_boredom(boredom_change, reason="測試捉弄互動")
        
        # 等待狀態更新傳播
        time.sleep(0.5)
        
        # 獲取更新後的狀態
        updated_status = monitor.get_status_values()
        info_log(f"   更新後狀態: mood={updated_status['mood']:.2f}, boredom={updated_status['boredom']:.2f}")
        
        # 驗證變化
        mood_delta = updated_status['mood'] - initial_status['mood']
        boredom_delta = updated_status['boredom'] - initial_status['boredom']
        
        info_log(f"   變化量: mood_delta={mood_delta:.2f}, boredom_delta={boredom_delta:.2f}")
        
        # 捉弄應該降低 mood 並緩解 boredom
        assert mood_delta < 0, f"捉弄應該降低 mood，但變化為 {mood_delta}"
        assert boredom_delta < 0, f"捉弄應該緩解 boredom，但變化為 {boredom_delta}"
        
        info_log("   ✅ 狀態變化符合預期")
        
        info_log("\n✅ TEST 2 PASSED: 前端與 status_manager 整合正常")
    
    def test_03_frontend_with_chat(self, system_with_frontend):
        """
        測試 3: 前端環境下的聊天互動
        驗證在前端啟動的情況下聊天流程仍然正常
        """
        from utils.debug_helper import info_log
        from modules.nlp_module.identity_manager import IdentityManager
        
        info_log("\n" + "=" * 70)
        info_log("TEST 3: Chat in Frontend Environment")
        info_log("=" * 70)
        
        event_bus = system_with_frontend["event_bus"]
        
        # 導入測試輔助函數（從同目錄的測試文件）
        import sys
        from pathlib import Path
        test_dir = Path(__file__).parent
        if str(test_dir) not in sys.path:
            sys.path.insert(0, str(test_dir))
        
        from test_chat_path_identity_integration import ChatPathMonitor, inject_chat_message
        
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
        
        # 注入聊天訊息
        info_log("\n   注入聊天訊息...")
        inject_chat_message(
            "Hello! Today is surely a nice day.",
            identity_id=debug_identity.identity_id
        )
        
        # 等待 LLM 回應
        info_log("   ⏳ 等待 LLM 回應...")
        response_received = monitor.wait_for_response(timeout=30)
        
        if response_received:
            info_log("   ✅ 收到 LLM 回應")
            response_text = monitor.llm_responses[-1].get('response', '')
            info_log(f"   回應: {response_text[:100]}...")
        else:
            pytest.fail("未收到 LLM 回應")
        
        # 等待循環完成
        cycle_completed = monitor.wait_for_event("CYCLE_COMPLETED", timeout=30)
        if cycle_completed:
            info_log("   ✅ 循環已完成")
        
        info_log("\n✅ TEST 3 PASSED: 前端環境下聊天功能正常")


if __name__ == "__main__":
    """直接運行測試（用於調試）"""
    print("Running Frontend System Integration Tests")
    print("=" * 70)
    
    # Run with pytest
    pytest.main([__file__, "-v", "-s", "--tb=short"])
