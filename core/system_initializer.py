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
        
        try:
            # Phase 1: Controller 初始化
            if not self._initialize_controller():
                return False
                
            # Phase 2: Framework 和模組初始化
            if not self._initialize_framework():
                return False
                
            # Phase 3: Router 初始化
            if not self._initialize_router():
                return False
                
            # Phase 4: State/Session Managers 初始化
            if not self._initialize_managers():
                return False
                
            # Phase 5: Working Context 設置
            if not self._setup_working_context():
                return False
                
            # Phase 6: 系統健康檢查
            if not self._health_check():
                return False
                
            # 完成初始化
            self.phase = InitializationPhase.READY
            elapsed = time.time() - self.startup_time
            info_log(f"✅ UEP 系統初始化完成！耗時: {elapsed:.2f}秒")
            info_log(f"📊 系統架構: Controller → Framework → Router → Managers → Context")
            
            return True
            
        except Exception as e:
            self.phase = InitializationPhase.FAILED
            error_log(f"❌ 系統初始化失敗: {e}")
            return False
    
    def _initialize_controller(self) -> bool:
        """初始化 Controller"""
        try:
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
            return False
    
    def _initialize_framework(self) -> bool:
        """初始化 Framework 和模組"""
        try:
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
            
            return True
            
        except Exception as e:
            error_log(f"❌ Framework 初始化失敗: {e}")
            return False
    
    def _initialize_router(self) -> bool:
        """初始化 Router"""
        try:
            self.phase = InitializationPhase.ROUTER_INIT
            info_log("🔀 初始化 Router...")
            
            # 導入 Router
            from core.router import router
            
            # Router 會自動初始化，這裡驗證其狀態
            if hasattr(router, 'is_initialized'):
                if not router.is_initialized:
                    # 如果 Router 需要明確初始化
                    if hasattr(router, 'initialize'):
                        success = router.initialize()
                        if not success:
                            error_log("   ❌ Router 初始化失敗")
                            return False
                        
                info_log("   ✅ Router 已就緒，等待用戶輸入")
            else:
                info_log("   ✅ Router 已載入")
            
            return True
            
        except Exception as e:
            error_log(f"❌ Router 初始化失敗: {e}")
            return False
    
    def _initialize_managers(self) -> bool:
        """初始化 State Manager 和 Session Manager"""
        try:
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
            return False
    
    def _setup_working_context(self) -> bool:
        """設置 Working Context"""
        try:
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
            if hasattr(working_context_manager, '_decision_handlers'):
                handler_count = len(working_context_manager._decision_handlers)
                info_log(f"   🎯 已註冊 {handler_count} 個決策處理器")
            
            info_log("   ✅ Working Context 已設置")
            
            return True
            
        except Exception as e:
            error_log(f"❌ Working Context 設置失敗: {e}")
            return False
    
    def _health_check(self) -> bool:
        """系統健康檢查"""
        try:
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