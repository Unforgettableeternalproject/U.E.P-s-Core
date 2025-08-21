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
from typing import Dict, Any, Optional, List

from core.module_base import BaseModule
from core.schemas import NLPModuleData, create_nlp_data
from core.schema_adapter import NLPSchemaAdapter
from core.working_context import working_context_manager, ContextType
from core.state_queue import get_state_queue_manager, SystemState
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
        self.schema_adapter = NLPSchemaAdapter()
        
        # 模組狀態
        self.is_initialized = False
        
        info_log("[NLP] NLP模組 Phase 2 初始化")
    
    def debug(self):
        """除錯資訊輸出"""
        debug_log(1, "[NLP] Debug 模式啟用")
        debug_log(2, f"[NLP] 模組設定: {self.config}")
        debug_log(3, f"[NLP] 身份管理器狀態: {'已載入' if self.identity_manager else '未載入'}")
        debug_log(3, f"[NLP] 意圖分析器狀態: {'已載入' if self.intent_analyzer else '未載入'}")
    
    def initialize(self) -> bool:
        """初始化模組"""
        try:
            debug_log(1, "[NLP] 開始初始化...")
            self.debug()
            
            # 初始化身份管理器
            identity_storage_path = self.config.get("identity_storage_path", "memory/identities")
            self.identity_manager = IdentityManager(identity_storage_path)
            
            # 註冊身份決策處理器到Working Context
            decision_handler = self.identity_manager.get_decision_handler()
            working_context_manager.register_decision_handler(
                ContextType.SPEAKER_ACCUMULATION, 
                decision_handler
            )
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
    
    def handle(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """處理NLP請求"""
        try:
            # 驗證輸入
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
            
            return final_result.dict()
            
        except Exception as e:
            error_log(f"[NLP] 處理失敗：{e}")
            return self._create_error_response(str(e))
    
    def _process_speaker_identity(self, input_data: NLPInput) -> Dict[str, Any]:
        """處理語者身份識別"""
        result = {
            "identity": None,
            "identity_action": None,
            "processing_notes": []
        }
        
        try:
            if not input_data.speaker_id or not input_data.enable_identity_processing:
                result["processing_notes"].append("跳過身份處理")
                return result
            
            # 使用身份管理器處理語者識別
            identity, action = self.identity_manager.process_speaker_identification(
                input_data.speaker_id,
                input_data.speaker_status or "unknown",
                input_data.speaker_confidence or 0.0
            )
            
            result["identity"] = identity
            result["identity_action"] = action
            
            if action == "accumulating":
                # 語者樣本累積狀態，添加到Working Context
                self._add_to_speaker_accumulation(input_data)
                result["processing_notes"].append("語者樣本累積中")
                
            elif action == "loaded":
                result["processing_notes"].append(f"載入身份：{identity.display_name}")
                
            debug_log(3, f"[NLP] 語者身份處理：{action}, 身份={identity.identity_id if identity else 'None'}")
            
        except Exception as e:
            error_log(f"[NLP] 語者身份處理失敗：{e}")
            result["processing_notes"].append(f"身份處理錯誤：{str(e)}")
        
        return result
    
    def _analyze_intent(self, input_data: NLPInput, identity: Optional[UserProfile]) -> Dict[str, Any]:
        """分析文本意圖"""
        try:
            # 準備上下文
            context = {
                "current_system_state": input_data.current_system_state,
                "conversation_history": input_data.conversation_history,
                "identity": identity.dict() if identity else None
            }
            
            # 執行意圖分析
            result = self.intent_analyzer.analyze_intent(
                input_data.text,
                enable_segmentation=input_data.enable_segmentation,
                context=context
            )
            
            debug_log(3, f"[NLP] 意圖分析完成：{result['primary_intent']}, "
                       f"片段數={len(result['intent_segments'])}")
            
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
    
    def _update_interaction_history(self, identity: UserProfile, result: NLPOutput):
        """更新使用者互動歷史"""
        try:
            interaction_data = {
                "intent": result.primary_intent,
                "text_length": len(result.original_text),
                "confidence": result.overall_confidence,
                "command_type": None
            }
            
            # 提取指令類型
            if result.primary_intent in [IntentType.COMMAND, IntentType.COMPOUND]:
                entities = []
                for segment in result.intent_segments:
                    entities.extend(segment.entities)
                
                if entities:
                    interaction_data["command_type"] = entities[0].get("entity_type")
            
            self.identity_manager.update_identity_interaction(
                identity.identity_id, 
                interaction_data
            )
            
        except Exception as e:
            debug_log(3, f"[NLP] 更新互動歷史失敗：{e}")
    
    def _create_error_response(self, error_message: str) -> Dict[str, Any]:
        """創建錯誤回應"""
        return NLPOutput(
            original_text="",
            primary_intent=IntentType.UNKNOWN,
            intent_segments=[],
            overall_confidence=0.0,
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
            "state_transition_suggestion"
        ]
    
    def get_module_info(self) -> Dict[str, Any]:
        """獲取模組資訊"""
        return {
            "module_id": "nlp_module",
            "version": "2.0.0", 
            "status": "active" if self.is_initialized else "inactive",
            "capabilities": self.get_capabilities(),
            "description": "自然語言處理模組 - 支援語者身份管理與分段意圖分析"
        }
