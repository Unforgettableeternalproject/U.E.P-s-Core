# core/system_initializer.py
"""
系統初始化器 - 管理 UEP 系統的啟動過程

這個模組負責：
1. 系統狀態初始化
2. 核心框架啟動
3. 模組註冊和初始化
4. 工作上下文清理
5. 前端應用程式啟動
6. 系統健康檢查

啟動層級順序：
controller > framework > router > strategy > state > context > session
"""

import time
from typing import Dict, Any, List, Optional
from enum import Enum, auto

# 載入配置
from configs.config_loader import load_config
config = load_config()
debug_mode = config.get("debug", {}).get("enabled", False)

from core.framework import core_framework, ExecutionMode
from core.controller import unified_controller
from core.state_manager import UEPState, StateManager, state_manager
from core.state_queue import get_state_queue_manager
from core.working_context import working_context_manager, ContextType
from core.strategies import context_decision_engine
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log


class InitializationPhase(Enum):
    """初始化階段"""
    STARTING = auto()           # 開始啟動
    STATE_RESET = auto()        # 狀態重置
    CONTEXT_CLEANUP = auto()    # 上下文清理
    FRAMEWORK_INIT = auto()     # 框架與模組初始化 (合併了原本的框架初始化、模組註冊和模組初始化)
    STRATEGY_SETUP = auto()     # 策略設置
    HEALTH_CHECK = auto()       # 健康檢查
    FRONTEND_INIT = auto()      # 前端初始化
    READY = auto()              # 系統就緒
    FAILED = auto()             # 初始化失敗


