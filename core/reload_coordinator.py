# core/reload_coordinator.py
"""
模組重載協調器 (現在好像還沒有用到)

負責協調模組的熱重載，確保在安全的時機進行：
- 追蹤需要重載的模組
- 監控系統循環狀態
- 協調重載時機
- 顯示前端 overlay
"""

import time
import threading
from typing import Dict, Set, Optional, Callable, Any
from enum import Enum, auto
from dataclasses import dataclass, field
from datetime import datetime

from utils.debug_helper import debug_log, info_log, error_log


class ReloadPriority(Enum):
    """重載優先級"""
    CRITICAL = "critical"      # 關鍵模組，需要重啟循環（STT, NLP, SystemLoop）
    PROCESSING = "processing"  # 處理層模組，等待處理完成（MEM, LLM）
    OUTPUT = "output"          # 輸出層模組，等待輸出完成（TTS）
    UI = "ui"                  # UI 模組，隨時可重載


class ReloadStatus(Enum):
    """重載狀態"""
    IDLE = "idle"              # 空閒，沒有待重載項目
    PENDING = "pending"        # 等待中，循環進行中
    RELOADING = "reloading"    # 重載中
    COMPLETED = "completed"    # 完成
    ERROR = "error"            # 錯誤


@dataclass
class ReloadTask:
    """重載任務"""
    module_name: str
    priority: ReloadPriority
    setting_path: str
    old_value: Any
    new_value: Any
    created_at: datetime = field(default_factory=datetime.now)
    requires_cycle_restart: bool = False  # 是否需要重啟循環
    interface_type: Optional[str] = None  # UI 介面類型: None, "uep_main", "user_widget"
    
    def __str__(self):
        ui_info = f" [{self.interface_type}]" if self.interface_type else ""
        return f"{self.module_name} ({self.priority.value}): {self.setting_path}{ui_info}"


