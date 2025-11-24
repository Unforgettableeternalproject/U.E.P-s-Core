import asyncio
from core.registry import get_module
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log

config = load_config()
enabled = config.get("modules_enabled", {})

# 載入模式控制：True=預先載入所有模組(舊版終端), False=按需載入(GUI模式)
PRELOAD_MODULES = None  # 預設為 None，等待明確設定

def safe_get_module(name):
    if not enabled.get(name, False):
        # print(f"[Controller] [X] 模組 '{name}' 未啟用，請檢查配置") # Ignored
        return None

    info_log(f"[Controller] 嘗試載入模組 '{name}'")

    try:
        mod = get_module(name)
        if mod is None:
            raise ImportError(f"{name} register() 回傳為 None")
        info_log(f"[Controller] [OK] 載入模組成功：{name}")
        return mod
    except NotImplementedError:
        error_log(f"[Controller] [X] 模組 '{name}' 尚未被實作")
        return None
    except Exception as e:
        error_log(f"[Controller] [X] 無法載入模組 '{name}': {e}")
        return None

# 模組字典 - 延遲初始化
modules = {}
modules_load_times = {}  # 儲存模組載入的時間戳

def _setup_module_connections():
    """設置模組間的連接（例如 LLM-SYS MCP 連接）"""
    try:
        # 1. 連接 LLM 和 SYS 的 MCP Server
        llm_module = modules.get("llm")
        sys_module = modules.get("sysmod")
        
        if llm_module and sys_module:
            # 檢查 SYS 模組是否有 MCP Server
            if hasattr(sys_module, 'mcp_server'):
                # 將 MCP Server 傳遞給 LLM 模組
                if hasattr(llm_module, 'set_mcp_server'):
                    llm_module.set_mcp_server(sys_module.mcp_server)
                    info_log("[Controller] ✅ LLM-SYS MCP 連接已建立")
                else:
                    debug_log(2, "[Controller] ⚠️  LLM 模組沒有 set_mcp_server 方法")
            else:
                debug_log(2, "[Controller] ⚠️  SYS 模組沒有 mcp_server 屬性")
        else:
            debug_log(2, f"[Controller] ⚠️  模組不可用 - LLM: {llm_module is not None}, SYS: {sys_module is not None}")
        
        # 未來可以在這裡添加其他模組間連接
        
    except Exception as e:
        error_log(f"[Controller] 模組間連接設置失敗: {e}")

def _initialize_modules():
    """根據當前載入模式初始化模組字典"""
    global modules, modules_load_times
    
    if PRELOAD_MODULES is True:
        # 舊版模式：預先載入所有模組（但排除UI相關模組，避免終端測試時的問題）
        info_log("[Controller] 初始化：預先載入模組（終端模式，排除UI）")
        
        # 模組名稱映射：full_name -> short_name
        module_mapping = {
            "stt_module": "stt",
            "nlp_module": "nlp",
            "mem_module": "mem",
            "llm_module": "llm",
            "tts_module": "tts",
            "sys_module": "sysmod"  # 注意：sys_module 映射到 sysmod 而不是 sys
        }
        
        # 清空並重新載入模組字典
        modules.clear()
        for full_name, short_name in module_mapping.items():
            module_instance = safe_get_module(full_name)
            modules[short_name] = module_instance
            
            # 記錄載入時間
            if module_instance is not None:
                from datetime import datetime
                modules_load_times[short_name] = datetime.now().strftime('%H:%M:%S')
        
        # 為UI相關模組設定為None（在終端模式下不載入）
        modules["ui"] = None
        modules["ani"] = None  
        modules["mov"] = None
        
        # 🔗 建立模組間連接（在所有模組初始化後）
        _setup_module_connections()
    else:
        # GUI模式：延遲載入
        info_log("[Controller] 初始化：按需載入模式")
        modules.clear()
        modules.update({
            "stt": None,
            "nlp": None,
            "mem": None,
            "llm": None,
            "tts": None,
            "sysmod": None,
            "ui": None,
            "ani": None,
            "mov": None
        })
        # 初始化載入時間字典，預設為空
        modules_load_times.clear()

def set_loading_mode(preload=True, reinitialize=False):
    """設定模組載入模式
    Args:
        preload (bool): True=預先載入所有模組, False=按需載入
        reinitialize (bool): True=強制重新初始化模組字典
    """
    global PRELOAD_MODULES
    PRELOAD_MODULES = preload

    info_log(f"[Controller] 設定載入模式：{'預先載入' if preload else '按需載入'}")
    
    # 如果模組字典尚未初始化，或者要求重新初始化，則進行初始化
    if not modules or reinitialize:
        _initialize_modules()

