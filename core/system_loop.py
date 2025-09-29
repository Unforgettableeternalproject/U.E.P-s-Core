# core/system_loop.py
"""
系統主循環 - 基於狀態和上下文的智能處理循環

這個模組實現了 UEP 的核心運行邏輯：
1. 監控系統狀態變化
2. 根據當前狀態決定處理策略
3. 處理輸入事件和模組間通訊
4. 管理系統生命週期

主要運行模式：
- IDLE: 待機模式，監聽語音輸入
- CHAT: 對話模式，處理自然對話
- WORK: 工作模式，執行任務和工作流
"""

import time
import asyncio
import threading
from typing import Dict, Any, Optional, Callable
from enum import Enum

from core.framework import core_framework, ExecutionMode
from core.controller import unified_controller
from core.states.state_manager import UEPState, state_manager
from core.working_context import working_context_manager, ContextType
from core.router import router
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log


class LoopStatus(Enum):
    """循環狀態"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    ERROR = "error"


class SystemLoop:
    """系統主循環"""
    
    def __init__(self):
        self.config = load_config()
        self.status = LoopStatus.STOPPED
        self.loop_thread: Optional[threading.Thread] = None
        self.should_stop = threading.Event()
        
        # 使用新的 system 設置區塊中的參數
        system_config = self.config.get('system', {})
        self.loop_interval = system_config.get('main_loop_interval', 0.1)  # 從系統設定獲取循環間隔
        self.shutdown_timeout = system_config.get('shutdown_timeout', 5.0)  # 關閉超時時間
        
        # 事件處理器
        self.event_handlers: Dict[str, Callable] = {}
        self._setup_default_handlers()
        
    def _setup_default_handlers(self):
        """設置默認事件處理器"""
        self.event_handlers.update({
            'speech_input': self._handle_speech_input,
            'text_input': self._handle_text_input,
            'system_command': self._handle_system_command,
            'module_error': self._handle_module_error,
            'context_trigger': self._handle_context_trigger
        })
    
    def start(self) -> bool:
        """啟動系統循環"""
        if self.status != LoopStatus.STOPPED:
            error_log("系統循環已在運行或正在啟動")
            return False
            
        try:
            self.status = LoopStatus.STARTING
            info_log("🔄 啟動系統主循環...")
            
            # 重置停止信號
            self.should_stop.clear()
            
            # 啟動循環線程
            self.loop_thread = threading.Thread(target=self._main_loop, daemon=True)
            self.loop_thread.start()
            
            self.status = LoopStatus.RUNNING
            info_log("✅ 系統主循環已啟動")
            return True
            
        except Exception as e:
            self.status = LoopStatus.ERROR
            error_log(f"❌ 啟動系統循環失敗: {e}")
            return False
    
    def stop(self):
        """停止系統循環"""
        if self.status not in [LoopStatus.RUNNING, LoopStatus.PAUSED]:
            return
            
        self.status = LoopStatus.STOPPING
        info_log("🛑 停止系統主循環...")
        
        # 設置停止信號
        self.should_stop.set()
        
        # 等待線程結束，使用設定的超時時間
        if self.loop_thread and self.loop_thread.is_alive():
            self.loop_thread.join(timeout=self.shutdown_timeout)
            
        self.status = LoopStatus.STOPPED
        info_log("✅ 系統主循環已停止")
    
    def pause(self):
        """暫停系統循環"""
        if self.status == LoopStatus.RUNNING:
            self.status = LoopStatus.PAUSING
            info_log("⏸️ 暫停系統主循環")
    
    def resume(self):
        """恢復系統循環"""
        if self.status == LoopStatus.PAUSED:
            self.status = LoopStatus.RUNNING
            info_log("▶️ 恢復系統主循環")
    
    def _main_loop(self):
        """主循環邏輯"""
        info_log("🔄 進入系統主循環")
        
        try:
            while not self.should_stop.is_set():
                # 檢查暫停狀態
                if self.status == LoopStatus.PAUSING:
                    self.status = LoopStatus.PAUSED
                    info_log("⏸️ 系統循環已暫停")
                
                if self.status == LoopStatus.PAUSED:
                    time.sleep(0.5)
                    continue
                
                # 執行一次循環迭代
                self._loop_iteration()
                
                # 短暫休息
                time.sleep(self.loop_interval)
                
        except Exception as e:
            self.status = LoopStatus.ERROR
            error_log(f"❌ 系統循環發生錯誤: {e}")
        finally:
            info_log("🔄 退出系統主循環")
    
    def _loop_iteration(self):
        """單次循環迭代"""
        try:
            current_state = state_manager.get_state()
            
            # 根據當前狀態執行不同的處理邏輯
            if current_state == UEPState.IDLE:
                self._handle_idle_state()
            elif current_state == UEPState.CHAT:
                self._handle_chat_state()
            elif current_state == UEPState.WORK:
                self._handle_work_state()
            elif current_state == UEPState.ERROR:
                self._handle_error_state()
            
            # 檢查工作上下文觸發
            self._check_context_triggers()
            
            # 處理待處理的事件
            self._process_pending_events()
            
        except Exception as e:
            debug_log(3, f"循環迭代錯誤: {e}")
    
    def _handle_idle_state(self):
        """處理閒置狀態"""
        # 在閒置狀態下，主要是監聽語音輸入
        # 這裡可以檢查是否有 STT 模組在監聽
        
        # 檢查是否有持續監聽的 STT 模組
        stt_module = core_framework.get_module('stt_module')
        if stt_module and hasattr(stt_module, 'is_listening'):
            if not stt_module.is_listening():
                # 如果沒有在監聽，啟動持續監聽
                debug_log(3, "IDLE: 啟動 STT 持續監聽")
                try:
                    stt_module.handle({
                        'mode': 'continuous',
                        'duration': 30,  # 30秒監聽週期
                        'enable_speaker_id': True
                    })
                except Exception as e:
                    debug_log(2, f"STT 持續監聽啟動失敗: {e}")
    
    def _handle_chat_state(self):
        """處理對話狀態"""
        # 在對話狀態下，處理對話邏輯
        debug_log(3, "CHAT: 處理對話狀態")
        
        # 這裡可以檢查是否有待處理的對話
        # 例如檢查 NLP 模組是否有新的意圖識別結果
        pass
    
    def _handle_work_state(self):
        """處理工作狀態"""
        # 在工作狀態下，執行任務和工作流
        debug_log(3, "WORK: 處理工作狀態")
        
        # 檢查是否有活動的工作會話
        if hasattr(state_manager, '_active_session') and state_manager._active_session:
            session = state_manager._active_session
            if session.awaiting_input:
                # 工作流正在等待輸入，可能需要提示用戶
                debug_log(3, f"工作流 {session.session_id} 等待輸入")
            elif session.completed:
                # 工作流已完成，切換回閒置狀態
                state_manager.set_state(UEPState.IDLE)
                info_log(f"工作流 {session.session_id} 已完成，返回閒置狀態")
    
    def _handle_error_state(self):
        """處理錯誤狀態"""
        # 在錯誤狀態下，嘗試恢復或記錄錯誤
        debug_log(3, "ERROR: 處理錯誤狀態")
        
        # 可以嘗試自動恢復到閒置狀態
        time.sleep(1.0)  # 等待一秒
        state_manager.set_state(UEPState.IDLE)
        info_log("從錯誤狀態恢復到閒置狀態")
    
    def _check_context_triggers(self):
        """檢查工作上下文觸發條件"""
        try:
            # 獲取所有活動上下文
            active_contexts = working_context_manager.get_all_contexts()
            
            for context_id, context_info in active_contexts.items():
                context_type = context_info.get('context_type')
                data_count = context_info.get('data_count', 0)
                threshold = context_info.get('threshold', 5)
                
                # 檢查是否達到觸發條件
                if data_count >= threshold:
                    debug_log(2, f"上下文 {context_id} 達到觸發條件")
                    self._trigger_event('context_trigger', {
                        'context_id': context_id,
                        'context_type': context_type,
                        'data_count': data_count
                    })
                    
        except Exception as e:
            debug_log(3, f"檢查上下文觸發失敗: {e}")
    
    def _process_pending_events(self):
        """處理待處理的事件"""
        # 這裡可以實現事件隊列處理
        # 目前暫時跳過
        pass
    
    def _trigger_event(self, event_type: str, event_data: Dict[str, Any]):
        """觸發事件"""
        try:
            if event_type in self.event_handlers:
                self.event_handlers[event_type](event_data)
            else:
                debug_log(3, f"未知事件類型: {event_type}")
                
        except Exception as e:
            error_log(f"處理事件 {event_type} 時發生錯誤: {e}")
    
    # ========== 事件處理器 ==========
    
    def _handle_speech_input(self, event_data: Dict[str, Any]):
        """處理語音輸入事件"""
        info_log(f"🎤 收到語音輸入: {event_data}")
        
        # 根據當前狀態決定如何處理語音輸入
        current_state = state_manager.get_state()
        
        if current_state == UEPState.IDLE:
            # 在閒置狀態下，語音輸入可能觸發對話或工作模式
            text = event_data.get('text', '')
            if text:
                # 使用路由器決定下一步處理
                route_result = router.route_request({
                    'type': 'speech_input',
                    'data': event_data,
                    'context': {'current_state': current_state.name}
                })
                
                if route_result:
                    info_log(f"路由結果: {route_result}")
    
    def _handle_text_input(self, event_data: Dict[str, Any]):
        """處理文本輸入事件"""
        info_log(f"💬 收到文本輸入: {event_data}")
    
    def _handle_system_command(self, event_data: Dict[str, Any]):
        """處理系統命令事件"""
        info_log(f"⚙️ 收到系統命令: {event_data}")
    
    def _handle_module_error(self, event_data: Dict[str, Any]):
        """處理模組錯誤事件"""
        error_log(f"❌ 模組錯誤: {event_data}")
    
    def _handle_context_trigger(self, event_data: Dict[str, Any]):
        """處理上下文觸發事件"""
        info_log(f"🎯 上下文觸發: {event_data}")
        
        context_type = event_data.get('context_type')
        context_id = event_data.get('context_id')
        
        if context_type == ContextType.SPEAKER_ACCUMULATION.value:
            # 語者樣本累積觸發
            info_log(f"語者樣本累積觸發: {context_id}")
            # 這裡可以觸發創建新語者的邏輯
    
    def register_event_handler(self, event_type: str, handler: Callable):
        """註冊事件處理器"""
        self.event_handlers[event_type] = handler
        debug_log(2, f"註冊事件處理器: {event_type}")
    
    def get_status(self) -> Dict[str, Any]:
        """獲取循環狀態"""
        return {
            'status': self.status.value,
            'current_state': state_manager.get_state().name,
            'is_running': self.status == LoopStatus.RUNNING,
            'thread_alive': self.loop_thread.is_alive() if self.loop_thread else False
        }


# 全局系統循環實例
system_loop = SystemLoop()
