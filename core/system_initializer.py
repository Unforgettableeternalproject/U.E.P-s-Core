# core/system_initializer.py
"""
系統初始化器 - 管理 UEP 系統的啟動過程

這個模組負責：
1. 系統狀態初始化
2. 核心框架啟動
3. 模組註冊和初始化
4. 工作上下文清理
5. 前端應用程式啟動（未來）
6. 系統健康檢查

啟動層級順序：
controller > framework > router > strategy > state > context > session
"""

import time
from typing import Dict, Any, List, Optional
from enum import Enum, auto

from core.framework import core_framework, ExecutionMode
from core.controller import unified_controller
from core.state_manager import UEPState, StateManager, state_manager
from core.working_context import working_context_manager, ContextType
from core.strategies import context_decision_engine
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log


class InitializationPhase(Enum):
    """初始化階段"""
    STARTING = auto()           # 開始啟動
    STATE_RESET = auto()        # 狀態重置
    CONTEXT_CLEANUP = auto()    # 上下文清理
    FRAMEWORK_INIT = auto()     # 框架初始化
    MODULE_REGISTRATION = auto() # 模組註冊
    MODULE_INITIALIZATION = auto() # 模組初始化
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
                
            # Phase 3: 框架初始化
            if not self._initialize_framework(production_mode):
                return False
                
            # Phase 4: 模組註冊
            if not self._register_modules(production_mode):
                return False
                
            # Phase 5: 模組初始化
            if not self._initialize_modules():
                return False
                
            # Phase 6: 策略設置
            if not self._setup_strategies():
                return False
                
            # Phase 7: 健康檢查
            if not self._health_check():
                return False
                
            # Phase 8: 前端初始化（未來實現）
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
            
            # 將系統狀態設為 IDLE
            state_manager.set_state(UEPState.IDLE)
            info_log(f"   狀態設置為: {state_manager.get_state().name}")
            
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
    
    def _initialize_framework(self, production_mode: bool) -> bool:
        """初始化核心框架"""
        try:
            self.phase = InitializationPhase.FRAMEWORK_INIT
            info_log("🏗️ 初始化核心框架...")
            
            # 重置框架狀態
            if hasattr(core_framework, 'reset'):
                core_framework.reset()
                info_log("   框架狀態已重置")
            
            info_log(f"   生產模式: {production_mode}")
            
            return True
            
        except Exception as e:
            error_log(f"❌ 框架初始化失敗: {e}")
            return False
    
    def _register_modules(self, production_mode: bool) -> bool:
        """註冊模組"""
        try:
            self.phase = InitializationPhase.MODULE_REGISTRATION
            info_log("📝 註冊模組...")
            
            # 使用 UnifiedController 來註冊模組
            if hasattr(unified_controller, 'initialize'):
                success = unified_controller.initialize()
                if success:
                    info_log("   模組註冊完成")
                    return True
                else:
                    error_log("   模組註冊失敗")
                    return False
            else:
                error_log("   UnifiedController 未實現 initialize 方法")
                return False
                
        except Exception as e:
            error_log(f"❌ 模組註冊失敗: {e}")
            return False
    
    def _initialize_modules(self) -> bool:
        """初始化已註冊的模組"""
        try:
            self.phase = InitializationPhase.MODULE_INITIALIZATION
            info_log("🔧 初始化模組...")
            
            # 獲取已註冊的模組
            registered_modules = core_framework.get_available_modules()
            if not registered_modules:
                error_log("   沒有已註冊的模組")
                return False
                
            info_log(f"   發現 {len(registered_modules)} 個已註冊模組")
            
            # 初始化每個模組
            for module_id, module_info in registered_modules.items():
                try:
                    module_instance = module_info.module_instance
                    if hasattr(module_instance, 'initialize'):
                        if module_instance.initialize():
                            self.initialized_modules.append(module_id)
                            info_log(f"   ✅ {module_id} 初始化成功")
                        else:
                            self.failed_modules.append(module_id)
                            error_log(f"   ❌ {module_id} 初始化失敗")
                    else:
                        # 假設沒有 initialize 方法的模組已經準備就緒
                        self.initialized_modules.append(module_id)
                        info_log(f"   ✅ {module_id} 已就緒（無需初始化）")
                        
                except Exception as e:
                    self.failed_modules.append(module_id)
                    error_log(f"   ❌ {module_id} 初始化異常: {e}")
            
            # 如果有模組成功初始化，認為這個階段成功
            return len(self.initialized_modules) > 0
            
        except Exception as e:
            error_log(f"❌ 模組初始化失敗: {e}")
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
                'framework': hasattr(core_framework, 'modules') and core_framework.modules is not None,
                'controller': hasattr(unified_controller, 'is_initialized') and unified_controller.is_initialized,
                'modules': len(self.initialized_modules) > 0
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
        """初始化前端應用程式（未來實現）"""
        try:
            self.phase = InitializationPhase.FRONTEND_INIT
            info_log("🖥️ 初始化前端應用程式...")
            
            # 目前暫時跳過前端初始化
            info_log("   前端初始化暫時跳過（未實現）")
            return True
            
        except Exception as e:
            error_log(f"❌ 前端初始化失敗: {e}")
            return False
    
    def get_system_status(self) -> Dict[str, Any]:
        """獲取系統狀態"""
        return {
            'phase': self.phase.name,
            'system_state': state_manager.get_state().name,
            'initialized_modules': self.initialized_modules,
            'failed_modules': self.failed_modules,
            'startup_time': time.time() - self.startup_time if self.startup_time else None,
            'is_ready': self.phase == InitializationPhase.READY
        }
    
    def shutdown_system(self):
        """關閉系統"""
        info_log("🛑 開始關閉 UEP 系統...")
        
        try:
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