def complete_reset_all_modules():
    """完全重置所有模組實例，回歸原始狀態"""
    global modules, modules_load_times
    
    info_log("[Controller] 開始完全重置所有模組...")
    
    # 清理所有現有模組實例
    all_module_names = ['stt', 'nlp', 'mem', 'llm', 'tts', 'sysmod', 'ui', 'ani', 'mov']
    
    for module_name in all_module_names:
        if module_name in modules and modules[module_name] is not None:
            try:
                module_instance = modules[module_name]
                
                # 嘗試調用shutdown方法
                if hasattr(module_instance, 'shutdown'):
                    module_instance.shutdown()
                    info_log(f"[Controller] 已關閉 {module_name} 模組")
                # 如果沒有shutdown，嘗試stop方法
                elif hasattr(module_instance, 'stop'):
                    module_instance.stop()
                    info_log(f"[Controller] 已停止 {module_name} 模組")
                
            except Exception as e:
                error_log(f"[Controller] 關閉 {module_name} 模組時發生錯誤: {e}")
    
    # 嘗試清理QApplication實例（但保留調試介面使用的QApplication）
    try:
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            info_log("[Controller] 檢測到QApplication實例")
            
            # 檢查是否有調試介面正在運行
            debug_window_active = False
            for widget in app.topLevelWidgets():
                widget_name = widget.__class__.__name__
                if 'Debug' in widget_name or 'debug' in widget_name.lower():
                    debug_window_active = True
                    info_log(f"[Controller] 檢測到調試介面視窗: {widget_name}")
                    break
            
            if debug_window_active:
                info_log("[Controller] 調試介面正在運行，保留QApplication但關閉其他視窗")
                # 只關閉非調試介面的視窗
                for widget in list(app.topLevelWidgets()):
                    widget_name = widget.__class__.__name__
                    if ('Debug' not in widget_name and 
                        'debug' not in widget_name.lower() and
                        widget.isVisible()):
                        try:
                            info_log(f"[Controller] 關閉非調試視窗: {widget_name}")
                            widget.close()
                        except Exception as e:
                            error_log(f"[Controller] 關閉視窗失敗: {e}")
            else:
                info_log("[Controller] 無調試介面運行，關閉所有視窗和QApplication")
                # 關閉所有頂級視窗
                for widget in app.topLevelWidgets():
                    try:
                        if widget.isVisible():
                            info_log(f"[Controller] 關閉頂級視窗: {widget}")
                            widget.close()
                    except Exception as e:
                        error_log(f"[Controller] 關閉頂級視窗失敗: {e}")
                
                # 處理所有待處理事件
                app.processEvents()
                
                # 嘗試退出QApplication
                try:
                    app.quit()
                    info_log("[Controller] QApplication已退出")
                except Exception as e:
                    error_log(f"[Controller] QApplication退出失敗: {e}")
                
    except ImportError:
        # PyQt5未安裝或不可用
        pass
    except Exception as e:
        error_log(f"[Controller] 清理QApplication時發生錯誤: {e}")
    
    # 完全清空模組字典和載入時間記錄
    modules.clear()
    modules_load_times.clear()
    
    # 清理core.registry的模組快取
    try:
        from core.registry import _loaded_modules
        _loaded_modules.clear()
        info_log("[Controller] core.registry模組快取已清理")
    except ImportError:
        info_log("[Controller] 無法導入core.registry，跳過快取清理")
    except Exception as e:
        error_log(f"[Controller] 清理core.registry快取時發生錯誤: {e}")
    
    # 強制垃圾回收
    import gc
    gc.collect()
    
    info_log("[Controller] 所有模組已完全重置，已清空模組字典和執行垃圾回收")

def switch_to_terminal_mode():
    """切換到終端模式 - 完全重置後預先載入非UI模組"""
    # 完全重置所有模組
    complete_reset_all_modules()
    set_loading_mode(preload=True, reinitialize=True)

def switch_to_gui_mode():
    """切換到GUI模式 - 完全重置後按需載入所有模組"""
    # 完全重置所有模組
    complete_reset_all_modules()
    set_loading_mode(preload=False, reinitialize=True)

def cleanup_frontend_modules():
    """清理前端模組實例，防止GUI關閉後繼續運行"""
    frontend_modules = ['ui', 'ani', 'mov']
    
    info_log("[Controller] 開始清理前端模組...")
    
    # 首先嘗試清理UI模組的介面實例
    if 'ui' in modules and modules['ui'] is not None:
        try:
            ui_module = modules['ui']
            
            # 直接訪問UI模組的interfaces字典並清理DesktopPetApp
            if hasattr(ui_module, 'interfaces'):
                for interface_type, interface in list(ui_module.interfaces.items()):
                    if interface is not None:
                        try:
                            info_log(f"[Controller] 清理UI介面實例: {interface_type}")
                            if hasattr(interface, 'close'):
                                interface.close()
                            elif hasattr(interface, 'shutdown'):
                                interface.shutdown()
                        except Exception as e:
                            error_log(f"[Controller] 清理UI介面 {interface_type} 失敗: {e}")
                
                # 清空interfaces字典
                ui_module.interfaces.clear()
                if hasattr(ui_module, 'active_interfaces'):
                    ui_module.active_interfaces.clear()
                    
            info_log("[Controller] UI模組介面實例清理完成")
            
        except Exception as e:
            error_log(f"[Controller] 清理UI模組介面時發生錯誤: {e}")
    
    # 然後清理模組實例
    for module_name in frontend_modules:
        if module_name in modules and modules[module_name] is not None:
            try:
                module_instance = modules[module_name]
                
                # 嘗試調用shutdown方法
                if hasattr(module_instance, 'shutdown'):
                    module_instance.shutdown()
                    info_log(f"[Controller] 已關閉 {module_name} 模組")
                # 如果沒有shutdown，嘗試stop方法
                elif hasattr(module_instance, 'stop'):
                    module_instance.stop()
                    info_log(f"[Controller] 已停止 {module_name} 模組")
                else:
                    info_log(f"[Controller] {module_name} 模組沒有關閉方法")
                
                # 清除模組引用
                modules[module_name] = None
                modules_load_times.pop(module_name, None)
                
            except Exception as e:
                error_log(f"[Controller] 清理 {module_name} 模組時發生錯誤: {e}")
    
    # 嘗試清理所有QApplication實例
    try:
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            info_log("[Controller] 檢測到QApplication實例，正在關閉...")
            
            # 關閉所有頂級視窗
            for widget in app.topLevelWidgets():
                try:
                    if widget.isVisible():
                        info_log(f"[Controller] 關閉頂級視窗: {widget}")
                        widget.close()
                except Exception as e:
                    error_log(f"[Controller] 關閉頂級視窗失敗: {e}")
            
            # 處理所有待處理事件
            app.processEvents()
            
            # 嘗試退出QApplication
            try:
                app.quit()
                info_log("[Controller] QApplication已退出")
            except Exception as e:
                error_log(f"[Controller] QApplication退出失敗: {e}")
                
    except ImportError:
        # PyQt5未安裝或不可用
        pass
    except Exception as e:
        error_log(f"[Controller] 清理QApplication時發生錯誤: {e}")
    
    # 強制垃圾回收
    import gc
    gc.collect()
    info_log("[Controller] 前端模組清理完成，已執行垃圾回收")

def get_module_load_time(name):
    """獲取模組載入時間
    Args:
        name (str): 模組名稱
    
    Returns:
        str: 載入時間 (HH:MM:SS) 或 'N/A' 如未載入
    """
    return modules_load_times.get(name, 'N/A')

