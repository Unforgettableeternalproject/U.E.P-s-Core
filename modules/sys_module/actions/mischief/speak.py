"""
MISCHIEF 行為實現：講話

使用 TTS 模組說出預定的訊息。
"""

from typing import Dict, Any, Tuple

from . import MischiefAction, MoodContext
from utils.debug_helper import debug_log, error_log, SYSTEM_LEVEL


class SpeakAction(MischiefAction):
    """講話行為"""
    
    def __init__(self):
        super().__init__()
        self.display_name = "Speak"
        self.description = "Say something out loud using TTS"
        self.mood_context = MoodContext.ANY
        self.animation_name = "talk_ani_f"
        self.allowed_intensities = ["low", "medium", "high"]
        self.requires_params = ["text"]  # 需要 LLM 提供要說的話
    
    def execute(self, params: Dict[str, Any]) -> Tuple[bool, str]:
        """
        執行講話
        
        注意：不直接調用 TTS 模組，而是通過 Module Coordinator
        將文字交給 TTS 處理，遵循系統的層級式架構。
        """
        
        # 驗證參數
        valid, error = self.validate_params(params)
        if not valid:
            return False, error
        
        try:
            text = params.get("text", "")
            
            debug_log(SYSTEM_LEVEL, f"[Speak] 準備發送文字到 TTS: {text[:30]}...")
            
            # 通過 Module Coordinator 發送到 TTS
            # 這裡我們僅返回成功，實際的 TTS 調用由 MC 處理
            # MISCHIEF 行為在單次循環中執行，TTS 會在輸出層異步處理
            
            # 注意：在 MISCHIEF 狀態下，我們只是準備數據
            # 實際的 TTS 調用會由系統循環處理
            debug_log(SYSTEM_LEVEL, f"[Speak] 文字已準備好交給系統處理")
            
            # 返回文字內容，由調用方決定如何處理
            # 在實際整合時，這會被傳遞給 Module Coordinator
            return True, text
            
        except Exception as e:
            error_log(f"[Speak] 執行失敗: {e}")
            return False, f"講話時發生錯誤: {str(e)}"
