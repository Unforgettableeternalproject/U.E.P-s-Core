# core/router.py
from typing import Tuple, Any, Dict, Optional, List
from utils.debug_helper import info_log, debug_log
from core.state_manager import UEPState, StateManager
from core.strategies import smart_strategy, priority_strategy, conditional_strategy, context_decision_engine
from core.framework import RouteStrategy, DecisionEngine, ModuleInfo
from enum import Enum


class RouterMode(Enum):
    """路由器模式"""
    DIRECT = "direct"      # 直接路由
    STRATEGY = "strategy"  # 策略路由
    CONDITIONAL = "conditional"  # 條件路由


class Router:
    """
    統一路由器 - 根據系統狀態和輸入層->處理層->輸出層架構進行模組路由

    新的架構：
    輸入層 (STT/NLP) -> 處理層 (MEM/SYS/LLM) -> 輸出層 (TTS)

    Router根據當前系統狀態決定應該存取哪些模組，不被會話所拘束。
    """

    def __init__(self, mode: RouterMode = RouterMode.STRATEGY):
        self.mode = mode
        self.current_strategy: RouteStrategy = smart_strategy
        self.decision_engine: DecisionEngine = context_decision_engine

        # 模組層次定義
        self.layer_definitions = {
            "input": ["stt", "nlp"],           # 輸入層
            "processing": ["mem", "sys", "llm"], # 處理層
            "output": ["tts"]                  # 輸出層
        }

        # 狀態特定的路由規則
        self.state_routing_rules = {
            UEPState.IDLE: {
                "default_modules": ["llm"],
                "supported_intents": ["chat", "command", "memory_query"],
                "flow": ["processing"]  # 只有處理層
            },
            UEPState.CHAT: {
                "default_modules": ["llm", "mem"],
                "supported_intents": ["chat", "memory_query", "memory_store"],
                "flow": ["processing"]  # 處理層，可能包含記憶
            },
            UEPState.WORK: {
                "default_modules": ["sys", "llm"],
                "supported_intents": ["command", "chat"],
                "flow": ["processing"]  # 處理層，系統操作優先
            },
            UEPState.ERROR: {
                "default_modules": ["sys"],
                "supported_intents": ["command"],
                "flow": ["processing"]  # 只有系統處理
            }
        }

    def set_strategy(self, strategy: RouteStrategy) -> None:
        """設置路由策略"""
        self.current_strategy = strategy
        debug_log(1, f"[Router] 切換路由策略: {strategy.name}")

    def route(self,
              intent: str,
              detail: Any,
              state: UEPState,
              context: Optional[Dict[str, Any]] = None,
              available_modules: Optional[Dict[str, ModuleInfo]] = None
             ) -> Tuple[str, Dict[str, Any]]:
        """
        根據intent、當前狀態和上下文決定路由

        Args:
            intent: 意圖類型
            detail: 使用者輸入內容
            state: 當前系統狀態
            context: 額外上下文資訊
            available_modules: 可用模組資訊

        Returns:
            (module_key, args) 或 (flow_type, module_sequence)
        """
        if context is None:
            context = {}

        # 添加狀態資訊到上下文
        context.update({
            "current_state": state,
            "intent": intent,
            "detail": detail
        })

        debug_log(1, f"[Router] 路由請求 - 意圖:{intent}, 狀態:{state.value}")

        # 根據路由模式選擇路由邏輯
        if self.mode == RouterMode.DIRECT:
            return self._direct_route(intent, detail, state, context)
        elif self.mode == RouterMode.CONDITIONAL:
            return self._conditional_route(intent, detail, state, context, available_modules)
        else:  # STRATEGY mode
            return self._strategy_route(intent, detail, state, context, available_modules)

    def _direct_route(self, intent: str, detail: Any, state: UEPState, context: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """直接路由 - 基於狀態規則的簡單映射"""
        state_rules = self.state_routing_rules.get(state, self.state_routing_rules[UEPState.IDLE])

        # 檢查intent是否被支援
        if intent not in state_rules["supported_intents"]:
            # fallback到預設intent
            intent = "chat"
            debug_log(2, f"[Router] Intent '{intent}' 不支援，fallback到 'chat'")

        # 簡單的intent到模組映射
        module_mapping = {
            "chat": "llm",
            "command": "sys",
            "memory_query": "mem",
            "memory_store": "mem",
            "voice_recognition": "stt"
        }

        module_key = module_mapping.get(intent, "llm")
        args = self._prepare_module_args(module_key, intent, detail, context)

        debug_log(1, f"[Router] 直接路由: {intent} → {module_key}")
        return module_key, args

    def _strategy_route(self,
                       intent: str,
                       detail: Any,
                       state: UEPState,
                       context: Dict[str, Any],
                       available_modules: Optional[Dict[str, ModuleInfo]]
                       ) -> Tuple[str, Dict[str, Any]]:
        """策略路由 - 使用路由策略計算最佳路徑"""
        if not available_modules:
            # 如果沒有提供可用模組資訊，回退到直接路由
            return self._direct_route(intent, detail, state, context)

        # 使用策略計算路由
        module_sequence = self.current_strategy.calculate_route(intent, context, available_modules)

        if not module_sequence:
            # 策略沒有找到路由，回退到直接路由
            return self._direct_route(intent, detail, state, context)

        # 對於多模組序列，返回第一個模組（主要處理模組）
        primary_module = module_sequence[0]
        args = self._prepare_module_args(primary_module, intent, detail, context)

        # 如果有後續模組，添加到上下文中供後續處理
        if len(module_sequence) > 1:
            args["module_sequence"] = module_sequence
            args["next_modules"] = module_sequence[1:]

        debug_log(1, f"[Router] 策略路由: {intent} → {primary_module} (序列: {' → '.join(module_sequence)})")
        return primary_module, args

    def _conditional_route(self,
                          intent: str,
                          detail: Any,
                          state: UEPState,
                          context: Dict[str, Any],
                          available_modules: Optional[Dict[str, ModuleInfo]]
                          ) -> Tuple[str, Dict[str, Any]]:
        """條件路由 - 使用條件策略"""
        # 使用條件策略
        module_sequence = conditional_strategy.calculate_route(intent, context, available_modules or {})

        if not module_sequence:
            return self._direct_route(intent, detail, state, context)

        primary_module = module_sequence[0]
        args = self._prepare_module_args(primary_module, intent, detail, context)

        debug_log(1, f"[Router] 條件路由: {intent} → {primary_module}")
        return primary_module, args

    def _prepare_module_args(self, module_key: str, intent: str, detail: Any, context: Dict[str, Any]) -> Dict[str, Any]:
        """準備模組參數"""
        base_args = {"intent": intent}

        # 根據模組類型準備特定參數
        if module_key == "llm":
            base_args.update({
                "text": str(detail),
                "enable_memory_retrieval": self._should_retrieve_memory(intent, detail)
            })
        elif module_key == "mem":
            base_args.update(self._prepare_memory_args(intent, detail, context))
        elif module_key == "sys":
            base_args.update({
                "command": str(detail),
                "mode": "execute_command"
            })
        elif module_key == "stt":
            base_args.update({
                "audio_data": detail,
                "language": context.get("language", "zh-TW")
            })
        elif module_key == "nlp":
            base_args.update({
                "text": str(detail),
                "analyze_intent": True,
                "extract_entities": True
            })
        elif module_key == "tts":
            base_args.update({
                "text": str(detail),
                "voice": context.get("voice", "default")
            })
        else:
            # 通用參數
            base_args["data"] = detail

        return base_args

    def _should_retrieve_memory(self, intent: str, detail: Any) -> bool:
        """判斷是否需要檢索記憶"""
        if intent == "chat":
            return True

        if isinstance(detail, str):
            memory_keywords = ["記得", "之前", "上次", "昨天", "前面", "剛才", "想起"]
            return any(keyword in detail for keyword in memory_keywords)

        return False

    def _prepare_memory_args(self, intent: str, detail: Any, context: Dict[str, Any]) -> Dict[str, Any]:
        """準備記憶模組參數"""
        base_args = {
            "operation_type": "query" if intent == "memory_query" else "store",
            "timestamp": None,
            "max_results": 5
        }

        if intent == "memory_query":
            base_args.update({
                "query_text": str(detail),
                "similarity_threshold": 0.7
            })
        elif intent == "memory_store":
            base_args.update({
                "content": str(detail),
                "memory_type": "user_input"
            })

        return base_args

    def handle_response(self,
                       module_key: str,
                       response: Dict[str, Any],
                       state_manager: Optional[StateManager] = None,
                       context: Optional[Dict[str, Any]] = None
                       ) -> Optional[Dict[str, Any]]:
        """
        處理模組回應，決定下一步動作

        Args:
            module_key: 回應的模組
            response: 模組回應
            state_manager: 狀態管理器
            context: 當前上下文

        Returns:
            下一步動作資訊，如果沒有則返回None
        """
        if context is None:
            context = {}

        debug_log(1, f"[Router] 處理 {module_key} 回應")

        # 處理不同模組的回應
        if module_key == "llm":
            return self._handle_llm_response(response, state_manager, context)
        elif module_key == "mem":
            return self._handle_memory_response(response, state_manager, context)
        elif module_key == "sys":
            return self._handle_sys_response(response, state_manager, context)
        elif module_key == "stt":
            return self._handle_stt_response(response, state_manager, context)
        elif module_key == "nlp":
            return self._handle_nlp_response(response, state_manager, context)
        elif module_key == "tts":
            return self._handle_tts_response(response, state_manager, context)

        return None

    def _handle_llm_response(self, response: Dict[str, Any], state_manager: Optional[StateManager], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """處理LLM回應"""
        # 檢查是否包含系統動作
        if "sys_action" in response:
            sys_action = response.get("sys_action")
            if isinstance(sys_action, dict):
                debug_log(1, f"[Router] LLM回應包含系統動作: {sys_action}")
                return {
                    "action": "route_to_sys",
                    "module": "sys",
                    "args": {
                        "mode": "execute_sys_action",
                        "sys_action": sys_action
                    }
                }

        # 檢查是否需要TTS輸出
        if response.get("should_speak", False) or context.get("voice_output", False):
            return {
                "action": "route_to_tts",
                "module": "tts",
                "args": {
                    "text": response.get("text", ""),
                    "voice": context.get("voice", "default")
                }
            }

        return None

    def _handle_memory_response(self, response: Dict[str, Any], state_manager: Optional[StateManager], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """處理記憶模組回應"""
        operation_type = response.get("operation_type", "")
        status = response.get("status", "success")

        if status != "success":
            debug_log(1, f"[Router] MEM操作失敗: {response.get('error', '未知錯誤')}")
            return None

        # 根據操作類型處理
        if operation_type == "query":
            memories = response.get("memories", [])
            if memories:
                # 記憶查詢成功，返回增強上下文的動作
                return {
                    "action": "enhance_context",
                    "module": "llm",
                    "args": {
                        "memory_context": self._format_memory_context(memories),
                        "original_intent": context.get("intent", "chat")
                    }
                }

        elif operation_type == "store":
            # 記憶儲存確認
            debug_log(1, f"[Router] 記憶儲存成功: {response.get('stored_count', 0)} 條")
            # 記憶儲存通常不需要後續動作
            return None

        return None

    def _handle_sys_response(self, response: Dict[str, Any], state_manager: Optional[StateManager], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """處理系統模組回應"""
        # 系統操作通常是終點，不需要進一步路由
        debug_log(1, f"[Router] 系統操作完成: {response.get('status', 'unknown')}")
        return None

    def _handle_stt_response(self, response: Dict[str, Any], state_manager: Optional[StateManager], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """處理語音識別回應"""
        if response.get("status") == "success":
            recognized_text = response.get("text", "")
            if recognized_text:
                # 語音識別成功，路由到NLP處理
                return {
                    "action": "route_to_nlp",
                    "module": "nlp",
                    "args": {
                        "text": recognized_text,
                        "source": "stt"
                    }
                }

        return None

    def _handle_nlp_response(self, response: Dict[str, Any], state_manager: Optional[StateManager], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """處理NLP回應"""
        if response.get("status") == "success":
            intent = response.get("intent", "chat")
            entities = response.get("entities", {})

            # NLP處理成功，根據intent路由到適當的處理模組
            if intent in ["chat", "memory_query"]:
                target_module = "llm"
            elif intent == "command":
                target_module = "sys"
            else:
                target_module = "llm"  # 預設

            return {
                "action": "route_to_processing",
                "module": target_module,
                "args": {
                    "intent": intent,
                    "detail": response.get("text", ""),
                    "entities": entities,
                    "nlp_processed": True
                }
            }

        return None

    def _handle_tts_response(self, response: Dict[str, Any], state_manager: Optional[StateManager], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """處理TTS回應"""
        # TTS通常是終點
        debug_log(1, f"[Router] TTS輸出完成")
        return None

    def _format_memory_context(self, memories: List[Dict[str, Any]]) -> str:
        """格式化記憶上下文"""
        if not memories:
            return ""

        context_parts = ["基於您的歷史記憶，我回想起以下相關資訊："]

        for i, memory in enumerate(memories, 1):
            content = memory.get("content", "")
            timestamp = memory.get("timestamp", "")
            similarity = memory.get("similarity", 0.0)

            # 格式化時間戳
            if timestamp:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    time_str = dt.strftime("%Y年%m月%d日 %H:%M")
                except:
                    time_str = str(timestamp)
            else:
                time_str = "未知時間"

            context_parts.append(f"{i}. ({time_str}) {content}")
            if similarity > 0:
                context_parts.append(f"   相關度: {similarity:.2f}")

        return "\n".join(context_parts)


# 全局路由器實例
router = Router()