def get_or_load_module(name):
    """獲取或載入模組 - 支援兩種模式
    在預先載入模式下直接返回已載入的模組
    在按需載入模式下動態載入模組
    """
    # 如果尚未初始化，先使用預設的按需載入模式
    if PRELOAD_MODULES is None:
        info_log("[Controller] 警告：模組載入模式尚未設定，使用預設按需載入模式")
        set_loading_mode(preload=False)
    
    # 檢查模組字典是否已初始化
    if name not in modules:
        info_log(f"[Controller] 警告：模組字典未正確初始化，重新初始化")
        _initialize_modules()
    
    if PRELOAD_MODULES:
        return modules[name]
    else:
        if modules[name] is None:
            # 載入模組
            modules[name] = safe_get_module(f"{name}_module")
            
            # 如果模組載入成功，記錄載入時間
            if modules[name] is not None:
                from datetime import datetime
                modules_load_times[name] = datetime.now().strftime('%H:%M:%S')
                debug_log(1, f"[Controller] 模組 '{name}' 載入時間: {modules_load_times[name]}")
                
                # 🔗 按需載入時，檢查是否需要建立模組間連接
                # 如果剛載入的是 LLM 或 SYS，嘗試建立 MCP 連接
                if name in ['llm', 'sysmod']:
                    _check_and_setup_mcp_connection()
        
        return modules[name]

def _check_and_setup_mcp_connection():
    """檢查並建立 LLM-SYS MCP 連接（用於按需載入模式）"""
    try:
        llm_module = modules.get("llm")
        sys_module = modules.get("sysmod")
        
        # 只有當兩個模組都載入且尚未連接時才建立連接
        if llm_module and sys_module:
            # 檢查是否已經連接
            if hasattr(llm_module, 'mcp_client') and hasattr(llm_module.mcp_client, 'mcp_server'):
                if llm_module.mcp_client.mcp_server is not None:
                    # 已經連接，不需要重複建立
                    return
            
            # 建立連接
            if hasattr(sys_module, 'mcp_server') and hasattr(llm_module, 'set_mcp_server'):
                llm_module.set_mcp_server(sys_module.mcp_server)
                info_log("[Controller] ✅ LLM-SYS MCP 連接已建立（按需載入模式）")
    except Exception as e:
        debug_log(2, f"[Controller] 檢查 MCP 連接時出錯: {e}")


# 測試 STT 模組
from .module_tests.stt_tests import (
    stt_test_single, stt_test_continuous_listening, stt_get_stats,
    stt_speaker_list, stt_speaker_rename, stt_speaker_delete,
    stt_speaker_clear_all, stt_speaker_backup, stt_speaker_restore,
    stt_speaker_info, stt_speaker_adjust_threshold
)

# 測試 NLP 模組
from .module_tests.nlp_tests import (
    nlp_test, nlp_test_state_queue_integration, nlp_test_multi_intent,
    nlp_test_identity_management, nlp_analyze_context_queue, nlp_clear_contexts
)

# 測試 Frontend 模組
from .module_tests.frontend_tests import (
    show_desktop_pet, hide_desktop_pet, control_desktop_pet,
    test_mov_ani_integration, test_behavior_modes, test_animation_state_machine,
    frontend_test_full, frontend_get_status, frontend_test_animations,
    frontend_test_user_interaction
)

# 測試 MEM 模組（簡化版 - 純功能測試）
from .module_tests.mem_tests import (
    mem_test_store_memory, mem_test_memory_query, 
    mem_test_conversation_snapshot, mem_test_identity_stats,
    mem_test_write_then_query
)

# 測試 LLM 模組（簡化版 - 純功能測試）
from .module_tests.llm_tests import (
    llm_test_chat, llm_test_command,
    llm_test_learning_engine,
    llm_test_system_status_monitoring
)

# 測試 TTS 模組
from .module_tests.tts_tests import (
    tts_emotion_variation_test, tts_interactive_synthesis, tts_streaming_test
)

# SYS 模組測試（已重構）
# 注意：不在這裡頂層導入，而是在 wrapper 函數內部導入以避免名稱衝突

# 創建包裝函數，自動傳遞 modules 參數

# STT 模組包裝函數
def stt_test_single_wrapper(enable_speaker_id=True, language="en-US"):
    from .module_tests.stt_tests import stt_test_single as stt_test_single_func
    return stt_test_single_func(modules, enable_speaker_id, language)

def stt_test_continuous_listening_wrapper(duration=30):
    from .module_tests.stt_tests import stt_test_continuous_listening as stt_test_continuous_listening_func
    return stt_test_continuous_listening_func(modules, duration)

def stt_get_stats_wrapper():
    from .module_tests.stt_tests import stt_get_stats as stt_get_stats_func
    return stt_get_stats_func(modules)

def stt_speaker_list_wrapper():
    from .module_tests.stt_tests import stt_speaker_list as stt_speaker_list_func
    return stt_speaker_list_func(modules)

def stt_speaker_rename_wrapper(old_id: str, new_id: str):
    from .module_tests.stt_tests import stt_speaker_rename as stt_speaker_rename_func
    return stt_speaker_rename_func(modules, old_id, new_id)

def stt_speaker_delete_wrapper(speaker_id: str):
    from .module_tests.stt_tests import stt_speaker_delete as stt_speaker_delete_func
    return stt_speaker_delete_func(modules, speaker_id)

def stt_speaker_clear_all_wrapper():
    from .module_tests.stt_tests import stt_speaker_clear_all as stt_speaker_clear_all_func
    return stt_speaker_clear_all_func(modules)

def stt_speaker_backup_wrapper():
    from .module_tests.stt_tests import stt_speaker_backup as stt_speaker_backup_func
    return stt_speaker_backup_func(modules)

def stt_speaker_restore_wrapper(backup_path: str = None):
    from .module_tests.stt_tests import stt_speaker_restore as stt_speaker_restore_func
    return stt_speaker_restore_func(modules, backup_path)

def stt_speaker_info_wrapper():
    from .module_tests.stt_tests import stt_speaker_info as stt_speaker_info_func
    return stt_speaker_info_func(modules)

def stt_speaker_adjust_threshold_wrapper(threshold: float = None):
    from .module_tests.stt_tests import stt_speaker_adjust_threshold as stt_speaker_adjust_threshold_func
    return stt_speaker_adjust_threshold_func(modules, threshold)

# NLP 模組包裝函數
def nlp_test_wrapper(text: str = "", enable_identity: bool = True, enable_segmentation: bool = True):
    from .module_tests.nlp_tests import nlp_test as nlp_test_func
    return nlp_test_func(modules, text, enable_identity, enable_segmentation)

def nlp_test_state_queue_integration_wrapper(text: str = ""):
    from .module_tests.nlp_tests import nlp_test_state_queue_integration as nlp_test_state_queue_integration_func
    return nlp_test_state_queue_integration_func(modules, text)

def nlp_test_multi_intent_wrapper(text: str = ""):
    from .module_tests.nlp_tests import nlp_test_multi_intent as nlp_test_multi_intent_func
    return nlp_test_multi_intent_func(modules, text)

