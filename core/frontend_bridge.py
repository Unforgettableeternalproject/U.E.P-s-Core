"""
前端橋接器 (Frontend Bridge)
管理 UI/ANI/MOV 三個前端模組的生命週期與協調
"""

from typing import Optional, Dict, Any
from utils.debug_helper import debug_log, info_log, error_log
from core.event_bus import SystemEvent
from core.states.state_manager import UEPState


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
        self._wake_in_progress = False  # 追蹤是否正在進行喚醒流程
        self._on_call_in_progress = False  # ON_CALL 進行中標記
        self._current_on_call_dialog = None  # ON_CALL 對話框實例
        
        info_log("[FrontendBridge] 前端橋接器已創建")
    
    def initialize(self, coordinator_only: bool = False) -> bool:
        """
        初始化前端橋接器
        
        Args:
            coordinator_only: 僅作為協調器模式
                - True: 只訂閱事件並轉發給前端模組（debug GUI 模式）
                - False: 完整初始化，包含後端整合（生產模式）
        
        步驟：
        1. 從 Framework 獲取前端模組實例
        2. 訂閱系統事件
        3. 註冊 StatusManager 回調（僅 coordinator_only=False）
        4. 建立模組間連接
        
        Returns:
            bool: 初始化是否成功
        """
        try:
            mode_desc = "協調器模式" if coordinator_only else "完整模式"
            info_log(f"[FrontendBridge] 開始初始化前端橋接器（{mode_desc}）...")
            
            # 1. 獲取前端模組實例
            if not self._load_frontend_modules():
                error_log("[FrontendBridge] 無法載入前端模組")
                return False
            
            # 2. 訂閱系統事件
            self._setup_event_subscriptions()
            
            # 3. 註冊 StatusManager 回調（僅生產模式）
            if not coordinator_only:
                self._setup_status_callbacks()
                debug_log(2, "[FrontendBridge] 已註冊 StatusManager 回調")
            else:
                debug_log(2, "[FrontendBridge] 協調器模式：跳過 StatusManager 整合")
            
            # 4. 建立模組間連接
            self._connect_modules()
            
            self._initialized = True
            info_log(f"[FrontendBridge] ✅ 前端橋接器初始化完成（{mode_desc}）")
            return True
            
        except Exception as e:
            error_log(f"[FrontendBridge] ❌ 初始化失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _load_frontend_modules(self) -> bool:
        """從 Framework/Registry 載入前端模組"""
        try:
            from core.framework import core_framework
            from core.registry import get_module
            
            # 嘗試載入 UI 模組
            try:
                self.ui_module = core_framework.get_module("ui") or get_module("ui_module")
                if self.ui_module:
                    info_log("[FrontendBridge] ✓ UI 模組已載入")
            except Exception as e:
                debug_log(2, f"[FrontendBridge] UI 模組未載入: {e}")
            
            # 嘗試載入 ANI 模組（從 Registry 載入，因為它不在 CoreFramework 中註冊）
            try:
                self.ani_module = get_module("ani_module")
                if self.ani_module:
                    info_log("[FrontendBridge] ✓ ANI 模組已載入")
            except Exception as e:
                debug_log(2, f"[FrontendBridge] ANI 模組未載入: {e}")
            
            # 嘗試載入 MOV 模組（從 Registry 載入，因為它不在 CoreFramework 中註冊）
            try:
                self.mov_module = get_module("mov_module")
                if self.mov_module:
                    info_log("[FrontendBridge] ✓ MOV 模組已載入")
            except Exception as e:
                debug_log(2, f"[FrontendBridge] MOV 模組未載入: {e}")
            
            # 在協調器模式下，允許沒有模組（延遲載入）
            has_modules = any([self.ui_module, self.ani_module, self.mov_module])
            if not has_modules:
                debug_log(2, "[FrontendBridge] 當前沒有前端模組，等待延遲註冊")
            
            return True  # 總是返回 True，允許延遲載入
            
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
                handler_name="frontend_bridge"
            )
            
            # 訂閱會話事件
            event_bus.subscribe(
                SystemEvent.SESSION_STARTED,
                self._on_session_started,
                handler_name="frontend_bridge"
            )
            
            event_bus.subscribe(
                SystemEvent.SESSION_ENDED,
                self._on_session_ended,
                handler_name="frontend_bridge"
            )
            
            # 訂閱工作流事件
            event_bus.subscribe(
                SystemEvent.WORKFLOW_REQUIRES_INPUT,
                self._on_workflow_input_required,
                handler_name="frontend_bridge"
            )
            
            # 訂閱模組狀態事件
            event_bus.subscribe(
                SystemEvent.MODULE_BUSY,
                self._on_module_busy,
                handler_name="frontend_bridge"
            )
            
            # 訂閱 SLEEP/WAKE 事件
            event_bus.subscribe(
                SystemEvent.SLEEP_ENTERED,
                self._on_sleep_entered,
                handler_name="frontend_bridge"
            )
            
            event_bus.subscribe(
                SystemEvent.SLEEP_EXITED,
                self._on_sleep_exited,
                handler_name="frontend_bridge"
            )
            
            event_bus.subscribe(
                SystemEvent.WAKE_READY,
                self._on_wake_ready,
                handler_name="frontend_bridge"
            )
            
            # 訂閱層級事件（三層架構）
            event_bus.subscribe(
                SystemEvent.INTERACTION_STARTED,
                self._on_interaction_started,
                handler_name="frontend_bridge"
            )
            
            event_bus.subscribe(
                SystemEvent.INPUT_LAYER_COMPLETE,
                self._on_input_layer_complete,
                handler_name="frontend_bridge"
            )
            
            event_bus.subscribe(
                SystemEvent.PROCESSING_LAYER_COMPLETE,
                self._on_processing_layer_complete,
                handler_name="frontend_bridge"
            )
            
            event_bus.subscribe(
                SystemEvent.OUTPUT_LAYER_COMPLETE,
                self._on_output_layer_complete,
                handler_name="frontend_bridge"
            )
            
            event_bus.subscribe(
                SystemEvent.CYCLE_COMPLETED,
                self._on_cycle_completed,
                handler_name="frontend_bridge"
            )
            
            # 訂閱 ON_CALL 事件
            event_bus.subscribe(
                SystemEvent.ON_CALL_TRIGGERED,
                self._on_call_triggered,
                handler_name="frontend_bridge"
            )
            
            event_bus.subscribe(
                SystemEvent.ON_CALL_ENDED,
                self._on_call_ended,
                handler_name="frontend_bridge"
            )
            
            # 訂閱 GS 生命週期事件
            event_bus.subscribe(
                SystemEvent.GS_ADVANCED,
                self._on_gs_advanced,
                handler_name="frontend_bridge"
            )
            
            event_bus.subscribe(
                SystemEvent.MODULE_ERROR,
                self._on_module_error,
                handler_name="frontend_bridge"
            )
            
            info_log("[FrontendBridge] ✓ 事件訂閱已設置（系統狀態 + 會話 + 層級 + GS + SLEEP + ON_CALL）")
            
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
    
    def register_module(self, module_type: str, module_instance):
        """
        註冊前端模組（用於延遲載入）
        
        Args:
            module_type: 模組類型 ('ui', 'ani', 'mov')
            module_instance: 模組實例
        """
        try:
            if module_type == 'ui':
                self.ui_module = module_instance
                info_log("[FrontendBridge] ✓ UI 模組已註冊")
            elif module_type == 'ani':
                self.ani_module = module_instance
                info_log("[FrontendBridge] ✓ ANI 模組已註冊")
            elif module_type == 'mov':
                self.mov_module = module_instance
                info_log("[FrontendBridge] ✓ MOV 模組已註冊")
                # MOV 註冊時立刻建立連接
                self._connect_modules()
            else:
                error_log(f"[FrontendBridge] 未知模組類型: {module_type}")
        except Exception as e:
            error_log(f"[FrontendBridge] 註冊模組 {module_type} 失敗: {e}")
    
    def _connect_modules(self):
        """建立前端模組間的連接"""
        try:
            # UI 需要知道 ANI 來請求動畫
            if self.ui_module and self.ani_module:
                if hasattr(self.ui_module, 'set_animation_controller'):
                    self.ui_module.set_animation_controller(self.ani_module)
                    debug_log(2, "[FrontendBridge] UI → ANI 連接已建立")
            
            # MOV 需要知道 ANI 來播放動畫（MOV 控制動畫決策）
            if self.mov_module and self.ani_module:
                if hasattr(self.mov_module, 'attach_ani'):
                    self.mov_module.attach_ani(self.ani_module)
                    debug_log(2, "[FrontendBridge] MOV → ANI 連接已建立（MOV 控制動畫）")
            
            if self.ui_module or self.ani_module or self.mov_module:
                info_log("[FrontendBridge] ✓ 模組間連接已建立")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 建立模組連接失敗: {e}")
    
    # ===== 事件處理器 =====
    
        def _on_user_interaction(self, event):
            """處理來自前端的用戶互動事件
        
            前端 UI/MOV 模組在檢測到用戶互動時（如點擊、拖拽）應發布此事件
            此處理器會更新 last_interaction_time，防止系統進入 SLEEP 狀態
            """
            try:
                from core.status_manager import StatusManager
            
                interaction_type = event.data.get('type', '前端互動') if hasattr(event, 'data') and event.data else '前端互動'
            
                status_mgr = StatusManager()
                status_mgr.record_interaction(successful=True, task_type=interaction_type)
            
                debug_log(3, f"[FrontendBridge] 已記錄用戶互動: {interaction_type}")
            
            except Exception as e:
                error_log(f"[FrontendBridge] 處理用戶互動事件失敗: {e}")
    
    def _on_state_changed(self, event):
        """系統狀態改變處理"""
        try:
            new_state = event.data.get('new_state')
            old_state = event.data.get('old_state')
            
            debug_log(2, f"[FrontendBridge] 狀態變化: {old_state} → {new_state}")
            
            # 通知前端模組（ANI 不需要，MOV 會自己訂閱）
            if self.mov_module and hasattr(self.mov_module, 'on_system_state_changed'):
                self.mov_module.on_system_state_changed(old_state, new_state)
            
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
            
            # 等待動畫由 MOV 根據層級事件處理（input 層）
                
        except Exception as e:
            error_log(f"[FrontendBridge] 處理工作流輸入請求失敗: {e}")
    
    def _on_module_busy(self, event):
        """模組忙碌狀態處理"""
        try:
            module_name = event.data.get('module_name')
            debug_log(3, f"[FrontendBridge] 模組忙碌: {module_name}")
            
            # MOV 會根據層級事件自動處理動畫，這裡不需要直接控制
            # 若需要特殊處理，由 MOV 模組決定
                
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
            
            # 錯誤動畫由 MOV 根據 ERROR 狀態處理
                
        except Exception as e:
            error_log(f"[FrontendBridge] 處理模組錯誤失敗: {e}")
    
    def _on_status_change(self, field: str, old_value: float, new_value: float, reason: str):
        """StatusManager 狀態變化回調"""
        try:
            old_val_str = f"{old_value:.2f}" if old_value is not None else "N/A"
            new_val_str = f"{new_value:.2f}" if new_value is not None else "N/A"
            debug_log(3, f"[FrontendBridge] 狀態變化: {field} {old_val_str} → {new_val_str} ({reason})")
            
            # 根據不同狀態欄位處理
            if field == "mood" and new_value is not None:
                self._handle_mood_change(new_value)
            elif field == "boredom" and new_value is not None:
                self._handle_boredom_change(new_value)
            elif field == "helpfulness" and new_value is not None:
                self._handle_helpfulness_change(new_value)
                
        except Exception as e:
            error_log(f"[FrontendBridge] 處理狀態變化失敗: {e}")
    
    def _handle_mood_change(self, mood: float):
        """處理情緒變化"""
        try:
            # MOV 會在輸出層根據 mood 選擇 talk 動畫（talk_ani_f 或 talk_ani2_f）
            debug_log(3, f"[FrontendBridge] 情緒更新: mood={mood:.2f}")
            
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
    
    def _on_sleep_entered(self, event):
        """系統進入睡眠狀態處理
        
        通過 on_system_state_changed 通知前端模組觸發睡眠動畫
        這樣保持了 FrontendBridge 作為事件轉發中心的設計原則
        """
        try:
            sleep_reason = event.data.get('reason', 'unknown') if hasattr(event, 'data') and event.data else 'unknown'
            
            info_log(f"[FrontendBridge] 系統進入睡眠狀態 (原因: {sleep_reason})")
            
            # 通知 MOV 模組觸發睡眠動畫（通過統一的狀態變化接口）
            if self.mov_module and hasattr(self.mov_module, 'on_system_state_changed'):
                self.mov_module.on_system_state_changed(UEPState.IDLE, UEPState.SLEEP)
                debug_log(2, "[FrontendBridge] 已通知 MOV 進入睡眠狀態")
            
            # 通知 UI 模組
            if self.ui_module and hasattr(self.ui_module, 'on_state_change'):
                self.ui_module.on_state_change(UEPState.SLEEP)
                debug_log(2, "[FrontendBridge] 已通知 UI 進入睡眠狀態")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理 SLEEP_ENTERED 事件失敗: {e}")
    
    def _on_sleep_exited(self, event):
        """系統退出睡眠狀態處理
        
        在此階段，後端已開始準備模組重載，通知前端開始播放 l_to_g 喚醒動畫
        等待 WAKE_READY 事件，確認模組已重載後才切換回 IDLE
        """
        try:
            wake_reason = event.data.get('wake_reason', 'unknown') if hasattr(event, 'data') and event.data else 'unknown'
            
            info_log(f"[FrontendBridge] 系統開始喚醒流程 (原因: {wake_reason})，通知 MOV 開始播放 l_to_g 喚醒動畫...")
            
            # 設置內部標記表示喚醒正在進行中
            self._wake_in_progress = True
            
            # 🔧 通知 MOV 模組開始退出睡眠狀態（播放 l_to_g 動畫）
            # 這會設置 _pending_wake_transition = True，等待 WAKE_READY 事件完成喚醒
            if self.mov_module and hasattr(self.mov_module, '_exit_sleep_state'):
                self.mov_module._exit_sleep_state()
                debug_log(2, "[FrontendBridge] 已通知 MOV 模組開始退出睡眠")
            
            debug_log(2, "[FrontendBridge] 等待後端模組重載完成...")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理 SLEEP_EXITED 事件失敗: {e}")
    
    def _on_wake_ready(self, event):
        """系統完全恢復就緒處理
        
        後端模組重載已完成，通知 MOV 完成喚醒流程
        MOV 會在 l_to_g 動畫完成後自動切換回 IDLE 狀態
        恢復使用者互動
        """
        try:
            wake_reason = event.data.get('wake_reason', 'unknown') if hasattr(event, 'data') and event.data else 'unknown'
            modules_reloaded = event.data.get('modules_reloaded', []) if hasattr(event, 'data') and event.data else []
            
            info_log(f"[FrontendBridge] 系統喚醒完成 (原因: {wake_reason})，{len(modules_reloaded)} 個模組已重載")
            
            # 清除喚醒進行中標記
            self._wake_in_progress = False
            
            # 🔧 通知 MOV 模組已重載完成，可以安全切換回 IDLE
            if self.mov_module and hasattr(self.mov_module, '_on_wake_ready'):
                self.mov_module._on_wake_ready(event)
                debug_log(2, "[FrontendBridge] 已通知 MOV 模組重載完成")
            
            # 通知 UI 恢復互動
            if self.ui_module and hasattr(self.ui_module, 'on_wake_ready'):
                self.ui_module.on_wake_ready()
                debug_log(2, "[FrontendBridge] 已通知 UI 恢復互動")
            
            info_log("[FrontendBridge] ✅ 前端已完全退出睡眠狀態")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理 WAKE_READY 事件失敗: {e}")
    
    def _on_interaction_started(self, event):
        """使用者互動開始事件轉發"""
        try:
            debug_log(2, "[FrontendBridge] 收到 INTERACTION_STARTED 事件")
            
            # 轉發給 MOV 模組
            if self.mov_module and hasattr(self.mov_module, '_on_interaction_started'):
                self.mov_module._on_interaction_started(event)
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理 INTERACTION_STARTED 事件失敗: {e}")
    
    def _on_input_layer_complete(self, event):
        """輸入層完成事件轉發"""
        try:
            debug_log(2, "[FrontendBridge] 收到 INPUT_LAYER_COMPLETE 事件")
            
            # 轉發給 MOV 模組
            if self.mov_module and hasattr(self.mov_module, '_on_input_layer_complete'):
                self.mov_module._on_input_layer_complete(event)
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理 INPUT_LAYER_COMPLETE 事件失敗: {e}")
    
    def _on_processing_layer_complete(self, event):
        """處理層完成事件轉發"""
        try:
            debug_log(2, "[FrontendBridge] 收到 PROCESSING_LAYER_COMPLETE 事件")
            
            # 轉發給 MOV 模組
            if self.mov_module and hasattr(self.mov_module, '_on_processing_layer_complete'):
                self.mov_module._on_processing_layer_complete(event)
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理 PROCESSING_LAYER_COMPLETE 事件失敗: {e}")
    
    def _on_output_layer_complete(self, event):
        """輸出層完成事件轉發"""
        try:
            debug_log(2, "[FrontendBridge] 收到 OUTPUT_LAYER_COMPLETE 事件")
            
            # 轉發給 MOV 模組
            if self.mov_module and hasattr(self.mov_module, '_on_output_layer_complete'):
                self.mov_module._on_output_layer_complete(event)
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理 OUTPUT_LAYER_COMPLETE 事件失敗: {e}")
    
    def _on_cycle_completed(self, event):
        """處理循環完成事件轉發"""
        try:
            debug_log(2, "[FrontendBridge] 收到 CYCLE_COMPLETED 事件")
            
            # 轉發給 MOV 模組
            if self.mov_module and hasattr(self.mov_module, '_on_cycle_completed'):
                self.mov_module._on_cycle_completed(event)
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理 CYCLE_COMPLETED 事件失敗: {e}")
    
    # === ON_CALL 公共方法 ===
    def toggle_on_call(self, mode: str = "vad") -> Dict[str, Any]:
        """
        切換 ON_CALL 狀態 - 啟動或結束 ON_CALL
        
        Args:
            mode: on_call 模式 ("vad" 或 "text")
        
        Returns:
            操作結果
        """
        try:
            from core.working_context import working_context_manager
            
            # 檢查是否已啟動
            if working_context_manager.is_activated():
                # 已啟動，執行結束邏輯
                debug_log(2, "[FrontendBridge] ON_CALL 已啟動，執行結束邏輯")
                return self.end_on_call()
            else:
                # 未啟動，執行啟動邏輯
                debug_log(2, "[FrontendBridge] ON_CALL 未啟動，執行啟動邏輯")
                return self.trigger_on_call(mode)
        
        except Exception as e:
            error_log(f"[FrontendBridge] ON_CALL 切換失敗: {e}")
            return {"status": "error", "message": str(e)}
    
    def trigger_on_call(self, mode: str = "vad") -> Dict[str, Any]:
        """
        觸發 ON_CALL 切換 - 若未在 ON_CALL 則啟動，若已在 ON_CALL 則結束
        
        Args:
            mode: on_call 模式 ("vad" 或 "text")
        
        Returns:
            操作結果
        """
        # 如果已在 ON_CALL 狀態，則結束 ON_CALL
        if hasattr(self, '_on_call_in_progress') and self._on_call_in_progress:
            debug_log(2, "[FrontendBridge] 已在 ON_CALL 狀態中，執行結束操作")
            return self.end_on_call()
        
        try:
            from core.working_context import working_context_manager
            from core.event_bus import event_bus, SystemEvent
            from core.system_loop import system_loop
            import time
            
            self._on_call_in_progress = True
            
            # 設置啟動標記（類似 NLP 偵測到 CALL 意圖時的標記）
            # 這樣 VAD 模式下可以無需 CALL 意圖直接使用
            working_context_manager.set_activation_flag(True)
            debug_log(2, "[FrontendBridge] 已設置啟動標記")
            
            # 暫停系統循環以防止干擾
            system_loop.pause()
            debug_log(2, "[FrontendBridge] 系統循環已暫停")
            
            # 🎤 通知 MOV 模組進入 ON_CALL 狀態（暫停行為機和追蹤）
            try:
                from core.framework import core_framework
                if 'mov' in core_framework.modules:
                    mov_module = core_framework.modules['mov'].module_instance
                    if hasattr(mov_module, '_on_call_active'):
                        mov_module._on_call_active = True
                        debug_log(2, "[FrontendBridge] MOV 模組已進入 ON_CALL 狀態")
            except Exception as e:
                debug_log(2, f"[FrontendBridge] 無法通知 MOV 模組: {e}")
            
            # 🎤 如果是 text 模式，顯示文字輸入對話框（非阻擋模式）
            if mode == "text":
                try:
                    from modules.ui_module.main.on_call_input_dialog import show_on_call_input_dialog
                    
                    # 顯示對話框（非阻擋，返回對話框實例）
                    dialog = show_on_call_input_dialog()
                    
                    # 直接連接信號（異步邏輯已在處理器中實現）
                    dialog.input_submitted.connect(self._handle_text_input)
                    dialog.dialog_closed.connect(self._handle_dialog_cancel)
                    
                    # 保存對話框實例便於後續關閉
                    self._current_on_call_dialog = dialog
                    
                    debug_log(2, "[FrontendBridge] 底部輸入框已顯示，等待使用者輸入")
                except Exception as e:
                    error_log(f"[FrontendBridge] 文字輸入對話框載入失敗: {e}")
                    import traceback
                    traceback.print_exc()
            
            # 發布 ON_CALL_TRIGGERED 事件（轉發給 MOV 模組播放 notice 動畫）
            event_bus.publish(
                SystemEvent.ON_CALL_TRIGGERED,
                {
                    "mode": mode,
                    "timestamp": time.time()
                },
                source="frontend_bridge"
            )
            
            info_log(f"[FrontendBridge] ✅ ON_CALL 已啟動 (模式: {mode})")
            return {
                "status": "success",
                "message": f"ON_CALL 已啟動 (模式: {mode})",
                "mode": mode
            }
            
        except Exception as e:
            error_log(f"[FrontendBridge] ON_CALL 觸發失敗: {e}")
            return {"status": "error", "message": str(e)}
    
    def _handle_text_input(self, text: str):
        """處理文字輸入提交（異步執行，不阻塞 UI）"""
        # 在下一次事件迴圈中執行，避免阻塞
        try:
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._execute_text_input(text))
        except:
            # 若不在 Qt 環境，直接執行
            self._execute_text_input(text)
    
    def _execute_text_input(self, text: str):
        """真正執行文字輸入邏輯"""
        debug_log(2, f"[FrontendBridge] 使用者輸入: {text}")
        # 注入文字到系統
        self.inject_text_input(text)
        # 結束 ON_CALL
        self.end_on_call()
    
    def _handle_dialog_cancel(self):
        """處理對話框取消（異步執行，不阻塞 UI）"""
        try:
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, self._execute_dialog_cancel)
        except:
            self._execute_dialog_cancel()
    
    def _execute_dialog_cancel(self):
        """真正執行取消邏輯"""
        debug_log(2, "[FrontendBridge] 使用者取消文字輸入")
        # 直接結束 ON_CALL
        self.end_on_call()
    
    def end_on_call(self) -> Dict[str, Any]:
        """
        結束 ON_CALL - 恢復系統並清除啟動標記
        
        Returns:
            操作結果
        """
        try:
            from core.working_context import working_context_manager
            from core.event_bus import event_bus, SystemEvent
            
            # 清除 ON_CALL 進行中標記
            self._on_call_in_progress = False
            
            # 關閉對話框
            if hasattr(self, '_current_on_call_dialog') and self._current_on_call_dialog is not None:
                try:
                    # 確保是 QWidget 物件再呼叫 close()
                    if hasattr(self._current_on_call_dialog, 'close') and callable(self._current_on_call_dialog.close):
                        self._current_on_call_dialog.close()
                    self._current_on_call_dialog = None
                    debug_log(2, "[FrontendBridge] 已關閉輸入對話框")
                except Exception as close_err:
                    debug_log(2, f"[FrontendBridge] 關閉對話框異常: {close_err}")
                    self._current_on_call_dialog = None
            
            from core.system_loop import system_loop
            import time
            
            # 獲取當前 ON_CALL 模式
            mode = "vad"  # 預設值
            
            # 清除啟動標記
            working_context_manager.clear_activation_flag()
            debug_log(2, "[FrontendBridge] 已清除啟動標記")
            
            # 🎤 通知 MOV 模組離開 ON_CALL 狀態（恢復行為機和追蹤）
            try:
                from core.framework import core_framework
                if 'mov' in core_framework.modules:
                    mov_module = core_framework.modules['mov'].module_instance
                    if hasattr(mov_module, '_on_call_active'):
                        mov_module._on_call_active = False
                        debug_log(2, "[FrontendBridge] MOV 模組已離開 ON_CALL 狀態")
                    
                    # 調用 MOV 的 end_on_call_animation 方法（會正確處理優先度）
                    if hasattr(mov_module, 'end_on_call_animation'):
                        try:
                            mov_module.end_on_call_animation(mode)
                            debug_log(2, "[FrontendBridge] 已調用 MOV 結束 ON_CALL 動畫")
                        except Exception as anim_err:
                            debug_log(2, f"[FrontendBridge] 結束動畫失敗: {anim_err}")
            except Exception as e:
                debug_log(2, f"[FrontendBridge] 無法通知 MOV 模組: {e}")
            
            # 恢復系統循環
            system_loop.resume()
            debug_log(2, "[FrontendBridge] 系統循環已恢復")
            
            # 發布 ON_CALL_ENDED 事件（轉發給 MOV 模組結束 notice 動畫）
            event_bus.publish(
                SystemEvent.ON_CALL_ENDED,
                {
                    "mode": mode,
                    "timestamp": time.time()
                },
                source="frontend_bridge"
            )
            
            info_log(f"[FrontendBridge] ✅ ON_CALL 已結束")
            return {
                "status": "success",
                "message": "ON_CALL 已結束",
                "mode": mode
            }
            
        except Exception as e:
            error_log(f"[FrontendBridge] ON_CALL 結束失敗: {e}")
            return {"status": "error", "message": str(e)}
    
    def inject_text_input(self, text: str) -> Dict[str, Any]:
        """
        注入文本輸入 - 作為用戶輸入送入系統循環
        
        Args:
            text: 要注入的文本
        
        Returns:
            操作結果
        """
        try:
            from core.framework import core_framework
            
            # 獲取 STT 模組
            stt_module = core_framework.get_module('stt')
            if not stt_module:
                error_log("[FrontendBridge] 無法獲取 STT 模組用於文本注入")
                return {"status": "error", "message": "STT 模組未載入"}
            
            # 通過 STT 模組將文本視為用戶輸入
            result = stt_module.handle_text_input(text)
            debug_log(2, f"[FrontendBridge] 已注入文本輸入: {text}")
            
            return {
                "status": "success",
                "message": f"已注入文本輸入: {text}"
            }
            
        except Exception as e:
            error_log(f"[FrontendBridge] 文本注入失敗: {e}")
            return {"status": "error", "message": str(e)}
    
    def _on_call_triggered(self, event):
        """ON_CALL_TRIGGERED 事件處理 - 播放 notice 動畫"""
        try:
            mode = event.data.get("mode", "vad")
            debug_log(2, f"[FrontendBridge] 收到 ON_CALL_TRIGGERED 事件 (模式: {mode})")
            
            # 轉發給 MOV 模組播放 notice 動畫
            if self.mov_module and hasattr(self.mov_module, 'trigger_on_call_animation'):
                self.mov_module.trigger_on_call_animation(mode)
            else:
                debug_log(1, "[FrontendBridge] MOV 模組未載入或不支援 on_call 動畫")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理 ON_CALL_TRIGGERED 事件失敗: {e}")
    
    def _on_call_ended(self, event):
        """ON_CALL_ENDED 事件處理 - 結束 notice 動畫"""
        try:
            mode = event.data.get("mode", "vad")
            debug_log(2, f"[FrontendBridge] 收到 ON_CALL_ENDED 事件 (模式: {mode})")
            
            # 轉發給 MOV 模組結束 notice 動畫
            if self.mov_module and hasattr(self.mov_module, 'end_on_call_animation'):
                self.mov_module.end_on_call_animation(mode)
            else:
                debug_log(1, "[FrontendBridge] MOV 模組未載入或不支援結束 on_call 動畫")
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理 ON_CALL_ENDED 事件失敗: {e}")
    
    def _on_gs_advanced(self, event):
        """GS 推進事件轉發"""
        try:
            debug_log(2, "[FrontendBridge] 收到 GS_ADVANCED 事件")
            
            # 轉發給 MOV 模組
            if self.mov_module and hasattr(self.mov_module, '_on_gs_advanced'):
                self.mov_module._on_gs_advanced(event)
            
        except Exception as e:
            error_log(f"[FrontendBridge] 處理 GS_ADVANCED 事件失敗: {e}")
    
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
