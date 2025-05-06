# core/router.py
from typing import Tuple, Any, Dict

class Router:
    """
    將意圖(intent)與當前狀態(state)對應到具體要呼叫的模組與參數。
    """

    def __init__(self):
        # intent → (module_key, template_args_key)
        # template_args_key 表示 args 用哪個 payload 欄位
        self._map = {
            "chat": ("tts", "text"),       # chat → TTS 模組，用 text 做輸入
            "command": ("sys", "detail"),  # command → SYS 模組，用 detail 做輸入
            # 其他 intent 可繼續擴
        }

    def route(self,
              intent: str,
              detail: Any,
              state: str
             ) -> Tuple[str, Dict[str, Any]]:
        """
        根據 intent、detail，以及當前 state 決定：
          1. module_key: 要呼叫哪個模組
          2. args: 傳給模組的參數 dict

        Returns:
            module_key, args
        """
        if intent not in self._map:
            # 預設 fallback
            return "tts", {"text": detail}

        module_key, arg_key = self._map[intent]
        return module_key, {arg_key: detail}