def nlp_test_identity_management_wrapper(speaker_id: str = "test_user"):
    from .module_tests.nlp_tests import nlp_test_identity_management as nlp_test_identity_management_func
    return nlp_test_identity_management_func(modules, speaker_id)

def nlp_analyze_context_queue_wrapper():
    from .module_tests.nlp_tests import nlp_analyze_context_queue as nlp_analyze_context_queue_func
    return nlp_analyze_context_queue_func(modules)

def nlp_clear_contexts_wrapper():
    from .module_tests.nlp_tests import nlp_clear_contexts as nlp_clear_contexts_func
    return nlp_clear_contexts_func(modules)

# SYS 模組包裝函數（新版工作流測試）
def sys_test_echo_wrapper():
    """SYS Echo 工作流測試"""
    from .module_tests.sys_tests import sys_test_echo as sys_test_echo_func
    return sys_test_echo_func(modules)

def sys_test_countdown_wrapper():
    """SYS Countdown 工作流測試"""
    from .module_tests.sys_tests import sys_test_countdown as sys_test_countdown_func
    return sys_test_countdown_func(modules)

def sys_test_data_collector_wrapper():
    """SYS Data Collector 工作流測試"""
    from .module_tests.sys_tests import sys_test_data_collector as sys_test_data_collector_func
    return sys_test_data_collector_func(modules)

def sys_test_random_fail_wrapper():
    """SYS Random Fail 工作流測試"""
    from .module_tests.sys_tests import sys_test_random_fail as sys_test_random_fail_func
    return sys_test_random_fail_func(modules)

# Frontend 模組包裝函數
def show_desktop_pet_wrapper():
    from .module_tests.frontend_tests import show_desktop_pet as show_desktop_pet_func
    return show_desktop_pet_func(modules)

def hide_desktop_pet_wrapper():
    from .module_tests.frontend_tests import hide_desktop_pet as hide_desktop_pet_func
    return hide_desktop_pet_func(modules)

def control_desktop_pet_wrapper(action="wave", duration=3, x=None, y=None):
    from .module_tests.frontend_tests import control_desktop_pet as control_desktop_pet_func
    return control_desktop_pet_func(modules, action, duration, x, y)

def test_mov_ani_integration_wrapper():
    from .module_tests.frontend_tests import test_mov_ani_integration as test_mov_ani_integration_func
    return test_mov_ani_integration_func(modules)

def test_behavior_modes_wrapper():
    from .module_tests.frontend_tests import test_behavior_modes as test_behavior_modes_func
    return test_behavior_modes_func(modules)

def test_animation_state_machine_wrapper():
    from .module_tests.frontend_tests import test_animation_state_machine as test_animation_state_machine_func
    return test_animation_state_machine_func(modules)

def frontend_test_full_wrapper():
    from .module_tests.frontend_tests import frontend_test_full as frontend_test_full_func
    return frontend_test_full_func(modules)

def frontend_get_status_wrapper():
    from .module_tests.frontend_tests import frontend_get_status as frontend_get_status_func
    return frontend_get_status_func(modules)

def frontend_test_animations_wrapper():
    from .module_tests.frontend_tests import frontend_test_animations as frontend_test_animations_func
    return frontend_test_animations_func(modules)

def frontend_test_user_interaction_wrapper():
    from .module_tests.frontend_tests import frontend_test_user_interaction as frontend_test_user_interaction_func
    return frontend_test_user_interaction_func(modules)

def launch_animation_tester():
    """啟動動畫測試器（獨立GUI工具）"""
    try:
        info_log("[Debug API] 正在啟動動畫測試器...")
        
        # 檢查 PyQt5 是否可用
        try:
            from PyQt5.QtWidgets import QApplication
        except ImportError:
            error_log("[Debug API] 無法導入 PyQt5，請確認已安裝")
            return {"success": False, "error": "PyQt5 未安裝"}
        
        # 導入並啟動動畫測試器
        import subprocess
        import sys
        from pathlib import Path
        
        # 獲取動畫測試器腳本路徑
        script_path = Path(__file__).parent / "animation_tester.py"
        
        if not script_path.exists():
            error_log(f"[Debug API] 找不到動畫測試器: {script_path}")
            return {"success": False, "error": "動畫測試器腳本不存在"}
        
        # 使用當前 Python 環境啟動
        subprocess.Popen([sys.executable, str(script_path)])
        
        info_log("[Debug API] 動畫測試器已在新進程中啟動")
        return {"success": True, "message": "動畫測試器已啟動"}
        
    except Exception as e:
        error_log(f"[Debug API] 啟動動畫測試器失敗: {e}")
        return {"success": False, "error": str(e)}

# MEM 模組包裝函數（簡化版 - 純功能測試）
def mem_test_store_memory_wrapper(identity="test_user", content="測試記憶內容", memory_type="long_term"):
    """MEM 記憶存儲測試包裝函數"""
    # 設置測試環境
    env_result = setup_test_environment_for_module("mem")
    if not env_result["success"]:
        return env_result
    
    try:
        from .module_tests.mem_tests import mem_test_store_memory as mem_test_func
        result = mem_test_func(modules, identity, content, memory_type)
        return result
    finally:
        # 清理測試環境
        cleanup_test_environment()

def mem_test_conversation_snapshot_wrapper(identity="test_user", conversation="你好，今天天氣如何？"):
    """MEM 對話快照測試包裝函數"""
    # 設置測試環境
    env_result = setup_test_environment_for_module("mem")
    if not env_result["success"]:
        return env_result
    
    try:
        from .module_tests.mem_tests import mem_test_conversation_snapshot as mem_test_func
        result = mem_test_func(modules, identity, conversation)
        return result
    finally:
        # 清理測試環境
        cleanup_test_environment()

def mem_test_write_then_query_wrapper(identity="test_user"):
    """MEM 寫入後查詢測試包裝函數"""
    # 設置測試環境
    env_result = setup_test_environment_for_module("mem")
    if not env_result["success"]:
        return env_result
    
    try:
        from .module_tests.mem_tests import mem_test_write_then_query as mem_test_func
        result = mem_test_func(modules, identity)
        return result
    finally:
        # 清理測試環境
        cleanup_test_environment()

def mem_test_memory_query_wrapper(identity="test_user", query_text="天氣"):
    """MEM 記憶查詢測試包裝函數"""
    # 設置測試環境
    env_result = setup_test_environment_for_module("mem")
    if not env_result["success"]:
        return env_result
    
    try:
        from .module_tests.mem_tests import mem_test_memory_query as mem_test_func
        result = mem_test_func(modules, identity, query_text)
        return result
    finally:
        # 清理測試環境
        cleanup_test_environment()

