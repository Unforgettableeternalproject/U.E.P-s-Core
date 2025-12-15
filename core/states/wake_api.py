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
            - modules_reloaded: List[str] - 重載的模組列表（異步完成後才有）
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
        
        # 1. 使用 SleepManager 喚醒（會發布 SLEEP_EXITED 事件）
        wake_success = sleep_manager.wake_up(reason)
        
        if not wake_success:
            error_log("[WakeAPI] SleepManager 喚醒失敗")
            return {
                "success": False,
                "message": "SleepManager 喚醒失敗",
                "modules_reloaded": []
            }
        
        # 2. 異步重新載入模組（在後台執行緒中，避免阻塞 UI）
        # UI 會在此期間播放 struggle_l 過渡動畫
        # 模組重載完成後會發布 WAKE_READY 事件
        _reload_modules_async(reason)
        
        # 注意：StateQueue 完成和 state_manager 退出狀態會在 WAKE_READY 時處理
        # 這裡不做任何狀態切換，保持前端播放 struggle_l
        
        info_log(f"[WakeAPI] ✅ 系統喚醒流程已啟動，模組重載中（前端播放 struggle_l）")
        
        return {
            "success": True,
            "message": "系統喚醒中，模組重載進行中",
            "modules_reloaded": []  # 異步進行，立即返回空列表
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


def _reload_modules_async(reason: str) -> None:
    """
    異步重新載入模組（在後台執行緒中運行）
    
    完成後發布 WAKE_READY 事件，觸發 l_to_g 動畫
    
    Args:
        reason: 喚醒原因
    """
    import threading
    
    def _reload_in_thread():
        """在背景執行緒中重載模組"""
        try:
            info_log("[WakeAPI] 🔄 後台執行緒開始重載模組...")
            reloaded = _reload_modules()
            info_log(f"[WakeAPI] 🎉 後台模組重載完成: {len(reloaded)} 個模組")
            
            # 模組重載完成，通知 StateQueue 完成 SLEEP 狀態
            try:
                from core.states.state_queue import get_state_queue_manager
                from core.states.state_manager import UEPState
                state_queue = get_state_queue_manager()
                
                if state_queue.current_item and state_queue.current_item.state == UEPState.SLEEP:
                    info_log("[WakeAPI] 📤 通知 StateQueue 完成 SLEEP 狀態")
                    state_queue.complete_current_state(
                        success=True,
                        result_data={
                            "wake_reason": reason,
                            "modules_reloaded": reloaded,
                            "wake_time": time.time()
                        }
                    )
            except Exception as e:
                error_log(f"[WakeAPI] 通知 StateQueue 失敗: {e}")
            
            # 發布 WAKE_READY 事件（觸發 l_to_g 動畫）
            try:
                from core.event_bus import event_bus, SystemEvent
                event_bus.publish(
                    SystemEvent.WAKE_READY,
                    {
                        "wake_reason": reason,
                        "modules_reloaded": reloaded,
                    },
                    source="wake_api",
                )
                info_log("[WakeAPI] ✅ 已發布 WAKE_READY 事件，可播放 l_to_g 動畫")
            except Exception as e:
                error_log(f"[WakeAPI] 發布 WAKE_READY 事件失敗: {e}")
            
            # 退出 SLEEP 狀態，回到 IDLE
            try:
                from core.states.state_manager import state_manager
                state_manager.exit_special_state(reason)
            except Exception as e:
                error_log(f"[WakeAPI] 退出 SLEEP 狀態失敗: {e}")
                
        except Exception as e:
            error_log(f"[WakeAPI] 後台模組重載失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
    
    # 創建並啟動後台執行緒
    reload_thread = threading.Thread(target=_reload_in_thread, daemon=True, name="ModuleReloadThread")
    reload_thread.start()
    debug_log(2, "[WakeAPI] 🔄 啟動後台模組重載執行緒（前端將播放 struggle_l）")


def _reload_modules() -> list:
    """
    重新載入模組
    
    睡眠時被卸載的模組需要重新載入並註冊到 Framework
    
    Returns:
        List[str]: 成功重載的模組名稱列表
    """
    try:
        from core.framework import core_framework, ModuleType, ModuleCapabilities
        
        info_log("[WakeAPI] 🔄 開始重載被卸載的模組...")
        
        # 定義需要重載的模組（與 _handle_sleep_entry 中卸載的模組一致）
        # 包含完整的模組配置信息
        modules_to_reload = [
            {"module_id": "stt", "module_name": "stt_module", "module_type": ModuleType.INPUT, "capabilities": ModuleCapabilities.STT_CAPABILITIES, "priority": 100},
            {"module_id": "nlp", "module_name": "nlp_module", "module_type": ModuleType.PROCESSING, "capabilities": ModuleCapabilities.NLP_CAPABILITIES, "priority": 90},
            {"module_id": "llm", "module_name": "llm_module", "module_type": ModuleType.PROCESSING, "capabilities": ModuleCapabilities.LLM_CAPABILITIES, "priority": 80},
            {"module_id": "mem", "module_name": "mem_module", "module_type": ModuleType.PROCESSING, "capabilities": ModuleCapabilities.MEM_CAPABILITIES, "priority": 70},
            {"module_id": "tts", "module_name": "tts_module", "module_type": ModuleType.OUTPUT, "capabilities": ModuleCapabilities.TTS_CAPABILITIES, "priority": 60},
            {"module_id": "sys", "module_name": "sys_module", "module_type": ModuleType.PROCESSING, "capabilities": ModuleCapabilities.SYS_CAPABILITIES, "priority": 30}
        ]
        
        reloaded_modules = []
        
        for config in modules_to_reload:
            module_id = config["module_id"]
            
            # 檢查模組是否已在 Framework 中
            if core_framework.get_module(module_id) is not None:
                debug_log(2, f"[WakeAPI] 模組 {module_id} 已載入，跳過")
                reloaded_modules.append(module_id)
                continue
            
            # 重新載入並註冊模組（使用 Framework 的方法）
            try:
                info_log(f"[WakeAPI] 🔄 重載模組: {module_id}")
                
                # 使用 Framework 的 _try_register_module 方法
                success = core_framework._try_register_module(config)
                
                if success:
                    reloaded_modules.append(module_id)
                    info_log(f"[WakeAPI] ✅ 模組 {module_id} 重載成功")
                else:
                    error_log(f"[WakeAPI] ❌ 模組 {module_id} 重載失敗")
                    
            except Exception as e:
                error_log(f"[WakeAPI] 重載模組 {module_id} 時發生錯誤: {e}")
                import traceback
                error_log(traceback.format_exc())
        
        info_log(f"[WakeAPI] ✅ 模組重載完成: {len(reloaded_modules)}/{len(modules_to_reload)}")
        
        # 強制垃圾回收，清理重載過程中的臨時對象
        import gc
        gc.collect()
        debug_log(2, "[WakeAPI] 🗑️ 垃圾回收完成")
        
        # 檢查關鍵模組狀態
        available_modules = []
        for config in modules_to_reload:
            module_id = config["module_id"]
            module = core_framework.get_module(module_id)
            if module is not None:
                available_modules.append(module_id)
                debug_log(3, f"[WakeAPI] ✓ 模組可用: {module_id}")
            else:
                debug_log(2, f"[WakeAPI] ✗ 模組未載入: {module_id}")
        
        info_log(f"[WakeAPI] 模組檢查完成，可用: {len(available_modules)}/{len(modules_to_reload)}")
        
        # 恢復 sys_module 的監控任務
        if "sys" in available_modules:
            try:
                sys_module = core_framework.get_module("sys")
                if sys_module and hasattr(sys_module, '_restore_monitoring_tasks'):
                    sys_module._restore_monitoring_tasks()
                    info_log("[WakeAPI] ✅ 已恢復 sys_module 監控任務")
            except Exception as e:
                error_log(f"[WakeAPI] 恢復監控任務失敗: {e}")
        
        # 🔗 重新建立 LLM-SYS MCP 連接
        if "llm" in available_modules and "sys" in available_modules:
            try:
                llm_module = core_framework.get_module("llm")
                sys_module = core_framework.get_module("sys")
                
                if llm_module and sys_module and hasattr(sys_module, 'mcp_server'):
                    if hasattr(llm_module, 'set_mcp_server'):
                        llm_module.set_mcp_server(sys_module.mcp_server)
                        info_log("[WakeAPI] ✅ LLM-SYS MCP 連接已重新建立")
                    else:
                        debug_log(2, "[WakeAPI] ⚠️  LLM 模組沒有 set_mcp_server 方法")
                else:
                    debug_log(2, "[WakeAPI] ⚠️  無法建立 MCP 連接：模組或 mcp_server 不可用")
            except Exception as e:
                error_log(f"[WakeAPI] 重新建立 MCP 連接失敗: {e}")
        
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
