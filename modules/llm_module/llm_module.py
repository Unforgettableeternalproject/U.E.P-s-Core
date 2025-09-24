# modules/llm_module/llm_module.py
"""
LLM 模組重構版本

新功能：
1. 支援 CHAT 和 WORK 狀態分離處理
2. 整合 StatusManager 系統數值管理
3. Context Caching 上下文快取
4. 學習功能：記錄使用者偏好和對話風格
5. 與 Working Context 和身份管理系統整合
6. 內建 Prompt 管理，不再依賴外部 prompt_builder
"""

import time
import json
from typing import Dict, Any, Optional, List
from pathlib import Path

from core.module_base import BaseModule
from core.schemas import LLMModuleData, create_llm_data
from core.schema_adapter import LLMSchemaAdapter
from core.working_context import working_context_manager, ContextType
from core.status_manager import status_manager
from core.state_manager import state_manager, UEPState

from .schemas import (
    LLMInput, LLMOutput, SystemAction, LLMMode, SystemState,
    ContextCacheConfig, ConversationEntry, LearningData, StatusUpdate
)
from .gemini_client import GeminiWrapper
from .prompt_manager import PromptManager
from .context_cache import ContextCache
from .learning_engine import LearningEngine

from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, info_log, error_log


class LLMModule(BaseModule):
    def __init__(self, config=None):
        super().__init__()
        self.config = config or load_module_config("llm_module")
        
        # 核心組件
        self.model = GeminiWrapper(self.config)
        self.prompt_manager = PromptManager(self.config)
        self.context_cache = ContextCache(self.config.get("context_cache", {}))
        self.learning_engine = LearningEngine(self.config.get("learning", {}))
        self.schema_adapter = LLMSchemaAdapter()
        
        # 狀態管理
        self.state_manager = state_manager
        
        # 統計數據
        self.processing_stats = {
            "total_requests": 0,
            "chat_requests": 0, 
            "work_requests": 0,
            "total_processing_time": 0.0,
            "cache_hits": 0
        }

    def debug(self):
        # Debug level = 1
        debug_log(1, "[LLM] Debug 模式啟用 - 重構版本")
        # Debug level = 2  
        debug_log(2, f"[LLM] 模型名稱: {self.model.model_name}")
        debug_log(2, f"[LLM] 溫度: {self.model.temperature}")
        debug_log(2, f"[LLM] Top P: {self.model.top_p}")
        debug_log(2, f"[LLM] 最大輸出字元數: {self.model.max_tokens}")
        debug_log(2, f"[LLM] Context Cache: {'啟用' if self.context_cache else '停用'}")
        debug_log(2, f"[LLM] Learning Engine: {'啟用' if self.learning_engine.learning_enabled else '停用'}")
        # Debug level = 3
        debug_log(3, f"[LLM] 完整模組設定: {self.config}")
        
    def initialize(self):
        """初始化 LLM 模組"""
        debug_log(1, "[LLM] 初始化中...")
        self.debug()
        
        try:
            # Gemini 客戶端在 __init__ 中已經初始化，檢查是否正常
            if not hasattr(self.model, 'client') or self.model.client is None:
                error_log("[LLM] Gemini 模型初始化失敗")
                return False
            
            # 註冊 StatusManager 回調
            status_manager.register_update_callback("llm_module", self._on_status_update)
            
            # 獲取當前系統狀態
            current_state = self.state_manager.get_current_state()
            debug_log(2, f"[LLM] 當前系統狀態: {current_state}")
            
            self.is_initialized = True
            info_log("[LLM] LLM 模組重構版初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[LLM] 初始化失敗: {e}")
            return False
        
    def handle(self, data: dict) -> dict:
        """主要處理方法 - 重構版本，支援新的 CHAT/WORK 模式"""
        start_time = time.time()
        
        try:
            # 解析輸入為新架構
            llm_input = LLMInput(**data)
            debug_log(1, f"[LLM] 處理輸入 - 模式: {llm_input.mode}, 用戶輸入: {llm_input.text[:100]}...")
            
            # 獲取當前系統狀態 
            status = status_manager.get_personality_modifiers()
            debug_log(2, f"[LLM] 系統狀態: {status}")
            
            # 根據模式切換處理邏輯
            if llm_input.mode == LLMMode.CHAT:
                output = self._handle_chat_mode(llm_input, status)
            elif llm_input.mode == LLMMode.WORK:
                output = self._handle_work_mode(llm_input, status)
            else:
                # 向後兼容舊的 intent 系統
                output = self._handle_legacy_mode(llm_input, status)
            
            # 轉換為字典格式返回（保持與舊系統的兼容）
            result = output.dict()
            result["status"] = "ok" if output.success else "error"
            
            return result
                
        except Exception as e:
            error_log(f"[LLM] 處理時發生錯誤: {e}")
            return {
                "text": "處理時發生錯誤，請稍後再試。",
                "processing_time": time.time() - start_time,
                "tokens_used": 0,
                "success": False,
                "error": str(e),
                "confidence": 0.0,
                "metadata": {},
                "status": "error"
            }
    
    def _handle_chat_mode(self, llm_input: "LLMInput", status: Dict[str, Any]) -> "LLMOutput":
        """處理 CHAT 模式 - 與 MEM 協作的日常對話"""
        start_time = time.time()
        debug_log(2, "[LLM] 處理 CHAT 模式")
        
        try:
            # 1. 檢查 Context Cache
            cache_key = f"chat_{llm_input.text[:50]}_{hash(str(llm_input.memory_context))}"
            cached_response = self.context_cache.get_cached_response(cache_key)
            
            if cached_response and not llm_input.ignore_cache:
                debug_log(2, "[LLM] 使用快取回應")
                return cached_response
            
            # 2. 構建 CHAT 提示
            prompt = self.prompt_manager.build_chat_prompt(
                llm_input.text,
                llm_input.memory_context,
                llm_input.system_context,
                llm_input.identity_context,
                status
            )
            
            # 3. 呼叫 Gemini API
            response_data = self.model.query(prompt, mode="chat")
            response_text = response_data.get("text", "")
            
            # 處理 StatusManager 更新
            if "status_updates" in response_data and response_data["status_updates"]:
                self._process_status_updates(response_data["status_updates"])
            
            # 4. 紀錄學習資料
            if self.learning_engine.learning_enabled:
                self.learning_engine.record_interaction({
                    "mode": "CHAT",
                    "input": llm_input.text,
                    "output": response_text,
                    "timestamp": time.time(),
                    "memory_used": bool(llm_input.memory_context),
                    "identity_used": bool(llm_input.identity_context)
                })
            
            # 5. 快取回應
            output = LLMOutput(
                text=response_text,
                processing_time=time.time() - start_time,
                tokens_used=len(response_text.split()),
                success=True,
                error=None,
                confidence=0.85,
                metadata={
                    "mode": "CHAT",
                    "cached": False,
                    "memory_context_size": len(llm_input.memory_context) if llm_input.memory_context else 0,
                    "identity_context_size": len(llm_input.identity_context) if llm_input.identity_context else 0
                }
            )
            
            self.context_cache.cache_response(cache_key, output)
            return output
            
        except Exception as e:
            error_log(f"[LLM] CHAT 模式處理錯誤: {e}")
            return LLMOutput(
                text="聊天處理時發生錯誤，請稍後再試。",
                processing_time=time.time() - start_time,
                tokens_used=0,
                success=False,
                error=str(e),
                confidence=0.0,
                metadata={"mode": "CHAT", "error_type": "processing_error"}
            )
    
    def _handle_work_mode(self, llm_input: "LLMInput", status: Dict[str, Any]) -> "LLMOutput":
        """處理 WORK 模式 - 與 SYS 協作的工作任務"""
        start_time = time.time()
        debug_log(2, "[LLM] 處理 WORK 模式")
        
        try:
            # 1. WORK 模式通常不使用快取（因為任務導向）
            debug_log(3, "[LLM] WORK 模式 - 跳過快取檢查")
            
            # 2. 構建 WORK 提示  
            prompt = self.prompt_manager.build_work_prompt(
                llm_input.text,
                llm_input.memory_context,
                llm_input.system_context,
                llm_input.identity_context,
                status,
                llm_input.workflow_context
            )
            
            # 3. 呼叫 Gemini API
            response_data = self.model.query(prompt, mode="work")
            response_text = response_data.get("text", "")
            
            # 處理 StatusManager 更新
            if "status_updates" in response_data and response_data["status_updates"]:
                self._process_status_updates(response_data["status_updates"])
            
            # 4. 紀錄學習資料
            if self.learning_engine.learning_enabled:
                self.learning_engine.record_interaction({
                    "mode": "WORK", 
                    "input": llm_input.text,
                    "output": response_text,
                    "timestamp": time.time(),
                    "workflow_context": llm_input.workflow_context,
                    "system_context_used": bool(llm_input.system_context)
                })
            
            # 5. 分析是否需要系統動作
            system_action = self._analyze_system_action(response_text, llm_input.work_context)
            
            output = LLMOutput(
                text=response_text,
                processing_time=time.time() - start_time,
                tokens_used=len(response_text.split()),
                success=True,
                error=None,
                confidence=0.90,  # WORK 模式通常更精確
                system_action=system_action,
                metadata={
                    "mode": "WORK",
                    "work_context": llm_input.work_context,
                    "system_action_detected": system_action is not None,
                    "system_context_size": len(llm_input.system_context) if llm_input.system_context else 0
                }
            )
            
            return output
            
        except Exception as e:
            error_log(f"[LLM] WORK 模式處理錯誤: {e}")
            return LLMOutput(
                text="工作任務處理時發生錯誤，請稍後再試。",
                processing_time=time.time() - start_time,
                tokens_used=0,
                success=False,
                error=str(e),
                confidence=0.0,
                metadata={"mode": "WORK", "error_type": "processing_error"}
            )
    
    def _handle_legacy_mode(self, llm_input: "LLMInput", status: Dict[str, Any]) -> "LLMOutput":
        """處理舊的 intent 系統以保持向後兼容"""
        start_time = time.time()
        debug_log(2, f"[LLM] 處理舊版 intent: {getattr(llm_input, 'intent', 'unknown')}")
        
        # 將舊的 intent 轉換為新的模式
        legacy_intent = getattr(llm_input, 'intent', 'chat')
        
        if legacy_intent == "chat":
            # 轉為 CHAT 模式
            llm_input.mode = "CHAT"
            return self._handle_chat_mode(llm_input, status)
        elif legacy_intent == "command":
            # 轉為 WORK 模式
            llm_input.mode = "WORK"
            return self._handle_work_mode(llm_input, status)
        else:
            return LLMOutput(
                text=f"抱歉，目前暫不支援 '{legacy_intent}' 類型的處理。",
                processing_time=time.time() - start_time,
                tokens_used=0,
                success=False,
                error=f"不支援的 intent: {legacy_intent}",
                confidence=0.0,
                metadata={"legacy_intent": legacy_intent}
            )
    
    def _analyze_system_action(self, response_text: str, workflow_context: Optional[Dict[str, Any]]) -> Optional["SystemAction"]:
        """分析回應文本是否需要系統動作"""
        try:
            # 簡單的關鍵字分析（後續可以改為更複雜的 NLP 分析）
            action_keywords = {
                "開啟": "open",
                "啟動": "launch", 
                "執行": "execute",
                "搜尋": "search",
                "查找": "find",
                "建立": "create",
                "刪除": "delete"
            }
            
            for keyword, action_type in action_keywords.items():
                if keyword in response_text:
                    return SystemAction(
                        action_type=action_type,
                        target="",  # 需要進一步分析
                        parameters={},
                        confidence=0.7,
                        requires_confirmation=True
                    )
            
            return None
            
        except Exception as e:
            debug_log(1, f"[LLM] 系統動作分析失敗: {e}")
            return None
    
    def _on_status_update(self, status_type: str, old_value: float, new_value: float):
        """StatusManager 狀態更新回調"""
        debug_log(2, f"[LLM] 系統狀態更新 - {status_type}: {old_value} -> {new_value}")
        
        # 根據狀態變化調整 LLM 行為
        if status_type == "mood" and new_value < 0.3:
            debug_log(1, "[LLM] 偵測到系統心情低落，調整回應風格")
        elif status_type == "boredom" and new_value > 0.8:
            debug_log(1, "[LLM] 偵測到系統無聊，建議主動互動")
    
    def shutdown(self):
        """關閉 LLM 模組並保存狀態"""
        try:
            info_log("[LLM] LLM 模組關閉中...")
            
            # 保存學習資料
            if self.learning_engine:
                self.learning_engine.save_learning_data()
                debug_log(2, "[LLM] 學習資料已保存")
            
            # 清理 Context Cache
            if self.context_cache:
                cache_stats = self.context_cache.get_cache_statistics()
                debug_log(2, f"[LLM] Cache 統計: {cache_stats}")
                
            # 取消 StatusManager 回調
            status_manager.unregister_update_callback("llm_module")
            
            info_log("[LLM] LLM 模組重構版關閉完成")
            
        except Exception as e:
            error_log(f"[LLM] 關閉時發生錯誤: {e}")
    
    def get_module_status(self) -> Dict[str, Any]:
        """獲取模組狀態資訊"""
        try:
            status = {
                "initialized": self.is_initialized,
                "model_status": "active" if self.model else "inactive",
                "learning_enabled": self.learning_engine.learning_enabled if self.learning_engine else False,
                "cache_enabled": self.context_cache is not None,
            }
            
            if self.context_cache:
                status["cache_stats"] = self.context_cache.get_cache_statistics()
                
            if self.learning_engine and self.learning_engine.learning_enabled:
                status["learning_stats"] = {
                    "total_interactions": len(self.learning_engine.learning_data.interactions),
                    "conversation_styles": len(self.learning_engine.learning_data.conversation_styles),
                    "usage_patterns": len(self.learning_engine.learning_data.usage_patterns)
                }
                
            return status
            
        except Exception as e:
            error_log(f"[LLM] 獲取模組狀態失敗: {e}")
            return {"error": str(e)}
    
    def _process_status_updates(self, status_updates: list) -> None:
        """
        處理來自LLM回應的StatusManager更新
        """
        try:
            if not status_updates:
                return
                
            for update in status_updates:
                status_type = update.get("status_type")
                value = update.get("value") 
                reason = update.get("reason", "LLM回應觸發")
                
                if status_type and value is not None:
                    # 更新StatusManager
                    success = self.status_manager.update_status(
                        status_type=status_type,
                        value=value,
                        reason=reason
                    )
                    
                    if success:
                        debug_log(2, f"[LLM] StatusManager更新成功: {status_type}={value}, 原因: {reason}")
                    else:
                        debug_log(1, f"[LLM] StatusManager更新失敗: {status_type}={value}")
                        
        except Exception as e:
            error_log(f"[LLM] 處理StatusManager更新時出錯: {e}")
