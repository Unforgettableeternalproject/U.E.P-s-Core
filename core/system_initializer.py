# core/system_initializer.py
"""
系統初始化器 - 管理 UEP 系統的啟動過程

這個模組負責：
1. Controller 啟動 Framework 初始化
2. 模組自動發現和註冊  
3. Router 初始化（做第一次啟動準備）
4. State Manager 和 Session Manager 初始化
5. Working Context 清理和準備
6. 系統健康檢查

啟動層級順序：
Controller → Framework → Router → State/Session Managers → Working Context
"""

import time
from typing import Dict, Any, List, Optional
from enum import Enum, auto

from utils.debug_helper import debug_log, info_log, error_log


class InitializationPhase(Enum):
    """初始化階段"""
    STARTING = auto()           # 開始啟動
    CONTROLLER_INIT = auto()    # Controller 初始化
    FRAMEWORK_INIT = auto()     # Framework 和模組初始化
    ROUTER_INIT = auto()        # Router 初始化
    MANAGERS_INIT = auto()      # State/Session Managers 初始化
    CONTEXT_SETUP = auto()      # Working Context 設置
    HEALTH_CHECK = auto()       # 健康檢查
    READY = auto()              # 系統就緒
    FAILED = auto()             # 初始化失敗


class SystemInitializer:
    """系統初始化器 - 協調整個系統啟動過程"""
    
    def __init__(self):
        """初始化系統初始化器"""
        self.phase = InitializationPhase.STARTING
        self.startup_time = 0
        self.initialized_modules = []
        self.failed_modules = []
        
        # 載入配置
        from configs.config_loader import load_config
        self.config = load_config()
        
        info_log("[SystemInitializer] 系統初始化器已創建")
    
    def initialize_system(self, production_mode: bool = False) -> bool:
        """
        初始化整個 UEP 系統
        
        遵循新的系統架構：
        Controller → Framework → Router → State/Session Managers → Working Context
        
        Args:
            production_mode: 是否為生產模式
            
        Returns:
            bool: 初始化是否成功
        """
        info_log("🚀 開始初始化 UEP 系統...")
        self.startup_time = time.time()
        debug_log(4, "[SystemInitializer] initialize_system 進入 (production_mode=%s)" % production_mode)
        debug_log(4, f"[SystemInitializer] 目前配置 debug.enable_frontend={self.config.get('debug', {}).get('enable_frontend', False)}")
        
        try:
            # Phase 1: Controller 初始化
            _t_controller = time.time()
            if not self._initialize_controller():
                return False
            debug_log(4, f"[SystemInitializer] Controller 初始化耗時 {time.time() - _t_controller:.3f}s")
                
            # Phase 2: Framework 和模組初始化
            _t_framework = time.time()
            if not self._initialize_framework():
                return False
            debug_log(4, f"[SystemInitializer] Framework 初始化耗時 {time.time() - _t_framework:.3f}s")
                
            # Phase 3: Router 初始化
            _t_router = time.time()
            if not self._initialize_router():
                return False
            debug_log(4, f"[SystemInitializer] Router 初始化耗時 {time.time() - _t_router:.3f}s")
                
            # Phase 4: State/Session Managers 初始化
            _t_managers = time.time()
            if not self._initialize_managers():
                return False
            debug_log(4, f"[SystemInitializer] Managers 初始化耗時 {time.time() - _t_managers:.3f}s")
                
            # Phase 5: Working Context 設置
            _t_context = time.time()
            if not self._setup_working_context():
                return False
            debug_log(4, f"[SystemInitializer] Working Context 設置耗時 {time.time() - _t_context:.3f}s")
            
            # Phase 5.5: 設置默認測試 Identity（臨時測試用）
            _t_identity = time.time()
            if not self._setup_default_identity():
                return False
            debug_log(4, f"[SystemInitializer] 默認 Identity 設置耗時 {time.time() - _t_identity:.3f}s")
            
            # Phase 6: 前端初始化（可選）
            _t_frontend = time.time()
            if not self._initialize_frontend():
                # 前端初始化失敗不阻止系統啟動（前端是可選的）
                info_log("⚠️  前端初始化失敗或未啟用，繼續啟動核心系統")
            debug_log(4, f"[SystemInitializer] 前端初始化階段耗時 {time.time() - _t_frontend:.3f}s")
                
            # Phase 7: 系統健康檢查
            _t_health = time.time()
            if not self._health_check():
                return False
            debug_log(4, f"[SystemInitializer] 健康檢查耗時 {time.time() - _t_health:.3f}s")
                
            # 完成初始化
            self.phase = InitializationPhase.READY
            elapsed = time.time() - self.startup_time
            info_log(f"✅ UEP 系統初始化完成！耗時: {elapsed:.2f}秒")
            info_log(f"📊 系統架構: Controller → Framework → Router → Managers → Context")
            debug_log(4, f"[SystemInitializer] 全部初始化結束 (總耗時={elapsed:.3f}s, 模組數={len(self.initialized_modules)})")
            
            return True
            
        except Exception as e:
            self.phase = InitializationPhase.FAILED
            error_log(f"❌ 系統初始化失敗: {e}")
            debug_log(1, "[SystemInitializer] initialize_system 捕獲異常，進入 FAILED 狀態")
            return False
    
    def _initialize_controller(self) -> bool:
        """初始化 Controller"""
        try:
            debug_log(4, "[SystemInitializer] _initialize_controller 開始")
            self.phase = InitializationPhase.CONTROLLER_INIT
            info_log("🎮 初始化 Controller...")
            
            # 導入並初始化 Controller
            from core.controller import unified_controller
            
            # Controller 會自動初始化，這裡驗證其狀態
            if hasattr(unified_controller, 'is_initialized') and unified_controller.is_initialized:
                info_log("   ✅ Controller 已初始化")
            else:
                # 如果 Controller 需要明確初始化，調用其方法
                if hasattr(unified_controller, 'initialize'):
                    success = unified_controller.initialize()
                    if not success:
                        error_log("   ❌ Controller 初始化失敗")
                        return False
                info_log("   ✅ Controller 初始化完成")
            
            return True
            
        except Exception as e:
            error_log(f"❌ Controller 初始化失敗: {e}")
            debug_log(1, "[SystemInitializer] _initialize_controller 發生異常")
            return False
    
    def _initialize_framework(self) -> bool:
        """初始化 Framework 和模組"""
        try:
            debug_log(4, "[SystemInitializer] _initialize_framework 開始")
            self.phase = InitializationPhase.FRAMEWORK_INIT
            info_log("🏗️ 初始化 Framework 和模組...")
            
            # 導入並初始化 Framework
            from core.framework import core_framework
            
            # Framework 初始化（包含模組自動發現和註冊）
            success = core_framework.initialize()
            if not success:
                error_log("   ❌ Framework 初始化失敗")
                return False
            
            info_log("   ✅ Framework 初始化完成")
            
            # 獲取已註冊的模組列表
            registered_modules = list(core_framework.modules.keys())
            info_log(f"   📦 已註冊模組: {registered_modules}")
            self.initialized_modules = registered_modules
            
            # 啟用效能監控
            core_framework.enable_performance_monitoring(True)
            info_log("   📊 效能監控已啟用")
            
            # 🔗 建立模組間連接（在所有模組初始化後）
            if not self._setup_module_connections():
                error_log("   ⚠️  模組間連接設置失敗（非致命）")
            
            # 🔄 註冊模組的設定重載回調
            if not self._register_settings_callbacks():
                error_log("   ⚠️  設定重載回調註冊失敗（非致命）")
            
            return True
            
        except Exception as e:
            error_log(f"❌ Framework 初始化失敗: {e}")
            debug_log(1, "[SystemInitializer] _initialize_framework 發生異常")
            return False
    
    def _setup_module_connections(self) -> bool:
        """設置模組間的連接（例如 LLM-SYS MCP 連接）"""
        try:
            debug_log(4, "[SystemInitializer] _setup_module_connections 開始")
            info_log("   🔗 設置模組間連接...")
            
            # 1. 連接 LLM 和 SYS 的 MCP Server
            from core.registry import get_module
            
            llm_module = get_module("llm_module")
            sys_module = get_module("sys_module")
            
            if llm_module and sys_module:
                # 檢查 SYS 模組是否有 MCP Server
                if hasattr(sys_module, 'mcp_server'):
                    # 將 MCP Server 傳遞給 LLM 模組
                    if hasattr(llm_module, 'set_mcp_server'):
                        llm_module.set_mcp_server(sys_module.mcp_server)
                        info_log("   ✅ LLM-SYS MCP 連接已建立")
                    else:
                        debug_log(2, "   ⚠️  LLM 模組沒有 set_mcp_server 方法")
                else:
                    debug_log(2, "   ⚠️  SYS 模組沒有 mcp_server 屬性")
            else:
                debug_log(2, f"   ⚠️  模組不可用 - LLM: {llm_module is not None}, SYS: {sys_module is not None}")
            
            # 未來可以在這裡添加其他模組間連接
            
            return True
            
        except Exception as e:
            error_log(f"   ❌ 模組間連接設置失敗: {e}")
            return False
    
    def _register_settings_callbacks(self) -> bool:
        """註冊各模組的使用者設定重載回調"""
        try:
            debug_log(4, "[SystemInitializer] _register_settings_callbacks 開始")
            info_log("   🔄 註冊使用者設定重載回調...")
            t_start = time.time()
            
            from configs.user_settings_manager import user_settings_manager
            from core.registry import get_module
            
            # 檢查前端是否啟用（用於跳過 UI/MOV 模組）
            enable_frontend = self.config.get('debug', {}).get('enable_frontend', False)
            debug_log(4, f"      [回調] 前端啟用標誌 enable_frontend={enable_frontend}")
            
            # 註冊 STT 模組的回調
            segment_t = time.time()
            debug_log(4, f"      [回調] 準備載入 stt_module (elapsed={time.time()-t_start:.3f}s)")
            stt_module = get_module("stt_module")
            if stt_module and hasattr(stt_module, '_reload_from_user_settings'):
                user_settings_manager.register_reload_callback("stt_module", stt_module._reload_from_user_settings)
                debug_log(2, "      ✅ STT 模組回調已註冊")
            debug_log(4, f"      [回調] stt_module 完成 (segment={time.time()-segment_t:.3f}s, total={time.time()-t_start:.3f}s)")
            
            # 註冊 TTS 模組的回調
            segment_t = time.time()
            debug_log(4, f"      [回調] 準備載入 tts_module (elapsed={time.time()-t_start:.3f}s)")
            tts_module = get_module("tts_module")
            if tts_module and hasattr(tts_module, '_reload_from_user_settings'):
                user_settings_manager.register_reload_callback("tts_module", tts_module._reload_from_user_settings)
                debug_log(2, "      ✅ TTS 模組回調已註冊")
            debug_log(4, f"      [回調] tts_module 完成 (segment={time.time()-segment_t:.3f}s, total={time.time()-t_start:.3f}s)")
            
            # 註冊 NLP 模組的回調
            segment_t = time.time()
            debug_log(4, f"      [回調] 準備載入 nlp_module (elapsed={time.time()-t_start:.3f}s)")
            nlp_module = get_module("nlp_module")
            if nlp_module and hasattr(nlp_module, '_reload_from_user_settings'):
                user_settings_manager.register_reload_callback("nlp_module", nlp_module._reload_from_user_settings)
                debug_log(2, "      ✅ NLP 模組回調已註冊")
            debug_log(4, f"      [回調] nlp_module 完成 (segment={time.time()-segment_t:.3f}s, total={time.time()-t_start:.3f}s)")
            
            # 註冊 LLM 模組的回調
            segment_t = time.time()
            debug_log(4, f"      [回調] 準備載入 llm_module (elapsed={time.time()-t_start:.3f}s)")
            llm_module = get_module("llm_module")
            if llm_module and hasattr(llm_module, '_reload_from_user_settings'):
                user_settings_manager.register_reload_callback("llm_module", llm_module._reload_from_user_settings)
                debug_log(2, "      ✅ LLM 模組回調已註冊")
            debug_log(4, f"      [回調] llm_module 完成 (segment={time.time()-segment_t:.3f}s, total={time.time()-t_start:.3f}s)")
            
            # 註冊 MOV 模組的回調 (只在前端啟用時)
            # 注意：MOV 模組的回調註冊會在 UI 模組初始化 MOV 後手動執行
            # 因為 MOV 需要 QApplication 已經建立才能正確初始化
            if enable_frontend:
                debug_log(4, f"      [回調] 跳過 mov_module (將在 UI 模組初始化後註冊)")
            else:
                debug_log(4, f"      [回調] 跳過 mov_module (前端未啟用)")
            
            # 註冊 UI 模組的回調 (只在前端啟用時)
            enable_frontend = self.config.get('debug', {}).get('enable_frontend', False)
            if enable_frontend:
                segment_t = time.time()
                debug_log(4, f"      [回調] 準備載入 ui_module (elapsed={time.time()-t_start:.3f}s)")
                ui_module = get_module("ui_module")
                if ui_module and hasattr(ui_module, '_reload_from_user_settings'):
                    user_settings_manager.register_reload_callback("ui_module", ui_module._reload_from_user_settings)
                    debug_log(2, "      ✅ UI 模組回調已註冊")
                debug_log(4, f"      [回調] ui_module 完成 (segment={time.time()-segment_t:.3f}s, total={time.time()-t_start:.3f}s)")
            else:
                debug_log(4, f"      [回調] 跳過 ui_module (前端未啟用)")
            
            # 註冊 MEM 模組的回調
            segment_t = time.time()
            debug_log(4, f"      [回調] 準備載入 mem_module (elapsed={time.time()-t_start:.3f}s)")
            mem_module = get_module("mem_module")
            if mem_module and hasattr(mem_module, '_reload_from_user_settings'):
                user_settings_manager.register_reload_callback("mem_module", mem_module._reload_from_user_settings)
                debug_log(2, "      ✅ MEM 模組回調已註冊")
            debug_log(4, f"      [回調] mem_module 完成 (segment={time.time()-segment_t:.3f}s, total={time.time()-t_start:.3f}s)")
            
            # 註冊 SYS 模組的回調
            segment_t = time.time()
            debug_log(4, f"      [回調] 準備載入 sys_module (elapsed={time.time()-t_start:.3f}s)")
            sys_module = get_module("sys_module")
            if sys_module and hasattr(sys_module, '_reload_from_user_settings'):
                user_settings_manager.register_reload_callback("sys_module", sys_module._reload_from_user_settings)
                debug_log(2, "      ✅ SYS 模組回調已註冊")
            debug_log(4, f"      [回調] sys_module 完成 (segment={time.time()-segment_t:.3f}s, total={time.time()-t_start:.3f}s)")
            
            # 註冊 SystemLoop 的回調
            segment_t = time.time()
            debug_log(4, f"      [回調] 準備載入 system_loop (elapsed={time.time()-t_start:.3f}s)")
            from core.system_loop import system_loop
            if system_loop and hasattr(system_loop, '_reload_from_user_settings'):
                user_settings_manager.register_reload_callback("system_loop", system_loop._reload_from_user_settings)
                debug_log(2, "      ✅ SystemLoop 回調已註冊")
            debug_log(4, f"      [回調] system_loop 完成 (segment={time.time()-segment_t:.3f}s, total={time.time()-t_start:.3f}s)")
            
            info_log("   ✅ 使用者設定重載回調註冊完成")
            debug_log(4, f"[SystemInitializer] _register_settings_callbacks 完成 (總耗時={time.time()-t_start:.3f}s)")
            debug_log(4, "[SystemInitializer] _register_settings_callbacks 結束")
            return True
            
        except Exception as e:
            error_log(f"   ❌ 設定重載回調註冊失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _initialize_router(self) -> bool:
        """初始化 Router"""
        try:
            debug_log(4, "[SystemInitializer] _initialize_router 開始")
            self.phase = InitializationPhase.ROUTER_INIT
            info_log("🔀 初始化 Router...")
            
            # 導入 Router - Router 是無狀態的,導入即可用
            from core.router import router
            
            info_log("   ✅ Router 已載入,等待文字路由請求")
            
            return True
            
        except Exception as e:
            error_log(f"❌ Router 初始化失敗: {e}")
            debug_log(1, "[SystemInitializer] _initialize_router 發生異常")
            return False
    
    def _initialize_managers(self) -> bool:
        """初始化 State Manager 和 Session Manager"""
        try:
            debug_log(4, "[SystemInitializer] _initialize_managers 開始")
            self.phase = InitializationPhase.MANAGERS_INIT
            info_log("⚙️ 初始化 State 和 Session Managers...")
            
            # 初始化 State Manager
            from core.states.state_manager import state_manager, UEPState
            from core.states.state_queue import get_state_queue_manager
            
            # 重置系統狀態到 IDLE
            state_manager.set_state(UEPState.IDLE)
            info_log("   🔄 系統狀態設為 IDLE")
            
            # 清空狀態佇列
            state_queue_manager = get_state_queue_manager()
            if hasattr(state_queue_manager, 'clear_queue'):
                state_queue_manager.clear_queue()
                info_log("   🧹 狀態佇列已清空")
            
            # 初始化 Session Manager
            from core.sessions.session_manager import unified_session_manager
            
            # Session Manager 會自動初始化，驗證其狀態
            if hasattr(unified_session_manager, 'cleanup_expired_sessions'):
                unified_session_manager.cleanup_expired_sessions()
                info_log("   🧹 已清理過期會話")
            
            info_log("   ✅ State 和 Session Managers 已就緒")
            
            return True
            
        except Exception as e:
            error_log(f"❌ Managers 初始化失敗: {e}")
            debug_log(1, "[SystemInitializer] _initialize_managers 發生異常")
            return False
    
    def _setup_working_context(self) -> bool:
        """設置 Working Context"""
        try:
            debug_log(4, "[SystemInitializer] _setup_working_context 開始")
            self.phase = InitializationPhase.CONTEXT_SETUP
            info_log("🔗 設置 Working Context...")
            
            # 導入 Working Context Manager
            from core.working_context import working_context_manager
            
            # 清理過期的上下文
            if hasattr(working_context_manager, 'cleanup_expired_contexts'):
                cleaned = working_context_manager.cleanup_expired_contexts()
                if cleaned > 0:
                    info_log(f"   🧹 清理了 {cleaned} 個過期上下文")
            
            # 確認決策處理器已註冊
            if hasattr(working_context_manager, 'decision_handlers'):
                handler_count = len(working_context_manager.decision_handlers)
                info_log(f"   🎯 已註冊 {handler_count} 個決策處理器")
            
            info_log("   ✅ Working Context 已設置")
            
            # 初始化監控系統
            if not self._initialize_monitoring_system():
                # 監控系統初始化失敗不阻止系統啟動
                info_log("   ⚠️  監控系統初始化失敗，繼續啟動")
            
            return True
            
        except Exception as e:
            error_log(f"❌ Working Context 設置失敗: {e}")
            debug_log(1, "[SystemInitializer] _setup_working_context 發生異常")
            return False
    
    def _initialize_monitoring_system(self) -> bool:
        """初始化監控系統（待辦事項和行事曆）"""
        try:
            debug_log(4, "[SystemInitializer] _initialize_monitoring_system 開始")
            info_log("   📊 初始化監控系統...")
            
            # 導入監控接口初始化函數
            from modules.sys_module.actions.monitoring_interface import initialize_monitoring_system
            
            # 初始化監控系統 - 會發送 SYSTEM_STARTUP 事件
            initialize_monitoring_system()
            
            info_log("   ✅ 監控系統已初始化，SYSTEM_STARTUP 事件已發送")
            return True
            
        except Exception as e:
            error_log(f"   ❌ 監控系統初始化失敗: {e}")
            return False
    
    def _initialize_frontend(self) -> bool:
        """初始化前端模組（UI, ANI, MOV）與 FrontendBridge 事件路由
        
        根據配置決定是否啟動前端，前端初始化失敗不影響核心系統運行
        
        注意：UIModule 會自動初始化 ANI 和 MOV 模組，所以這裡只需：
        1. 確保 UI 已初始化（Framework 已提前載入）
        2. 初始化 FrontendBridge 來訂閱事件並轉發給前端模組
        """
        try:
            debug_log(4, "[SystemInitializer] _initialize_frontend 開始")
            # 檢查配置是否啟用前端
            enable_frontend = self.config.get('debug', {}).get('enable_frontend', False)
            debug_log(4, f"[SystemInitializer] 前端啟用標誌 enable_frontend={enable_frontend}")
            
            if not enable_frontend:
                info_log("📺 前端未啟用（debug.enable_frontend=false），跳過前端初始化")
                return True  # 返回 True 表示沒有錯誤（只是未啟用）
            
            info_log("📺 初始化前端模組（UI, ANI, MOV）與事件橋接...")
            
            # 1️⃣ 獲取 Framework（UI 模組已由 Framework 負責初始化）
            from core.framework import core_framework
            
            # 2️⃣ 初始化 FrontendBridge 以訂閱系統事件並轉發給前端模組
            from core.frontend_bridge import FrontendBridge
            
            frontend_bridge = FrontendBridge()
            
            # 使用完整模式（不是協調器模式），以便整合 StatusManager
            if not frontend_bridge.initialize(coordinator_only=False):
                error_log("   ❌ FrontendBridge 初始化失敗")
                return False
            
            # 將 FrontendBridge 註冊到 core_framework 以供其他組件訪問
            core_framework.frontend_bridge = frontend_bridge
            info_log("   ✅ FrontendBridge 已初始化並註冊到 Framework")
            
            # 3️⃣ 獲取 UI 模組並顯示介面
            ui = core_framework.get_module("ui")
            if ui and hasattr(ui, 'handle_frontend_request'):
                # 顯示 UEP 主程式
                show_result = ui.handle_frontend_request({
                    'command': 'show_interface',
                    'interface': 'main_desktop_pet'
                })
                if show_result.get('success'):
                    info_log("   🎈 UEP 主程式已顯示")
                else:
                    error_log(f"   ⚠️  顯示主程式失敗: {show_result.get('error', '未知錯誤')}")
                    # 不返回 False，因為前端已啟動，只是視窗顯示失敗
                
                # 顯示 access_widget
                access_widget_result = ui.handle_frontend_request({
                    'command': 'show_interface',
                    'interface': 'user_access_widget'
                })
                if access_widget_result.get('success'):
                    info_log("   🎛️  Access Widget 已顯示")
                else:
                    error_log(f"   ⚠️  顯示 Access Widget 失敗: {access_widget_result.get('error', '未知錯誤')}")
            else:
                debug_log(4, "[SystemInitializer] ui_module 不可用或缺少 handle_frontend_request，跳過介面顯示")
            
            info_log("   ✅ 前端系統已就緒（含事件橋接）")
            return True
            
        except Exception as e:
            error_log(f"❌ 前端初始化失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _setup_default_identity(self) -> bool:
        """設置默認測試 Identity（臨時測試階段使用）
        
        ⚠️ 這是臨時測試功能，用於在沒有正式身分指定機制前進行測試
        正式版本應該移除此功能，讓使用者通過語音或其他方式指定身分
        """
        try:
            debug_log(4, "[SystemInitializer] _setup_default_identity 開始")
            info_log("👤 設置默認測試 Identity (Bernie)...")
            
            # 導入必要的模組
            from core.framework import core_framework
            from core.working_context import working_context_manager
            from core.status_manager import status_manager
            
            # 獲取 NLP 模組（包含 IdentityManager）
            nlp_module = core_framework.get_module('nlp')
            if not nlp_module or not hasattr(nlp_module, 'identity_manager'):
                error_log("   ❌ NLP 模組或 IdentityManager 不可用")
                return False
            
            identity_manager = nlp_module.identity_manager
            
            # 創建或獲取 Bernie Identity
            identity = identity_manager.get_or_create_identity(
                speaker_id="test_bernie_speaker",
                display_name="Bernie"
            )
            
            if not identity:
                error_log("   ❌ 無法創建或獲取 Bernie Identity")
                return False
            
            info_log(f"   ✅ Identity 已就緒: {identity.identity_id} ({identity.display_name})")
            
            # 設置到 Working Context 全局數據
            working_context_manager.global_context_data['declared_identity'] = True
            working_context_manager.global_context_data['current_identity_id'] = identity.identity_id
            working_context_manager.global_context_data['current_identity'] = {
                'identity_id': identity.identity_id,
                'display_name': identity.display_name,
                'speaker_id': identity.speaker_id
            }
            info_log(f"   📝 已設置到 Working Context")
            
            # 切換 StatusManager 到此 Identity
            status_manager.switch_identity(identity.identity_id)
            info_log(f"   🔄 StatusManager 已切換到 Identity: {identity.identity_id}")
            
            # 記錄測試配置
            info_log("   ⚠️  注意：這是臨時測試配置")
            info_log("   📊 現在所有語音樣本都會累積到 Bernie 的 Identity")
            info_log("   🎯 可以測試：語音樣本添加、記憶路徑、完整系統循環")
            
            return True
            
        except Exception as e:
            error_log(f"❌ 設置默認 Identity 失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _health_check(self) -> bool:
        """系統健康檢查"""
        try:
            debug_log(4, "[SystemInitializer] _health_check 開始")
            self.phase = InitializationPhase.HEALTH_CHECK
            info_log("🏥 執行系統健康檢查...")
            
            # 檢查 Framework 狀態
            from core.framework import core_framework
            if not core_framework.is_initialized:
                error_log("   ❌ Framework 未正確初始化")
                return False
            
            # 檢查狀態管理器
            from core.states.state_manager import state_manager, UEPState
            current_state = state_manager.get_current_state()
            if current_state != UEPState.IDLE:
                error_log(f"   ❌ 系統狀態不正確: {current_state}")
                return False
            
            # 檢查已註冊的模組數量
            module_count = len(core_framework.modules)
            if module_count == 0:
                error_log("   ❌ 沒有已註冊的模組")
                return False
            
            info_log(f"   ✅ 健康檢查通過: {module_count} 個模組已註冊")
            info_log(f"   ✅ 系統狀態: {current_state.value}")
            
            return True
            
        except Exception as e:
            error_log(f"❌ 系統健康檢查失敗: {e}")
            debug_log(1, "[SystemInitializer] _health_check 發生異常")
            return False
    
    def get_initialization_status(self) -> Dict[str, Any]:
        """獲取初始化狀態"""
        return {
            "phase": self.phase,
            "initialized_modules": self.initialized_modules,
            "failed_modules": self.failed_modules,
            "startup_time": self.startup_time,
            "is_ready": self.phase == InitializationPhase.READY
        }


# 全局系統初始化器實例
system_initializer = SystemInitializer()