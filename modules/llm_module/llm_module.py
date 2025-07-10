from core.module_base import BaseModule
from modules.llm_module.schemas import LLMInput, LLMOutput
from configs.config_loader import load_module_config
from modules.llm_module.gemini_client import GeminiWrapper
from utils.prompt_builder import build_prompt
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log

class LLMModule(BaseModule):
    def __init__(self, config=None):
        self.config = config or load_module_config("llm_module")
        self.model = GeminiWrapper(self.config)

    def debug(self):
        # Debug level = 1
        debug_log(1, "[LLM] Debug 模式啟用")
        # Debug level = 2
        debug_log_e(2, f"[LLM] 模型名稱: {self.model.model_name}")
        debug_log_e(2, f"[LLM] 溫度: {self.model.temperature}")
        debug_log_e(2, f"[LLM] Top P: {self.model.top_p}")
        debug_log_e(2, f"[LLM] 最大輸出字元數: {self.model.max_tokens}")
        # Debug level = 3
        debug_log_e(3, f"[LLM] 模組設定: {self.config}")
        
    def initialize(self):
        debug_log(1, "[LLM] 初始化中...")
        self.debug()
        info_log("[LLM] Gemini 模型初始化完成")
        
    def handle(self, data: dict) -> dict:
        payload = LLMInput(**data)
        debug_log(1, f"[LLM] 使用者輸入：{payload.text}")
        
        # Get session context if provided
        session_info = data.get("session_info", {})
        has_active_session = session_info.get("active_session", False)
        session_state = session_info.get("state", "none")
        is_internal = data.get("is_internal", False)
        
        # Handle different intents
        if payload.intent == "direct":
            # Direct mode - bypass all prompting templates and system instructions
            # 使用直接模式，完全跳過任何提示詞模板和系統指令
            prompt = payload.text
            debug_log(2, f"[LLM] 使用直接模式調用，不使用任何提示詞模板")
        elif payload.intent == "chat":
            # Standard chat handling
            prompt = build_prompt(
                user_input=payload.text,
                memory=payload.memory or "",
                intent=payload.intent,
                is_internal=is_internal
            )
        elif payload.intent == "command":
            # Command intent - analyze user's command and suggest actions
            # Check if we need to get system functions
            # is_internal 已經在前面定義
            get_sys_functions = data.get("get_sys_functions", False)
            
            # Get system functions if requested
            available_functions = ""
            if get_sys_functions:
                from modules.sys_module.sys_module import SYSModule
                try:
                    sys_module = SYSModule()
                    available_functions = sys_module._load_function_specs()
                    # Format functions for prompt
                    from utils.prompt_templates import format_sys_functions_for_prompt
                    available_functions = format_sys_functions_for_prompt(available_functions)
                except Exception as e:
                    error_log(f"[LLM] 獲取系統功能失敗: {e}")
                    available_functions = "系統功能暫時無法獲取"
            
            # Build appropriate prompt
            from utils.prompt_templates import build_command_prompt
            command_prompt = build_command_prompt(
                command=payload.text,
                available_functions=available_functions
            )
            
            # Add system action instruction template if not internal
            if not is_internal:
                from utils.prompt_templates import SYSTEM_ACTION_TEMPLATE
                command_prompt += "\n\n" + SYSTEM_ACTION_TEMPLATE
            
            prompt = build_prompt(
                user_input=command_prompt,
                memory=payload.memory or "",
                intent="command",
                is_internal=is_internal
            )
            debug_log(1, f"[LLM] 處理指令意圖: {payload.text}")
        else:
            info_log(f"[LLM] 暫不支援 intent: {payload.intent}", "WARNING")
            return {
                "text": f"抱歉，目前暫不支援 '{payload.intent}' 類型的處理。",
                "status": "skipped"
            }

        debug_log(3, f"[LLM] 完成 prompt 建構，長度: {len(prompt)} 字元", exclusive=True)

        try:
            debug_log(2, f"[LLM] 開始查詢 Gemini 模型...")
            response = self.model.query(
                prompt=prompt
            )
            debug_log(2, f"[LLM] Gemini 模型回應：{response}", exclusive=True)
            
            result = LLMOutput(
                text=response.get("text", ""),
                emotion=response.get("emotion", "neutral"),
                sys_action=response.get("sys_action")
            ).dict() | {"status": "ok"}
            
            # Add session context for command workflows if applicable
            if has_active_session and session_state == "awaiting":
                result["session_context"] = {
                    "active": True,
                    "state": session_state,
                    "message": "正在處理多步驟工作流程，請依照指示操作。"
                }
                
            return result
        except Exception as e:
            error_log(f"[LLM] Gemini 回應錯誤: {e}")
            return {
                "text": "系統錯誤，無法產生回應。",
                "status": "error"
            }

    def shutdown(self):
        info_log("[LLM] 模組關閉")
