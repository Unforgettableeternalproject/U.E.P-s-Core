# modules/nlp_module/nlp_module.py
"""
NLP 模組 Phase 2 - 重構版本

新功能：
1. 語者身份管理與Working Context整合
2. 分段意圖分析，支援複合指令
3. 新增call類型意圖
4. 實體抽取和語義分析
5. 系統狀態轉換建議
6. 個性化語言理解
"""

import os
import time
import threading
from typing import Dict, Any, Optional, List, Union

from core.bases.module_base import BaseModule
from core.schemas import NLPModuleData
from core.working_context import working_context_manager, ContextType
from core.states.state_queue import get_state_queue_manager
from core.states.state_manager import UEPState as SystemState
from utils.debug_helper import debug_log, info_log, error_log

from .schemas import (
    NLPInput, NLPOutput, IntentType, UserProfile, IdentityStatus,
    SystemStateTransition
)
from .identity_manager import IdentityManager
from .intent_analyzer import IntentAnalyzer
from .multi_intent_context import get_multi_intent_context_manager


class NLPModule(BaseModule):
    """重構後的NLP模組"""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """初始化NLP模組"""
        super().__init__()
        
        self.config = config or {}
        
        # 模組組件
        self.identity_manager: Optional[IdentityManager] = None
        self.intent_analyzer: Optional[IntentAnalyzer] = None
        self.context_manager = get_multi_intent_context_manager()
        self.state_queue_manager = get_state_queue_manager()
        

        # 模組狀態
        self.is_initialized = False
        
        info_log("[NLP] NLP模組初始化")
    
    def debug(self):
        """除錯資訊輸出"""
        debug_log(1, "[NLP] Debug 模式啟用")
        debug_log(2, f"[NLP] 模組設定: {self.config}")
        debug_log(3, f"[NLP] 身份管理器狀態: {'已載入' if self.identity_manager else '未載入'}")
        debug_log(3, f"[NLP] 意圖分析器狀態: {'已載入' if self.intent_analyzer else '未載入'}")
        debug_log(4, f"[NLP] 完整模組設定: {self.config}")
    
    def initialize(self) -> bool:
        """初始化模組"""
        try:
            debug_log(1, "[NLP] 開始初始化...")
            self.debug()
            
            # 初始化身份管理器
            identity_storage_path = self.config.get("identity_storage_path", "memory/identities")
            identity_config = self.config.get("identity_config", {
                "sample_threshold": 15,
                "confirmation_threshold": 0.8
            })
            self.identity_manager = IdentityManager(identity_storage_path, identity_config)
            
            # 註冊身份決策處理器到Working Context
            decision_handler = self.identity_manager.get_decision_handler()
            working_context_manager.register_decision_handler(
                ContextType.SPEAKER_ACCUMULATION, 
                decision_handler
            )
            
            # 將身份管理器註冊到工作上下文
            working_context_manager.set_context_data("identity_manager", self.identity_manager)
            info_log("[NLP] 身份決策處理器已註冊到Working Context")
            
            # 初始化意圖分析器
            self.intent_analyzer = IntentAnalyzer(self.config)
            if not self.intent_analyzer.initialize():
                error_log("[NLP] 意圖分析器初始化失敗")
                return False
            
            self.is_initialized = True
            info_log("[NLP] 模組初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[NLP] 初始化失敗：{e}")
            return False
    
    def handle(self, data: Union[Dict[str, Any], NLPInput]) -> Dict[str, Any]:
        """處理NLP請求"""
        try:
            # 通知 Controller 有活動
            self._notify_controller_activity()
            
            # 驗證輸入 - 支援字典或 NLPInput 物件
            if isinstance(data, NLPInput):
                validated_input = data
            else:
                validated_input = NLPInput(**data)
            debug_log(2, f"[NLP] 接收到請求：文本長度={len(validated_input.text)}, "
                       f"語者ID={validated_input.speaker_id}")
            
            # 第一階段：語者身份處理
            identity_result = self._process_speaker_identity(validated_input)
            
            # 第二階段：意圖分析
            intent_result = self._analyze_intent(validated_input, identity_result.get("identity"))
            
            # 第三階段：系統狀態處理
            state_result = self._process_system_state(intent_result, validated_input)
            
            # 組合結果
            final_result = self._combine_results(validated_input, identity_result, 
                                               intent_result, state_result)
            
            # 更新使用者互動記錄
            if identity_result.get("identity"):
                self._update_interaction_history(identity_result["identity"], final_result)
            
            debug_log(1, f"[NLP] 處理完成：主要意圖={final_result.primary_intent}, "
                       f"身份={final_result.identity.identity_id if final_result.identity else 'None'}")
            
            # 在調用Router之前，先執行狀態轉換
            self._execute_state_transition(final_result, state_result)
            
            # 不再直接調用Router，而是通知System Loop進行下一步處理
            self._notify_system_loop_nlp_completed(validated_input, final_result)
            
            return final_result.model_dump()
            
        except Exception as e:
            error_log(f"[NLP] 處理失敗：{e}")
            return self._create_error_response(str(e))
    
    def _process_speaker_identity(self, input_data: NLPInput) -> Dict[str, Any]:
        """處理語者身份識別 - 從Working Context獲取說話人資料"""
        result = {
            "identity": None,
            "identity_action": None,
            "processing_notes": []
        }
        
        try:
            # 檢查是否為文字輸入模式 (繞過說話人識別)
            metadata = getattr(input_data, 'metadata', {})
            is_text_input = metadata.get('input_mode') == 'text' or metadata.get('bypass_speaker_id', False)
            
            if is_text_input:
                debug_log(2, "[NLP] 檢測到文字輸入模式，跳過說話人識別，使用預設身份")
                # 使用預設身份 - 不查詢也不創建 Identity
                default_identity = self._create_default_identity()
                if default_identity:
                    result["identity"] = default_identity
                    result["identity_action"] = "text_input_default"
                    result["processing_notes"].append("文字輸入模式：使用預設身份")
                    # 將預設身份設置到Working Context
                    self._add_identity_to_working_context(default_identity)
                return result
            
            # 從Working Context獲取說話人資料，而非直接從input_data
            speaker_data = self._get_speaker_from_working_context()
            
            if not speaker_data or not input_data.enable_identity_processing:
                result["processing_notes"].append("無說話人資料或跳過身份處理")
                # 如果沒有說話人資料，創建通用身份
                if input_data.enable_identity_processing:
                    generic_identity = self._create_generic_identity()
                    if generic_identity:
                        result["identity"] = generic_identity
                        result["identity_action"] = "generic"
                        result["processing_notes"].append("創建通用身份")
                        # 將通用身份設置到Working Context
                        self._add_identity_to_working_context(generic_identity)
                return result
            
            speaker_id = speaker_data.get('speaker_id')
            speaker_status = speaker_data.get('status', 'unknown')
            speaker_confidence = speaker_data.get('confidence', 0.0)
            
            debug_log(2, f"[NLP] 從Working Context獲取說話人: {speaker_id} (信心度: {speaker_confidence})")
            
            # 使用身份管理器處理語者識別
            identity, action = self.identity_manager.process_speaker_identification(  # type: ignore
                speaker_id or "unknown",  # 確保 speaker_id 不為 None
                speaker_status,
                speaker_confidence
            )
            
            result["identity"] = identity
            result["identity_action"] = action
            
            if action == "accumulating":
                # 語者樣本累積狀態，創建通用身份供當前使用
                generic_identity = self._create_generic_identity()
                if generic_identity:
                    result["identity"] = generic_identity
                    result["identity_action"] = "accumulating_with_generic"
                    result["processing_notes"].append("語者樣本累積中，使用通用身份")
                    # 將通用身份也設置到Working Context
                    self._add_identity_to_working_context(generic_identity)
                
            elif action == "loaded":
                if identity:
                    result["processing_notes"].append(f"載入正式身份：{identity.display_name}")
                    # 將使用者身份資訊添加到Working Context
                    self._add_identity_to_working_context(identity)
            elif action == "created":
                if identity:
                    result["processing_notes"].append(f"創建新身份：{identity.display_name}")
                    # 將新身份資訊添加到Working Context
                    self._add_identity_to_working_context(identity)
                
            debug_log(3, f"[NLP] 語者身份處理：{action}, 身份={identity.identity_id if identity else 'None'}")
            
        except Exception as e:
            error_log(f"[NLP] 語者身份處理失敗：{e}")
            result["processing_notes"].append(f"身份處理錯誤：{str(e)}")
            # 出錯時使用通用身份
            if input_data.enable_identity_processing:
                generic_identity = self._create_generic_identity()
                if generic_identity:
                    result["identity"] = generic_identity
                    result["identity_action"] = "error_fallback"
                    # 將通用身份設置到Working Context
                    self._add_identity_to_working_context(generic_identity)
        
        return result
    
    def _analyze_intent(self, input_data: NLPInput, identity: Optional[UserProfile]) -> Dict[str, Any]:
        """分析文本意圖 - 支援 CS 感知邏輯"""
        try:
            # 檢查當前是否有活動的 Chatting Session
            active_cs_context = self._check_active_chatting_sessions()
            
            # 準備上下文
            context = {
                "current_system_state": input_data.current_system_state,
                "conversation_history": input_data.conversation_history,
                "identity": identity.dict() if identity else None,
                "active_chatting_sessions": active_cs_context
            }
            
            # 添加使用者偏好資訊 (如果有身份)
            if identity:
                context["user_preferences"] = {
                    "system_habits": identity.system_habits,
                    "conversation_style": identity.conversation_style
                }
            
            # CS 感知意圖調整
            intent_bias = self._determine_intent_bias(active_cs_context, input_data.text)
            
            # 執行意圖分析
            result = self.intent_analyzer.analyze_intent(  # type: ignore
                input_data.text,
                enable_segmentation=input_data.enable_segmentation,
                context=context,
                intent_bias=intent_bias  # 添加意圖偏向
            )
            
            # 應用 CS 感知邏輯調整結果
            if active_cs_context and active_cs_context["has_active_sessions"]:
                result = self._apply_cs_aware_adjustments(result, active_cs_context, input_data.text)
            
            # 處理指令中斷（在 CHAT 狀態中）
            if result.get("command_interruption"):
                self._handle_command_interruption(result["command_interruption"], input_data.text, context)
            
            debug_log(3, f"[NLP] 意圖分析完成：{result['primary_intent']}, "
                       f"片段數={len(result['intent_segments'])}, "
                       f"CS感知={'是' if active_cs_context['has_active_sessions'] else '否'}")
            
            return result
            
        except Exception as e:
            error_log(f"[NLP] 意圖分析失敗：{e}")
            return {
                "primary_intent": IntentType.UNKNOWN,
                "intent_segments": [],
                "overall_confidence": 0.0,
                "entities": [],
                "state_transition": None
            }
    
    def _process_system_state(self, intent_result: Dict[str, Any], 
                            input_data: NLPInput) -> Dict[str, Any]:
        """處理系統狀態相關邏輯 - 增強版支持多意圖上下文"""
        result = {
            "next_modules": [],
            "awaiting_input": False,
            "timeout_seconds": None,
            "context_ids": [],
            "execution_plan": []
        }
        
        try:
            primary_intent = intent_result["primary_intent"]
            context_ids = intent_result.get("context_ids", [])
            execution_plan = intent_result.get("execution_plan", [])
            
            result["context_ids"] = context_ids
            result["execution_plan"] = execution_plan
            
            # 根據意圖決定下一步處理
            if primary_intent == IntentType.CALL:
                # call類型：等待進一步指示
                result["awaiting_input"] = True
                result["timeout_seconds"] = 30  # 30秒超時
                result["next_modules"] = []  # 暫不轉發到其他模組
                
            elif primary_intent == IntentType.CHAT:
                # 聊天類型：轉發到MEM和LLM
                result["next_modules"] = ["mem_module", "llm_module"]
                
            elif primary_intent in [IntentType.COMMAND, IntentType.COMPOUND]:
                # 指令類型：轉發到SYS, MEM, LLM
                result["next_modules"] = ["mem_module", "llm_module", "sys_module"]
                
            elif primary_intent == IntentType.NON_SENSE:
                # 無意義內容：可能轉發到LLM進行處理
                result["next_modules"] = ["llm_module"]
            
            debug_log(3, f"[NLP] 系統狀態處理：下一步模組={result['next_modules']}, "
                       f"等待輸入={result['awaiting_input']}, 上下文數={len(context_ids)}")
            
            # 將意圖分段添加到狀態佇列
            added_states = self._process_intent_to_state_queue(intent_result)
            result["added_states"] = added_states
            
            # 處理多意圖上下文的狀態轉換
            if intent_result.get("state_transition"):
                state_transition = intent_result["state_transition"]
                info_log(f"[NLP] 建議狀態轉換: {state_transition['to_state']} "
                        f"(上下文: {state_transition.get('context_id', 'N/A')})")
                result["recommended_state"] = state_transition["to_state"]
                result["transition_context"] = state_transition.get("context_id")
            
        except Exception as e:
            error_log(f"[NLP] 系統狀態處理失敗：{e}")
        
        return result
    
    def _process_intent_to_state_queue(self, intent_result: Dict[str, Any]) -> List:
        """將意圖分析結果轉換為系統狀態並添加到佇列"""
        try:
            intent_segments = intent_result.get("intent_segments", [])
            if not intent_segments:
                debug_log(2, "[NLP] 沒有意圖片段需要處理")
                return []
            
            # 使用狀態佇列管理器處理意圖
            added_states = self.state_queue_manager.process_nlp_intents(intent_segments)
            
            if added_states:
                info_log(f"[NLP] 添加系統狀態到佇列: {[state.value for state in added_states]}")
                debug_log(3, f"[NLP] 目前佇列狀態: {self.state_queue_manager.get_queue_status()}")
            else:
                debug_log(2, "[NLP] 沒有新的系統狀態需要添加")
            
            return added_states
                
        except Exception as e:
            error_log(f"[NLP] 狀態佇列處理失敗: {e}")
            return []
    
    def _combine_results(self, input_data: NLPInput, identity_result: Dict[str, Any],
                        intent_result: Dict[str, Any], state_result: Dict[str, Any]) -> NLPOutput:
        """組合所有處理結果"""
        
        # 創建意圖片段對象
        intent_segments = []
        for segment_data in intent_result.get("intent_segments", []):
            if isinstance(segment_data, dict):
                # 如果是字典，轉換為IntentSegment對象
                from .schemas import IntentSegment
                segment = IntentSegment(**segment_data)
            else:
                segment = segment_data
            intent_segments.append(segment)
        
        # 創建狀態轉換對象
        state_transition = None
        if intent_result.get("state_transition"):
            state_transition = SystemStateTransition(**intent_result["state_transition"])
        
        # 組合處理註記
        processing_notes = identity_result.get("processing_notes", [])
        processing_notes.append(f"意圖分析完成，信心度：{intent_result.get('overall_confidence', 0):.3f}")
        
        # 獲取狀態佇列信息
        queue_status = self.state_queue_manager.get_queue_status()
        added_states = state_result.get("added_states", [])
        
        return NLPOutput(
            original_text=input_data.text,
            timestamp=time.time(),
            identity=identity_result.get("identity"),
            identity_action=identity_result.get("identity_action"),
            primary_intent=intent_result.get("primary_intent", IntentType.UNKNOWN),
            intent_segments=intent_segments,
            overall_confidence=intent_result.get("overall_confidence", 0.0),
            state_transition=state_transition,
            next_modules=state_result.get("next_modules", []),
            processing_notes=processing_notes,
            awaiting_further_input=state_result.get("awaiting_input", False),
            timeout_seconds=state_result.get("timeout_seconds"),
            queue_states_added=added_states,
            current_system_state=queue_status.get("current_state")
        )
    
    def _add_to_speaker_accumulation(self, input_data: NLPInput):
        """將語者資料添加到Working Context進行累積"""
        try:
            # 準備樣本資料
            sample_data = {
                "text": input_data.text,
                "timestamp": time.time(),
                "confidence": input_data.speaker_confidence
            }
            
            # 準備元數據
            metadata = {
                "speaker_id": input_data.speaker_id,
                "total_samples": 1
            }
            
            # 添加到Working Context
            working_context_manager.add_data_to_context(
                context_type=ContextType.SPEAKER_ACCUMULATION,
                data_item=sample_data,
                metadata=metadata
            )
            
            debug_log(3, f"[NLP] 語者樣本已添加到Working Context：{input_data.speaker_id}")
            
        except Exception as e:
            error_log(f"[NLP] 添加語者樣本失敗：{e}")
    
    def _add_identity_to_working_context(self, identity: UserProfile):
        """將使用者身份資訊添加到Working Context"""
        try:
            # 使用新的便利方法設置身份相關數據
            working_context_manager.set_current_identity(identity.dict())
            
            # 設置記憶庫令牌 (用於MEM模組)
            if identity.memory_token:
                working_context_manager.set_memory_token(identity.memory_token)
            
            # 設置語音偏好 (用於TTS模組)
            if identity.voice_preferences:
                working_context_manager.set_voice_preferences(identity.voice_preferences)
            
            # 設置對話風格 (用於LLM模組)
            if identity.conversation_style:
                working_context_manager.set_conversation_style(identity.conversation_style)
            
            info_log(f"[NLP] 身份 {identity.identity_id} 已添加到全局工作上下文")
            
        except Exception as e:
            error_log(f"[NLP] 添加身份到工作上下文失敗：{e}")
    
    def _update_interaction_history(self, identity: UserProfile, result: NLPOutput):
        """更新使用者互動歷史"""
        try:
            # 準備互動數據
            interaction_data = {
                "module": "nlp",  # 標記數據來源模組
                "intent": result.primary_intent,
                "text_length": len(result.original_text),
                "confidence": result.overall_confidence,
                "command_type": None,
                "timestamp": time.time()
            }
            
            # 提取指令類型
            if result.primary_intent in [IntentType.COMMAND, IntentType.COMPOUND]:
                entities = []
                for segment in result.intent_segments:
                    entities.extend(segment.entities)
                
                if entities:
                    interaction_data["command_type"] = entities[0].get("entity_type")
                    
                    # 如果是系統指令，則標記為 SYS 模組互動
                    if result.primary_intent == IntentType.COMMAND:
                        interaction_data["module"] = "sys"
            
            # 如果是聊天類型，則標記為 LLM 模組互動
            elif result.primary_intent == IntentType.CHAT:
                interaction_data["module"] = "llm"
            
            # 更新互動記錄
            self.identity_manager.update_identity_interaction(  # type: ignore
                identity.identity_id, 
                interaction_data
            )
            
        except Exception as e:
            debug_log(3, f"[NLP] 更新互動歷史失敗：{e}")
    
    def _create_error_response(self, error_message: str) -> Dict[str, Any]:
        """創建錯誤回應"""
        return NLPOutput(
            original_text="",
            timestamp=time.time(),
            identity=None,
            identity_action=None,
            primary_intent=IntentType.UNKNOWN,
            intent_segments=[],
            overall_confidence=0.0,
            state_transition=None,
            awaiting_further_input=False,
            timeout_seconds=None,
            queue_states_added=None,
            current_system_state=None,
            processing_notes=[f"處理錯誤：{error_message}"]
        ).dict()
    
    def shutdown(self):
        """關閉模組"""
        info_log("[NLP] 模組關閉")
        self.is_initialized = False
    
    def get_capabilities(self) -> List[str]:
        """獲取模組能力"""
        return [
            "intent_classification",
            "speaker_identity_management", 
            "entity_extraction",
            "segmented_analysis",
            "state_transition_suggestion",
            "memory_token_management",
            "chatting_session_awareness",    # 新增：CS 感知能力
            "context_aware_intent_bias"     # 新增：上下文感知意圖偏向
        ]
    
    def _check_active_chatting_sessions(self) -> Dict[str, Any]:
        """檢查當前是否有活動的 Chatting Sessions"""
        try:
            # 延遲導入避免循環依賴
            from core.sessions.session_manager import session_manager
            
            # 使用統一介面獲取所有活躍會話
            all_active_sessions = session_manager.get_all_active_sessions()
            active_chatting_sessions = all_active_sessions.get('chatting', [])
            
            return {
                "has_active_sessions": len(active_chatting_sessions) > 0,
                "session_count": len(active_chatting_sessions),
                "session_ids": [session.session_id for session in active_chatting_sessions],
                "session_contexts": [
                    {
                        "session_id": session.session_id,
                        "identity_id": session.identity_context.get("user_id") if session.identity_context else None,
                        "turn_count": session.turn_counter,
                        "memory_token": session.memory_token
                    }
                    for session in active_chatting_sessions
                ]
            }
        except ImportError:
            debug_log(2, "[NLP] Chatting Session 管理器不可用")
            return {
                "has_active_sessions": False,
                "session_count": 0,
                "session_ids": [],
                "session_contexts": []
            }
        except Exception as e:
            error_log(f"[NLP] 檢查活動 CS 時發生錯誤: {e}")
            return {
                "has_active_sessions": False,
                "session_count": 0,
                "session_ids": [],
                "session_contexts": []
            }
    
    def _determine_intent_bias(self, cs_context: Dict[str, Any], text: str) -> Dict[str, float]:
        """根據 CS 上下文確定意圖偏向"""
        intent_bias = {}
        
        if cs_context.get("has_active_sessions", False):
            # 在有活動 CS 時，偏向聊天意圖
            intent_bias["chat"] = 0.3  # 增加聊天意圖權重
            
            # 除非文本明顯是指令
            command_indicators = [
                "執行", "開始", "停止", "關閉", "設定", "配置", "安裝", 
                "啟動", "終止", "運行", "編輯", "建立", "創建", "刪除"
            ]
            
            if any(indicator in text for indicator in command_indicators):
                # 明顯的指令關鍵字，減少聊天偏向
                intent_bias["chat"] = -0.2
                intent_bias["command"] = 0.4
            else:
                # 沒有明顯指令關鍵字，強化聊天偏向
                intent_bias["chat"] = 0.5
                intent_bias["command"] = -0.3
            
            debug_log(4, f"[NLP] CS感知意圖偏向: {intent_bias}")
        
        return intent_bias
    
    def _apply_cs_aware_adjustments(self, intent_result: Dict[str, Any], 
                                   cs_context: Dict[str, Any], 
                                   text: str) -> Dict[str, Any]:
        """應用 CS 感知邏輯調整意圖分析結果"""
        
        # 複製結果以避免修改原始數據
        adjusted_result = intent_result.copy()
        
        # 如果有活動的 CS 且當前意圖不是明確的指令
        if cs_context.get("has_active_sessions", False):
            
            # 檢查是否為明確的系統指令
            explicit_commands = [
                "關閉對話", "結束聊天", "退出", "離開", "停止對話",
                "切換模式", "執行指令", "系統設定"
            ]
            
            is_explicit_command = any(cmd in text for cmd in explicit_commands)
            
            # 如果不是明確指令且當前意圖模糊
            if not is_explicit_command and adjusted_result.get("overall_confidence", 0.0) < 0.7:
                
                # 將模糊意圖調整為 chat
                original_intent = adjusted_result.get("primary_intent")
                adjusted_result["primary_intent"] = "chat"
                
                # 調整意圖片段
                for segment in adjusted_result.get("intent_segments", []):
                    if hasattr(segment, "intent"):
                        if segment.intent not in ["command"] or segment.confidence < 0.8:
                            segment.intent = "chat"
                            segment.confidence = min(segment.confidence + 0.2, 0.95)
                    elif isinstance(segment, dict):
                        if segment.get("intent") not in ["command"] or segment.get("confidence", 0) < 0.8:
                            segment["intent"] = "chat"
                            segment["confidence"] = min(segment.get("confidence", 0) + 0.2, 0.95)
                
                # 更新整體信心度
                adjusted_result["overall_confidence"] = min(adjusted_result.get("overall_confidence", 0) + 0.3, 0.95)
                
                debug_log(4, f"[NLP] CS感知調整: {original_intent} -> chat (信心度: {adjusted_result['overall_confidence']:.2f})")
            
            # 添加 CS 上下文信息到結果
            adjusted_result["cs_context"] = {
                "active_sessions": cs_context.get("session_count", 0),
                "cs_influenced": True,
                "adjustment_applied": not is_explicit_command
            }
        
        return adjusted_result
    
    def _handle_command_interruption(self, interruption_info: Dict[str, Any], 
                                  original_text: str, context: Dict[str, Any]) -> None:
        """處理指令中斷 - 在 CHAT 狀態中檢測到明顯指令時插入 WORK 狀態"""
        try:
            if not interruption_info.get("needs_interruption", False):
                return
            
            debug_log(1, f"[NLP] 處理指令中斷: {interruption_info.get('reason', 'unknown')}")
            
            # 準備中斷元數據
            metadata = {
                "nlp_detection": interruption_info,
                "original_text": original_text,
                "detection_confidence": interruption_info.get("confidence", 0.0),
                "command_segments": interruption_info.get("command_segments", []),
                "trigger_source": "nlp_module"
            }
            
            # 從上下文獲取觸發用戶
            trigger_user = None
            if context.get("identity"):
                trigger_user = context["identity"].get("identity_id")
            
            # 調用狀態佇列進行聊天中斷
            success = self.state_queue_manager.interrupt_chat_for_work(
                command_task=original_text,
                trigger_user=trigger_user,
                metadata=metadata
            )
            
            if success:
                info_log(f"[NLP] 成功插入工作中斷: 信心度={interruption_info.get('confidence', 0.0):.2f}")
            else:
                error_log("[NLP] 工作中斷插入失敗")
                
        except Exception as e:
            error_log(f"[NLP] 處理指令中斷失敗: {e}")
    
    def _notify_system_loop_nlp_completed(self, input_data: NLPInput, nlp_result: NLPOutput):
        """
        ✅ 事件驅動版本：發布輸入層完成事件
        使用事件總線解耦，不再直接調用 System Loop
        
        事件數據包含 session_id 和 cycle_index 用於 flow-based 去重
        """
        try:
            info_log(f"[NLP] 輸入層處理完成，發布事件: 意圖={nlp_result.primary_intent}, 文本='{input_data.text[:50]}...'")
            
            # 獲取當前 GS session_id 和 cycle_index (用於去重)
            session_id = self._get_current_gs_id()
            cycle_index = self._get_current_cycle_index()
            
            # 準備輸入層完成數據
            input_layer_completion_data = {
                # Flow-based 去重所需欄位
                "session_id": session_id,
                "cycle_index": cycle_index,
                "layer": "INPUT",
                
                # 原有數據
                "input_data": input_data.model_dump(),
                "nlp_result": nlp_result.model_dump(),
                "timestamp": time.time(),
                "source_module": "nlp",
                "completion_type": "input_layer_finished"
            }
            
            # ✅ 使用事件總線發布事件
            from core.event_bus import event_bus, SystemEvent
            event_bus.publish(
                event_type=SystemEvent.INPUT_LAYER_COMPLETE,
                data=input_layer_completion_data,
                source="nlp"
            )
            
            debug_log(2, f"[NLP] 輸入層完成事件已發布 (session={session_id}, cycle={cycle_index})")
            
        except Exception as e:
            error_log(f"[NLP] 發布輸入層完成事件失敗: {e}")

    def _notify_controller_activity(self):
        """通知 Controller 有 NLP 活動 - 預留方法,目前無實作"""
        # NOTE: Controller 活動通知機制可能已變更或移除
        # 保留此方法以維持向後兼容性,實際實作待確認
        pass
    
    def _get_current_gs_id(self) -> str:
        """
        獲取當前 General Session ID
        從 working_context 的全局數據中讀取 (由 SystemLoop 設置)
        
        Returns:
            str: 當前 GS ID,如果無法獲取則返回 'unknown'
        """
        try:
            from core.working_context import working_context_manager
            gs_id = working_context_manager.global_context_data.get('current_gs_id', 'unknown')
            return gs_id
        except Exception as e:
            error_log(f"[NLP] 獲取 GS ID 失敗: {e}")
            return 'unknown'
    
    def _get_current_cycle_index(self) -> int:
        """
        獲取當前循環計數
        從 working_context 的全局數據中讀取 (由 SystemLoop 設置)
        
        Returns:
            int: 當前 cycle_index,如果無法獲取則返回 -1
        """
        try:
            from core.working_context import working_context_manager
            cycle_index = working_context_manager.global_context_data.get('current_cycle_index', -1)
            return cycle_index
        except Exception as e:
            error_log(f"[NLP] 獲取 cycle_index 失敗: {e}")
            return -1

    # === 以下是原有不當的路由邏輯，已移除 ===
    # _invoke_target_module() 和 _prepare_module_input() 方法
    # 這些邏輯應該由System Loop或Router負責，不屬於NLP模組的職責

    def _execute_state_transition(self, nlp_result: 'NLPOutput', state_result: Dict[str, Any]):
        """執行實際的狀態轉換，確保系統狀態從IDLE轉換到目標狀態"""
        try:
            # 導入狀態管理器
            from core.states.state_manager import state_manager, UEPState
            
            # 獲取當前狀態
            current_state = state_manager.get_current_state()
            debug_log(2, f"[NLP] 當前系統狀態: {current_state}")
            
            # 根據主要意圖決定目標狀態
            primary_intent = nlp_result.primary_intent
            target_state = None
            
            if primary_intent == "chat":
                target_state = UEPState.CHAT
            elif primary_intent == "command":
                target_state = UEPState.WORK
            elif primary_intent == "call":
                # CALL 類型：終止當前系統循環，跳過後續處理
                info_log(f"[NLP] 檢測到 CALL 意圖，終止當前系統循環")
                
                # 創建特殊的 CALL 回應，指示系統循環應該終止
                call_response = NLPOutput(
                    original_text=nlp_result.original_text,
                    timestamp=time.time(),
                    identity=nlp_result.identity,
                    identity_action=nlp_result.identity_action,
                    primary_intent=IntentType.CALL,
                    intent_segments=nlp_result.intent_segments,
                    overall_confidence=nlp_result.overall_confidence,
                    state_transition=None,
                    awaiting_further_input=True,  # CALL 需要等待進一步指示
                    timeout_seconds=30,  # 30秒超時
                    queue_states_added=None,
                    current_system_state=current_state.value if current_state else None,
                    processing_notes=["CALL 意圖檢測，終止當前循環"]
                )
                
                # 直接返回 CALL 回應，不進行路由
                return call_response.dict()
            elif primary_intent == "compound":
                target_state = UEPState.WORK
            
            if target_state and current_state != target_state:
                info_log(f"[NLP] 執行狀態轉換: {current_state} → {target_state}")
                
                # 設置工作上下文
                context_data = {
                    "text": nlp_result.original_text,
                    "intent": primary_intent,
                    "identity": nlp_result.identity.identity_id if nlp_result.identity else None,
                    "segments": [segment.model_dump() for segment in nlp_result.intent_segments],
                    "timestamp": nlp_result.timestamp
                }
                
                # 執行狀態轉換
                success = state_manager.set_state(target_state, context=context_data)
                
                if success:
                    info_log(f"[NLP] 狀態轉換成功: {target_state}, 上下文已設置")
                    debug_log(2, f"[NLP] 上下文資料: {context_data}")
                else:
                    error_log(f"[NLP] 狀態轉換失敗: {current_state} → {target_state}")
                    # 注意：即使狀態轉換失敗，也要繼續處理流程，避免系統卡住
            else:
                debug_log(2, f"[NLP] 無需狀態轉換或目標狀態未定義: {primary_intent}")
                
        except Exception as e:
            error_log(f"[NLP] 狀態轉換執行失敗: {e}")

    def _get_speaker_from_working_context(self) -> Optional[Dict[str, Any]]:
        """從Working Context獲取當前說話人資料"""
        try:
            from core.working_context import working_context_manager, ContextType
            
            # 尋找最近的SPEAKER_ACCUMULATION上下文
            contexts = working_context_manager.get_contexts_by_type(ContextType.SPEAKER_ACCUMULATION)
            
            if not contexts:
                debug_log(3, "[NLP] Working Context中無說話人資料")
                return None
            
            # 獲取最新的上下文 - contexts 是 WorkingContext 物件列表
            latest_context = max(contexts, key=lambda c: c.created_at)
            context_data = working_context_manager.get_context_data(latest_context.context_id)
            
            # 防禦性檢查: context_data 可能為 None
            if context_data is None:
                debug_log(3, "[NLP] Working Context返回的資料為空")
                return None
            
            speaker_data = context_data.get('current_speaker')
            if speaker_data:
                debug_log(2, f"[NLP] 從Working Context獲取說話人: {speaker_data.get('speaker_id')}")
                return speaker_data
            else:
                debug_log(3, "[NLP] Working Context中無當前說話人資料")
                return None
                
        except Exception as e:
            error_log(f"[NLP] 從Working Context獲取說話人失敗: {e}")
            return None

    def _create_default_identity(self) -> Optional['UserProfile']:
        """創建預設身份，用於文字輸入模式 (不進行身份識別和查詢)"""
        try:
            from .schemas import UserProfile, IdentityStatus
            from datetime import datetime
            
            # 使用固定的預設身份ID,避免重複創建
            default_id = "default_text_user"
            
            default_identity = UserProfile(
                identity_id=default_id,
                speaker_id=None,  # 文字輸入模式沒有語者ID
                display_name="預設用戶",
                status=IdentityStatus.TEMPORARY,
                memory_token=None,  # 預設身份沒有記憶令牌
                created_at=datetime.now(),
                last_interaction=datetime.now(),
                total_interactions=0,
                preferences={},
                system_habits={},
                voice_preferences={},
                conversation_style={}
            )
            
            debug_log(2, f"[NLP] 使用預設身份: {default_id} (文字輸入模式)")
            return default_identity
            
        except Exception as e:
            error_log(f"[NLP] 創建預設身份失敗: {e}")
            return None
    
    def _create_generic_identity(self) -> Optional['UserProfile']:
        """創建通用身份，用於說話人累積期間或無身份識別時"""
        try:
            from .schemas import UserProfile, IdentityStatus
            from datetime import datetime
            import uuid
            
            generic_id = f"generic_{uuid.uuid4().hex[:8]}"
            
            generic_identity = UserProfile(
                identity_id=generic_id,
                speaker_id=None,  # 通用身份沒有對應的語者ID
                display_name="通用用戶",
                status=IdentityStatus.TEMPORARY,
                memory_token=None,  # 通用身份沒有記憶令牌
                created_at=datetime.now(),
                last_interaction=datetime.now(),
                total_interactions=0,  # 初始化互動次數
                preferences={},
                system_habits={},
                voice_preferences={},
                conversation_style={}
            )
            
            debug_log(2, f"[NLP] 創建通用身份: {generic_id}")
            return generic_identity
            
        except Exception as e:
            error_log(f"[NLP] 創建通用身份失敗: {e}")
            return None

    def get_module_info(self) -> Dict[str, Any]:
        """獲取模組資訊"""
        return {
            "module_id": "nlp_module",
            "version": "2.1.0",  # 版本更新 
            "status": "active" if self.is_initialized else "inactive",
            "capabilities": self.get_capabilities(),
            "description": "自然語言處理模組 - 支援增強型身份管理、記憶令牌、使用者偏好與多模組集成"
        }