class ReloadCoordinator:
    """模組重載協調器"""
    
    # 模組優先級映射
    MODULE_PRIORITY_MAP = {
        "stt_module": ReloadPriority.CRITICAL,
        "nlp_module": ReloadPriority.CRITICAL,
        "system_loop": ReloadPriority.CRITICAL,
        "mem_module": ReloadPriority.PROCESSING,
        "llm_module": ReloadPriority.PROCESSING,
        "tts_module": ReloadPriority.OUTPUT,
        "ui_module": ReloadPriority.UI,
        "mov_module": ReloadPriority.UI,
        "ani_module": ReloadPriority.UI,
    }
    
    # 設定路徑到模組的映射
    # 格式: "setting_path": ("module_name", requires_cycle_restart, "interface_type")
    # interface_type 可選值: None (不涉及 UI), "uep_main" (UEP 主程式), "user_widget" (使用者小工具)
    SETTING_TO_MODULE_MAP = {
        "interaction.speech_input.enabled": ("system_loop", True, None),
        "interaction.speech_input.vad_sensitivity": ("stt_module", False, None),
        "interaction.speech_input.min_speech_duration": ("stt_module", False, None),
        "interaction.speech_input.microphone_device_index": ("stt_module", True, None),
        "interaction.speech_input.enable_continuous_mode": ("stt_module", True, None),
        "interaction.conversation.temperature": ("llm_module", False, None),
        "interaction.speech_output.speed": ("tts_module", False, None),
        "interaction.speech_output.volume": ("tts_module", False, None),
        # UI 相關設定
        "behavior.movement.enable_cursor_tracking": ("mov_module", False, "uep_main"),
        "behavior.movement.enable_throw_behavior": ("mov_module", False, "uep_main"),
        "interface.access_widget.auto_hide": ("ui_module", False, "user_widget"),
        "interface.access_widget.hide_edge_threshold": ("ui_module", False, "user_widget"),
        # 可以繼續添加更多映射
    }
    
    def __init__(self):
        self.status = ReloadStatus.IDLE
        self.pending_tasks: Dict[str, ReloadTask] = {}  # module_name -> ReloadTask
        self._lock = threading.Lock()
        self._reload_thread: Optional[threading.Thread] = None
        
        # 循環狀態追蹤
        self._cycle_in_progress = False
        self._cycle_lock = threading.Lock()
        
        # 回調函數
        self._on_reload_start: Optional[Callable] = None
        self._on_reload_complete: Optional[Callable] = None
        self._on_show_overlay: Optional[Callable[[str], None]] = None
        self._on_hide_overlay: Optional[Callable] = None
        
        # 訂閱系統事件以追蹤循環狀態
        self._subscribe_to_system_events()
        
        info_log("[ReloadCoordinator] 模組重載協調器已初始化")
    
    def _subscribe_to_system_events(self):
        """訂閱系統事件以追蹤循環狀態"""
        try:
            from core.event_bus import event_bus, SystemEvent
            
            # 訂閱 INTERACTION_STARTED 事件（循環開始）
            event_bus.subscribe(
                SystemEvent.INTERACTION_STARTED,
                self._on_interaction_started,
                handler_name="ReloadCoordinator.interaction_started"
            )
            
            # 訂閱 CYCLE_COMPLETED 事件（循環結束）
            event_bus.subscribe(
                SystemEvent.CYCLE_COMPLETED,
                self._on_cycle_completed,
                handler_name="ReloadCoordinator.cycle_completed"
            )
            
            debug_log(2, "[ReloadCoordinator] 已訂閱系統事件")
        except Exception as e:
            error_log(f"[ReloadCoordinator] 訂閱系統事件失敗: {e}")
    
    def _on_interaction_started(self, event):
        """處理 INTERACTION_STARTED 事件"""
        with self._cycle_lock:
            self._cycle_in_progress = True
            debug_log(3, "[ReloadCoordinator] 循環開始")
    
    def _on_cycle_completed(self, event):
        """處理 CYCLE_COMPLETED 事件"""
        with self._cycle_lock:
            self._cycle_in_progress = False
            debug_log(3, "[ReloadCoordinator] 循環結束")
    
    def register_callbacks(self,
                          on_reload_start: Optional[Callable] = None,
                          on_reload_complete: Optional[Callable] = None,
                          on_show_overlay: Optional[Callable[[str], None]] = None,
                          on_hide_overlay: Optional[Callable] = None):
        """註冊回調函數"""
        self._on_reload_start = on_reload_start
        self._on_reload_complete = on_reload_complete
        self._on_show_overlay = on_show_overlay
        self._on_hide_overlay = on_hide_overlay
    
    def request_reload(self, setting_path: str, old_value: Any, new_value: Any) -> bool:
        """
        請求重載模組
        
        Args:
            setting_path: 設定路徑，如 "interaction.speech_input.enabled"
            old_value: 舊值
            new_value: 新值
            
        Returns:
            bool: 是否成功添加重載任務
        """
        try:
            # 檢查該設定是否需要重載模組
            if setting_path not in self.SETTING_TO_MODULE_MAP:
                debug_log(3, f"[ReloadCoordinator] 設定 {setting_path} 不需要重載模組")
                return False
            
            module_name, requires_cycle_restart, interface_type = self.SETTING_TO_MODULE_MAP[setting_path]
            priority = self.MODULE_PRIORITY_MAP.get(module_name, ReloadPriority.PROCESSING)
            
            with self._lock:
                # 創建重載任務
                task = ReloadTask(
                    module_name=module_name,
                    priority=priority,
                    setting_path=setting_path,
                    old_value=old_value,
                    new_value=new_value,
                    requires_cycle_restart=requires_cycle_restart,
                    interface_type=interface_type
                )
                
                # 如果該模組已有待重載任務，覆蓋它
                self.pending_tasks[module_name] = task
                
                info_log(f"[ReloadCoordinator] 添加重載任務: {task}")
                
                # 如果沒有重載線程在運行，啟動一個
                if self._reload_thread is None or not self._reload_thread.is_alive():
                    self._start_reload_process()
                
                return True
                
        except Exception as e:
            error_log(f"[ReloadCoordinator] 請求重載失敗: {e}")
            return False
    
    def _start_reload_process(self):
        """啟動重載流程"""
        self._reload_thread = threading.Thread(target=self._reload_worker, daemon=True)
        self._reload_thread.start()
        debug_log(2, "[ReloadCoordinator] 重載工作線程已啟動")
    
    def _reload_worker(self):
        """重載工作線程"""
        try:
            # 等待安全時機
            self._wait_for_safe_timing()
            
            # 執行重載
            with self._lock:
                if not self.pending_tasks:
                    debug_log(2, "[ReloadCoordinator] 沒有待重載任務")
                    return
                
                self.status = ReloadStatus.RELOADING
                
                # 顯示前端 overlay
                if self._on_show_overlay:
                    self._on_show_overlay("正在更新設定...")
                
                # 觸發重載開始回調
                if self._on_reload_start:
                    self._on_reload_start()
                
                # 按優先級分組任務
                critical_tasks = [t for t in self.pending_tasks.values() if t.priority == ReloadPriority.CRITICAL]
                processing_tasks = [t for t in self.pending_tasks.values() if t.priority == ReloadPriority.PROCESSING]
                output_tasks = [t for t in self.pending_tasks.values() if t.priority == ReloadPriority.OUTPUT]
                ui_tasks = [t for t in self.pending_tasks.values() if t.priority == ReloadPriority.UI]
                
                success = True
                
                # 1. 處理關鍵模組（需要停止循環）
                if critical_tasks:
                    info_log(f"[ReloadCoordinator] 重載關鍵模組: {[t.module_name for t in critical_tasks]}")
                    success &= self._reload_critical_modules(critical_tasks)
                
                # 2. 處理處理層模組
                if processing_tasks:
                    info_log(f"[ReloadCoordinator] 重載處理層模組: {[t.module_name for t in processing_tasks]}")
                    success &= self._reload_processing_modules(processing_tasks)
                
                # 3. 處理輸出層模組
                if output_tasks:
                    info_log(f"[ReloadCoordinator] 重載輸出層模組: {[t.module_name for t in output_tasks]}")
                    success &= self._reload_output_modules(output_tasks)
                
                # 4. 處理 UI 模組
                if ui_tasks:
                    info_log(f"[ReloadCoordinator] 重載 UI 模組: {[t.module_name for t in ui_tasks]}")
                    success &= self._reload_ui_modules(ui_tasks)
                
                # 清空任務列表
                self.pending_tasks.clear()
                
                # 隱藏 overlay
                if self._on_hide_overlay:
                    self._on_hide_overlay()
                
                # 觸發重載完成回調
                if self._on_reload_complete:
                    self._on_reload_complete()
                
                self.status = ReloadStatus.COMPLETED if success else ReloadStatus.ERROR
                
                if success:
                    info_log("[ReloadCoordinator] ✅ 模組重載完成")
                else:
                    error_log("[ReloadCoordinator] ⚠️ 模組重載部分失敗")
                
        except Exception as e:
            error_log(f"[ReloadCoordinator] 重載流程錯誤: {e}")
            self.status = ReloadStatus.ERROR
            
            # 確保隱藏 overlay
            if self._on_hide_overlay:
                self._on_hide_overlay()
    
    def _wait_for_safe_timing(self):
        """等待安全的重載時機"""
        self.status = ReloadStatus.PENDING
        info_log("[ReloadCoordinator] 等待安全時機進行重載...")
        
        max_wait_time = 10.0  # 最多等待 10 秒
        start_time = time.time()
        
        while time.time() - start_time < max_wait_time:
            # 檢查是否有正在進行的循環
            with self._cycle_lock:
                cycle_in_progress = self._cycle_in_progress
            
            if not cycle_in_progress:
                debug_log(2, "[ReloadCoordinator] 系統循環空閒，可以進行重載")
                return
            
            debug_log(3, "[ReloadCoordinator] 等待系統循環完成...")
            time.sleep(0.2)
        
        # 超時，強制進行重載
        error_log("[ReloadCoordinator] ⚠️ 等待超時，強制進行重載")
    
    def _reload_critical_modules(self, tasks: list) -> bool:
        """重載關鍵模組（需要重啟循環）"""
        try:
            from core.system_loop import system_loop
            from core.framework import core_framework
            
            # 檢查是否需要重啟循環
            needs_cycle_restart = any(t.requires_cycle_restart for t in tasks)
            
            if needs_cycle_restart:
                info_log("[ReloadCoordinator] 停止系統循環...")
                
                # 停止 STT 監聽
                stt_module = core_framework.get_module('stt')
                if stt_module and hasattr(stt_module, 'stop_listening'):
                    stt_module.stop_listening()
                    debug_log(2, "[ReloadCoordinator] STT 監聽已停止")
            
            # 逐個重載模組
            for task in tasks:
                if task.module_name == "system_loop":
                    # SystemLoop 只需要更新配置，不需要完全重載
                    debug_log(2, "[ReloadCoordinator] 更新 SystemLoop 配置")
                    # 配置已經通過 _reload_from_user_settings 回調更新
                else:
                    # 重載其他關鍵模組
                    module = core_framework.get_module(task.module_name.replace("_module", ""))
                    if module and hasattr(module, 'reload_config'):
                        module.reload_config()
                        debug_log(2, f"[ReloadCoordinator] 已重載 {task.module_name} 配置")
            
            if needs_cycle_restart:
                info_log("[ReloadCoordinator] 重啟系統循環...")
                
                # 根據新的輸入模式重啟 STT
                if system_loop.input_mode == "vad":
                    system_loop._start_stt_listening()
                elif system_loop.input_mode == "text":
                    system_loop._start_text_input()
                
                info_log("[ReloadCoordinator] ✅ 系統循環已重啟")
            
            return True
            
        except Exception as e:
            error_log(f"[ReloadCoordinator] 重載關鍵模組失敗: {e}")
            return False
    
    def _reload_processing_modules(self, tasks: list) -> bool:
        """重載處理層模組"""
        try:
            # 處理層模組（MEM, LLM）的配置更新已經通過 user_settings 的回調機制處理
            # 這裡只需要記錄即可
            for task in tasks:
                info_log(f"[ReloadCoordinator] 處理層模組 {task.module_name} 配置已更新（通過回調）")
            
            return True
            
        except Exception as e:
            error_log(f"[ReloadCoordinator] 重載處理層模組失敗: {e}")
            return False
    
    def _reload_output_modules(self, tasks: list) -> bool:
        """重載輸出層模組"""
        try:
            # 輸出層模組（TTS）的配置更新已經通過 user_settings 的回調機制處理
            # 這裡只需要記錄即可
            for task in tasks:
                info_log(f"[ReloadCoordinator] 輸出層模組 {task.module_name} 配置已更新（通過回調）")
            
            return True
            
        except Exception as e:
            error_log(f"[ReloadCoordinator] 重載輸出層模組失敗: {e}")
            return False
    
    def _reload_ui_modules(self, tasks: list) -> bool:
        """重載 UI 模組"""
        try:
            from core.framework import core_framework
            
            # 分類任務：UEP 主程式 vs 使用者小工具
            uep_main_tasks = [t for t in tasks if t.interface_type == "uep_main"]
            user_widget_tasks = [t for t in tasks if t.interface_type == "user_widget"]
            other_tasks = [t for t in tasks if t.interface_type is None]
            
            # 處理 UEP 主程式相關（需要重建 DesktopPet）
            if uep_main_tasks:
                info_log(f"[ReloadCoordinator] 重建 UEP 主程式: {[t.setting_path for t in uep_main_tasks]}")
                ui_module = core_framework.get_module('ui')
                if ui_module:
                    # 重建 MAIN_DESKTOP_PET
                    success = self._rebuild_main_desktop_pet(ui_module, uep_main_tasks)
                    if not success:
                        error_log("[ReloadCoordinator] 重建 UEP 主程式失敗")
                        return False
            
            # 處理使用者小工具相關（動態更新透過 user_settings 回調）
            if user_widget_tasks:
                info_log(f"[ReloadCoordinator] 更新使用者小工具: {[t.setting_path for t in user_widget_tasks]}")
                # UserAccessWidget 的配置更新已經通過 user_settings 的回調機制處理
                # 例如 auto_hide, hide_edge_threshold 等設定會自動應用
                for task in user_widget_tasks:
                    debug_log(2, f"[ReloadCoordinator] 小工具設定已更新（通過回調）: {task.setting_path}")
            
            # 處理其他 UI 相關任務
            for task in other_tasks:
                info_log(f"[ReloadCoordinator] UI 模組 {task.module_name} 配置已更新（通過回調）")
            
            return True
            
        except Exception as e:
            error_log(f"[ReloadCoordinator] 重載 UI 模組失敗: {e}")
            return False
    
    def _rebuild_main_desktop_pet(self, ui_module, tasks: list) -> bool:
        """重建 UEP 主程式（DesktopPet）"""
        try:
            from modules.ui_module.ui_module import UIInterfaceType
            
            info_log("[ReloadCoordinator] 開始重建 DesktopPet...")
            
            # 1. 隱藏並銷毀舊的 DesktopPet
            old_pet = ui_module.interfaces.get(UIInterfaceType.MAIN_DESKTOP_PET)
            if old_pet:
                # 停止動畫計時器
                if hasattr(old_pet, 'animation_timer') and old_pet.animation_timer:
                    old_pet.animation_timer.stop()
                    debug_log(2, "[ReloadCoordinator] 已停止舊的動畫計時器")
                
                old_pet.hide()
                old_pet.close()
                old_pet.deleteLater()
                debug_log(2, "[ReloadCoordinator] 已銷毀舊的 DesktopPet")
            
            # 2. 重新創建 DesktopPet（使用實際的導入路徑和參數）
            from modules.ui_module.main.desktop_pet_app import DesktopPetApp
            ui_module.interfaces[UIInterfaceType.MAIN_DESKTOP_PET] = DesktopPetApp(
                ui_module=ui_module,
                ani_module=ui_module.ani_module,
                mov_module=ui_module.mov_module
            )
            
            # 3. 重新註冊到 MOV 模組（使用實際存在的方法）
            if ui_module.mov_module and hasattr(ui_module.mov_module, 'set_pet_app'):
                ui_module.mov_module.set_pet_app(ui_module.interfaces[UIInterfaceType.MAIN_DESKTOP_PET])
                debug_log(2, "[ReloadCoordinator] 已重新註冊到 MOV 模組")
            
            # 4. 應用 always_on_top 設定
            if hasattr(ui_module, 'always_on_top_enabled') and ui_module.always_on_top_enabled:
                pet_window = ui_module.interfaces[UIInterfaceType.MAIN_DESKTOP_PET]
                if hasattr(pet_window, 'setWindowFlags'):
                    try:
                        from PyQt5.QtCore import Qt
                        current_flags = pet_window.windowFlags()
                        pet_window.setWindowFlags(current_flags | Qt.WindowStaysOnTopHint)
                        debug_log(2, "[ReloadCoordinator] 已應用置頂設定")
                    except Exception as e:
                        debug_log(2, f"[ReloadCoordinator] 應用置頂設定失敗: {e}")
            
            # 5. 顯示新的 DesktopPet
            ui_module.interfaces[UIInterfaceType.MAIN_DESKTOP_PET].show()
            ui_module.active_interfaces.add(UIInterfaceType.MAIN_DESKTOP_PET)
            
            info_log("[ReloadCoordinator] ✅ DesktopPet 重建完成")
            return True
            
        except Exception as e:
            error_log(f"[ReloadCoordinator] 重建 DesktopPet 失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
            return False
    
    def get_status(self) -> Dict[str, Any]:
        """獲取重載狀態"""
        with self._lock:
            return {
                "status": self.status.value,
                "pending_tasks": [str(t) for t in self.pending_tasks.values()],
                "task_count": len(self.pending_tasks)
            }


# 全局實例
reload_coordinator = ReloadCoordinator()
