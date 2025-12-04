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

from configs.user_settings_manager import get_user_setting
from core.bases.module_base import BaseModule
from core.schemas import NLPModuleData
from core.working_context import working_context_manager, ContextType
from core.states.state_queue import get_state_queue_manager
from core.states.state_manager import UEPState as SystemState
from utils.debug_helper import debug_log, info_log, error_log

from .schemas import (
    NLPInput, NLPOutput, UserProfile, IdentityStatus,
    SystemStateTransition
)
from .intent_types import IntentType  # Use Stage 4 IntentType
from .identity_manager import IdentityManager
from .intent_analyzer import IntentAnalyzer
from .multi_intent_context import get_multi_intent_context_manager
from .intent_segmenter import get_intent_segmenter
from .intent_types import IntentSegment


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
            
            # 🆕 註冊配置變更回調 - 監聽 STT 模式變更
            from configs.user_settings_manager import user_settings_manager
            def on_stt_config_change(key_path: str, value: Any) -> bool:
                """當 STT 配置變更時的回調"""
                if key_path == "interaction.speech_input.enabled":
                    mode = "VAD" if value else "文字輸入"
                    debug_log(2, f"[NLP] 檢測到 STT 模式變更：現在為 {mode} 模式")
                    # 清除啟動標記，因為模式改變了
                    working_context_manager.clear_activation_flag()
                    debug_log(2, "[NLP] 已清除啟動標記（STT 模式已變更）")
                return True
            
            user_settings_manager.register_reload_callback("nlp", on_stt_config_change)
            debug_log(2, "[NLP] 已註冊配置變更回調（監聽 STT 模式變更）")
            
            # 初始化意圖分析器 (Stage 3 - deprecated, kept for compatibility)
            try:
                self.intent_analyzer = IntentAnalyzer(self.config)
                if not self.intent_analyzer.initialize():
                    debug_log(2, "[NLP] ⚠️  Stage 3 IntentAnalyzer 初始化失敗，將僅使用 Stage 4 IntentSegmenter")
                    self.intent_analyzer = None
            except Exception as e:
                debug_log(2, f"[NLP] ⚠️  Stage 3 IntentAnalyzer 跳過: {e}")
                self.intent_analyzer = None
            
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
            
            # 🔧 在處理開始時獲取並保存 session_id 和 cycle_index
            self._current_processing_session_id = self._get_current_gs_id()
            self._current_processing_cycle_index = self._get_current_cycle_index()
            
            # 驗證輸入 - 支援字典或 NLPInput 物件
            if isinstance(data, NLPInput):
                validated_input = data
            else:
                validated_input = NLPInput(**data)
            debug_log(2, f"[NLP] 接收到請求：文本長度={len(validated_input.text)}, "
                       f"語者ID={validated_input.speaker_id}")
            debug_log(3, f"[NLP] 記錄處理上下文: session={self._current_processing_session_id}, cycle={self._current_processing_cycle_index}")
            
            # 第一階段：語者身份處理
            identity_result = self._process_speaker_identity(validated_input)
            
            # 第二階段：意圖分析
            intent_result = self._analyze_intent(validated_input, identity_result.get("identity"))
            
            # 第三階段：系統狀態處理
            state_result = self._process_system_state(intent_result, validated_input)
            
            # 如果有 segment 被 SYS 更正，更新 intent_result
            if "corrected_segments" in state_result and state_result["corrected_segments"]:
                corrected_segments = state_result["corrected_segments"]
                # 更新 intent_segments
                for old_seg, new_seg in corrected_segments:
                    # 在 intent_result 的 segments 中找到並替換
                    for i, seg in enumerate(intent_result["intent_segments"]):
                        if seg is old_seg:
                            intent_result["intent_segments"][i] = new_seg
                            break
                
                # 重新計算 primary_intent（取最高優先級的 segment）
                from .intent_types import IntentSegment as NewIntentSegment
                if intent_result["intent_segments"]:
                    primary_segment = NewIntentSegment.get_highest_priority_segment(intent_result["intent_segments"])
                    intent_result["primary_intent"] = primary_segment.intent_type
                    debug_log(2, f"[NLP] Updated primary_intent after SYS correction: {primary_segment.intent_type.name}")
            
            # 如果狀態處理後有過濾的 segments（例如 COMPOUND 過濾掉 CALL），更新 primary_intent
            if "filtered_segments" in state_result and state_result["filtered_segments"]:
                from .intent_types import IntentSegment as NewIntentSegment
                filtered_segs = state_result["filtered_segments"]
                if filtered_segs:
                    primary_segment = NewIntentSegment.get_highest_priority_segment(filtered_segs)
                    intent_result["primary_intent"] = primary_segment.intent_type
                    debug_log(2, f"[NLP] Updated primary_intent after filtering: {primary_segment.intent_type.name}")
            
            # 組合結果
            final_result = self._combine_results(validated_input, identity_result, 
                                               intent_result, state_result)
            
            # 🆕 檢查是否為等待啟動狀態 - 不記錄互動
            if intent_result.get("awaiting_activation"):
                debug_log(2, "[NLP] 等待啟動中，不記錄互動歷史")
            else:
                # 更新使用者互動記錄（僅在實際處理時記錄）
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
        """處理語者身份識別 - 從Working Context獲取說話人資料
        
        新架構（Identity 為主，Speaker 為輔）：
        1. 優先檢查 declared_identity（主動聲明）
        2. 如果有 declared_identity，直接添加 Speaker 樣本到該 Identity
        3. 如果沒有，使用原有的被動推斷邏輯
        """
        result = {
            "identity": None,
            "identity_action": None,
            "processing_notes": []
        }
        
        try:
            # 檢查是否為文字輸入模式 (繞過說話人識別)
            metadata = getattr(input_data, 'metadata', {})
            # 🆕 優先檢查是否有主動聲明的 Identity（不管是否文字輸入）
            from core.working_context import working_context_manager
            
            # 從 global_context_data 獲取 current_identity_id
            declared_identity_id = working_context_manager.global_context_data.get('current_identity_id')
            
            if declared_identity_id and declared_identity_id != 'unknown':
                # 方案 A: 主動聲明模式
                debug_log(2, f"[NLP] 檢測到主動聲明的 Identity: {declared_identity_id}")
                result.update(self._handle_declared_identity(declared_identity_id, input_data))
                return result
            
            # 檢查是否為文字輸入模式
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
            
            # 方案 B: 被動推斷模式（原有邏輯）
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
            
            # 🆕 如果確認了正式 Identity，同步 StatusManager
            if identity and action in ["existing", "newly_created"]:
                from core.status_manager import status_manager
                status_manager.switch_identity(identity.identity_id)
                
                # 同步 Identity 的 last_interaction 到 StatusManager
                if hasattr(identity, 'last_interaction') and identity.last_interaction:
                    from datetime import datetime
                    if isinstance(identity.last_interaction, datetime):
                        status_manager.status.last_interaction_time = identity.last_interaction.timestamp()
                        debug_log(3, f"[NLP] 同步 last_interaction 到 StatusManager: {identity.last_interaction}")
                
                debug_log(2, f"[NLP] 已切換 StatusManager 到 Identity: {identity.identity_id}")
            
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
        """
        分析文本意圖 - Stage 4 使用 IntentSegmenter
        
        Returns intent segments from BIOS Tagger model
        """
        try:
            # Use new IntentSegmenter for Stage 4
            intent_segmenter = get_intent_segmenter()
            segments = intent_segmenter.segment_intents(input_data.text)
            
            if not segments:
                error_log("[NLP] IntentSegmenter returned no segments")
                return {
                    "intent_segments": [],
                    "primary_intent": IntentType.UNKNOWN,
                    "overall_confidence": 0.0,
                    "entities": [],
                    "state_transition": None
                }
            
            # ⚠️ 暫時禁用 MCP 意圖校正 - 完全依賴 BIO 模型
            # 原因：關鍵字匹配有嚴重 bug（子字串匹配問題），可能導致錯誤識別
            # 例如："Show me the time" 被錯誤匹配到 "summarize_tag" (因為 "time" in "summarize")
            # TODO: 未來考慮從 YAML 讀取完整工作流信息進行更精確的匹配
            # corrected_segments = []
            # for segment in segments:
            #     corrected_segment = self._correct_intent_with_mcp(segment)
            #     corrected_segments.append(corrected_segment)
            # segments = corrected_segments
            
            # 🆕 暱稱檢測：如果輸入包含 UEP 暱稱，視為包含 CALL 意圖
            from configs.user_settings_manager import get_user_setting
            uep_nickname = get_user_setting("general.identity.uep_nickname", None)
            has_nickname_call = False
            
            if uep_nickname and uep_nickname.strip():
                # 檢查是否包含暱稱（不區分大小寫）
                # 🔧 使用完整單詞匹配，避免子字串誤判
                import re
                text_lower = input_data.text.lower()
                nickname_lower = uep_nickname.strip().lower()
                # 使用單詞邊界 \b 確保完整匹配（支援中文和英文）
                pattern = r'\b' + re.escape(nickname_lower) + r'\b'
                if re.search(pattern, text_lower):
                    has_nickname_call = True
                    debug_log(2, f"[NLP] 檢測到暱稱 '{uep_nickname}'，視為 CALL 意圖")
                    
                    # 如果 segments 中沒有 CALL，添加一個 CALL segment
                    has_call = any(s.intent_type == IntentType.CALL for s in segments)
                    if not has_call:
                        from .intent_types import IntentSegment as NewIntentSegment
                        nickname_call_seg = NewIntentSegment(
                            segment_text=uep_nickname,
                            intent_type=IntentType.CALL,
                            confidence=0.95,
                            priority=70
                        )
                        segments.insert(0, nickname_call_seg)
                        debug_log(2, "[NLP] 已添加 CALL segment 至意圖列表")
            
            # 🆕 CALL 意圖處理邏輯（新版）
            # 1. 如果輸入只有 CALL 意圖，設置啟動標記並中斷循環
            # 2. 如果為複合意圖且包含 CALL，設置啟動標記並正常處理其他意圖
            # 3. 如果沒有 CALL 且未啟動，忽略輸入（但文字輸入模式除外）
            from .intent_types import IntentSegment as NewIntentSegment
            from core.working_context import working_context_manager
            
            filtered_segments = segments
            has_call = any(s.intent_type == IntentType.CALL for s in segments)
            non_call_segs = [s for s in segments if s.intent_type != IntentType.CALL and s.intent_type != IntentType.UNKNOWN]
            
            # 檢查是否為 VAD 模式（基於使用者設定）
            speech_input_enabled = get_user_setting("interaction.speech_input.enabled", True)
            is_vad_mode = speech_input_enabled
            
            # 檢查是否已啟動或有活躍會話
            is_activated = working_context_manager.is_activated()
            from core.sessions.session_manager import session_manager
            has_active_session = (len(session_manager.get_active_chatting_sessions()) > 0 or 
                                len(session_manager.get_active_workflow_sessions()) > 0)
            
            if has_call:
                # 設置啟動標記
                working_context_manager.set_activation_flag(True)
                debug_log(2, "[NLP] 檢測到 CALL 意圖，已設置啟動標記")
                
                if non_call_segs:
                    # 複合意圖：過濾掉 CALL，保留其他意圖
                    debug_log(2, f"[NLP] COMPOUND with CALL: 過濾 CALL，保留 {len(non_call_segs)} 個實質意圖")
                    filtered_segments = non_call_segs
                else:
                    # 只有 CALL：保留 CALL segment，但會在後續處理中中斷循環
                    debug_log(2, "[NLP] 只有 CALL 意圖，保留並等待下次輸入")
                    filtered_segments = segments
            else:
                # 沒有 CALL 意圖
                # VAD 模式下需要檢查啟動狀態；文字輸入模式則直接處理
                if is_vad_mode and not is_activated and not has_active_session:
                    # VAD 模式下未啟動且無活躍會話：返回空 segments，由後續處理設置 skip_input_layer
                    debug_log(1, "[NLP] VAD 模式下無 CALL 意圖且未啟動，返回空 segments 以中斷循環")
                    return {
                        "intent_segments": [],  # 空 segments 會被視為需要跳過
                        "primary_intent": IntentType.UNKNOWN,
                        "overall_confidence": 0.0,
                        "entities": [],
                        "state_transition": None,
                        "awaiting_activation": True  # 標記為等待啟動
                    }
                else:
                    # 文字輸入模式、已啟動或有活躍會話：正常處理
                    if not is_vad_mode:
                        debug_log(2, "[NLP] 文字輸入模式，繞過啟動檢查，正常處理")
                    else:
                        debug_log(2, "[NLP] 已啟動或有活躍會話，正常處理輸入")
                    filtered_segments = segments
            
            # Determine primary intent (highest priority from filtered segments)
            if NewIntentSegment.is_compound_input(filtered_segments):
                primary_segment = NewIntentSegment.get_highest_priority_segment(filtered_segments)
                primary_intent_type = primary_segment.intent_type if primary_segment else filtered_segments[0].intent_type
            else:
                primary_intent_type = filtered_segments[0].intent_type
            
            # Use Stage 4 IntentType directly (no mapping needed)
            # Primary intent is now directly from Stage 4
            primary_intent = primary_intent_type
            
            # 使用過濾後的 segments 作為最終結果
            segments = filtered_segments
            
            # Calculate overall confidence (average of all segments)
            overall_confidence = sum(s.confidence for s in segments) / len(segments)
            
            debug_log(2, f"[NLP] IntentSegmenter analysis: {len(segments)} segment(s), "
                         f"primary={primary_intent_type.name}, confidence={overall_confidence:.3f}")
            for i, seg in enumerate(segments):
                debug_log(3, f"[NLP]   Segment {i+1}: '{seg.segment_text}' -> {seg.intent_type.name} "
                             f"(priority={seg.priority}, conf={seg.confidence:.3f})")
            
            return {
                "intent_segments": segments,  # List[IntentSegment] from Stage 4
                "primary_intent": primary_intent,
                "overall_confidence": overall_confidence,
                "entities": [],  # Entity extraction not implemented yet
                "state_transition": None
            }
            
        except Exception as e:
            error_log(f"[NLP] 意圖分析失敗：{e}")
            import traceback
            error_log(f"[NLP] Traceback: {traceback.format_exc()}")
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
            original_intent = intent_result["primary_intent"]
            context_ids = intent_result.get("context_ids", [])
            execution_plan = intent_result.get("execution_plan", [])
            
            result["context_ids"] = context_ids
            result["execution_plan"] = execution_plan
            
            # 直接使用原始 intent，不再覆蓋
            # 新架構：BW/DW 都會立即中斷 CS 並處理 WORK
            primary_intent = original_intent
            
            # 根據意圖決定下一步處理
            if primary_intent == IntentType.CALL:
                # call類型：等待進一步指示
                result["awaiting_input"] = True
                result["timeout_seconds"] = 30  # 30秒超時
                result["next_modules"] = []  # 暫不轉發到其他模組
                
            elif primary_intent == IntentType.CHAT:
                # 聊天類型：轉發到MEM和LLM
                result["next_modules"] = ["mem_module", "llm_module"]
                
            elif primary_intent == IntentType.WORK:
                # 工作類型：僅轉發到LLM（Cycle 0 三階段：LLM決策→SYS執行→LLM回應）
                # MEM 不參與 WORK 模式，SYS 在第二階段由 ModuleCoordinator 調用
                result["next_modules"] = ["llm_module"]
            
            elif primary_intent == IntentType.RESPONSE:
                # 工作流回應類型：僅轉發到LLM（用於工作流輸入步驟）
                result["next_modules"] = ["llm_module"]
                debug_log(3, "[NLP] RESPONSE intent - routing to LLM for workflow input processing")
                
            elif primary_intent == IntentType.UNKNOWN:
                # 未知內容：可能轉發到LLM進行處理
                result["next_modules"] = ["llm_module"]
            
            # 將意圖分段添加到狀態佇列
            queue_result = self._process_intent_to_state_queue(intent_result)
            result["added_states"] = queue_result.get("added_states", [])
            result["corrected_segments"] = queue_result.get("corrected_segments", [])
            result["session_control"] = queue_result.get("session_control")  # 保存 session_control 信息
            
            debug_log(3, f"[NLP] 系統狀態處理：下一步模組={result['next_modules']}, "
                       f"等待輸入={result['awaiting_input']}, 上下文數={len(context_ids)}")
            
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
    
    def _process_intent_to_state_queue(self, intent_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        將意圖分析結果轉換為系統狀態並添加到佇列
        
        Stage 4: Uses session state restrictions per NLP狀態處理.md
        
        Returns:
            Dict with "added_states" (list) and "corrected_segments" (list)
        """
        try:
            intent_segments = intent_result.get("intent_segments", [])
            
            # 🔧 檢查是否為等待啟動狀態（無 CALL 且未啟動）
            if not intent_segments and intent_result.get("awaiting_activation"):
                debug_log(2, "[NLP] Awaiting activation - no segments to process")
                # ⚠️ 不設置 skip_input_layer，讓下一個循環正常進入輸入層
                return {"added_states": [], "corrected_segments": []}
            
            if not intent_segments:
                debug_log(2, "[NLP] No intent segments to process")
                return {"added_states": [], "corrected_segments": []}
            
            # Check current session state
            session_state = self._check_session_state()
            debug_log(2, f"[NLP] Session state: {session_state}")
            
            # Route to appropriate handler based on session state
            from core.sessions.session_manager import session_manager
            
            if session_state == 'no_session':
                # No active CS or WS
                result = self._process_no_session_state(intent_segments)
                
            elif session_state == 'active_cs':
                # Active chatting session
                cs_sessions = session_manager.get_active_chatting_sessions()
                result = self._process_active_cs_state(intent_segments, cs_sessions)
                
                # Handle CS pause if needed
                if result.get("interrupt_cs"):
                    for session_id in result.get("pause_cs_sessions", []):
                        try:
                            cs_session = session_manager.get_chatting_session(session_id)
                            if cs_session and hasattr(cs_session, 'pause'):
                                cs_session.pause()
                                info_log(f"[NLP] Paused CS: {session_id}")
                        except Exception as e:
                            error_log(f"[NLP] Failed to pause CS {session_id}: {e}")
                
            elif session_state == 'active_ws':
                # Active workflow session
                ws_sessions = session_manager.get_active_workflow_sessions()
                result = self._process_active_ws_state(intent_segments, ws_sessions)
                
                # In WS, inputs are handled by workflow logic, not state queue
                # Store metadata in working context for downstream processing
                if result.get("response_metadata"):
                    working_context_manager.set_context_data(
                        "ws_response_metadata",
                        result["response_metadata"]
                    )
                    debug_log(3, f"[NLP] Set WS response metadata: {result['response_metadata']}")
                
                # ✅ 如果工作流輸入模式修正了意圖，應用修正
                corrected_segments = []
                if result.get("corrected_intent"):
                    corrected_intent = result["corrected_intent"]
                    # 創建新的 segment 對象並替換意圖 (使用 intent_types.IntentSegment - dataclass)
                    from .intent_types import IntentSegment as NewIntentSegment
                    for seg in intent_segments:
                        # 使用 dataclass 構造方式
                        new_seg = NewIntentSegment(
                            segment_text=seg.segment_text,
                            intent_type=corrected_intent,  # ✅ dataclass 使用 intent_type
                            confidence=seg.confidence,
                            priority=seg.priority,
                            metadata=seg.metadata
                        )
                        corrected_segments.append((seg, new_seg))  # ✅ 返回 (old, new) tuple
                    debug_log(2, f"[NLP] Applied intent correction: {corrected_intent}")
                
                # Don't add states in WS mode
                return {"added_states": [], "corrected_segments": corrected_segments}
            
            else:
                error_log(f"[NLP] Unknown session state: {session_state}")
                return {"added_states": [], "corrected_segments": []}
            
            # Set skip_input_layer flag if needed
            if result.get("skip_input_layer"):
                working_context_manager.set_context_data("skip_input_layer", True)
                debug_log(2, "[NLP] Set skip_input_layer=True for interrupt")
            
            # Add states to queue
            # 不合併相同類型的狀態，因為用戶可能有多個不同的主題或任務要處理
            added_states = []
            
            for state_info in result.get("states_to_add", []):
                segment = state_info.get("segment")  # Resume 狀態可能沒有 segment
                state_type_str = state_info["state_type"]
                priority = state_info["priority"]
                work_mode = state_info.get("work_mode")
                is_resume = state_info.get("is_resume", False)
                resume_context = state_info.get("resume_context")
                
                # Map state_type string to UEPState enum
                if state_type_str == "CHAT":
                    state_type = SystemState.CHAT
                elif state_type_str == "WORK":
                    state_type = SystemState.WORK
                else:
                    error_log(f"[NLP] Unknown state type: {state_type_str}")
                    continue
                
                # 準備狀態數據
                if is_resume:
                    # Resume 狀態：使用保存的上下文
                    trigger_text = "恢復對話會話"
                    metadata = {
                        "is_resume": True,
                        "resume_context": resume_context,
                        "intent_type": "CHAT"  # 就是普通 CHAT 狀態，只是帶有 resume 標記
                    }
                    debug_log(2, f"[NLP] 添加 resume CHAT 狀態到佇列 (priority={priority})")
                else:
                    # 正常狀態：使用 segment 的文本
                    trigger_text = segment.segment_text if segment else ""
                    metadata = {
                        "intent_type": segment.intent_type.name if segment else "UNKNOWN",
                        "confidence": segment.confidence if segment else 0.0
                    }
                
                # Add to state queue
                self.state_queue_manager.add_state(
                    state_type,
                    trigger_content=trigger_text,
                    context_content=trigger_text,
                    metadata=metadata,
                    custom_priority=priority,
                    work_mode=work_mode
                )
                added_states.append(state_type)
                
                if is_resume:
                    debug_log(3, f"[NLP] Added resume state: {state_type.value} (priority={priority})")
                else:
                    debug_log(3, f"[NLP] Added state: {state_type.value} (priority={priority}, work_mode={work_mode})")
            
            if added_states:
                info_log(f"[NLP] Added {len(added_states)} state(s) to queue: "
                         f"{[s.value for s in added_states]}")
                debug_log(3, f"[NLP] Queue status: {self.state_queue_manager.get_queue_status()}")
            else:
                debug_log(2, "[NLP] No states added to queue")
            
            # Log processing notes
            for note in result.get("processing_notes", []):
                debug_log(3, f"[NLP] {note}")
            
            # 返回字典包含 added_states、corrected_segments、filtered_segments 和 session_control
            return_dict = {
                "added_states": added_states,
                "corrected_segments": result.get("corrected_segments", []),
                "filtered_segments": result.get("filtered_segments", []),
                "session_control": result.get("session_control")  # 🆕 傳遞 session_control
            }
            return return_dict
                
        except Exception as e:
            error_log(f"[NLP] State queue processing failed: {e}")
            import traceback
            error_log(f"[NLP] Traceback: {traceback.format_exc()}")
            return {"added_states": [], "corrected_segments": []}
    
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
        
        # 🔍 調試：檢查 session_control
        session_control_value = state_result.get("session_control")
        if session_control_value:
            debug_log(1, f"[NLP] _combine_results: session_control = {session_control_value}")
        else:
            debug_log(3, "[NLP] _combine_results: session_control is None")
        
        # Convert intent_types.IntentSegment (dataclass) to schemas.IntentSegment (Pydantic model)
        from modules.nlp_module.schemas import IntentSegment as SchemaIntentSegment
        pydantic_segments = []
        for seg in intent_segments:
            # Create Pydantic model from dataclass
            pydantic_seg = SchemaIntentSegment(
                text=seg.segment_text,
                intent=seg.intent_type,  # This is already IntentType enum
                confidence=seg.confidence,
                start_pos=seg.metadata.get('start_pos', 0) if seg.metadata else 0,
                end_pos=seg.metadata.get('end_pos', len(seg.segment_text)) if seg.metadata else len(seg.segment_text),
                entities=seg.metadata.get('entities', []) if seg.metadata else [],
                context_hints=seg.metadata.get('context_hints', []) if seg.metadata else []
            )
            pydantic_segments.append(pydantic_seg)
        
        return NLPOutput(
            original_text=input_data.text,
            timestamp=time.time(),
            identity=identity_result.get("identity"),
            identity_action=identity_result.get("identity_action"),
            primary_intent=intent_result.get("primary_intent", IntentType.UNKNOWN),
            intent_segments=pydantic_segments,
            overall_confidence=intent_result.get("overall_confidence", 0.0),
            state_transition=state_transition,
            next_modules=state_result.get("next_modules", []),
            processing_notes=processing_notes,
            awaiting_further_input=state_result.get("awaiting_input", False),
            timeout_seconds=state_result.get("timeout_seconds"),
            queue_states_added=added_states,
            current_system_state=queue_status.get("current_state"),
            session_control=state_result.get("session_control")  # 🆕 傳遞 session_control
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
            if result.primary_intent == IntentType.WORK:
                entities = []
                for segment in result.intent_segments:
                    entities.extend(segment.entities)
                
                if entities:
                    interaction_data["command_type"] = entities[0].get("entity_type")
                    
                # 標記為 SYS 模組互動
                interaction_data["module"] = "sys"
            
            # 如果是聊天類型，則標記為 LLM 模組互動
            elif result.primary_intent == IntentType.CHAT:
                interaction_data["module"] = "llm"
            
            # 更新互動記錄
            self.identity_manager.update_identity_interaction(  # type: ignore
                identity.identity_id, 
                interaction_data
            )
            
            # 🆕 更新 StatusManager 的 last_interaction_time
            # 確保系統不會因為長時間無互動而進入 SLEEP
            from core.status_manager import StatusManager
            status_manager = StatusManager()
            
            # 判斷互動是否成功（confidence > 0.5 視為成功）
            is_successful = result.overall_confidence > 0.5
            task_type = interaction_data["module"]
            
            status_manager.record_interaction(
                successful=is_successful,
                task_type=task_type
            )
            
            debug_log(3, f"[NLP] 已記錄互動時間 (Identity: {identity.identity_id}, "
                       f"Type: {task_type}, Success: {is_successful})")
            
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
            processing_notes=[f"處理錯誤：{error_message}"],
            session_control=None
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
    
    def _check_session_state(self) -> str:
        """
        Check current session state for Stage 4 intent priority logic
        
        Returns:
            'no_session': No active CS or WS
            'active_cs': Active chatting session
            'active_ws': Active workflow session
        """
        try:
            from core.sessions.session_manager import session_manager
            
            # Check for active workflow sessions first (higher priority)
            active_ws = session_manager.get_active_workflow_sessions()
            if active_ws:
                debug_log(2, f"[NLP] Active WS detected: {len(active_ws)} session(s)")
                return 'active_ws'
            
            # Check for active chatting sessions
            active_cs = session_manager.get_active_chatting_sessions()
            if active_cs:
                debug_log(2, f"[NLP] Active CS detected: {len(active_cs)} session(s)")
                return 'active_cs'
            
            debug_log(2, "[NLP] No active sessions detected")
            return 'no_session'
            
        except Exception as e:
            error_log(f"[NLP] Error checking session state: {e}")
            return 'no_session'
    
    def _process_no_session_state(self, segments: List[IntentSegment]) -> Dict[str, Any]:
        """
        Process intents when no CS or WS is active
        
        Rules per NLP狀態處理.md:
        - DW and BW both treated as COMMAND
        - CALL: Ignore input, interrupt loop, expect CHAT/COMMAND next
        - CHAT: Add CHAT state to queue
        - COMMAND (DW/BW): Add WORK state to queue
        - UNKNOWN: Ignore input, interrupt loop
        - COMPOUND: Apply filtering rules, add states by priority
        """
        result = {
            "states_to_add": [],
            "skip_input_layer": False,
            "processing_notes": []
        }
        
        try:
            # Filter out UNKNOWN segments
            valid_segments = [s for s in segments if s.intent_type != IntentType.UNKNOWN]
            
            if not valid_segments:
                # All segments are UNKNOWN - ignore input and interrupt
                result["skip_input_layer"] = True
                result["processing_notes"].append("All segments UNKNOWN - ignoring input")
                debug_log(2, "[NLP] No session: All UNKNOWN segments, interrupting")
                return result
            
            # Check for COMPOUND (multiple segments after filtering)
            if len(valid_segments) > 1:
                result["processing_notes"].append(f"COMPOUND intent detected: {len(valid_segments)} segments")
                # Apply COMPOUND filtering rules
                valid_segments = self._filter_compound_no_session(valid_segments)
                # 保存過濾後的 segments，用於更新 primary_intent
                result["filtered_segments"] = valid_segments
            
            # Process each segment
            for segment in valid_segments:
                if segment.intent_type == IntentType.CALL:
                    # CALL: Ignore input, interrupt loop
                    result["skip_input_layer"] = True
                    result["processing_notes"].append("CALL detected - interrupting for next input")
                    debug_log(2, "[NLP] No session: CALL - interrupting loop")
                    
                elif segment.intent_type == IntentType.CHAT:
                    # CHAT: Add CHAT state
                    result["states_to_add"].append({
                        "segment": segment,
                        "state_type": "CHAT",
                        "priority": segment.priority
                    })
                    debug_log(2, f"[NLP] No session: CHAT - adding CHAT state (priority={segment.priority})")
                    
                elif segment.intent_type == IntentType.WORK:
                    # WORK: work_mode 已在 _correct_intent_with_mcp 中設定
                    work_mode = segment.metadata.get('work_mode', 'background') if segment.metadata else 'background'
                    final_priority = 100 if work_mode == "direct" else 30
                    
                    result["states_to_add"].append({
                        "segment": segment,
                        "state_type": "WORK",
                        "priority": final_priority,
                        "work_mode": work_mode
                    })
                    debug_log(2, f"[NLP] No session: WORK - adding WORK state (mode={work_mode}, priority={final_priority})")
            
        except Exception as e:
            error_log(f"[NLP] Error in _process_no_session_state: {e}")
            result["processing_notes"].append(f"Error: {str(e)}")
        
        return result
    
    def _filter_compound_no_session(self, segments: List[IntentSegment]) -> List[IntentSegment]:
        """
        Filter COMPOUND intents for no session state
        
        Rules:
        - CALL + CHAT/COMMAND: Ignore CALL, process CHAT/COMMAND
        - CHAT + COMMAND: Prioritize COMMAND (add first regardless of DW/BW)
        - CALL + UNKNOWN: Ignore UNKNOWN, process CALL
        """
        # Separate by type
        call_segs = [s for s in segments if s.intent_type == IntentType.CALL]
        chat_segs = [s for s in segments if s.intent_type == IntentType.CHAT]
        command_segs = [s for s in segments if s.intent_type == IntentType.WORK]
        
        # Rule: CALL + CHAT/COMMAND -> ignore CALL
        if call_segs and (chat_segs or command_segs):
            debug_log(3, "[NLP] COMPOUND filter: Ignoring CALL in presence of CHAT/COMMAND")
            segments = [s for s in segments if s.intent_type != IntentType.CALL]
        
        # Rule: CHAT + COMMAND -> prioritize COMMAND (add first)
        if chat_segs and command_segs:
            debug_log(3, "[NLP] COMPOUND filter: Prioritizing COMMAND over CHAT")
            # Sort: COMMAND first, then CHAT
            segments = command_segs + chat_segs
        
        return segments
    
    def _process_active_cs_state(self, segments: List[IntentSegment], cs_sessions: List[Any]) -> Dict[str, Any]:
        """
        Process intents when CS is active
        
        Rules per NLP狀態處理.md:
        - Default all inputs to CHAT, but detect DW and BW
        - CALL -> CHAT (treat as chat continuation)
        - UNKNOWN -> Ignore but continue CS
        - CHAT: Don't queue, continue CS, route to processing layer
        - DW: **Interrupt CS**, add WORK state, **end current loop**
        - BW: Add WORK state, **don't interrupt CS**
        - COMPOUND: Complex filtering with interrupt logic
        """
        result = {
            "states_to_add": [],
            "interrupt_cs": False,
            "pause_cs_sessions": [],
            "processing_notes": [],
            "session_control": None  # For DW interrupt - follows LLM pattern
        }
        
        try:
            # Filter out UNKNOWN segments (continue CS)
            valid_segments = [s for s in segments if s.intent_type != IntentType.UNKNOWN]
            
            if not valid_segments:
                # All UNKNOWN - continue CS, don't interrupt
                result["processing_notes"].append("All segments UNKNOWN - continuing CS")
                debug_log(2, "[NLP] Active CS: All UNKNOWN, continuing CS")
                return result
            
            # Check for COMPOUND
            if len(valid_segments) > 1:
                result["processing_notes"].append(f"COMPOUND intent in CS: {len(valid_segments)} segments")
                # Apply COMPOUND filtering for CS
                valid_segments, should_interrupt = self._filter_compound_active_cs(valid_segments)
                if should_interrupt:
                    result["interrupt_cs"] = True
                    result["pause_cs_sessions"] = [cs.session_id for cs in cs_sessions]
                    # Set session_control for DW in COMPOUND case
                    result["session_control"] = {
                        "action": "end_session",
                        "should_end_session": True,
                        "reason": "Direct work interrupt (COMPOUND)",
                        "confidence": 1.0
                    }
                    debug_log(2, "[NLP] COMPOUND with DW - set session_control for immediate interrupt")
            
            # Process each segment
            for segment in valid_segments:
                if segment.intent_type == IntentType.CALL:
                    # CALL -> treat as CHAT in CS (per NLP狀態處理.md)
                    # Don't queue, just continue CS as if it's CHAT
                    result["processing_notes"].append("CALL in CS - treating as CHAT, continuing conversation")
                    debug_log(2, "[NLP] Active CS: CALL -> treat as CHAT (no queue, continue CS)")
                    
                elif segment.intent_type == IntentType.CHAT:
                    # CHAT: Continue CS, don't queue
                    result["processing_notes"].append("CHAT in CS - continuing conversation")
                    debug_log(2, "[NLP] Active CS: CHAT - continuing CS")
                    
                elif segment.intent_type == IntentType.WORK:
                    # WORK: Check work_mode to decide if interrupt and resume
                    work_mode = segment.metadata.get('work_mode', 'background') if segment.metadata else 'background'
                    priority = 100 if work_mode == "direct" else 30
                    
                    # 🆕 新架構：BW 和 DW 都立即中斷 CS，差別在於是否恢復
                    result["interrupt_cs"] = True
                    result["pause_cs_sessions"] = [cs.session_id for cs in cs_sessions]
                    result["session_control"] = {
                        "action": "end_session",
                        "should_end_session": True,
                        "reason": "Work interrupt (BW)" if work_mode == "background" else "Work interrupt (DW)",
                        "confidence": 1.0
                    }
                    
                    if work_mode == "background":
                        # BW: 中斷 CS，處理 WORK，然後恢復 CHAT
                        result["processing_notes"].append("BW in CS - interrupting CS, will resume after WORK")
                        debug_log(2, f"[NLP] Active CS: WORK (background) - interrupting CS, queuing WORK + resume CHAT (priority={priority})")
                        
                        # 保存 CS 上下文以便恢復
                        from core.sessions.session_manager import unified_session_manager
                        cs_context = None
                        if cs_sessions:
                            cs = cs_sessions[0]  # 取第一個活躍 CS
                            cs_context = cs.get_session_context()
                        
                        # 添加 WORK 狀態
                        result["states_to_add"].append({
                            "segment": segment,
                            "state_type": "WORK",
                            "priority": priority,
                            "work_mode": work_mode
                        })
                        
                        # 添加恢復 CHAT 狀態（優先級較低，在 WORK 後執行）
                        result["states_to_add"].append({
                            "segment": None,  # 無新輸入，恢復對話
                            "state_type": "CHAT",
                            "priority": 10,  # 低優先級，確保在 WORK 後
                            "resume_context": cs_context,  # 保存的 CS 上下文
                            "is_resume": True
                        })
                        debug_log(2, f"[NLP] BW: 已添加 WORK (priority=30) + resume CHAT (priority=10) 到佇列")
                    else:
                        # DW: 中斷 CS，處理 WORK，不恢復
                        result["processing_notes"].append("DW in CS - interrupting CS, no resume")
                        debug_log(2, f"[NLP] Active CS: WORK (direct) - interrupting CS, queuing WORK only (priority={priority})")
                        
                        # 只添加 WORK 狀態
                        result["states_to_add"].append({
                            "segment": segment,
                            "state_type": "WORK",
                            "priority": priority,
                            "work_mode": work_mode
                        })
                    
                    # 🔧 CRITICAL: 檢測到 WORK 意圖後，立即返回，不再處理後續分段
                    # 原因：WORK 已添加到佇列並將被優先處理，後續的 CHAT 分段應該被忽略
                    # 避免發布混淆的 INPUT_LAYER_COMPLETE 事件（意圖=CHAT 但狀態=WORK）
                    debug_log(2, f"[NLP] BW/DW 已添加到佇列，停止處理後續分段")
                    return result
            
        except Exception as e:
            error_log(f"[NLP] Error in _process_active_cs_state: {e}")
            result["processing_notes"].append(f"Error: {str(e)}")
        
        return result
    
    def _filter_compound_active_cs(self, segments: List[IntentSegment]) -> tuple[List[IntentSegment], bool]:
        """
        Filter COMPOUND intents for active CS state
        
        Rules:
        - DW + CHAT: Interrupt CS, add WORK (DW), then add CHAT, end loop
        - BW + CHAT: Add WORK (BW), don't interrupt CS
        - DW + BW: Interrupt CS, add both WORK states, end loop
        
        Returns:
            (filtered_segments, should_interrupt_cs)
        """
        work_segs = [s for s in segments if s.intent_type == IntentType.WORK]
        dw_segs = [s for s in work_segs if s.metadata and s.metadata.get('work_mode') == 'direct']
        bw_segs = [s for s in work_segs if s.metadata and s.metadata.get('work_mode') == 'background']
        chat_segs = [s for s in segments if s.intent_type == IntentType.CHAT]
        
        should_interrupt = False
        
        # Rule: DW + CHAT -> interrupt, add DW then CHAT
        if dw_segs and chat_segs:
            debug_log(3, "[NLP] COMPOUND CS filter: DW+CHAT -> interrupt CS")
            should_interrupt = True
            # Sort: DW first (higher priority), then CHAT
            segments = dw_segs + chat_segs
        
        # Rule: BW + CHAT -> no interrupt, add BW
        elif bw_segs and chat_segs:
            debug_log(3, "[NLP] COMPOUND CS filter: BW+CHAT -> queue BW, continue CS")
            # Keep all segments
            pass
        
        # Rule: DW + BW -> interrupt, add both
        elif dw_segs and bw_segs:
            debug_log(3, "[NLP] COMPOUND CS filter: DW+BW -> interrupt CS, add both")
            should_interrupt = True
            # Sort by priority (DW=100 > BW=30)
            segments = sorted(segments, key=lambda s: s.priority, reverse=True)
        
        # If any DW present, should interrupt
        if dw_segs:
            should_interrupt = True
        
        return segments, should_interrupt
    
    def _process_active_ws_state(self, segments: List[IntentSegment], ws_sessions: List[Any]) -> Dict[str, Any]:
        """
        Process intents when WS is active
        
        Rules per NLP狀態處理.md:
        - All inputs treated as Response for workflow steps
        - CHAT: Mark for LLM to ask if work should end
        - DW/BW: Treat as Response, mark as work content
        - UNKNOWN: Treat as Response, let LLM handle
        """
        result = {
            "states_to_add": [],
            "skip_input_layer": False,
            "response_metadata": {},
            "processing_notes": [],
            "workflow_input_mode": False  # ✅ 新增：標記為工作流輸入模式
        }
        
        try:
            # ✅ 檢查是否為工作流輸入場景（Interactive Input Step）
            from core.working_context import working_context_manager
            workflow_waiting_input = working_context_manager.is_workflow_waiting_input()
            
            if workflow_waiting_input:
                result["workflow_input_mode"] = True
                result["processing_notes"].append("Workflow Input Step - routing to LLM for semantic judgment")
                debug_log(2, "[NLP] Active WS: Workflow input detected - will trigger LLM to use provide_workflow_input")
                # 設置路由到 LLM (LLM 會檢查這個標記並調用 provide_workflow_input 工具)
                result["route_to_llm_for_input"] = True
                # ✅ 根據 NLP狀態處理.md：當存在 WS 時，所有輸入歸類為 Response
                result["corrected_intent"] = IntentType.RESPONSE
                debug_log(2, "[NLP] Active WS: Correcting intent to RESPONSE")
                return result  # 提前返回，不執行常規 WS 邏輯
            
            # In WS, all inputs are Response
            result["processing_notes"].append("Active WS - treating all inputs as Response")
            
            # Analyze intent types for metadata
            has_call = any(s.intent_type == IntentType.CALL for s in segments)
            has_chat = any(s.intent_type == IntentType.CHAT for s in segments)
            work_segs = [s for s in segments if s.intent_type == IntentType.WORK]
            has_dw = any(s.metadata and s.metadata.get('work_mode') == 'direct' for s in work_segs)
            has_bw = any(s.metadata and s.metadata.get('work_mode') == 'background' for s in work_segs)
            has_unknown = any(s.intent_type == IntentType.UNKNOWN for s in segments)
            
            # CALL in WS: treat as Response (per NLP狀態處理.md)
            if has_call:
                result["response_metadata"]["call_detected"] = True
                result["processing_notes"].append("CALL in WS - treating as Response")
                debug_log(2, "[NLP] Active WS: CALL detected - treat as Response")
            
            # Set metadata for LLM processing
            if has_chat:
                result["response_metadata"]["chat_detected"] = True
                result["response_metadata"]["suggest_end_work"] = True
                result["processing_notes"].append("CHAT in WS - suggesting LLM ask if work should end")
                debug_log(2, "[NLP] Active WS: CHAT detected - mark for end-work question")
            
            if has_dw or has_bw:
                result["response_metadata"]["work_content"] = True
                work_types = []
                if has_dw:
                    work_types.append("work_direct")
                if has_bw:
                    work_types.append("work_background")
                result["response_metadata"]["work_types"] = work_types
                result["processing_notes"].append(f"Work intent in WS - treating as work content: {work_types}")
                debug_log(2, f"[NLP] Active WS: Work intents detected - {work_types}")
            
            if has_unknown:
                result["response_metadata"]["uncertain_input"] = True
                result["processing_notes"].append("UNKNOWN in WS - let LLM handle")
                debug_log(2, "[NLP] Active WS: UNKNOWN - let LLM handle")
            
            # ✅ 在活躍 WS 時，所有輸入都應該被視為 RESPONSE
            # 根據 NLP狀態處理.md：當存在 WS 時，所有輸入歸類為 Response
            result["corrected_intent"] = IntentType.RESPONSE
            result["processing_notes"].append("Active WS - correcting intent to RESPONSE")
            debug_log(2, "[NLP] Active WS: Correcting all inputs to RESPONSE")
            
        except Exception as e:
            error_log(f"[NLP] Error in _process_active_ws_state: {e}")
            result["processing_notes"].append(f"Error: {str(e)}")
        
        return result
    
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
            
            # 🔧 實時獲取 session_id 和 cycle_index（StateManager可能在處理過程中創建了GS）
            session_id = self._get_current_gs_id()
            cycle_index = self._get_current_cycle_index()
            
            debug_log(3, f"[NLP] 發布事件使用: session={session_id}, cycle={cycle_index}")
            
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
        從 working_context 的全局數據中讀取 (由 Controller 在 GS 創建時設置)
        
        Returns:
            int: 當前 cycle_index,如果無法獲取則返回 0（假設為第一個 cycle）
        """
        try:
            from core.working_context import working_context_manager
            cycle_index = working_context_manager.global_context_data.get('current_cycle_index', 0)
            return cycle_index
        except Exception as e:
            error_log(f"[NLP] 獲取 cycle_index 失敗: {e}")
            return 0

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
            elif primary_intent in ["command", "work"]:
                # command (legacy) and work both map to WORK state
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
                    processing_notes=["CALL 意圖檢測，終止當前循環"],
                    session_control=None
                )
                
                # 直接返回 CALL 回應，不進行路由
                return call_response.dict()
            
            if target_state and current_state != target_state:
                info_log(f"[NLP] 執行狀態轉換: {current_state} → {target_state}")
                
                # ✅ 提取屬於目標狀態的分段文本
                # 每個狀態應該只處理對應意圖的分段，不是整個原始文本
                state_text = self._extract_state_text(nlp_result, target_state)
                
                # 設置狀態上下文
                context_data = {
                    "text": state_text,  # ✅ 只包含對應狀態的分段文本
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

    def _handle_declared_identity(self, identity_id: str, input_data: NLPInput) -> Dict[str, Any]:
        """處理主動聲明的 Identity（方案 A）
        
        Args:
            identity_id: 主動聲明的 Identity ID
            input_data: NLP 輸入數據
            
        Returns:
            Dict: 包含 identity, identity_action, processing_notes
        """
        result = {
            "identity": None,
            "identity_action": None,
            "processing_notes": []
        }
        
        try:
            # 載入該 Identity
            identity = self.identity_manager.get_identity_by_id(identity_id)
            
            if not identity:
                error_log(f"[NLP] 聲明的 Identity {identity_id} 不存在")
                result["processing_notes"].append(f"聲明的 Identity {identity_id} 不存在")
                return result
            
            # 🆕 從 Working Context 獲取 Speaker 數據
            speaker_data = self._get_speaker_from_working_context()
            
            if speaker_data:
                speaker_id = speaker_data.get('speaker_id')
                speaker_confidence = speaker_data.get('confidence', 0.0)
                
                # 從 voice_features 中提取 embedding（如果有）
                voice_features = speaker_data.get('voice_features')
                speaker_embedding = None
                
                if voice_features and isinstance(voice_features, dict):
                    speaker_embedding = voice_features.get('embedding')
                
                # 如果有 embedding，添加到 Identity 的 speaker_accumulation
                if speaker_embedding and isinstance(speaker_embedding, list):
                    success = self.identity_manager.add_speaker_sample(
                        identity_id,
                        speaker_embedding,
                        speaker_confidence,
                        audio_duration=voice_features.get('audio_duration') if voice_features else None,
                        metadata={
                            "speaker_id": speaker_id,
                            "timestamp": time.time(),
                            "mode": "declared_identity"
                        }
                    )
                    
                    if success:
                        debug_log(2, f"[NLP] 已添加 Speaker 樣本到 Identity {identity_id}")
                        result["processing_notes"].append(f"Speaker 樣本已添加到 Identity")
                        
                        # 關聯 Speaker ID 到 Identity（如果尚未關聯）
                        if speaker_id:
                            self.identity_manager.associate_speaker_to_identity(speaker_id, identity_id)
                    else:
                        error_log(f"[NLP] 添加 Speaker 樣本到 Identity {identity_id} 失敗")
                else:
                    debug_log(3, f"[NLP] Speaker 數據中無 embedding，嘗試從 SPEAKER_ACCUMULATION 獲取")
                    
                    # 🆕 從 Working Context 的 SPEAKER_ACCUMULATION 獲取 embedding
                    embeddings = self._get_speaker_embeddings_from_context()
                    if embeddings:
                        debug_log(2, f"[NLP] 從 SPEAKER_ACCUMULATION 獲取到 {len(embeddings)} 個 embedding")
                        for emb_data in embeddings:
                            self.identity_manager.add_speaker_sample(
                                identity_id,
                                emb_data['embedding'],
                                emb_data.get('confidence', 0.8),
                                metadata={
                                    "speaker_id": speaker_id,
                                    "timestamp": emb_data.get('timestamp', time.time()),
                                    "mode": "declared_identity_batch"
                                }
                            )
                        result["processing_notes"].append(f"從上下文添加了 {len(embeddings)} 個 Speaker 樣本")
                    else:
                        debug_log(3, f"[NLP] 無法獲取 Speaker embedding")
            
            # 設置結果
            result["identity"] = identity
            result["identity_action"] = "declared"
            result["processing_notes"].append(f"使用主動聲明的 Identity: {identity.display_name}")
            
            # 🆕 同步 StatusManager：切換到該 Identity 的系統狀態
            from core.status_manager import status_manager
            status_manager.switch_identity(identity_id)
            debug_log(2, f"[NLP] 已切換 StatusManager 到 Identity: {identity_id}")
            
            # 將身份添加到 Working Context
            self._add_identity_to_working_context(identity)
            
            info_log(f"[NLP] 使用主動聲明的 Identity: {identity.identity_id} ({identity.display_name})")
            
        except Exception as e:
            error_log(f"[NLP] 處理主動聲明 Identity 失敗: {e}")
            result["processing_notes"].append(f"處理聲明 Identity 錯誤: {str(e)}")
        
        return result
    
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
    
    def _get_speaker_embeddings_from_context(self) -> List[Dict[str, Any]]:
        """從 Working Context 的 SPEAKER_ACCUMULATION 獲取所有 embedding
        
        Returns:
            List[Dict]: 包含 embedding 的字典列表
        """
        embeddings = []
        
        try:
            from core.working_context import working_context_manager, ContextType
            import numpy as np
            
            # 獲取所有 SPEAKER_ACCUMULATION 上下文
            contexts = working_context_manager.get_contexts_by_type(ContextType.SPEAKER_ACCUMULATION)
            
            if not contexts:
                return embeddings
            
            # 遍歷所有上下文，提取 embedding
            for context in contexts:
                context_data = working_context_manager.get_context_data(context.context_id)
                
                if not context_data:
                    continue
                
                # data 字段包含累積的樣本
                data_items = context_data.get('data', [])
                
                for item in data_items:
                    # item 可能是 numpy array (embedding) 或包含 embedding 的字典
                    if isinstance(item, np.ndarray):
                        embeddings.append({
                            'embedding': item.tolist(),
                            'timestamp': time.time(),
                            'confidence': 0.8
                        })
                    elif isinstance(item, dict) and 'embedding' in item:
                        embeddings.append(item)
                
                debug_log(3, f"[NLP] 從上下文 {context.context_id} 提取了 {len(data_items)} 個樣本")
            
            return embeddings
            
        except Exception as e:
            error_log(f"[NLP] 從 SPEAKER_ACCUMULATION 獲取 embedding 失敗: {e}")
            return embeddings

    def _extract_state_text(self, nlp_result: 'NLPOutput', target_state: 'SystemState') -> str:
        """
        提取屬於目標狀態的分段文本
        
        架構設計：
        - 每個狀態只處理對應意圖的分段文本
        - CHAT 狀態 → CHAT 意圖的分段
        - WORK 狀態 → WORK 意圖的分段
        - 不是整個原始文本，而是特定意圖的分段組合
        
        Args:
            nlp_result: NLP 處理結果
            target_state: 目標狀態
            
        Returns:
            對應狀態的分段文本
        """
        from .intent_types import IntentType
        
        # 映射：狀態 → 意圖類型
        state_to_intent = {
            SystemState.CHAT: IntentType.CHAT,
            SystemState.WORK: IntentType.WORK,
        }
        
        target_intent = state_to_intent.get(target_state)
        if not target_intent:
            # 未知狀態，返回原始文本
            debug_log(2, f"[NLP] 未知目標狀態 {target_state}，使用原始文本")
            return nlp_result.original_text
        
        # 提取對應意圖的分段
        matching_segments = [
            seg for seg in nlp_result.intent_segments
            if seg.intent == target_intent
        ]
        
        if not matching_segments:
            # 沒有對應分段，返回原始文本（保護性邏輯）
            debug_log(2, f"[NLP] 沒有找到 {target_intent.name} 意圖的分段，使用原始文本")
            return nlp_result.original_text
        
        # 組合分段文本
        state_text = " ".join(seg.text for seg in matching_segments)
        debug_log(2, f"[NLP] 提取 {target_state.name} 狀態文本: '{state_text[:50]}...' (來自 {len(matching_segments)} 個分段)")
        
        return state_text
    
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

    def _correct_intent_with_mcp(self, segment: 'IntentSegment') -> 'IntentSegment':
        """
        使用 MCP 工具列表查詢來糾正意圖分類
        
        目的：防止工作請求被錯誤分類為 CHAT/UNKNOWN
        例如："What's the weather in Taipei?" 應該被識別為 WORK
        
        Args:
            segment: 原始意圖段落
            
        Returns:
            糾正後的意圖段落（如果找到匹配的工作流）
        """
        try:
            from core.framework import core_framework
            from .intent_types import IntentSegment as NewIntentSegment
            
            # 查詢 SYS 模組的 MCP 工具
            sys_module = core_framework.get_module('sys_module')
            if not sys_module:
                debug_log(3, "[NLP] SYS module not available for intent correction")
                return segment
            
            # 查詢所有註冊的工作流
            matches = sys_module.query_function_info(segment.segment_text, top_k=1)
            
            # 降低閾值到 0.3 以捕獲更多可能的工作請求
            if matches and matches[0]['relevance_score'] > 0.3:
                match = matches[0]
                work_mode = match['work_mode']
                workflow_name = match['name']
                score = match['relevance_score']
                
                debug_log(2, f"[NLP] MCP match found: '{workflow_name}' (score={score:.2f}, mode={work_mode})")
                
                # 如果當前不是 WORK 意圖，但找到了匹配的工作流，糾正為 WORK
                if segment.intent_type != IntentType.WORK:
                    debug_log(1, f"[NLP] Correcting intent: {segment.intent_type.name} -> WORK based on MCP match '{workflow_name}'")
                    
                    corrected_segment = NewIntentSegment(
                        segment_text=segment.segment_text,
                        intent_type=IntentType.WORK,
                        confidence=min(segment.confidence + 0.2, 0.95),  # 增加信心度
                        priority=0,  # 會根據 work_mode 重新計算
                        metadata={'work_mode': work_mode, 'mcp_corrected': True, 'matched_workflow': workflow_name}
                    )
                    return corrected_segment
                
                # 如果已經是 WORK，更新或確認 work_mode
                elif segment.intent_type == IntentType.WORK:
                    current_work_mode = segment.metadata.get('work_mode') if segment.metadata else None
                    
                    # 如果 work_mode 不一致，使用 MCP 的結果
                    if current_work_mode != work_mode:
                        debug_log(2, f"[NLP] Updating work_mode: {current_work_mode} -> {work_mode} based on MCP")
                        
                        updated_metadata = segment.metadata.copy() if segment.metadata else {}
                        updated_metadata['work_mode'] = work_mode
                        updated_metadata['mcp_verified'] = True
                        updated_metadata['matched_workflow'] = workflow_name
                        
                        corrected_segment = NewIntentSegment(
                            segment_text=segment.segment_text,
                            intent_type=IntentType.WORK,
                            confidence=segment.confidence,
                            priority=segment.priority,
                            metadata=updated_metadata
                        )
                        return corrected_segment
            
            # 沒有找到匹配或分數太低，返回原始 segment
            return segment
            
        except Exception as e:
            error_log(f"[NLP] Intent correction with MCP failed: {e}")
            return segment
    
    def get_module_info(self) -> Dict[str, Any]:
        """獲取模組資訊"""
        return {
            "module_id": "nlp_module",
            "version": "2.1.0",  # 版本更新 
            "status": "active" if self.is_initialized else "inactive",
            "capabilities": self.get_capabilities(),
            "description": "自然語言處理模組 - 支援增強型身份管理、記憶令牌、使用者偏好與多模組集成"
        }
    
    def _reload_from_user_settings(self, key_path: str, value: Any) -> bool:
        """處理使用者設定重載回調"""
        try:
            from configs.user_settings_manager import get_user_setting
            
            if key_path == "general.identity.user_name":
                # 更新當前 Identity 的 display_name
                if self.identity_manager:
                    current_identity_id = get_user_setting("general.identity.current_identity_id", "default")
                    if current_identity_id and current_identity_id != "default":
                        identity = self.identity_manager.identities.get(current_identity_id)
                        if identity:
                            old_name = identity.display_name
                            identity.display_name = str(value)
                            # 保存更新後的 Identity
                            self.identity_manager._save_identity(identity)
                            info_log(f"[NLP] Identity {current_identity_id} 名稱已更新: {old_name} → {value}")
                        else:
                            debug_log(2, f"[NLP] 未找到 Identity: {current_identity_id}")
                    else:
                        debug_log(2, "[NLP] 當前為 default 身份，無需更新")
                return True
                
            elif key_path == "general.identity.uep_nickname":
                # 更新 UEP 暱稱（即時生效，下次檢測時使用）
                info_log(f"[NLP] UEP 暱稱已更新為: {value}")
                return True
                
            return True
            
        except Exception as e:
            error_log(f"[NLP] 使用者設定重載失敗: {e}")
            return False