def mem_test_identity_stats_wrapper(identity="test_user"):
    """MEM 身份統計測試包裝函數"""
    # 設置測試環境
    env_result = setup_test_environment_for_module("mem")
    if not env_result["success"]:
        return env_result
    
    try:
        from .module_tests.mem_tests import mem_test_identity_stats as mem_test_func
        result = mem_test_func(modules, identity)
        return result
    finally:
        # 清理測試環境
        cleanup_test_environment()

# LLM 模組包裝函數（簡化版 - 純功能測試）
def llm_test_chat_wrapper(text: str = "你好，請介紹一下你自己"):
    """LLM 聊天測試包裝函數"""
    # 設置測試環境 - 指定 CHAT 模式
    env_result = setup_test_environment_for_module("llm", test_mode="chat")
    if not env_result["success"]:
        return env_result
    
    try:
        from .module_tests.llm_tests import llm_test_chat as llm_test_chat_func
        result = llm_test_chat_func(modules, text)
        return result
    finally:
        # 清理測試環境
        cleanup_test_environment()

def llm_test_command_wrapper(text: str = "幫我整理桌面文件"):
    """LLM 指令測試包裝函數"""
    # 設置測試環境 - 指定 WORK 模式
    env_result = setup_test_environment_for_module("llm", test_mode="work")
    if not env_result["success"]:
        return env_result
    
    try:
        from .module_tests.llm_tests import llm_test_command as llm_test_command_func
        result = llm_test_command_func(modules, text)
        return result
    finally:
        # 清理測試環境
        cleanup_test_environment()

def llm_test_cache_functionality_wrapper():
    """LLM 快取功能測試包裝函數"""
    # 設置測試環境
    env_result = setup_test_environment_for_module("llm")
    if not env_result["success"]:
        return env_result
    
    try:
        from .module_tests.llm_tests import llm_test_cache_functionality as llm_test_cache_func
        result = llm_test_cache_func(modules)
        return result
    finally:
        # 清理測試環境
        cleanup_test_environment()

def llm_test_learning_engine_wrapper():
    """LLM 學習引擎測試包裝函數"""
    # 設置測試環境
    env_result = setup_test_environment_for_module("llm")
    if not env_result["success"]:
        return env_result
    
    try:
        from .module_tests.llm_tests import llm_test_learning_engine as llm_test_learning_func
        result = llm_test_learning_func(modules)
        return result
    finally:
        # 清理測試環境
        cleanup_test_environment()

def llm_test_system_status_monitoring_wrapper():
    """LLM 系統狀態監控測試包裝器"""
    try:
        # 設置測試環境
        env_result = setup_test_environment_for_module("llm")
        from .module_tests.llm_tests import llm_test_system_status_monitoring as llm_test_status_func
        result = llm_test_status_func(modules)
        return result
    finally:
        # 清理測試環境
        cleanup_test_environment()


# TTS 模組包裝函數 (✅ 已重構)
def tts_interactive_synthesis_wrapper():
    """TTS 即時合成測試 - 連續輸入文本和情緒"""
    from .module_tests.tts_tests import tts_interactive_synthesis
    return tts_interactive_synthesis(modules)

def tts_emotion_variation_test_wrapper():
    """情感變化測試 - 同一文本,不同情緒"""
    from .module_tests.tts_tests import tts_emotion_variation_test
    return tts_emotion_variation_test(modules)

def tts_streaming_test_wrapper():
    """串流測試 - 長文本分段合成"""
    from .module_tests.tts_tests import tts_streaming_test
    return tts_streaming_test(modules)

# TTS GUI 測試包裝函數 (✅ 用於 Debug GUI)
def tts_synthesis_wrapper(text: str, emotion_vector=None, save=False, output_path=None, force_chunking=False):
    """
    GUI 語音合成包裝函數
    
    Args:
        text: 要合成的文本
        emotion_vector: 情感向量 (8D list)
        save: 是否儲存
        output_path: 儲存路徑
        force_chunking: 是否強制分段
    
    Returns:
        dict: 測試結果
    """
    import time
    import os
    
    tts_module = get_or_load_module("tts")
    if not tts_module:
        return {"success": False, "error": "TTS 模組未載入"}
    
    try:
        start_time = time.time()
        
        # 構建請求數據
        request_data = {
            "text": text,
            "save": save,
            "force_chunking": force_chunking
        }
        
        if emotion_vector:
            request_data["emotion_vector"] = emotion_vector
        
        # 調用 TTS 模組
        result = tts_module.handle(request_data)
        processing_time = time.time() - start_time
        
        if result.get("status") == "success":
            final_output_path = result.get("output_path")
            
            # 如果用戶指定了輸出路徑且選擇儲存,則移動文件
            if output_path and save and final_output_path and os.path.exists(final_output_path):
                import shutil
                try:
                    os.makedirs(os.path.dirname(output_path), exist_ok=True)
                    shutil.move(final_output_path, output_path)
                    final_output_path = output_path
                    info_log(f"[TTS GUI] 文件已移動到: {output_path}")
                except Exception as e:
                    error_log(f"[TTS GUI] 移動文件失敗: {e}")
            
            return {
                "success": True,
                "duration": result.get("duration", 0.0),
                "processing_time": processing_time,
                "chunk_count": result.get("chunk_count", 1),
                "output_path": final_output_path
            }
        else:
            return {
                "success": False,
                "error": result.get("message", "未知錯誤"),
                "processing_time": processing_time
            }
    
    except Exception as e:
        error_log(f"[TTS GUI] 合成失敗: {e}")
        return {"success": False, "error": str(e)}

def tts_stop_playback_wrapper():
    """停止 TTS 播放"""
    tts_module = get_or_load_module("tts")
    if not tts_module:
        return {"success": False, "error": "TTS 模組未載入"}
    
    try:
        if hasattr(tts_module, 'stop_playback'):
            tts_module.stop_playback()
            return {"success": True}
        elif hasattr(tts_module, '_current_playback_obj') and tts_module._current_playback_obj:
            tts_module._current_playback_obj.stop()
            return {"success": True}
        else:
            return {"success": True, "message": "當前沒有播放中的音頻"}
    except Exception as e:
        error_log(f"[TTS GUI] 停止播放失敗: {e}")
        return {"success": False, "error": str(e)}

