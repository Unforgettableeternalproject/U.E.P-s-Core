# core/unified_controller.py
"""
統一控制器 - 整合新的核心框架與現有模組系統

這個控制器負責：
1. 初始化和配置核心框架
2. 註冊所有模組到框架
3. 設置路由策略和決策引擎
4. 提供統一的模組調用接口
5. 管理整個系統的生命週期
"""

import asyncio
import time
from typing import Dict, Any, Optional, List
from enum import Enum

from core.framework import CoreFramework, ExecutionMode, core_framework
from core.strategies import (
    smart_strategy, priority_strategy, conditional_strategy, 
    context_decision_engine
)
from core.working_context import ContextType
from core.state_manager import UEPState
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log


class ModuleCapabilities:
    """模組能力定義"""
    
    # STT 模組能力
    STT_CAPABILITIES = [
        "speech_recognition", 
        "speaker_identification", 
        "voice_activity_detection",
        "real_time_transcription"
    ]
    
    # NLP 模組能力  
    NLP_CAPABILITIES = [
        "intent_recognition",
        "sentiment_analysis", 
        "text_classification",
        "language_understanding"
    ]
    
    # MEM 模組能力
    MEM_CAPABILITIES = [
        "memory_storage",
        "memory_retrieval", 
        "context_management",
        "personalization"
    ]
    
    # LLM 模組能力
    LLM_CAPABILITIES = [
        "language_model",
        "text_generation", 
        "conversation",
        "function_calling"
    ]
    
    # TTS 模組能力
    TTS_CAPABILITIES = [
        "speech_synthesis",
        "voice_cloning", 
        "emotion_control",
        "real_time_synthesis"
    ]
    
    # SYS 模組能力
    SYS_CAPABILITIES = [
        "system_control",
        "workflow_management", 
        "file_operations",
        "command_execution"
    ]


