"""
前端橋接器 (Frontend Bridge)
管理 UI/ANI/MOV 三個前端模組的生命週期與協調
"""

from typing import Optional, Dict, Any
from utils.debug_helper import debug_log, info_log, error_log
from core.schemas import SystemState, SystemEvent


class FrontendBridge:
    """
    前端模組橋接器
    
    職責：
    1. 管理前端模組（UI/ANI/MOV）的生命週期
    2. 訂閱系統事件並分發給前端模組
    3. 協調前端模組間的通訊
    4. 與 StatusManager 整合，根據狀態更新前端
    """
    
    def __init__(self):
        self.ui_module = None
        self.ani_module = None
        self.mov_module = None
        self._initialized = False
        self._event_subscriptions = []
        
        info_log("[FrontendBridge] 前端橋接器已創建")
    
    def initialize(self) -> bool:
        """
        初始化前端橋接器
        
        步驟：
        1. 從 Framework 獲取前端模組實例
        2. 訂閱系統事件
        3. 註冊 StatusManager 回調
        4. 建立模組間連接
        
        Returns:
            bool: 初始化是否成功
        """
        try:
            info_log("[FrontendBridge] 開始初始化前端橋接器...")
            
            # 1. 獲取前端模組實例
            if not self._load_frontend_modules():
                error_log("[FrontendBridge] 無法載入前端模組")
                return False
            
            # 2. 訂閱系統事件
            self._setup_event_subscriptions()
            
            # 3. 註冊 StatusManager 回調
            self._setup_status_callbacks()
            
            # 4. 建立模組間連接
            self._connect_modules()
            
            self._initialized = True
            info_log("[FrontendBridge] ✅ 前端橋接器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[FrontendBridge] ❌ 初始化失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _load_frontend_modules(self) -> bool:
        """從 Framework 載入前端模組"""
        try:
            from core.framework import core_framework
            
            # 嘗試載入 UI 模組
            try:
                self.ui_module = core_framework.get_module("ui")
                if self.ui_module:
                    info_log("[FrontendBridge] ✓ UI 模組已載入")
            except Exception as e:
                debug_log(2, f"[FrontendBridge] UI 模組未載入: {e}")
            
            # 嘗試載入 ANI 模組
            try:
                self.ani_module = core_framework.get_module("ani")
                if self.ani_module:
                    info_log("[FrontendBridge] ✓ ANI 模組已載入")
            except Exception as e:
                debug_log(2, f"[FrontendBridge] ANI 模組未載入: {e}")
            
            # 嘗試載入 MOV 模組
            try:
                self.mov_module = core_framework.get_module("mov")
                if self.mov_module:
                    info_log("[FrontendBridge] ✓ MOV 模組已載入")
            except Exception as e:
                debug_log(2, f"[FrontendBridge] MOV 模組未載入: {e}")
            
            # 至少需要一個前端模組
            if not any([self.ui_module, self.ani_module, self.mov_module]):
                error_log("[FrontendBridge] 沒有任何前端模組可用")
                return False
            
            return True
            
        except Exception as e:
            error_log(f"[FrontendBridge] 載入前端模組失敗: {e}")
            return False
    
    def _setup_event_subscriptions(self):
        """設置事件訂閱"""
        try:
            from core.event_bus import event_bus
            
            # 訂閱系統狀態變化事件
            event_bus.subscribe(
                SystemEvent.STATE_CHANGED,
                self._on_state_changed,
                source="frontend_bridge"
            )
            
            # 訂閱會話事件
            event_bus.subscribe(
                SystemEvent.SESSION_STARTED,
                self._on_session_started,
                source="frontend_bridge"
            )
            
            event_bus.subscribe(
                SystemEvent.SESSION_ENDED,
                self._on_session_ended,
                source="frontend_bridge"
            )
            
            # 訂閱工作流事件
            event_bus.subscribe(
                SystemEvent.WORKFLOW_REQUIRES_INPUT,
                self._on_workflow_input_required,
                source="frontend_bridge"
            )
            
            # 訂閱模組狀態事件
            event_bus.subscribe(
                SystemEvent.MODULE_BUSY,
                self._on_module_busy,
                source="frontend_bridge"
            )
            
            event_bus.subscribe(
                SystemEvent.MODULE_ERROR,
                self._on_module_error,
                source="frontend_bridge"
            )
            
            info_log("[FrontendBridge] ✓ 事件訂閱已設置")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 設置事件訂閱失敗: {e}")
    
    def _setup_status_callbacks(self):
        """註冊 StatusManager 回調"""
        try:
            from core.status_manager import status_manager
            
            # 註冊狀態變化回調
            status_manager.register_update_callback(
                "frontend_bridge",
                self._on_status_change
            )
            
            info_log("[FrontendBridge] ✓ StatusManager 回調已註冊")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 註冊 StatusManager 回調失敗: {e}")
    
    def _connect_modules(self):
        """建立前端模組間的連接"""
        try:
            # UI 需要知道 ANI 來請求動畫
            if self.ui_module and self.ani_module:
                if hasattr(self.ui_module, 'set_animation_controller'):
                    self.ui_module.set_animation_controller(self.ani_module)
                    debug_log(2, "[FrontendBridge] UI → ANI 連接已建立")
            
            # ANI 需要知道 MOV 來同步動畫與移動
            if self.ani_module and self.mov_module:
                if hasattr(self.ani_module, 'set_movement_controller'):
                    self.ani_module.set_movement_controller(self.mov_module)
                    debug_log(2, "[FrontendBridge] ANI → MOV 連接已建立")
            
            info_log("[FrontendBridge] ✓ 模組間連接已建立")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 建立模組連接失敗: {e}")
    
    # ===== 事件處理器 =====
    
    def _on_state_changed(self, event):
        """系統狀態改變處理"""
        try:
            new_state = event.data.get('new_state')
            old_state = event.data.get('old_state')
            
            debug_log(2, f"[FrontendBridge] 狀態變化: {old_state} → {new_state}")
            
            # 通知所有前端模組
            if self.ani_module and hasattr(self.ani_module, 'on_state_change'):
                self.ani_module.on_state_change(new_state)
            
            if self.mov_module and hasattr(self.mov_module, 'on_state_change'):
                self.mov_module.on_state_change(new_state)
            
            if self.ui_module and hasattr(self.ui_module, 'on_state_change'):
                self.ui_module.on_state_change(new_state)
                
        except Exception as e:
            error_log(f"[FrontendBridge] 處理狀態變化失敗: {e}")
    
    def _on_session_started(self, event):
        """會話開始處理"""
        try:
            session_id = event.data.get('session_id')
            debug_log(2, f"[FrontendBridge] 會話開始: {session_id}")
            
            # 通知前端模組重置狀態
            if self.ui_module and hasattr(self.ui_module, 'on_session_started'):
                self.ui_module.on_session_started(session_id)
                
        except Exception as e:
            error_log(f"[FrontendBridge] 處理會話開始失敗: {e}")
    
    def _on_session_ended(self, event):
        """會話結束處理"""
        try:
            session_id = event.data.get('session_id')
            debug_log(2, f"[FrontendBridge] 會話結束: {session_id}")
            
            if self.ui_module and hasattr(self.ui_module, 'on_session_ended'):
                self.ui_module.on_session_ended(session_id)
                
        except Exception as e:
            error_log(f"[FrontendBridge] 處理會話結束失敗: {e}")
    
    def _on_workflow_input_required(self, event):
        """工作流需要輸入處理"""
        try:
            workflow_id = event.data.get('workflow_id')
            prompt = event.data.get('prompt', '請輸入')
            
            debug_log(2, f"[FrontendBridge] 工作流需要輸入: {workflow_id}")
            
            # 通知 UI 顯示輸入對話框
            if self.ui_module and hasattr(self.ui_module, 'request_user_input'):
                self.ui_module.request_user_input(workflow_id, prompt)
            
            # ANI 顯示等待動畫
            if self.ani_module and hasattr(self.ani_module, 'play'):
                self.ani_module.play('waiting', loop=True)
                
        except Exception as e:
            error_log(f"[FrontendBridge] 處理工作流輸入請求失敗: {e}")
    
    def _on_module_busy(self, event):
        """模組忙碌狀態處理"""
        try:
            module_name = event.data.get('module_name')
            debug_log(3, f"[FrontendBridge] 模組忙碌: {module_name}")
            
            # ANI 可以顯示忙碌動畫
            if self.ani_module and hasattr(self.ani_module, 'play'):
                self.ani_module.play('processing', loop=True)
                
        except Exception as e:
            error_log(f"[FrontendBridge] 處理模組忙碌狀態失敗: {e}")
    
    def _on_module_error(self, event):
        """模組錯誤處理"""
        try:
            module_name = event.data.get('module_name')
            error_msg = event.data.get('error')
            
            error_log(f"[FrontendBridge] 模組錯誤 ({module_name}): {error_msg}")
            
            # UI 顯示錯誤通知
            if self.ui_module and hasattr(self.ui_module, 'show_error'):
                self.ui_module.show_error(f"{module_name}: {error_msg}")
            
            # ANI 顯示錯誤動畫
            if self.ani_module and hasattr(self.ani_module, 'play'):
                self.ani_module.play('error', loop=False)
                
        except Exception as e:
            error_log(f"[FrontendBridge] 處理模組錯誤失敗: {e}")
    
    def _on_status_change(self, field: str, old_value: float, new_value: float, reason: str):
        """StatusManager 狀態變化回調"""
        try:
            debug_log(3, f"[FrontendBridge] 狀態變化: {field} {old_value:.2f} → {new_value:.2f} ({reason})")
            
            # 根據不同狀態欄位處理
            if field == "mood":
                self._handle_mood_change(new_value)
            elif field == "boredom":
                self._handle_boredom_change(new_value)
            elif field == "helpfulness":
                self._handle_helpfulness_change(new_value)
                
        except Exception as e:
            error_log(f"[FrontendBridge] 處理狀態變化失敗: {e}")
    
    def _handle_mood_change(self, mood: float):
        """處理情緒變化"""
        try:
            # 根據情緒選擇表情動畫
            expression = self._map_mood_to_expression(mood)
            
            if self.ani_module and hasattr(self.ani_module, 'set_base_expression'):
                self.ani_module.set_base_expression(expression)
            
            debug_log(3, f"[FrontendBridge] 情緒表情更新: {expression} (mood={mood:.2f})")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理情緒變化失敗: {e}")
    
    def _handle_boredom_change(self, boredom: float):
        """處理無聊程度變化"""
        try:
            # 如果無聊程度過高，觸發注意力尋求行為
            if boredom > 0.8:
                if self.mov_module and hasattr(self.mov_module, 'trigger_attention_seeking'):
                    self.mov_module.trigger_attention_seeking()
                    debug_log(2, "[FrontendBridge] 觸發注意力尋求行為")
                    
        except Exception as e:
            error_log(f"[FrontendBridge] 處理無聊變化失敗: {e}")
    
    def _handle_helpfulness_change(self, helpfulness: float):
        """處理助人意願變化"""
        try:
            # 可以根據助人意願調整角色行為
            debug_log(3, f"[FrontendBridge] 助人意願更新: {helpfulness:.2f}")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理助人意願變化失敗: {e}")
    
    def _map_mood_to_expression(self, mood: float) -> str:
        """
        將情緒值映射到表情動畫
        
        mood 範圍: -1 (負面) 到 +1 (正面)
        """
        if mood >= 0.6:
            return "happy"
        elif mood >= 0.2:
            return "neutral_positive"
        elif mood >= -0.2:
            return "neutral"
        elif mood >= -0.6:
            return "neutral_negative"
        else:
            return "sad"
    
    def shutdown(self):
        """關閉前端橋接器"""
        try:
            info_log("[FrontendBridge] 關閉前端橋接器...")
            
            # 取消事件訂閱
            from core.event_bus import event_bus
            for event_type, callback in self._event_subscriptions:
                event_bus.unsubscribe(event_type, callback)
            
            # 取消 StatusManager 回調
            from core.status_manager import status_manager
            status_manager.unregister_update_callback("frontend_bridge")
            
            # 關閉前端模組
            if self.ui_module and hasattr(self.ui_module, 'shutdown'):
                self.ui_module.shutdown()
            
            if self.ani_module and hasattr(self.ani_module, 'shutdown'):
                self.ani_module.shutdown()
            
            if self.mov_module and hasattr(self.mov_module, 'shutdown'):
                self.mov_module.shutdown()
            
            self._initialized = False
            info_log("[FrontendBridge] ✅ 前端橋接器已關閉")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 關閉失敗: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """獲取前端橋接器狀態"""
        return {
            "initialized": self._initialized,
            "ui_loaded": self.ui_module is not None,
            "ani_loaded": self.ani_module is not None,
            "mov_loaded": self.mov_module is not None,
        }


# 全局單例
frontend_bridge = FrontendBridge()