def tts_clear_queue_wrapper():
    """清除 TTS 播放隊列"""
    tts_module = get_or_load_module("tts")
    if not tts_module:
        return {"success": False, "error": "TTS 模組未載入"}
    
    try:
        if hasattr(tts_module, 'chunker'):
            queue_size = len(tts_module.chunker.queue)
            tts_module.chunker.stop()
            info_log(f"[TTS GUI] 已清除 {queue_size} 個隊列項目")
            return {"success": True, "cleared_items": queue_size}
        else:
            return {"success": True, "message": "沒有待清除的隊列"}
    except Exception as e:
        error_log(f"[TTS GUI] 清除隊列失敗: {e}")
        return {"success": False, "error": str(e)}

# 為了向後兼容，保留原來的函數名稱
stt_test_single = stt_test_single_wrapper
stt_test_continuous_listening = stt_test_continuous_listening_wrapper
stt_get_stats = stt_get_stats_wrapper
stt_speaker_list = stt_speaker_list_wrapper
stt_speaker_rename = stt_speaker_rename_wrapper
stt_speaker_delete = stt_speaker_delete_wrapper
stt_speaker_clear_all = stt_speaker_clear_all_wrapper
stt_speaker_backup = stt_speaker_backup_wrapper
stt_speaker_restore = stt_speaker_restore_wrapper
stt_speaker_info = stt_speaker_info_wrapper
stt_speaker_adjust_threshold = stt_speaker_adjust_threshold_wrapper

nlp_test = nlp_test_wrapper
nlp_test_state_queue_integration = nlp_test_state_queue_integration_wrapper
nlp_test_multi_intent = nlp_test_multi_intent_wrapper
nlp_test_identity_management = nlp_test_identity_management_wrapper
nlp_analyze_context_queue = nlp_analyze_context_queue_wrapper
nlp_clear_contexts = nlp_clear_contexts_wrapper

# Frontend 函數別名（用於 debug_api 與 frontend_test_tab 整合）
show_desktop_pet = show_desktop_pet_wrapper
hide_desktop_pet = hide_desktop_pet_wrapper
control_desktop_pet = control_desktop_pet_wrapper
test_mov_ani_integration = test_mov_ani_integration_wrapper
test_behavior_modes = test_behavior_modes_wrapper
test_animation_state_machine = test_animation_state_machine_wrapper
frontend_test_full = frontend_test_full_wrapper
frontend_test_status = frontend_get_status_wrapper  # 別名匹配
frontend_get_status = frontend_get_status_wrapper
frontend_test_animations = frontend_test_animations_wrapper
frontend_test_user_interaction = frontend_test_user_interaction_wrapper

# MEM 函數別名（簡化版 - 純功能測試）
mem_test_store_memory = mem_test_store_memory_wrapper
mem_test_write_then_query = mem_test_write_then_query_wrapper
mem_test_conversation_snapshot = mem_test_conversation_snapshot_wrapper
mem_test_memory_query = mem_test_memory_query_wrapper
mem_test_identity_stats = mem_test_identity_stats_wrapper

# LLM 函數別名（簡化版 - 純功能測試）
llm_test_chat = llm_test_chat_wrapper
llm_test_command = llm_test_command_wrapper
llm_test_cache_functionality = llm_test_cache_functionality_wrapper
llm_test_learning_engine = llm_test_learning_engine_wrapper
llm_test_system_status_monitoring = llm_test_system_status_monitoring_wrapper

# 為了向後兼容，添加一些常用的別名
llm_test_generation = llm_test_chat_wrapper
llm_test_completion = llm_test_chat_wrapper
llm_test_qa = llm_test_chat_wrapper
llm_test_conversation = llm_test_chat_wrapper
llm_test_work = llm_test_command_wrapper
llm_test_instruction = llm_test_command_wrapper
llm_test_cache = llm_test_cache_functionality_wrapper
llm_test_learning = llm_test_learning_engine_wrapper
llm_test_status = llm_test_system_status_monitoring_wrapper
llm_test_status_monitor = llm_test_system_status_monitoring_wrapper

# TTS 函數別名 (✅ 已重構)
tts_interactive_synthesis = tts_interactive_synthesis_wrapper
tts_emotion_variation_test = tts_emotion_variation_test_wrapper
tts_streaming_test = tts_streaming_test_wrapper
# 向後兼容別名
tts_test = tts_interactive_synthesis_wrapper  # 預設使用互動式測試
tts_test_emotion = tts_emotion_variation_test_wrapper
tts_test_stream = tts_streaming_test_wrapper

# SYS 測試別名（新版工作流測試）
sys_test_echo = sys_test_echo_wrapper
sys_test_countdown = sys_test_countdown_wrapper
sys_test_data_collector = sys_test_data_collector_wrapper
sys_test_random_fail = sys_test_random_fail_wrapper

# 額外測試（暫時停用，等相關模組完成後再啟用）

def test_summrize():
    """摘要測試 - 暫時停用"""
    # test_chunk_and_summarize()
    print("⚠️ 摘要測試功能暫時停用，等相關模組完成後再啟用")

def test_chat():
    """聊天測試 - 暫時停用"""
    # test_uep_chatting(modules)
    print("⚠️ 聊天測試功能暫時停用，等相關模組完成後再啟用")

# === 工作上下文管理功能 ===

def setup_working_context():
    """初始化工作上下文管理器"""
    from core.working_context import working_context_manager, ContextType
    
    # 註冊決策處理器
    try:
        # 註冊語者識別決策處理器
        if modules.get("stt"):
            from modules.stt_module.speaker_context_handler import SpeakerContextHandler
            speaker_handler = SpeakerContextHandler(modules["stt"])
            working_context_manager.register_decision_handler(ContextType.SPEAKER_ACCUMULATION, speaker_handler)
            info_log("[Controller] 語者識別決策處理器已註冊")
    except Exception as e:
        error_log(f"[Controller] 註冊決策處理器失敗: {e}")
    
    info_log("[Controller] 工作上下文管理器已初始化")