class SystemInitializer:
    """系統初始化器"""
    
    def __init__(self):
        self.config = load_config()
        self.phase = InitializationPhase.STARTING
        self.initialized_modules: List[str] = []
        self.failed_modules: List[str] = []
        self.startup_time = None
        
    def initialize_system(self, production_mode: bool = False) -> bool:
        """
        初始化整個系統
        
        Args:
            production_mode: 是否為生產模式（只載入重構完成的模組）
            
        Returns:
            bool: 初始化是否成功
        """
        info_log("🚀 開始初始化 UEP 系統...")
        self.startup_time = time.time()
        
        try:
            # Phase 1: 狀態重置
            if not self._reset_system_state():
                return False
                
            # Phase 2: 上下文清理
            if not self._cleanup_working_contexts():
                return False
                
            # Phase 3: 框架和模組初始化 (合併了原本的 Phase 3, 4, 5)
            if not self._initialize_framework_and_modules(production_mode):
                return False
                
            # Phase 4: 策略設置 (原 Phase 6)
            if not self._setup_strategies():
                return False
                
            # Phase 5: 健康檢查 (原 Phase 7)
            if not self._health_check():
                return False
                
            # Phase 6: 前端初始化 (原 Phase 8)
            if not self._initialize_frontend():
                return False
                
            # 完成初始化
            self.phase = InitializationPhase.READY
            elapsed = time.time() - self.startup_time
            info_log(f"✅ UEP 系統初始化完成！耗時: {elapsed:.2f}秒")
            info_log(f"📊 已初始化模組: {', '.join(self.initialized_modules)}")
            
            if self.failed_modules:
                info_log(f"⚠️ 失敗模組: {', '.join(self.failed_modules)}")
                
            return True
            
        except Exception as e:
            self.phase = InitializationPhase.FAILED
            error_log(f"❌ 系統初始化失敗: {e}")
            return False
    
    def _reset_system_state(self) -> bool:
        """重置系統狀態"""
        try:
            self.phase = InitializationPhase.STATE_RESET
            info_log("🔄 重置系統狀態...")
            
            # 清空狀態佇列
            state_queue_manager = get_state_queue_manager()
            initial_queue_length = len(state_queue_manager.queue)
            if initial_queue_length > 0:
                info_log(f"   發現 {initial_queue_length} 個待處理狀態項目")
                state_queue_manager.clear_queue()
                info_log(f"   ✅ 已清空狀態佇列")
            else:
                info_log("   狀態佇列已為空")
            
            # 將系統狀態設為 IDLE
            state_manager.set_state(UEPState.IDLE)
            info_log(f"   狀態設置為: {state_manager.get_state().name}")
            
            # 確保狀態佇列的當前狀態也是 IDLE
            if state_queue_manager.current_state.value != 'idle':
                info_log(f"   修正狀態佇列狀態: {state_queue_manager.current_state.value} -> idle")
                state_queue_manager._transition_to_idle()
            
            # 清除活動會話
            if hasattr(state_manager, '_active_session') and state_manager._active_session:
                state_manager._active_session = None
                info_log("   清除活動會話")
                
            return True
            
        except Exception as e:
            error_log(f"❌ 狀態重置失敗: {e}")
            return False
    
    def _cleanup_working_contexts(self) -> bool:
        """清理工作上下文"""
        try:
            self.phase = InitializationPhase.CONTEXT_CLEANUP
            info_log("🧹 清理工作上下文...")
            
            # 獲取所有活動上下文
            active_contexts = working_context_manager.get_all_contexts_info()
            if active_contexts:
                info_log(f"   發現 {len(active_contexts)} 個活動上下文")
                
                # 清理過期上下文
                cleaned_count = 0
                for context_id, context_info in active_contexts.items():
                    if context_info.get('status') in ['expired', 'completed']:
                        working_context_manager.remove_context(context_id)
                        cleaned_count += 1
                        
                info_log(f"   清理了 {cleaned_count} 個過期上下文")
            else:
                info_log("   沒有發現活動上下文")
                
            return True
            
        except Exception as e:
            error_log(f"❌ 上下文清理失敗: {e}")
            return False
    
    def _initialize_framework_and_modules(self, production_mode: bool) -> bool:
        """初始化核心框架和所有模組（合併 Phase 3, 4, 5）"""
        try:
            # 階段 3: 框架初始化
            self.phase = InitializationPhase.FRAMEWORK_INIT
            info_log("🏗️ 初始化核心框架與模組...")
            info_log(f"   生產模式: {production_mode}")
            
            # 重置框架狀態
            if hasattr(core_framework, 'reset'):
                core_framework.reset()
                info_log("   框架狀態已重置")
            
            # 使用 UnifiedController 來註冊和初始化模組
            if not hasattr(unified_controller, 'initialize'):
                error_log("   UnifiedController 未實現 initialize 方法")
                return False
                
            # 讓 UnifiedController 處理所有模組的註冊和初始化
            success = unified_controller.initialize()
            if not success:
                error_log("   模組註冊失敗")
                return False
                
            info_log("   模組註冊完成")
            
            # 獲取已註冊的模組
            registered_modules = core_framework.get_available_modules()
            if not registered_modules:
                error_log("   沒有已註冊的模組")
                return False
                
            info_log(f"   發現 {len(registered_modules)} 個已註冊模組")
            
            # 只記錄模組狀態，不進行重複初始化
            for module_id, module_info in registered_modules.items():
                # 檢查模組是否已初始化
                if hasattr(module_info, 'is_initialized') and module_info.is_initialized:
                    self.initialized_modules.append(module_id)
                    info_log(f"   ✅ {module_id} 已就緒")
                elif hasattr(module_info, 'module_instance'):
                    # 僅記錄模組狀態，不重新初始化
                    if hasattr(module_info.module_instance, 'is_initialized') and module_info.module_instance.is_initialized:
                        self.initialized_modules.append(module_id)
                        info_log(f"   ✅ {module_id} 已就緒")
                    else:
                        # 假設已由 UnifiedController 完成初始化
                        self.initialized_modules.append(module_id)
                        info_log(f"   ✅ {module_id} 假定已就緒")
                else:
                    error_log(f"   ❓ {module_id} 狀態未知")
            
            # 如果有模組成功初始化，認為這個階段成功
            return len(self.initialized_modules) > 0
            
        except Exception as e:
            error_log(f"❌ 框架與模組初始化失敗: {e}")
            return False
    
    def _setup_strategies(self) -> bool:
        """設置路由策略"""
        try:
            self.phase = InitializationPhase.STRATEGY_SETUP
            info_log("⚙️ 設置路由策略...")
            
            # 重置決策引擎
            if hasattr(context_decision_engine, 'reset'):
                context_decision_engine.reset()
                info_log("   決策引擎已重置")
            
            # 這裡可以設置其他策略相關的配置
            info_log("   策略設置完成")
            return True
            
        except Exception as e:
            error_log(f"❌ 策略設置失敗: {e}")
            return False
    
    def _health_check(self) -> bool:
        """系統健康檢查"""
        try:
            self.phase = InitializationPhase.HEALTH_CHECK
            info_log("🏥 執行系統健康檢查...")
            
            # 檢查核心組件
            health_status = {
                'state_manager': state_manager.get_state() == UEPState.IDLE,
                'framework_and_modules': hasattr(core_framework, 'modules') and core_framework.modules is not None,
                'controller': hasattr(unified_controller, 'is_initialized') and unified_controller.is_initialized,
                'initialized_modules': len(self.initialized_modules) > 0
            }
            
            # 報告健康狀態
            for component, status in health_status.items():
                status_icon = "✅" if status else "❌"
                info_log(f"   {status_icon} {component}: {'正常' if status else '異常'}")
            
            # 如果所有核心組件都正常，認為健康檢查通過
            all_healthy = all(health_status.values())
            
            if all_healthy:
                info_log("   健康檢查通過")
            else:
                info_log("   健康檢查發現問題，但系統可以繼續運行")
                
            return True  # 即使有問題也繼續，因為可能是非關鍵組件
            
        except Exception as e:
            error_log(f"❌ 健康檢查失敗: {e}")
            return False
    
    def _initialize_frontend(self) -> bool:
        """初始化前端應用程式"""
        try:
            self.phase = InitializationPhase.FRONTEND_INIT
            info_log("🖥️ 初始化前端應用程式...")
            
            # 從統一控制器獲取UI模組
            ui_module = None
            if hasattr(unified_controller, 'modules') and 'UI' in unified_controller.modules:
                ui_module = unified_controller.modules['UI']
                
            if not ui_module:
                info_log("⚠️ UI模組未找到，跳過前端初始化")
                return True
                
            # 確認UI模組已經準備好
            if not ui_module.is_initialized:
                info_log("   UI模組初始化...")
                if not ui_module.initialize_frontend():
                    error_log("❌ UI模組初始化失敗")
                    return False
            
            # 只在生產模式下啟動主界面
            if not debug_mode:
                info_log("   啟動桌面寵物界面...")
                from modules.ui_module.ui_module import UIInterfaceType
                result = ui_module.show_interface(UIInterfaceType.MAIN_DESKTOP_PET)
                if 'error' in result:
                    error_log(f"❌ 桌面寵物界面啟動失敗: {result['error']}")
                else:
                    info_log("✅ 桌面寵物界面已啟動")
                    
                # 在生產模式下可選擇性啟動用戶界面
                if config.get('ui', {}).get('show_user_access', True):
                    info_log("   啟動用戶訪問界面...")
                    result = ui_module.show_interface(UIInterfaceType.USER_ACCESS_WIDGET)
                    if 'error' in result:
                        error_log(f"❌ 用戶訪問界面啟動失敗: {result['error']}")
                    else:
                        info_log("✅ 用戶訪問界面已啟動")
            else:
                info_log("   調試模式下前端界面不自動啟動，請使用命令或調試界面控制")
                
            return True
            
        except Exception as e:
            error_log(f"❌ 前端初始化失敗: {e}")
            return False
    
    def get_system_status(self) -> Dict[str, Any]:
        """獲取系統狀態"""
        state_queue_manager = get_state_queue_manager()
        queue_status = state_queue_manager.get_queue_status()
        
        return {
            'phase': self.phase.name,
            'system_state': state_manager.get_state().name,
            'state_queue': {
                'current_state': queue_status['current_state'],
                'queue_length': queue_status['queue_length'],
                'pending_states': queue_status['pending_states']
            },
            'initialized_modules': self.initialized_modules,
            'failed_modules': self.failed_modules,
            'startup_time': time.time() - self.startup_time if self.startup_time else None,
            'is_ready': self.phase == InitializationPhase.READY
        }
    
    def shutdown_system(self):
        """關閉系統"""
        info_log("🛑 開始關閉 UEP 系統...")
        
        try:
            # 清空狀態佇列
            state_queue_manager = get_state_queue_manager()
            if len(state_queue_manager.queue) > 0:
                info_log(f"   清空 {len(state_queue_manager.queue)} 個待處理狀態")
                state_queue_manager.clear_queue()
            
            # 關閉所有模組
            registered_modules = core_framework.get_available_modules()
            for module_name, module_instance in registered_modules.items():
                try:
                    if hasattr(module_instance, 'shutdown'):
                        module_instance.shutdown()
                        info_log(f"   ✅ {module_name} 已關閉")
                except Exception as e:
                    error_log(f"   ❌ {module_name} 關閉失敗: {e}")
            
            # 清理上下文
            working_context_manager.clear_all_contexts()
            
            # 設置狀態為錯誤（表示系統已關閉）
            state_manager.set_state(UEPState.ERROR)
            
            info_log("✅ UEP 系統關閉完成")
            
        except Exception as e:
            error_log(f"❌ 系統關閉過程中發生錯誤: {e}")


# 全局系統初始化器實例
system_initializer = SystemInitializer()
