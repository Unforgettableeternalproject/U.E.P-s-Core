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

import re
import time
import json
from typing import Dict, Any, Optional, List
from pathlib import Path

from core.bases.module_base import BaseModule
from core.schemas import LLMModuleData, create_llm_data
from core.schema_adapter import LLMSchemaAdapter
from core.working_context import working_context_manager, ContextType
from core.status_manager import status_manager
from core.states.state_manager import state_manager, UEPState

from .schemas import (
    LLMInput, LLMOutput, SystemAction, LLMMode, SystemState,
    ConversationEntry, LearningData, StatusUpdate
)
from .gemini_client import GeminiWrapper
from .prompt_manager import PromptManager
from .learning_engine import LearningEngine
from .cache_manager import cache_manager, CacheType
from .module_interfaces import (
    state_aware_interface, CollaborationChannel, set_collaboration_state
)

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
        
        # 狀態和會話管理
        self.state_manager = state_manager
        self.status_manager = status_manager
        self.session_info = {}
        
        # 狀態感知模組接口
        self.module_interface = state_aware_interface
        
        # 監聽系統狀態變化以自動切換協作管道
        self._setup_state_listener()
        
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
    
    def _setup_state_listener(self):
        """設定系統狀態監聽器，自動切換協作管道"""
        try:
            # 獲取當前系統狀態並設定初始協作管道
            current_state = self.state_manager.get_current_state()
            set_collaboration_state(current_state)
            
            debug_log(2, f"[LLM] 狀態感知模組接口設定完成，初始狀態: {current_state}")
            debug_log(3, f"[LLM] 管道狀態: {self.module_interface.get_channel_status()}")
            
        except Exception as e:
            error_log(f"[LLM] 狀態監聽器設定失敗: {e}")
    
    def _update_collaboration_channels(self, new_state: UEPState):
        """根據系統狀態更新協作管道"""
        try:
            old_status = self.module_interface.get_channel_status()
            set_collaboration_state(new_state)
            new_status = self.module_interface.get_channel_status()
            
            if old_status != new_status:
                debug_log(2, f"[LLM] 協作管道更新: {old_status} → {new_status}")
                
        except Exception as e:
            error_log(f"[LLM] 協作管道更新失敗: {e}")
        
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
            
            # 1.1 更新協作管道（確保與系統狀態同步）
            self._update_collaboration_channels(current_state)
            
            status = self._get_current_system_status()
            self.session_info = self._get_current_session_info()
            
            # 2. 處理身份上下文 (優先使用來自Router的)
            if llm_input.identity_context:
                identity_context = llm_input.identity_context
                debug_log(2, f"[LLM] 使用Router提供的Identity上下文: {identity_context}")
            else:
                identity_context = self._get_identity_context()
                debug_log(2, f"[LLM] 使用本地Identity上下文: {identity_context}")
            
            debug_log(2, f"[LLM] 系統狀態: {current_state}")
            debug_log(2, f"[LLM] StatusManager: {status}")
            debug_log(2, f"[LLM] 會話信息: {self.session_info}")
            
            # 3. 補充系統上下文到llm_input (整合Router數據)
            llm_input = self._enrich_with_system_context(
                llm_input, current_state, status, self.session_info, identity_context
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
            # 1. MEM 協作：檢索相關記憶 (CHAT狀態專用)
            relevant_memories = []
            if not llm_input.memory_context:  # 只有在沒有提供記憶上下文時才檢索
                relevant_memories = self._retrieve_relevant_memory(llm_input.text, max_results=5)
                if relevant_memories:
                    debug_log(2, f"[LLM] 整合 {len(relevant_memories)} 條相關記憶到對話上下文")
                    # 將檢索到的記憶轉換為記憶上下文
                    llm_input.memory_context = self._format_memories_for_context(relevant_memories)
            
            # 2. 檢查 Context Cache (包含動態記憶)
            import hashlib
            base = f"{llm_input.mode}|{self.session_info.get('session_id','')}"
            text_sig = hashlib.sha256(llm_input.text.encode("utf-8")).hexdigest()[:16]
            mem_sig  = hashlib.sha256((llm_input.memory_context or "").encode("utf-8")).hexdigest()[:16]
            cache_key = f"chat:{base}:{text_sig}:{mem_sig}:{len(relevant_memories)}"
            cached_response = self.cache_manager.get_cached_response(cache_key)
            
            if cached_response and not llm_input.ignore_cache:
                debug_log(2, "[LLM] 使用快取回應（包含記憶上下文）")
                return cached_response
            
            # 3. 構建 CHAT 提示（整合記憶）
            prompt = self.prompt_manager.build_chat_prompt(
                user_input=llm_input.text,
                identity_context=llm_input.identity_context,
                memory_context=llm_input.memory_context,
                conversation_history=getattr(llm_input, 'conversation_history', None),
                is_internal=False,
                relevant_memories=relevant_memories  # 新增：傳入檢索到的記憶
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
                ctx = llm_input.identity_context or {}
                if "learning_signals" in response_data and response_data["learning_signals"]:
                    
                    identity_id = (ctx.get("identity") or {}).get("id") or ctx.get("identity_id") or "default"
                    self.learning_engine.process_learning_signals(identity_id, response_data["learning_signals"])
                    
                # 保留舊的互動記錄（用於統計和分析）
                identity_id = (ctx.get("identity") or {}).get("id") or ctx.get("identity_id") or "default"
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
                ctx = llm_input.identity_context or {}
                if "learning_signals" in response_data and response_data["learning_signals"]:
                    identity_id = (ctx.get("identity") or {}).get("id") or ctx.get("identity_id") or "default"
                    self.learning_engine.process_learning_signals(identity_id, response_data["learning_signals"])
                
                # 保留舊的互動記錄（用於統計和分析）
                identity_id = (ctx.get("identity") or {}).get("id") or ctx.get("identity_id") or "default"
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
        """
        [DEPRECATED] 分析回應文本是否需要系統動作
        
        注意：此方法已廢棄。根據 U.E.P 架構設計：
        1. 意圖分析應該在 NLP 模組階段完成
        2. LLM 在 WORK 模式下應該從 Gemini 結構化回應中獲取系統動作
        3. 不應該重複分析文本來判斷系統功能需求
        
        此方法保留僅用於向後兼容，建議移除對此方法的調用。
        """
        debug_log(3, "[LLM] 警告：使用了已廢棄的 _analyze_system_action 方法")
        return None
    
    def _on_status_update(self, status_type: str, old_value: float, new_value: float, reason: str = ""):
        """StatusManager 狀態更新回調"""
        debug_log(2, f"[LLM] 系統狀態更新 - {status_type}: {old_value} -> {new_value} ({reason})")
        
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
        """獲取當前會話信息 - 優先獲取 CS 或 WS（LLM 作為邏輯中樞的執行會話）"""
        try:
            # 從統一會話管理器獲取會話信息
            from core.sessions.session_manager import session_manager
            
            # LLM 在 CHAT 狀態時應該獲取當前 CS
            active_cs_ids = session_manager.get_active_chatting_session_ids()
            if active_cs_ids:
                # 在架構下，同一時間只會有一個 CS 執行中
                current_cs_id = active_cs_ids[0]
                current_cs = session_manager.get_chatting_session(current_cs_id)
                
                if current_cs:
                    return {
                        "session_id": current_cs_id,
                        "session_type": "chatting",
                        "start_time": getattr(current_cs, 'start_time', None),
                        "interaction_count": getattr(current_cs, 'turn_count', 0),
                        "last_activity": getattr(current_cs, 'last_activity', None),
                        "active_session_type": "CS"
                    }
            
            # LLM 在 WORK 狀態時應該獲取當前 WS
            active_ws_ids = session_manager.get_active_workflow_session_ids()
            if active_ws_ids:
                # 在架構下，同一時間只會有一個 WS 執行中
                current_ws_id = active_ws_ids[0]
                current_ws = session_manager.get_workflow_session(current_ws_id)
                
                if current_ws:
                    return {
                        "session_id": current_ws_id,
                        "session_type": "workflow",
                        "start_time": getattr(current_ws, 'start_time', None),
                        "interaction_count": getattr(current_ws, 'step_count', 0),
                        "last_activity": getattr(current_ws, 'last_activity', None),
                        "active_session_type": "WS"
                    }
            
            # 如果沒有 CS 或 WS，可能系統處於 IDLE 狀態或其他狀態
            return {
                "session_id": "no_active_session", 
                "session_type": "idle",
                "start_time": None,
                "interaction_count": 0,
                "last_activity": None,
                "active_session_type": "NONE"
            }
            
        except Exception as e:
            error_log(f"[LLM] 獲取會話信息失敗: {e}")
            return {
                "session_id": "error", 
                "session_type": "error",
                "active_session_type": "ERROR"
            }
    
    def _get_identity_context(self) -> Dict[str, Any]:
        """從Working Context獲取Identity信息"""
        try:
            identity_data = working_context_manager.get_context_data(ContextType.IDENTITY)
            
            return {
                "identity": {
                    "name": identity_data.get("current_identity"),
                    "traits": identity_data.get("traits", {})
                },
                "preferences": identity_data.get("preferences", {})
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
                        "auto_generated": True,
                        "ttl_seconds": 60 * 60 * 24 * 7,     # 一週
                        "erasable": True
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
            
            sensitive_patterns = [r"\b\d{10}\b", r"@.+\.", r"\b[A-Z]\d{9}\b"]  # 可擴充
            if any(re.search(p, llm_input.text) for p in sensitive_patterns):
                return False  # 含敏感資訊不自動存
            
            # 檢查是否為重要對話
            important_keywords = ["remember", "record", "important", "remind", "save"]
            if any(keyword in llm_input.text for keyword in important_keywords):
                return True
            
            # 檢查是否包含個人信息或偏好
            personal_keywords = ["like", "hate", "prefer", "would have", "name", "birthday"]
            if any(keyword in llm_input.text for keyword in personal_keywords):
                return True
            
            # 預設儲存較長的有意義對話
            return len(llm_input.text) > 50
            
        except Exception as e:
            error_log(f"[LLM] 判斷對話儲存失敗: {e}")
            return False
    
    def _send_to_mem_module(self, memory_operations: List[Dict[str, Any]]) -> None:
        """向MEM模組發送記憶操作 - 通過狀態感知接口"""
        try:
            debug_log(1, f"[LLM] 準備發送 {len(memory_operations)} 個記憶操作到MEM模組")
            
            # 檢查 CHAT-MEM 協作管道是否啟用
            if not self.module_interface.is_channel_active(CollaborationChannel.CHAT_MEM):
                debug_log(2, "[LLM] 記憶操作跳過: MEM模組只在CHAT狀態下運行")
                return
            
            # 逐個處理記憶操作
            for i, operation in enumerate(memory_operations):
                operation_type = operation.get('operation', 'unknown')
                debug_log(3, f"[LLM] 記憶操作 #{i+1}: {operation_type}")
                
                try:
                    # 通過狀態感知接口發送對話儲存請求
                    conversation_data = {
                        "operation_type": operation_type,
                        "content": operation.get('content', {}),
                        "metadata": operation.get('metadata', {}),
                        "source_module": "llm_module"
                    }
                    
                    result = self.module_interface.get_chat_mem_data(
                        "conversation_storage",
                        conversation_data=conversation_data
                    )
                    
                    if result:
                        debug_log(2, f"[LLM] 記憶操作 #{i+1} 成功: {operation_type}")
                    else:
                        debug_log(2, f"[LLM] 記憶操作 #{i+1} 未執行: {operation_type}")
                        
                except Exception as op_error:
                    error_log(f"[LLM] 處理記憶操作 #{i+1} 時出錯: {op_error}")
            
        except Exception as e:
            error_log(f"[LLM] 發送記憶操作失敗: {e}")
    
    def _retrieve_relevant_memory(self, user_input: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """從MEM模組檢索相關記憶 - 通過狀態感知接口"""
        try:
            debug_log(2, f"[LLM] 檢索相關記憶: {user_input[:50]}...")
            
            # 檢查 CHAT-MEM 協作管道是否啟用
            if not self.module_interface.is_channel_active(CollaborationChannel.CHAT_MEM):
                debug_log(2, "[LLM] 記憶檢索失敗: MEM模組只在CHAT狀態下運行")
                return []
            
            # 通過狀態感知接口檢索記憶
            memories = self.module_interface.get_chat_mem_data(
                "memory_retrieval",
                query=user_input,
                max_results=max_results,
                memory_types=["conversation", "user_info", "context"]
            )
            
            if memories:
                debug_log(1, f"[LLM] 檢索到 {len(memories)} 條相關記憶")
                return memories
            else:
                debug_log(2, "[LLM] 未檢索到相關記憶")
                return []
            
        except Exception as e:
            error_log(f"[LLM] 記憶檢索失敗: {e}")
            return []
    
    def _format_memories_for_context(self, memories: List[Dict[str, Any]]) -> str:
        """將檢索到的記憶格式化為對話上下文"""
        try:
            if not memories:
                return ""
            
            context_parts = ["相關記憶上下文:"]
            
            for i, memory in enumerate(memories[:5], 1):  # 限制最多5條記憶
                memory_type = memory.get("type", "unknown")
                content = memory.get("content", "")
                timestamp = memory.get("timestamp", "")
                
                if memory_type == "conversation":
                    # 對話記憶格式
                    user_input = memory.get("user_input", "")
                    assistant_response = memory.get("assistant_response", "")
                    context_parts.append(f"{i}. [對話] 用戶:{user_input} 助手:{assistant_response}")
                elif memory_type == "user_info":
                    # 用戶信息記憶格式
                    context_parts.append(f"{i}. [用戶信息] {content}")
                else:
                    # 一般記憶格式
                    context_parts.append(f"{i}. [{memory_type}] {content}")
            
            formatted_context = "\n".join(context_parts)
            debug_log(3, f"[LLM] 格式化記憶上下文: {len(formatted_context)} 字符")
            
            return formatted_context
            
        except Exception as e:
            error_log(f"[LLM] 格式化記憶上下文失敗: {e}")
            return ""
    
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
        """向SYS模組發送系統動作 - 通過狀態感知接口"""
        try:
            debug_log(1, f"[LLM] 準備發送 {len(sys_actions)} 個系統動作到SYS模組")
            
            # 檢查 WORK-SYS 協作管道是否啟用
            if not self.module_interface.is_channel_active(CollaborationChannel.WORK_SYS):
                debug_log(2, "[LLM] 系統動作跳過: SYS模組只在WORK狀態下運行")
                return
            
            for i, action in enumerate(sys_actions):
                action_type = action.get('action_type', 'unknown')
                target = action.get('target', 'unknown')
                debug_log(3, f"[LLM] 系統動作 #{i+1}: {action_type} -> {target}")
                
                try:
                    # 通過狀態感知接口獲取工作流狀態並執行功能
                    workflow_status = self.module_interface.get_work_sys_data(
                        "workflow_status",
                        workflow_id=workflow_context.get('workflow_id') if workflow_context else 'default'
                    )
                    
                    if workflow_status:
                        debug_log(3, f"[LLM] 工作流狀態: {workflow_status.get('current_step', 'unknown')}")
                    
                    # 獲取可用功能並嘗試執行
                    available_functions = self.module_interface.get_work_sys_data(
                        "function_registry",
                        category=action_type
                    )
                    
                    if available_functions and action_type in available_functions:
                        debug_log(2, f"[LLM] 系統動作 #{i+1} 已處理: {action_type}")
                    else:
                        debug_log(2, f"[LLM] 系統動作 #{i+1} 功能不可用: {action_type}")
                        
                except Exception as action_error:
                    error_log(f"[LLM] 處理系統動作 #{i+1} 時出錯: {action_error}")
            
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