def cleanup_session_contexts(min_samples: int = 15):
    """
    清理會話結束時未完成的上下文
    
    Args:
        min_samples: 最小樣本數，低於此數值的語者上下文將被清理
    """
    from core.working_context import working_context_manager, ContextType
    
    info_log(f"[Controller] 開始清理會話上下文 (最小樣本數: {min_samples})")
    
    # 清理語者識別相關的未完成上下文
    cleaned_count = working_context_manager.cleanup_incomplete_contexts(
        context_type=ContextType.SPEAKER_ACCUMULATION,
        min_threshold=min_samples
    )
    
    if cleaned_count > 0:
        info_log(f"[Controller] 清理了 {cleaned_count} 個樣本不足的語者上下文")
    else:
        info_log("[Controller] 沒有需要清理的語者上下文")
    
    # 注意：不在這裡調用 cleanup_expired_contexts，因為已完成的上下文可能還有用
    
    return cleaned_count

def get_working_context_status():
    """獲取工作上下文狀態"""
    from core.working_context import working_context_manager
    
    contexts = working_context_manager.get_all_contexts_info()
    
    print("🔄 工作上下文狀態:")
    if not contexts:
        print("   無活躍的工作上下文")
        return
    
    for ctx in contexts:
        context_id = ctx['context_id']
        context_type = ctx['type']
        status = ctx['status']
        sample_count = ctx['sample_count']
        threshold = ctx['threshold']
        is_ready = ctx['is_ready']
        
        print(f"   {context_id}:")
        print(f"     類型: {context_type}")
        print(f"     狀態: {status}")
        print(f"     樣本: {sample_count}/{threshold}")
        print(f"     就緒: {'是' if is_ready else '否'}")
    
    return contexts

def get_deduplication_status():
    """
    獲取去重統計信息 (G. 監控與除錯)
    
    顯示 ModuleCoordinator 的去重命中次數、清理次數、活躍鍵數量等診斷資訊
    """
    from core.module_coordinator import module_coordinator
    
    stats = module_coordinator.get_deduplication_stats()
    
    print("🔍 去重系統診斷:")
    print(f"   去重命中次數: {stats['dedupe_hit_count']}")
    print(f"   清理次數: {stats['cleanup_count']}")
    print(f"   活躍去重鍵: {stats['active_dedupe_keys']} / {stats['max_dedupe_keys']}")
    print(f"   活躍流程數: {stats['active_flows']}")
    print(f"   記憶體壓力: {stats['memory_pressure']:.1%}")
    print(f"   各層分布: INPUT={stats['layers_distribution']['INPUT']}, "
          f"PROCESSING={stats['layers_distribution']['PROCESSING']}, "
          f"OUTPUT={stats['layers_distribution']['OUTPUT']}")
    
    return stats

def test_speaker_context_workflow():
    """測試語者上下文工作流程"""
    print("🎤 語者上下文工作流程測試")
    print("   這個測試會累積多個語音樣本，並觀察工作上下文的行為")
    
    # 初始化工作上下文
    setup_working_context()
    
    # 執行多次 STT 測試以累積樣本
    for i in range(5):
        print(f"\n--- 第 {i+1} 次語音識別 ---")
        result = stt_test_single(modules, enable_speaker_id=True, language="en-US")
        
        # 顯示工作上下文狀態
        get_working_context_status()
        
        if i < 4:  # 最後一次不需要暫停
            print("   按 Enter 繼續下一次測試...")
            input()
    
    print("\n✅ 語者上下文工作流程測試完成")

# ===== 統一測試環境管理 =====

def setup_test_environment_for_module(module_name: str, test_mode: str = None):
    """
    為指定模組設置測試環境（身份、會話、狀態）
    根據 U.E.P 三層會話架構，會自動創建 GS 容器以支援 CS/WS 測試
    Args:
        module_name (str): 模組名稱 (llm, mem, sys)
        test_mode (str): 測試模式 ("chat", "work" 等)，會覆蓋預設狀態映射
    """
    info_log(f"[Debug API] 為 {module_name} 模組設置測試環境 (模式: {test_mode or '預設'})...")
    
    try:
        # 1. 設置測試身份（包含記憶令牌）
        from core.working_context import working_context_manager
        test_identity = {
            "identity_id": f"debug_test_{module_name}",
            "user_identity": f"debug_test_{module_name}",
            "personality_profile": "default",
            "conversation_preferences": {},
            "memory_token": f"test_debug_token_{module_name}"
        }
        
        # 設置身份（記憶令牌作為身份的一部分）
        working_context_manager.set_identity(test_identity)
        
        info_log(f"[Debug API] 已設置 {module_name} 測試身份: {test_identity['user_identity']}")
        info_log(f"[Debug API] 記憶令牌（包含在身份中）: {test_identity['memory_token']}")
        debug_log(2, f"[Debug API] 測試身份詳情: {test_identity}")
        
        # 驗證設置結果
        verify_identity = working_context_manager.get_current_identity()
        debug_log(2, f"[Debug API] 驗證身份: {verify_identity}")
        if verify_identity:
            debug_log(2, f"[Debug API] 驗證記憶令牌: {verify_identity.get('memory_token')}")
        
        # 2. 確保有活躍的 GS 容器（CS/WS 的先決條件）
        from core.sessions.session_manager import unified_session_manager
        current_gs = unified_session_manager.get_current_general_session()
        gs_session_id = None
        
        if not current_gs:
            # 需要創建 GS 容器以支援 CS/WS 測試
            info_log(f"[Debug API] 沒有活躍的 GS，為 {module_name} 測試創建 GS 容器")
            import time
            
            trigger_event = {
                "source": "debug_api",
                "module": module_name,
                "content": f"Debug API 測試環境設置 - {module_name} 模組",
                "timestamp": time.time()
            }
            
            # 創建 DEBUG 類型的 GS
            gs_session_id = unified_session_manager.start_general_session("system_event", trigger_event)
            if gs_session_id:
                info_log(f"[Debug API] 已創建 GS 容器: {gs_session_id}")
            else:
                error_log(f"[Debug API] 創建 GS 容器失敗")
                return {"success": False, "error": "創建 GS 容器失敗"}
        else:
            gs_session_id = current_gs.session_id
            info_log(f"[Debug API] 使用現有 GS 容器: {gs_session_id}")
        
        # 3. 根據模組類型和測試模式設置相應的系統狀態
        from core.states.state_manager import state_manager, UEPState
        
        # 如果有指定測試模式，優先使用
        if test_mode:
            mode_mapping = {
                "chat": UEPState.CHAT,
                "work": UEPState.WORK,
                "idle": UEPState.IDLE
            }
            target_state = mode_mapping.get(test_mode, UEPState.IDLE)
            info_log(f"[Debug API] 使用指定測試模式: {test_mode} → {target_state.value}")
        else:
            # 否則使用預設模組狀態映射
            state_mapping = {
                "llm": UEPState.CHAT,  # LLM 預設使用 CHAT 狀態
                "mem": UEPState.CHAT,  # MEM 也在 CHAT 狀態下測試
                "sys": UEPState.WORK,  # SYS 在 WORK 狀態下測試
            }
            target_state = state_mapping.get(module_name, UEPState.IDLE)
            info_log(f"[Debug API] 使用預設模組狀態: {module_name} → {target_state.value}")
        
        original_state = state_manager.get_current_state()
        
        if original_state != target_state:
            state_manager.set_state(target_state)
            info_log(f"[Debug API] 已切換系統狀態: {original_state.value} → {target_state.value}")
            # 狀態切換後，對應的會話會自動創建
        
        # 4. 驗證會話是否已自動創建（由狀態管理器觸發）
        import time
        time.sleep(0.1)  # 短暫等待狀態切換完成
        
        return {
            "success": True,
            "identity": test_identity,
            "state": target_state,
            "original_state": original_state,
            "gs_session_id": gs_session_id
        }
        
    except Exception as e:
        error_log(f"[Debug API] 設置 {module_name} 測試環境失敗: {e}")
        return {"success": False, "error": str(e)}

