# core/states/wake_api.py
"""
喚醒 API - 供前端使用者小工具調用

提供系統喚醒功能，包括：
- 從 SLEEP 狀態喚醒
- 重新載入模組
- 恢復系統正常運作
"""

import time
from typing import Dict, Any
from utils.debug_helper import debug_log, info_log, error_log


def wake_up_system(reason: str = "user_widget") -> Dict[str, Any]:
    """
    喚醒系統（由前端使用者小工具調用）
    
    Args:
        reason: 喚醒原因
        
    Returns:
        Dict: 喚醒結果
            - success: bool - 是否成功
            - message: str - 結果訊息
            - modules_reloaded: List[str] - 重載的模組列表
    """
    try:
        from core.states.state_manager import state_manager, UEPState
        from core.states.sleep_manager import sleep_manager
        from core.status_manager import StatusManager
        
        info_log(f"[WakeAPI] ⏰ 收到喚醒請求: {reason}")
        
        # 記錄喚醒為一次用戶互動
        status_mgr = StatusManager()
        status_mgr.record_interaction(successful=True, task_type="系統喚醒")
        debug_log(3, "[WakeAPI] 已記錄喚醒互動時間")
        
        # 檢查當前狀態
        current_state = state_manager.get_current_state()
        
        if current_state != UEPState.SLEEP:
            info_log(f"[WakeAPI] 系統未在休眠狀態（當前: {current_state.value}），無需喚醒")
            return {
                "success": True,
                "message": f"系統當前狀態為 {current_state.value}，未在休眠",
                "modules_reloaded": []
            }
        
        # 1. 使用 SleepManager 喚醒（會發布事件）
        wake_success = sleep_manager.wake_up(reason)
        
        if not wake_success:
            error_log("[WakeAPI] SleepManager 喚醒失敗")
            return {
                "success": False,
                "message": "SleepManager 喚醒失敗",
                "modules_reloaded": []
            }
        
        # 2. 重新載入模組
        reloaded_modules = _reload_modules()
        
        # 3. 通知 StateQueue 完成 SLEEP 狀態
        try:
            from core.states.state_queue import get_state_queue_manager
            state_queue = get_state_queue_manager()
            
            # 檢查當前是否真的在處理 SLEEP 狀態
            if state_queue.current_item and state_queue.current_item.state == UEPState.SLEEP:
                info_log("[WakeAPI] 📤 通知 StateQueue 完成 SLEEP 狀態")
                state_queue.complete_current_state(
                    success=True,
                    result_data={
                        "wake_reason": reason,
                        "modules_reloaded": reloaded_modules,
                        "wake_time": time.time()
                    }
                )
            else:
                debug_log(2, f"[WakeAPI] StateQueue 當前項目不是 SLEEP: {state_queue.current_item.state.value if state_queue.current_item else 'None'}")
        except Exception as e:
            error_log(f"[WakeAPI] 通知 StateQueue 失敗: {e}")
        
        # 3.5 發布 WAKE_READY（通知前端與 MOV 可安全轉場）
        try:
            from core.event_bus import event_bus, SystemEvent
            event_bus.publish(
                SystemEvent.WAKE_READY,
                {
                    "wake_reason": reason,
                    "modules_reloaded": reloaded_modules,
                },
                source="wake_api",
            )
            debug_log(2, "[WakeAPI] 已發布 WAKE_READY 事件")
        except Exception as e:
            debug_log(2, f"[WakeAPI] 發布 WAKE_READY 事件失敗: {e}")

        # 4. 退出 SLEEP 狀態，回到 IDLE
        state_manager.exit_special_state(reason)
        
        info_log(f"[WakeAPI] ✅ 系統喚醒成功，已重載 {len(reloaded_modules)} 個模組")
        
        return {
            "success": True,
            "message": "系統已成功喚醒",
            "modules_reloaded": reloaded_modules
        }
        
    except Exception as e:
        error_log(f"[WakeAPI] 喚醒系統失敗: {e}")
        import traceback
        error_log(traceback.format_exc())
        return {
            "success": False,
            "message": f"喚醒失敗: {str(e)}",
            "modules_reloaded": []
        }


