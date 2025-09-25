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
from core.status_manager import StatusManager
from core.state_manager import state_manager, UEPState

from .schemas import (
    LLMInput, LLMOutput, SystemAction, LLMMode, SystemState,
    ConversationEntry, LearningData, StatusUpdate
)
from .gemini_client import GeminiWrapper
from .prompt_manager import PromptManager
from .learning_engine import LearningEngine
from .cache_manager import cache_manager, CacheType

from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, info_log, error_log


class LLMModule(BaseModule):
    def __init__(self, config=None):
        super().__init__()
        self.config = config or load_module_config("llm_module")
        
        # 核心組件
        self.model = GeminiWrapper(self.config)
        self.prompt_manager = PromptManager(self.config)
        self.learning_engine = LearningEngine(self.config.get("learning", {}))
        self.schema_adapter = LLMSchemaAdapter()
        
        # 統一快取管理器 (整合Gemini顯性快取 + 本地快取)
        self.cache_manager = cache_manager
        
        # 狀態管理
        self.state_manager = state_manager
        self.status_manager = StatusManager()
        
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
        debug_log(2, f"[LLM] 統一快取管理器: 啟用 (Gemini + 本地快取)")
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
            self.status_manager.register_update_callback("llm_module", self._on_status_update)
            
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
        """主要處理方法 - 重構版本，支援新的 CHAT/WORK 模式和新 Router 整合"""
        start_time = time.time()
        
        try:
            # 解析輸入為新架構
            llm_input = LLMInput(**data)
            debug_log(1, f"[LLM] 處理輸入 - 模式: {llm_input.mode}, 用戶輸入: {llm_input.text[:100]}...")
            
            # 檢查是否來自新 Router
            if llm_input.source_layer:
                debug_log(2, f"[LLM] 來自新Router - 來源層級: {llm_input.source_layer}")
                if llm_input.processing_context:
                    debug_log(3, f"[LLM] 處理層上下文: {llm_input.processing_context}")
            
            # 1. 獲取當前系統狀態和會話信息
            current_state = self.state_manager.get_current_state()
            status = self._get_current_system_status()
            session_info = self._get_current_session_info()
            
            # 2. 處理身份上下文 (優先使用來自Router的)
            if llm_input.identity_context:
                identity_context = llm_input.identity_context
                debug_log(2, f"[LLM] 使用Router提供的Identity上下文: {identity_context}")
            else:
                identity_context = self._get_identity_context()
                debug_log(2, f"[LLM] 使用本地Identity上下文: {identity_context}")
            
            debug_log(2, f"[LLM] 系統狀態: {current_state}")
            debug_log(2, f"[LLM] StatusManager: {status}")
            debug_log(2, f"[LLM] 會話信息: {session_info}")
            
            # 3. 補充系統上下文到llm_input (整合Router數據)
            llm_input = self._enrich_with_system_context(
                llm_input, current_state, status, session_info, identity_context
            )
            
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
            cached_response = self.cache_manager.get_cached_response(cache_key)
            
            if cached_response and not llm_input.ignore_cache:
                debug_log(2, "[LLM] 使用快取回應")
                return cached_response
            
            # 2. 構建 CHAT 提示
            prompt = self.prompt_manager.build_chat_prompt(
                user_input=llm_input.text,
                identity_context=llm_input.identity_context,
                memory_context=llm_input.memory_context,
                conversation_history=getattr(llm_input, 'conversation_history', None),
                is_internal=False
            )
            
            # 3. 獲取或創建系統快取
            cached_content_ids = self._get_system_caches("chat")
            
            # 4. 呼叫 Gemini API (使用快取)
            response_data = self.model.query(
                prompt, 
                mode="chat",
                cached_content=cached_content_ids.get("persona")
            )
            response_text = response_data.get("text", "")
            
            # 處理 StatusManager 更新
            if "status_updates" in response_data and response_data["status_updates"]:
                self._process_status_updates(response_data["status_updates"])
            
            # 4. 處理MEM模組整合 (CHAT模式)
            memory_operations = self._process_chat_memory_operations(
                llm_input, response_data, response_text
            )
            
            # 5. 處理學習信號
            if self.learning_engine.learning_enabled:
                # 處理新的累積評分學習信號
                if "learning_signals" in response_data and response_data["learning_signals"]:
                    identity_id = getattr(llm_input.identity_context, 'identity_id', 'default') if llm_input.identity_context else 'default'
                    self.learning_engine.process_learning_signals(identity_id, response_data["learning_signals"])
                
                # 保留舊的互動記錄（用於統計和分析）
                identity_id = getattr(llm_input.identity_context, 'identity_id', 'default') if llm_input.identity_context else 'default'
                self.learning_engine.record_interaction(
                    identity_id=identity_id,
                    interaction_type="CHAT",
                    user_input=llm_input.text,
                    system_response=response_text,
                    metadata={
                        "memory_used": bool(llm_input.memory_context),
                        "identity_used": bool(llm_input.identity_context)
                    }
                )
            
            # 6. 快取回應
            output = LLMOutput(
                text=response_text,
                processing_time=time.time() - start_time,
                tokens_used=len(response_text.split()),
                success=True,
                error=None,
                confidence=0.85,
                mode=LLMMode.CHAT,
                metadata={
                    "mode": "CHAT",
                    "cached": False,
                    "memory_context_size": len(llm_input.memory_context) if llm_input.memory_context else 0,
                    "identity_context_size": len(llm_input.identity_context) if llm_input.identity_context else 0,
                    "memory_operations_count": len(memory_operations),
                    "memory_operations": memory_operations
                }
            )
            
            self.cache_manager.cache_response(cache_key, output)
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
                user_input=llm_input.text,
                available_functions=None,  # TODO: 從 SYS 模組獲取
                workflow_context=getattr(llm_input, 'workflow_context', None),
                identity_context=llm_input.identity_context
            )
            
            # 3. 獲取或創建任務快取
            cached_content_ids = self._get_system_caches("work")
            
            # 4. 呼叫 Gemini API (使用快取)
            response_data = self.model.query(
                prompt, 
                mode="work",
                cached_content=cached_content_ids.get("functions")
            )
            response_text = response_data.get("text", "")
            
            # 處理 StatusManager 更新
            if "status_updates" in response_data and response_data["status_updates"]:
                self._process_status_updates(response_data["status_updates"])
            
            # 4. 處理SYS模組整合 (WORK模式)
            sys_actions = self._process_work_system_actions(
                llm_input, response_data, response_text
            )
            
            # 5. 處理學習信號
            if self.learning_engine.learning_enabled:
                # 處理新的累積評分學習信號
                if "learning_signals" in response_data and response_data["learning_signals"]:
                    identity_id = getattr(llm_input.identity_context, 'identity_id', 'default') if llm_input.identity_context else 'default'
                    self.learning_engine.process_learning_signals(identity_id, response_data["learning_signals"])
                
                # 保留舊的互動記錄（用於統計和分析）
                identity_id = getattr(llm_input.identity_context, 'identity_id', 'default') if llm_input.identity_context else 'default'
                self.learning_engine.record_interaction(
                    identity_id=identity_id,
                    interaction_type="WORK",
                    user_input=llm_input.text,
                    system_response=response_text,
                    metadata={
                        "workflow_context": llm_input.workflow_context,
                        "system_context_used": bool(llm_input.system_context)
                    }
                )
            
            # 6. 分析額外的系統動作（兼容舊邏輯）
            legacy_system_action = self._analyze_system_action(response_text, llm_input.workflow_context)
            
            output = LLMOutput(
                text=response_text,
                processing_time=time.time() - start_time,
                tokens_used=len(response_text.split()),
                success=True,
                error=None,
                confidence=0.90,  # WORK 模式通常更精確
                mode=LLMMode.WORK,
                metadata={
                    "mode": "WORK",
                    "workflow_context_size": len(llm_input.workflow_context) if llm_input.workflow_context else 0,
                    "sys_actions_count": len(sys_actions),
                    "sys_actions": sys_actions,
                    "legacy_system_action": legacy_system_action,
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
            if self.cache_manager:
                cache_stats = self.cache_manager.get_cache_statistics()
                debug_log(2, f"[LLM] Cache 統計: {cache_stats}")
                
            # 取消 StatusManager 回調
            self.status_manager.unregister_update_callback("llm_module")
            
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
                "cache_enabled": self.cache_manager is not None,
            }
            
            if self.cache_manager:
                status["cache_stats"] = self.cache_manager.get_cache_statistics()
                
            if self.learning_engine and self.learning_engine.learning_enabled:
                status["learning_stats"] = {
                    "total_interactions": len(self.learning_engine.interaction_history),
                    "conversation_styles": len(self.learning_engine.conversation_styles),
                    "usage_patterns": len(self.learning_engine.usage_patterns)
                }
                
            return status
            
        except Exception as e:
            error_log(f"[LLM] 獲取模組狀態失敗: {e}")
            return {"error": str(e)}
    
    def _process_status_updates(self, status_updates) -> None:
        """
        處理來自LLM回應的StatusManager更新
        支援物件格式（來自 schema）和陣列格式（舊版相容）
        """
        try:
            if not status_updates:
                return
            
            # 處理物件格式（來自 Gemini schema）
            if isinstance(status_updates, dict):
                # 使用 StatusManager 的專用 delta 更新方法
                if "mood_delta" in status_updates and status_updates["mood_delta"] is not None:
                    self.status_manager.update_mood(status_updates["mood_delta"], "LLM情緒分析")
                    debug_log(2, f"[LLM] Mood 更新: += {status_updates['mood_delta']}")
                
                if "pride_delta" in status_updates and status_updates["pride_delta"] is not None:
                    self.status_manager.update_pride(status_updates["pride_delta"], "LLM情緒分析")  
                    debug_log(2, f"[LLM] Pride 更新: += {status_updates['pride_delta']}")
                
                if "helpfulness_delta" in status_updates and status_updates["helpfulness_delta"] is not None:
                    self.status_manager.update_helpfulness(status_updates["helpfulness_delta"], "LLM情緒分析")
                    debug_log(2, f"[LLM] Helpfulness 更新: += {status_updates['helpfulness_delta']}")
                
                if "boredom_delta" in status_updates and status_updates["boredom_delta"] is not None:
                    self.status_manager.update_boredom(status_updates["boredom_delta"], "LLM情緒分析")
                    debug_log(2, f"[LLM] Boredom 更新: += {status_updates['boredom_delta']}")
                
                # 統計更新次數
                updates_count = sum(1 for key in ["mood_delta", "pride_delta", "helpfulness_delta", "boredom_delta"] 
                                  if key in status_updates and status_updates[key] is not None)
                if updates_count > 0:
                    debug_log(1, f"[LLM] StatusManager 已應用 {updates_count} 個狀態更新")
            
            # 處理陣列格式（舊版相容）
            elif isinstance(status_updates, list):
                for update in status_updates:
                    status_type = update.get("status_type")
                    value = update.get("value") 
                    reason = update.get("reason", "LLM回應觸發")
                    
                    if status_type and value is not None:
                        # 使用絕對值更新狀態
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
    
    def _get_current_system_status(self) -> Dict[str, Any]:
        """獲取當前系統狀態"""
        try:
            return {
                "status_values": self.status_manager.get_status_dict(),
                "personality_modifiers": self.status_manager.get_personality_modifiers(),
                "system_mode": self.state_manager.get_current_state().value
            }
        except Exception as e:
            error_log(f"[LLM] 獲取系統狀態失敗: {e}")
            return {"error": str(e)}
    
    def _get_current_session_info(self) -> Dict[str, Any]:
        """獲取當前會話信息"""
        try:
            # 從工作上下文獲取會話信息
            session_data = working_context_manager.get_context_data(ContextType.SESSION)
            
            return {
                "session_id": session_data.get("session_id", "default"),
                "session_type": session_data.get("session_type", "chat"),
                "start_time": session_data.get("start_time"),
                "interaction_count": session_data.get("interaction_count", 0),
                "last_activity": session_data.get("last_activity")
            }
        except Exception as e:
            error_log(f"[LLM] 獲取會話信息失敗: {e}")
            return {"session_id": "default", "session_type": "chat"}
    
    def _get_identity_context(self) -> Dict[str, Any]:
        """從Working Context獲取Identity信息"""
        try:
            identity_data = working_context_manager.get_context_data(ContextType.IDENTITY)
            
            return {
                "current_identity": identity_data.get("current_identity"),
                "identity_traits": identity_data.get("traits", {}),
                "identity_preferences": identity_data.get("preferences", {}),
                "identity_history": identity_data.get("interaction_history", [])
            }
        except Exception as e:
            error_log(f"[LLM] 獲取Identity上下文失敗: {e}")
            return {}
    
    def _enrich_with_system_context(self, 
                                  llm_input: LLMInput,
                                  current_state: Any,
                                  status: Dict[str, Any],
                                  session_info: Dict[str, Any],
                                  identity_context: Dict[str, Any]) -> LLMInput:
        """補充系統上下文到LLM輸入 - 支援新 Router 整合"""
        try:
            # 創建新的enriched input
            enriched_data = llm_input.dict()
            
            # 補充系統上下文
            if not enriched_data.get("system_context"):
                enriched_data["system_context"] = {}
            
            enriched_data["system_context"].update({
                "current_state": current_state.value if hasattr(current_state, 'value') else str(current_state),
                "status_manager": status,
                "session_info": session_info
            })
            
            # 補充身份上下文 (不覆蓋Router提供的)
            if not enriched_data.get("identity_context"):
                enriched_data["identity_context"] = {}
            # 只在沒有Router數據時補充本地身份上下文
            if not llm_input.source_layer:
                enriched_data["identity_context"].update(identity_context)
            
            # 處理新Router提供的協作上下文
            if llm_input.collaboration_context:
                debug_log(2, f"[LLM] 處理協作上下文: {list(llm_input.collaboration_context.keys())}")
                
                # 設置記憶檢索標誌
                if "mem" in llm_input.collaboration_context:
                    enriched_data["enable_memory_retrieval"] = True
                    mem_config = llm_input.collaboration_context["mem"]
                    if mem_config.get("retrieve_relevant"):
                        enriched_data["memory_context"] = "協作模式：需要檢索相關記憶"
                
                # 設置系統動作標誌
                if "sys" in llm_input.collaboration_context:
                    enriched_data["enable_system_actions"] = True
                    sys_config = llm_input.collaboration_context["sys"]
                    if sys_config.get("allow_execution"):
                        enriched_data["workflow_context"] = {"execution_allowed": True}
            
            # 處理Router的會話上下文
            if llm_input.session_context:
                enriched_data["session_id"] = llm_input.session_context.get("session_id")
                enriched_data["system_context"]["router_session"] = llm_input.session_context
            
            # 處理NLP實體信息
            if llm_input.entities:
                enriched_data["system_context"]["nlp_entities"] = llm_input.entities
            
            return LLMInput(**enriched_data)
            
        except Exception as e:
            error_log(f"[LLM] 補充系統上下文失敗: {e}")
            return llm_input
    
    def _process_chat_memory_operations(self, 
                                      llm_input: LLMInput,
                                      response_data: Dict[str, Any], 
                                      response_text: str) -> List[Dict[str, Any]]:
        """處理CHAT模式的MEM模組操作"""
        memory_operations = []
        
        try:
            # 1. 從Gemini回應中提取記憶操作
            if "memory_operations" in response_data:
                memory_operations.extend(response_data["memory_operations"])
                debug_log(2, f"[LLM] 從回應提取記憶操作: {len(memory_operations)}個")
            
            # 2. 自動記憶儲存邏輯
            if self._should_store_conversation(llm_input, response_text):
                store_operation = {
                    "operation": "store",
                    "content": {
                        "user_input": llm_input.text,
                        "assistant_response": response_text,
                        "timestamp": time.time(),
                        "conversation_context": llm_input.memory_context,
                        "identity_context": llm_input.identity_context
                    },
                    "metadata": {
                        "interaction_type": "chat",
                        "memory_type": "conversation",
                        "auto_generated": True
                    }
                }
                memory_operations.append(store_operation)
                debug_log(2, "[LLM] 自動添加對話記憶儲存")
            
            # 3. 發送記憶操作到MEM模組 (通過Router)
            if memory_operations:
                self._send_to_mem_module(memory_operations)
            
            return memory_operations
            
        except Exception as e:
            error_log(f"[LLM] 處理CHAT記憶操作失敗: {e}")
            return []
    
    def _should_store_conversation(self, llm_input: LLMInput, response_text: str) -> bool:
        """判斷是否應該儲存對話"""
        try:
            # 檢查對話長度
            if len(llm_input.text) < 10 or len(response_text) < 10:
                return False
            
            # 檢查是否為重要對話
            important_keywords = ["記住", "記錄", "重要", "提醒", "保存"]
            if any(keyword in llm_input.text for keyword in important_keywords):
                return True
            
            # 檢查是否包含個人信息或偏好
            personal_keywords = ["喜歡", "討厭", "偏好", "習慣", "姓名", "生日"]
            if any(keyword in llm_input.text for keyword in personal_keywords):
                return True
            
            # 預設儲存較長的有意義對話
            return len(llm_input.text) > 50
            
        except Exception as e:
            error_log(f"[LLM] 判斷對話儲存失敗: {e}")
            return False
    
    def _send_to_mem_module(self, memory_operations: List[Dict[str, Any]]) -> None:
        """通過Router將記憶操作發送到MEM模組"""
        try:
            # 這裡應該通過系統的模組管理器或Router來調用MEM模組
            # 目前先記錄日誌，實際實現需要與系統架構整合
            debug_log(1, f"[LLM] 準備發送 {len(memory_operations)} 個記憶操作到MEM模組")
            
            for i, operation in enumerate(memory_operations):
                debug_log(3, f"[LLM] 記憶操作 #{i+1}: {operation.get('operation', 'unknown')}")
                
            # TODO: 實際發送到MEM模組的邏輯
            # 可能需要通過 working_context_manager 或 registry 來調用
            
        except Exception as e:
            error_log(f"[LLM] 發送記憶操作失敗: {e}")
    
    def _retrieve_relevant_memory(self, user_input: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """從MEM模組檢索相關記憶"""
        try:
            debug_log(2, f"[LLM] 檢索相關記憶: {user_input[:50]}...")
            
            # TODO: 實際從MEM模組檢索記憶的邏輯
            # 目前返回空列表，實際實現需要與MEM模組整合
            
            return []
            
        except Exception as e:
            error_log(f"[LLM] 檢索記憶失敗: {e}")
            return []
    
    def _process_work_system_actions(self, 
                                   llm_input: LLMInput,
                                   response_data: Dict[str, Any], 
                                   response_text: str) -> List[Dict[str, Any]]:
        """處理WORK模式的SYS模組操作"""
        sys_actions = []
        
        try:
            # 1. 從Gemini回應中提取系統動作
            if "sys_action" in response_data:
                sys_action = response_data["sys_action"]
                if isinstance(sys_action, dict):
                    sys_actions.append(sys_action)
                    debug_log(2, f"[LLM] 從回應提取系統動作: {sys_action.get('action_type', 'unknown')}")
            
            if "sys_actions" in response_data:
                batch_actions = response_data["sys_actions"] 
                if isinstance(batch_actions, list):
                    sys_actions.extend(batch_actions)
                    debug_log(2, f"[LLM] 從回應提取批量系統動作: {len(batch_actions)}個")
            
            # 2. 分析文本中的隱含系統動作
            implicit_actions = self._analyze_implicit_system_actions(response_text, llm_input.workflow_context)
            if implicit_actions:
                sys_actions.extend(implicit_actions)
                debug_log(2, f"[LLM] 分析出隱含系統動作: {len(implicit_actions)}個")
            
            # 3. 發送系統動作到SYS模組 (通過Router)
            if sys_actions:
                self._send_to_sys_module(sys_actions, llm_input.workflow_context)
            
            return sys_actions
            
        except Exception as e:
            error_log(f"[LLM] 處理WORK系統動作失敗: {e}")
            return []
    
    def _analyze_implicit_system_actions(self, response_text: str, workflow_context: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """分析文本中的隱含系統動作"""
        implicit_actions = []
        
        try:
            # 文件操作關鍵字
            file_keywords = {
                "開啟檔案": {"action_type": "file_open", "category": "file"},
                "建立檔案": {"action_type": "file_create", "category": "file"},
                "刪除檔案": {"action_type": "file_delete", "category": "file"},
                "複製檔案": {"action_type": "file_copy", "category": "file"},
            }
            
            # 系統操作關鍵字
            system_keywords = {
                "啟動程式": {"action_type": "program_launch", "category": "system"},
                "執行指令": {"action_type": "command_execute", "category": "system"},
                "搜尋檔案": {"action_type": "file_search", "category": "search"},
                "查詢資訊": {"action_type": "info_query", "category": "search"},
            }
            
            all_keywords = {**file_keywords, **system_keywords}
            
            for keyword, action_info in all_keywords.items():
                if keyword in response_text:
                    action = {
                        "action_type": action_info["action_type"],
                        "category": action_info["category"],
                        "target": self._extract_action_target(response_text, keyword),
                        "parameters": {},
                        "confidence": 0.6,  # 隱含動作信心度較低
                        "source": "implicit_analysis",
                        "requires_confirmation": True
                    }
                    implicit_actions.append(action)
            
            return implicit_actions
            
        except Exception as e:
            error_log(f"[LLM] 分析隱含系統動作失敗: {e}")
            return []
    
    def _extract_action_target(self, text: str, keyword: str) -> str:
        """提取動作目標"""
        try:
            # 簡單的目標提取邏輯
            keyword_index = text.find(keyword)
            if keyword_index == -1:
                return ""
            
            # 取關鍵字後面的部分文字作為目標
            after_keyword = text[keyword_index + len(keyword):].strip()
            words = after_keyword.split()
            
            # 取前幾個詞作為目標
            target_words = []
            for word in words[:3]:  # 最多取3個詞
                if word and not word in ["的", "了", "。", "，", "？", "！"]:
                    target_words.append(word)
                else:
                    break
            
            return " ".join(target_words)
            
        except Exception as e:
            error_log(f"[LLM] 提取動作目標失敗: {e}")
            return ""
    
    def _send_to_sys_module(self, sys_actions: List[Dict[str, Any]], workflow_context: Optional[Dict[str, Any]]) -> None:
        """通過Router將系統動作發送到SYS模組"""
        try:
            debug_log(1, f"[LLM] 準備發送 {len(sys_actions)} 個系統動作到SYS模組")
            
            for i, action in enumerate(sys_actions):
                debug_log(3, f"[LLM] 系統動作 #{i+1}: {action.get('action_type', 'unknown')} -> {action.get('target', 'unknown')}")
            
            # TODO: 實際發送到SYS模組的邏輯
            # 可能需要通過 working_context_manager 或 registry 來調用
            
        except Exception as e:
            error_log(f"[LLM] 發送系統動作失敗: {e}")
    
    def _get_system_caches(self, mode: str) -> Dict[str, str]:
        """獲取系統快取ID"""
        cached_content_ids = {}
        
        try:
            if mode == "chat":
                # CHAT模式：persona + style_policy + session_anchor
                persona_cache = self.cache_manager.get_or_create_cache(
                    name="uep:persona:v1",
                    cache_type=CacheType.PERSONA,
                    content_builder=lambda: self._build_persona_cache_content()
                )
                if persona_cache:
                    cached_content_ids["persona"] = persona_cache
                
                style_cache = self.cache_manager.get_or_create_cache(
                    name="uep:style_policy:v1", 
                    cache_type=CacheType.STYLE_POLICY,
                    content_builder=lambda: self._build_style_policy_cache_content()
                )
                if style_cache:
                    cached_content_ids["style_policy"] = style_cache
                
            elif mode == "work":
                # WORK模式：functions + task_spec 
                functions_cache = self.cache_manager.get_or_create_cache(
                    name="uep:functions:v1",
                    cache_type=CacheType.FUNCTIONS,
                    content_builder=lambda: self._build_functions_cache_content()
                )
                if functions_cache:
                    cached_content_ids["functions"] = functions_cache
            
            debug_log(2, f"[LLM] 系統快取準備完成 ({mode}): {len(cached_content_ids)}個")
            return cached_content_ids
            
        except Exception as e:
            error_log(f"[LLM] 系統快取獲取失敗: {e}")
            return {}
    
    def _build_persona_cache_content(self) -> str:
        """構建persona快取內容"""
        return f"""
你是U.E.P (Unified Experience Partner)，一個智能的統一體驗夥伴。

核心特質：
- 友善、專業、樂於學習和幫助
- 具有記憶和學習能力，能夠記住用戶偏好
- 會根據系統狀態調整回應風格和行為

當前系統狀態：{self._get_current_system_status()}

回應語言：Traditional Chinese (zh-TW)
回應格式：根據模式要求的JSON結構
"""
    
    def _build_style_policy_cache_content(self) -> str:
        """構建風格策略快取內容"""
        return """
回應風格調整規則：
1. Mood值影響語氣：
   - 高(>0.7): 活潑、熱情、積極
   - 中(0.3-0.7): 平穩、友善、專業
   - 低(<0.3): 沉穩、謹慎、溫和

2. Pride值影響自信度：
   - 高(>0.7): 積極主動、自信表達
   - 中(0.3-0.7): 平衡謙遜、適度自信
   - 低(<0.3): 謙遜低調、保守表達

3. Boredom值影響主動性：
   - 高(>0.7): 主動提出建議、探索新話題
   - 中(0.3-0.7): 回應導向、適度延伸
   - 低(<0.3): 被動回應、簡潔回答

JSON回應安全規範：
- 所有字符串值必須正確轉義
- 避免使用可能破壞JSON結構的特殊字符
- 確保數值在有效範圍內
"""
    
    def _build_functions_cache_content(self) -> str:
        """構建functions快取內容"""
        return """
U.E.P 系統可用功能規格：

檔案操作功能：
- file_open: 開啟檔案 (參數: file_path)
- file_create: 建立檔案 (參數: file_path, content)
- file_delete: 刪除檔案 (參數: file_path)
- file_copy: 複製檔案 (參數: source_path, dest_path)

系統操作功能：
- program_launch: 啟動程式 (參數: program_name, arguments)
- command_execute: 執行指令 (參數: command, working_directory)
- file_search: 搜尋檔案 (參數: search_pattern, search_path)
- info_query: 查詢系統資訊 (參數: query_type, parameters)

記憶管理功能：
- memory_store: 儲存記憶 (參數: content, memory_type, metadata)
- memory_retrieve: 檢索記憶 (參數: query, max_results, similarity_threshold)
- memory_update: 更新記憶 (參數: memory_id, new_content)

狀態管理功能：
- status_update: 更新系統狀態 (參數: status_type, value, reason)
- mood_adjust: 調整心情值 (參數: adjustment, reason)
- pride_adjust: 調整自豪值 (參數: adjustment, reason)

所有功能調用都需要confidence值和requires_confirmation標記。
"""