def cleanup_test_environment():
    """清理測試環境，恢復到初始狀態"""
    try:
        from core.states.state_manager import state_manager, UEPState
        from core.sessions.session_manager import unified_session_manager
        
        # 1. 結束任何活躍的子會話 (CS/WS)
        current_gs = unified_session_manager.get_current_general_session()
        if current_gs:
            info_log(f"[Debug API] 檢查是否需要清理子會話 (GS: {current_gs.session_id})")
            
            # 嘗試結束活躍的 CS (如果存在)
            try:
                # 這裡我們假設 session_manager 有方法來獲取當前 CS
                # 如果沒有，這個調用會安全地失敗
                current_cs = unified_session_manager.get_active_chatting_session_ids()
                if current_cs:
                    for cs in current_cs:
                        info_log(f"[Debug API] 結束活躍的 CS: {cs}")
                        unified_session_manager.end_chatting_session(cs) 
            except (AttributeError, Exception) as e:
                # 如果方法不存在或其他問題，忽略並繼續
                debug_log(2, f"[Debug API] CS 清理略過: {e}")
            
            # 嘗試結束活躍的 WS (如果存在)
            try:
                current_ws = unified_session_manager.get_active_workflow_session_ids()
                if current_ws:
                    for ws in current_ws:
                        info_log(f"[Debug API] 結束活躍的 WS: {ws}")
                        unified_session_manager.end_workflow_session(ws)
            except (AttributeError, Exception) as e:
                debug_log(2, f"[Debug API] WS 清理略過: {e}")
        
        # 2. 恢復到 IDLE 狀態（這會觸發狀態相關的會話清理）
        current_state = state_manager.get_current_state()
        if current_state != UEPState.IDLE:
            state_manager.set_state(UEPState.IDLE)
            info_log(f"[Debug API] 已恢復系統狀態: {current_state.value} → IDLE")
            # 狀態切換時，對應的會話會自動結束
        
        # 3. 結束 GS 容器（如果是 debug 創建的）
        current_gs = unified_session_manager.get_current_general_session()
        if current_gs:
            # 檢查是否是 debug 創建的 GS
            if (hasattr(current_gs, 'trigger_event') and 
                current_gs.trigger_event.get('source') == 'debug_api'):
                info_log(f"[Debug API] 結束 debug 創建的 GS: {current_gs.session_id}")
                unified_session_manager.end_general_session()
            else:
                info_log(f"[Debug API] 保留非 debug 創建的 GS: {current_gs.session_id}")
        
        # 注意：保留身份設置，不清理工作上下文中的身份資訊
        # 這樣可以讓後續測試繼續使用同一個測試身份
        
        info_log("[Debug API] 測試環境清理完成 - 狀態已重置為 IDLE，會話已適當清理")
        return {"success": True}
        
    except Exception as e:
        error_log(f"[Debug API] 清理測試環境失敗: {e}")
        return {"success": False, "error": str(e)}

# ===== LLM 模組測試包裝函數 =====

def test_llm_with_mode(test_mode: str, text: str):
    """
    為 LLM 模組測試設置正確的狀態環境
    Args:
        test_mode (str): 測試模式 ("chat" 或 "work")
        text (str): 測試文本
    """
    from .module_tests.llm_tests import llm_test_chat, llm_test_command
    
    print(f"\n🧪 開始 LLM {test_mode.upper()} 模式測試")
    print("=" * 60)
    
    # 1. 設置測試環境（指定測試模式）
    info_log(f"[Debug API] 為 LLM {test_mode} 模式設置環境...")
    setup_result = setup_test_environment_for_module("llm", test_mode=test_mode)
    
    if not setup_result.get("success", False):
        error_msg = f"測試環境設置失敗: {setup_result.get('error')}"
        print(f"❌ {error_msg}")
        return {"success": False, "error": error_msg}
    
    print(f"✅ 測試環境設置完成")
    print(f"📄 測試狀態: {setup_result['state'].value}")
    print(f"🆔 測試身份: {setup_result['identity']['user_identity']}")
    
    try:
        # 2. 載入並執行相應測試
        llm_module = get_or_load_module("llm")
        modules = {"llm": llm_module}
        
        if test_mode == "chat":
            result = llm_test_chat(modules, text)
        elif test_mode == "work":
            result = llm_test_command(modules, text)
        else:
            result = {"success": False, "error": f"不支援的測試模式: {test_mode}"}
        
        return result
        
    finally:
        # 3. 清理測試環境
        print(f"\n🧹 清理 LLM {test_mode} 測試環境...")
        cleanup_result = cleanup_test_environment()
        if cleanup_result.get("success", False):
            print("✅ 測試環境清理完成")
        else:
            print(f"⚠️ 測試環境清理異常: {cleanup_result.get('error')}")

def test_llm_chat(text: str = "你好，這是一個聊天測試"):
    """測試 LLM CHAT 模式 - 使用 CHAT 狀態"""
    return test_llm_with_mode("chat", text)

def test_llm_work(text: str = "建立一個新的工作流程來整理文件"):
    """測試 LLM WORK 模式 - 使用 WORK 狀態"""
    return test_llm_with_mode("work", text)

# 在模組載入時自動初始化工作上下文
setup_working_context()