class UnifiedController:
    """統一控制器 - 管理整個 UEP 系統"""
    
    def __init__(self):
        """初始化統一控制器"""
        self.framework = core_framework
        self.config = load_config()
        self.enabled_modules = self.config.get("modules_enabled", {})
        
        # 模組實例儲存
        self.module_instances = {}
        
        # 初始化狀態
        self.is_initialized = False
        self.is_running = False
        
        info_log("[UnifiedController] 統一控制器初始化")
    
    def initialize(self) -> bool:
        """初始化整個系統"""
        try:
            info_log("[UnifiedController] 開始系統初始化...")
            
            # 1. 載入和註冊模組
            if not self._load_and_register_modules():
                error_log("[UnifiedController] 模組載入失敗")
                return False
            
            # 2. 註冊路由策略
            self._register_route_strategies()
            
            # 3. 註冊決策引擎  
            self._register_decision_engines()
            
            # 4. 設置事件處理器
            self._setup_event_handlers()
            
            # 5. 註冊決策處理器 (整合 Working Context)
            self._register_decision_handlers()
            
            # 6. 初始化模組
            self._initialize_modules()
            
            self.is_initialized = True
            info_log("[UnifiedController] 系統初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[UnifiedController] 系統初始化失敗: {e}")
            return False
    
    def _load_and_register_modules(self) -> bool:
        """載入和註冊所有啟用的模組"""
        try:
            from core.registry import get_module
            
            # 模組配置映射
            module_configs = {
                "stt": {
                    "name": "stt_module",
                    "capabilities": ModuleCapabilities.STT_CAPABILITIES,
                    "dependencies": [],
                    "priority": 5
                },
                "nlp": {
                    "name": "nlp_module", 
                    "capabilities": ModuleCapabilities.NLP_CAPABILITIES,
                    "dependencies": [],
                    "priority": 4
                },
                "mem": {
                    "name": "mem_module",
                    "capabilities": ModuleCapabilities.MEM_CAPABILITIES, 
                    "dependencies": [],
                    "priority": 3
                },
                "llm": {
                    "name": "llm_module",
                    "capabilities": ModuleCapabilities.LLM_CAPABILITIES,
                    "dependencies": [],
                    "priority": 6
                },
                "tts": {
                    "name": "tts_module",
                    "capabilities": ModuleCapabilities.TTS_CAPABILITIES,
                    "dependencies": [],
                    "priority": 2
                },
                "sys": {
                    "name": "sys_module", 
                    "capabilities": ModuleCapabilities.SYS_CAPABILITIES,
                    "dependencies": [],
                    "priority": 7
                }
            }
            
            # 載入和註冊每個啟用的模組
            for module_id, config in module_configs.items():
                # 使用完整的模組名稱檢查啟用狀態
                module_name = config["name"]
                if not self.enabled_modules.get(module_name, False):
                    debug_log(1, f"[UnifiedController] 模組 {module_name} 未啟用，跳過")
                    continue
                
                try:
                    # 載入模組實例
                    module_instance = get_module(config["name"])
                    if module_instance is None:
                        error_log(f"[UnifiedController] 無法載入模組: {module_id}")
                        continue
                    
                    # 註冊到框架
                    success = self.framework.register_module(
                        module_id=module_id,
                        module_instance=module_instance,
                        capabilities=config["capabilities"],
                        dependencies=config["dependencies"], 
                        priority=config["priority"]
                    )
                    
                    if success:
                        self.module_instances[module_id] = module_instance
                        info_log(f"[UnifiedController] 成功註冊模組: {module_id}")
                    else:
                        error_log(f"[UnifiedController] 註冊模組失敗: {module_id}")
                        
                except Exception as e:
                    error_log(f"[UnifiedController] 載入模組異常 {module_id}: {e}")
                    continue
            
            info_log(f"[UnifiedController] 已註冊 {len(self.module_instances)} 個模組")
            return len(self.module_instances) > 0
            
        except Exception as e:
            error_log(f"[UnifiedController] 模組載入失敗: {e}")
            return False
    
    def _register_route_strategies(self):
        """註冊路由策略"""
        self.framework.register_route_strategy("smart", smart_strategy)
        self.framework.register_route_strategy("priority", priority_strategy)
        self.framework.register_route_strategy("conditional", conditional_strategy)
        info_log("[UnifiedController] 路由策略註冊完成")
    
    def _register_decision_engines(self):
        """註冊決策引擎"""
        self.framework.decision_engines["context_aware"] = context_decision_engine
        info_log("[UnifiedController] 決策引擎註冊完成")
    
    def _setup_event_handlers(self):
        """設置事件處理器"""
        # 狀態變更事件
        self.framework.register_event_handler("state_changed", self._on_state_changed)
        
        # 模組執行事件
        self.framework.register_event_handler("module_executed", self._on_module_executed)
        
        # 模組註冊事件
        self.framework.register_event_handler("module_registered", self._on_module_registered)
        
        info_log("[UnifiedController] 事件處理器設置完成")
    
    def _register_decision_handlers(self):
        """註冊 Working Context 決策處理器"""
        # 註冊 STT 語者識別決策處理器
        stt_module = self.module_instances.get("stt")
        if stt_module and hasattr(stt_module, "speaker_module"):
            try:
                from modules.stt_module.speaker_context_handler import create_speaker_context_handler
                speaker_handler = create_speaker_context_handler(stt_module)
                self.framework.register_decision_handler(ContextType.SPEAKER_ACCUMULATION, speaker_handler)
                info_log("[UnifiedController] STT 語者決策處理器註冊完成")
            except Exception as e:
                error_log(f"[UnifiedController] STT 決策處理器註冊失敗: {e}")
    
    def _initialize_modules(self):
        """初始化所有已註冊的模組"""
        for module_id, module_instance in self.module_instances.items():
            try:
                if hasattr(module_instance, 'initialize'):
                    module_instance.initialize()
                    info_log(f"[UnifiedController] 模組初始化完成: {module_id}")
            except Exception as e:
                error_log(f"[UnifiedController] 模組初始化失敗 {module_id}: {e}")
    
    # ========== 事件處理器 ==========
    
    def _on_state_changed(self, event_data: Dict[str, Any]):
        """狀態變更事件處理器"""
        old_state = event_data.get("old_state")
        new_state = event_data.get("new_state")
        debug_log(2, f"[UnifiedController] 狀態變更: {old_state.name} → {new_state.name}")
    
    def _on_module_executed(self, event_data: Dict[str, Any]):
        """模組執行事件處理器"""
        module_id = event_data.get("module_id")
        intent = event_data.get("intent")
        result = event_data.get("result", {})
        
        debug_log(3, f"[UnifiedController] 模組執行: {module_id} - {intent}")
        
        # 更新狀態管理器
        self.framework.handle_state_event(intent, result)
    
    def _on_module_registered(self, event_data: Dict[str, Any]):
        """模組註冊事件處理器"""
        module_id = event_data.get("module_id")
        capabilities = event_data.get("capabilities", [])
        debug_log(2, f"[UnifiedController] 模組註冊: {module_id} - {capabilities}")
    
    # ========== 公共接口 ==========
    
    def process_input(self, intent: str, data: Dict[str, Any], strategy: str = "smart") -> Dict[str, Any]:
        """
        處理輸入的統一接口
        
        Args:
            intent: 處理意圖 (chat, command, etc.)
            data: 輸入資料
            strategy: 路由策略名稱
            
        Returns:
            處理結果
        """
        if not self.is_initialized:
            return {"status": "error", "message": "系統未初始化"}
        
        try:
            # 添加上下文資訊
            processing_context = {
                "current_state": self.framework.get_current_state(),
                "has_working_context": len(self.framework.working_context.contexts) > 0,
                "has_active_session": len(self.framework.active_sessions) > 0,
                "timestamp": time.time()
            }
            
            data.update(processing_context)
            
            # 執行處理管線
            result = self.framework.execute_pipeline(
                intent=intent,
                data=data,
                execution_mode=ExecutionMode.SEQUENTIAL
            )
            
            return result
            
        except Exception as e:
            error_log(f"[UnifiedController] 處理輸入失敗: {e}")
            return {"status": "error", "message": str(e)}
    
    def process_voice_input(self, callback: Optional[callable] = None) -> Dict[str, Any]:
        """
        處理語音輸入 (STT 實時模式)
        
        Args:
            callback: 結果回調函數
            
        Returns:
            操作結果
        """
        stt_module = self.module_instances.get("stt")
        if not stt_module:
            return {"status": "error", "message": "STT 模組不可用"}
        
        try:
            # 設置回調函數來處理 STT 結果
            def stt_result_handler(result):
                if callback:
                    callback(result)
                else:
                    # 預設處理邏輯
                    self._handle_stt_result(result)
            
            # 啟動實時語音識別
            stt_module.start_realtime(on_result=stt_result_handler)
            
            return {"status": "success", "message": "語音識別已啟動"}
            
        except Exception as e:
            error_log(f"[UnifiedController] 語音輸入處理失敗: {e}")
            return {"status": "error", "message": str(e)}
    
    def _handle_stt_result(self, result: Dict[str, Any]):
        """處理 STT 結果的預設邏輯"""
        if isinstance(result, dict):
            text = result.get("text", "")
            should_activate = result.get("should_activate", False)
            
            if should_activate and text:
                # 自動處理語音輸入
                nlp_result = self.process_input("voice_recognition", {"text": text})
                
                if nlp_result.get("intent"):
                    # 根據識別的意圖繼續處理
                    final_result = self.process_input(nlp_result["intent"], nlp_result)
                    debug_log(1, f"[UnifiedController] 語音處理完成: {final_result.get('status')}")
    
    def stop_voice_input(self) -> bool:
        """停止語音輸入"""
        stt_module = self.module_instances.get("stt")
        if stt_module and hasattr(stt_module, 'stop_realtime'):
            try:
                stt_module.stop_realtime()
                return True
            except Exception as e:
                error_log(f"[UnifiedController] 停止語音輸入失敗: {e}")
        return False
    
    def get_system_status(self) -> Dict[str, Any]:
        """獲取系統狀態"""
        framework_status = self.framework.get_framework_status()
        
        return {
            "initialized": self.is_initialized,
            "running": self.is_running,
            "framework_status": framework_status,
            "enabled_modules": list(self.module_instances.keys()),
            "system_health": self._check_system_health()
        }
    
    def _check_system_health(self) -> str:
        """檢查系統健康狀態"""
        try:
            available_modules = self.framework.get_available_modules()
            if len(available_modules) == 0:
                return "critical"
            elif len(available_modules) < len(self.module_instances) * 0.5:
                return "warning"
            else:
                return "healthy"
        except:
            return "unknown"
    
    def shutdown(self):
        """關閉系統"""
        try:
            info_log("[UnifiedController] 開始系統關閉...")
            
            # 停止語音輸入
            self.stop_voice_input()
            
            # 清理框架資源
            self.framework.cleanup()
            
            # 關閉模組
            for module_id, module_instance in self.module_instances.items():
                try:
                    if hasattr(module_instance, 'shutdown'):
                        module_instance.shutdown()
                        debug_log(2, f"[UnifiedController] 模組關閉: {module_id}")
                except Exception as e:
                    error_log(f"[UnifiedController] 模組關閉失敗 {module_id}: {e}")
            
            self.is_running = False
            self.is_initialized = False
            
            info_log("[UnifiedController] 系統關閉完成")
            
        except Exception as e:
            error_log(f"[UnifiedController] 系統關閉失敗: {e}")


# 全局統一控制器實例
unified_controller = UnifiedController()