def _reload_modules() -> list:
    """
    重新載入模組
    
    注意：實際的模組重載由 ReloadCoordinator 處理
    這裡只是發布事件並檢查模組狀態
    
    Returns:
        List[str]: 當前已載入的模組名稱列表
    """
    try:
        from core.framework import core_framework
        from core.reload_coordinator import reload_coordinator
        from core.status_manager import StatusManager
        
        info_log("[WakeAPI] 檢查模組狀態...")
        
        # 發布喚醒事件，讓 Framework 知道系統已喚醒
        # Framework 會根據配置自動重載必要的模組
        
        loaded_modules = []
        
        # 獲取當前已載入的模組
        if hasattr(core_framework, 'modules'):
            loaded_modules = list(core_framework.modules.keys())
            info_log(f"[WakeAPI] 當前已載入模組: {loaded_modules}")
        
        # 如果沒有載入模組，記錄警告
        if not loaded_modules:
            info_log("[WakeAPI] ⚠️ 檢測到模組未載入")
            info_log("[WakeAPI] 系統將在下次循環時自動初始化模組")
        
        # 檢查關鍵模組狀態
        essential_modules = ["stt", "nlp", "llm", "mem", "tts", "sys"]
        available_modules = []
        
        for module_name in essential_modules:
            module = core_framework.get_module(module_name)
            if module is not None:
                available_modules.append(module_name)
                debug_log(3, f"[WakeAPI] ✓ 模組可用: {module_name}")
            else:
                debug_log(2, f"[WakeAPI] ✗ 模組未載入: {module_name}")
        
        info_log(f"[WakeAPI] 模組檢查完成，可用: {len(available_modules)}/{len(essential_modules)}")
        
        return available_modules
        
    except Exception as e:
        error_log(f"[WakeAPI] 檢查模組狀態失敗: {e}")
        return []


def check_sleep_on_startup() -> bool:
    """
    系統啟動時檢查是否之前在 SLEEP 狀態
    
    如果是，則自動清理 SLEEP 狀態並恢復正常
    
    Returns:
        bool: 是否檢測到並處理了 SLEEP 狀態
    """
    try:
        from core.states.sleep_manager import sleep_manager
        from pathlib import Path
        import json
        
        # 檢查是否存在 sleep_context.json
        sleep_context_path = Path("memory/sleep_context.json")
        
        if not sleep_context_path.exists():
            return False
        
        info_log("[WakeAPI] 🌙 檢測到系統上次在 SLEEP 狀態")
        
        # 讀取休眠上下文
        try:
            with open(sleep_context_path, 'r', encoding='utf-8') as f:
                sleep_context = json.load(f)
            
            sleep_duration = sleep_context.get("sleep_start_time", 0)
            if sleep_duration > 0:
                import time
                actual_duration = time.time() - sleep_duration
                info_log(f"[WakeAPI] 休眠時長: {actual_duration/3600:.1f} 小時")
        except Exception as e:
            debug_log(2, f"[WakeAPI] 無法讀取休眠上下文: {e}")
        
        # 清理休眠狀態
        try:
            sleep_context_path.unlink()
            info_log("[WakeAPI] 已清理休眠上下文")
        except Exception as e:
            debug_log(2, f"[WakeAPI] 清理休眠上下文失敗: {e}")
        
        # 重置 SleepManager 狀態
        if hasattr(sleep_manager, '_is_sleeping'):
            sleep_manager._is_sleeping = False
            sleep_manager._sleep_context = None
        
        info_log("[WakeAPI] ✅ 系統已從休眠狀態恢復，將以正常模式啟動")
        
        return True
        
    except Exception as e:
        error_log(f"[WakeAPI] 檢查啟動時 SLEEP 狀態失敗: {e}")
        return False
