# modules/llm_module/core/session_controller.py
"""
Session Controller - 會話控制和生命週期管理

負責處理 LLM 建議的會話結束邏輯，與 ModuleCoordinator 協作
實現雙條件會話結束機制。
"""

from typing import Dict, Any, Optional
from utils.debug_helper import debug_log, error_log


class SessionController:
    """
    會話控制器
    
    處理會話生命週期相關邏輯：
    - 解析 LLM 的會話結束建議
    - 標記會話待結束（通過 session_control 元數據）
    - 提供會話結束信心度評估
    
    架構說明：
    1. LLM 通過 session_control 建議結束會話
    2. ModuleCoordinator 檢測到後標記 pending_end
    3. Controller 在 CYCLE_COMPLETED 時檢查並執行結束
    
    這確保：
    - LLM 回應能完整生成並輸出
    - TTS 能完成語音合成
    - 所有去重鍵能正確清理
    - 會話在循環邊界乾淨地結束
    """
    
    def __init__(self):
        """初始化會話控制器"""
        debug_log(2, "[SessionController] 會話控制器初始化完成")
    
    def process_session_control(
        self, 
        response_data: Dict[str, Any], 
        mode: str, 
        llm_input: Any
    ) -> Optional[Dict[str, Any]]:
        """
        處理會話控制建議 - LLM 決定會話是否應該結束
        
        Args:
            response_data: LLM 回應數據（包含 session_control）
            mode: 當前模式 (CHAT/WORK)
            llm_input: LLM 輸入對象
            
        Returns:
            會話控制結果，或 None 如果無需結束
        """
        try:
            session_control = response_data.get("session_control")
            if not session_control:
                return None
            
            should_end = session_control.get("should_end_session", False)
            end_reason = session_control.get("end_reason", "unknown")
            confidence = session_control.get("confidence", 0.5)
            
            if should_end and confidence >= 0.7:  # 只在高信心度時結束會話
                debug_log(1, f"[SessionController] 會話結束建議: {mode} 模式 - 原因: {end_reason} (信心度: {confidence:.2f})")
                
                # 記錄會話結束請求
                self._log_session_end_request(mode, end_reason, confidence)
                
                return {
                    "session_ended": True,
                    "reason": end_reason,
                    "confidence": confidence
                }
            elif should_end:
                debug_log(2, f"[SessionController] 會話結束建議信心度不足: {confidence:.2f} < 0.7")
            
            return None
            
        except Exception as e:
            error_log(f"[SessionController] 處理會話控制失敗: {e}")
            return None
    
    def _log_session_end_request(
        self, 
        mode: str, 
        reason: str, 
        confidence: float
    ) -> None:
        """
        記錄會話結束請求日誌
        
        注意：實際的會話結束由 ModuleCoordinator 和 Controller 協作完成
        這裡只負責日誌記錄和狀態追蹤
        
        Args:
            mode: 模式 (CHAT/WORK)
            reason: 結束原因
            confidence: 信心度
        """
        try:
            # ✅ 架構正確性：LLM 通過 session_control 建議結束
            # ModuleCoordinator 檢測到後標記 pending_end
            # Controller 會在 CYCLE_COMPLETED 時檢查並執行結束
            
            debug_log(1, f"[SessionController] 📋 會話結束請求已通過 session_control 發送: {reason} (mode={mode}, confidence={confidence:.2f})")
            
            if mode == "CHAT":
                debug_log(1, f"[SessionController] 🔚 標記 CS 待結束 (原因: {reason}, 信心度: {confidence:.2f})")
                debug_log(2, f"[SessionController] session_control 已設置，等待循環完成後由 ModuleCoordinator 處理")
                        
            elif mode == "WORK":
                debug_log(1, f"[SessionController] 🔚 標記 WS 待結束 (原因: {reason}, 信心度: {confidence:.2f})")
                debug_log(2, f"[SessionController] session_control 已設置，等待循環完成後由 ModuleCoordinator 處理")
            
        except Exception as e:
            error_log(f"[SessionController] 記錄會話結束請求���敗: {e}")
    
    def should_end_session(
        self, 
        session_control: Optional[Dict[str, Any]]
    ) -> bool:
        """
        判斷是否應該結束會話
        
        Args:
            session_control: 會話控制數據
            
        Returns:
            True 如果應該結束會話
        """
        if not session_control:
            return False
        
        should_end = session_control.get("should_end_session", False)
        confidence = session_control.get("confidence", 0.0)
        
        # 只在高信心度時結束
        return should_end and confidence >= 0.7
    
    def get_session_control_metadata(
        self, 
        should_end: bool, 
        reason: str = "task_completed",
        confidence: float = 0.8
    ) -> Dict[str, Any]:
        """
        構建會話控制元數據
        
        用於在 LLM 回應中設置會話控制建議
        
        Args:
            should_end: 是否建議結束會話
            reason: 結束原因
            confidence: 信心度 (0.0-1.0)
            
        Returns:
            會話控制元數據字典
        """
        return {
            "action": "end_session" if should_end else "continue",
            "should_end_session": should_end,
            "end_reason": reason,
            "confidence": confidence
        }
