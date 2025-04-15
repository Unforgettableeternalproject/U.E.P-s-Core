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

        if payload.intent != "chat":
            info_log(f"[LLM] 暫不支援 intent: {payload.intent}", "WARNING")
            return {
                "text": "抱歉，目前僅支援聊天內容處理。",
                "status": "skipped"
            }

        debug_log(1, f"[LLM] 使用者輸入：{payload.text}")

        prompt = build_prompt(
            user_input=payload.text,
            memory=payload.memory or "",
            intent=payload.intent
        )

        debug_log(3, f"[LLM] 完成 prompt 建構，長度: {len(prompt)} 字元", exclusive=True)

        try:
            debug_log(2, f"[LLM] 開始查詢 Gemini 模型...")
            response = self.model.query(
                prompt=prompt
            )
            debug_log(2, f"[LLM] Gemini 模型回應：{response}", exclusive=True)
            return LLMOutput(
                text=response.get("text", ""),
                emotion=response.get("emotion", "neutral"),
                sys_action=response.get("sys_action")
            ).dict()

        except Exception as e:
            error_log(f"[LLM] Gemini 回應錯誤: {str(e)}")
            return {
                "text": "系統錯誤，無法產生回應。",
                "status": "error"
            }

    def shutdown(self):
        info_log("[LLM] 模組關閉")
