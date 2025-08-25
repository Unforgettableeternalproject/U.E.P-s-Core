from logging import config
from core.registry import get_module
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log
# 導入整合測試
from .module_tests.integration_tests import test_stt_nlp
# 暫時註解掉這個導入，等相關文件創建後再啟用
# from .module_tests.extra_tests import test_chunk_and_summarize, test_uep_chatting
import time
import asyncio

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

def _initialize_modules():
    """根據當前載入模式初始化模組字典"""
    global modules, modules_load_times
    
    if PRELOAD_MODULES is True:
        # 舊版模式：預先載入所有模組
        info_log("[Controller] 初始化：預先載入所有模組")
        
        # 模組列表
        module_names = ["stt_module", "nlp_module", "mem_module", "llm_module", 
                        "tts_module", "sys_module", "ui_module", "ani_module", "mov_module"]
        
        # 載入每個模組並記錄時間
        modules = {}
        for full_name in module_names:
            short_name = full_name.split('_')[0]
            module_instance = safe_get_module(full_name)
            modules[short_name] = module_instance
            
            # 記錄載入時間
            if module_instance is not None:
                from datetime import datetime
                modules_load_times[short_name] = datetime.now().strftime('%H:%M:%S')
    else:
        # GUI模式：延遲載入
        info_log("[Controller] 初始化：按需載入模式")
        modules = {
            "stt": None,
            "nlp": None,
            "mem": None,
            "llm": None,
            "tts": None,
            "sysmod": None,
            "ui": None,
            "ani": None,
            "mov": None
        }
        # 初始化載入時間字典，預設為空
        modules_load_times = {}

def set_loading_mode(preload=True):
    """設定模組載入模式
    Args:
        preload (bool): True=預先載入所有模組, False=按需載入
    """
    global PRELOAD_MODULES
    PRELOAD_MODULES = preload
    
    info_log(f"[Controller] 設定載入模式：{'預先載入' if preload else '按需載入'}")
    
    # 重新初始化模組字典
    _initialize_modules()

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
        
        return modules[name]


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

# 測試 MEM 模組（尚未重構）
from .module_tests.mem_tests import (
    mem_fetch_test, mem_store_test, mem_clear_test, mem_list_all_test
)

# 測試 LLM 模組（尚未重構）
from .module_tests.llm_tests import (
    llm_test_chat, llm_test_command
)

# 測試 TTS 模組（尚未重構）
from .module_tests.tts_tests import (
    tts_test
)

# 測試 SYS 模組（尚未重構）
from .module_tests.sys_tests import (
    sys_list_functions, test_command_workflow, sys_test_functions,
    sys_test_workflows, sys_list_test_workflows, test_file_workflow
)

# 創建包裝函數，自動傳遞 modules 參數

# STT 模組包裝函數
def stt_test_single_wrapper(enable_speaker_id=True, language="en-US"):
    return stt_test_single(modules, enable_speaker_id, language)

def stt_test_continuous_listening_wrapper(duration=30):
    return stt_test_continuous_listening(modules, duration)

def stt_get_stats_wrapper():
    return stt_get_stats(modules)

def stt_speaker_list_wrapper():
    return stt_speaker_list(modules)

def stt_speaker_rename_wrapper(old_id: str, new_id: str):
    return stt_speaker_rename(modules, old_id, new_id)

def stt_speaker_delete_wrapper(speaker_id: str):
    return stt_speaker_delete(modules, speaker_id)

def stt_speaker_clear_all_wrapper():
    return stt_speaker_clear_all(modules)

def stt_speaker_backup_wrapper():
    return stt_speaker_backup(modules)

def stt_speaker_restore_wrapper(backup_path: str = None):
    return stt_speaker_restore(modules, backup_path)

def stt_speaker_info_wrapper():
    return stt_speaker_info(modules)

def stt_speaker_adjust_threshold_wrapper(threshold: float = None):
    return stt_speaker_adjust_threshold(modules, threshold)

# NLP 模組包裝函數
def nlp_test_wrapper(text: str = "", enable_identity: bool = True, enable_segmentation: bool = True):
    return nlp_test(modules, text, enable_identity, enable_segmentation)

def nlp_test_state_queue_integration_wrapper(text: str = ""):
    return nlp_test_state_queue_integration(modules, text)

def nlp_test_multi_intent_wrapper(text: str = ""):
    return nlp_test_multi_intent(modules, text)

def nlp_test_identity_management_wrapper(speaker_id: str = "test_user"):
    return nlp_test_identity_management(modules, speaker_id)

def nlp_analyze_context_queue_wrapper():
    return nlp_analyze_context_queue(modules)

def nlp_clear_contexts_wrapper():
    return nlp_clear_contexts(modules)

# Frontend 模組包裝函數
def show_desktop_pet_wrapper():
    return show_desktop_pet(modules)

def hide_desktop_pet_wrapper():
    return hide_desktop_pet(modules)

def control_desktop_pet_wrapper(action="wave", duration=3):
    return control_desktop_pet(modules, action, duration)

def test_mov_ani_integration_wrapper():
    return test_mov_ani_integration(modules)

def test_behavior_modes_wrapper():
    return test_behavior_modes(modules)

def test_animation_state_machine_wrapper():
    return test_animation_state_machine(modules)

def frontend_test_full_wrapper():
    return frontend_test_full(modules)

def frontend_get_status_wrapper():
    return frontend_get_status(modules)

def frontend_test_animations_wrapper():
    return frontend_test_animations(modules)

def frontend_test_user_interaction_wrapper():
    return frontend_test_user_interaction(modules)

# MEM 模組包裝函數（尚未重構）
def mem_fetch_test_wrapper(text: str = ""):
    return mem_fetch_test(modules, text)

def mem_store_test_wrapper(user_text: str = "Test chat", response_text: str = "Test response"):
    return mem_store_test(modules, user_text, response_text)

def mem_clear_test_wrapper(text: str = "ALL", top_k: int = 1):
    return mem_clear_test(modules, text, top_k)

def mem_list_all_test_wrapper(page: int = 1):
    return mem_list_all_test(modules, page)

# LLM 模組包裝函數（尚未重構）
def llm_test_chat_wrapper(text: str):
    return llm_test_chat(modules, text)

def llm_test_command_wrapper(text: str):
    return llm_test_command(modules, text)

# TTS 模組包裝函數（尚未重構）
def tts_test_wrapper(text: str, mood: str = "neutral", save: bool = False):
    return tts_test(modules, text, mood, save)

# SYS 模組包裝函數（尚未重構）
def sys_list_functions_wrapper():
    return sys_list_functions(modules)

def test_command_workflow_wrapper(command_text: str = "幫我整理和摘要桌面上的文件"):
    return test_command_workflow(modules, command_text)

def sys_test_functions_wrapper(mode: int = 1, sub: int = 1):
    return sys_test_functions(modules, mode, sub)

def sys_test_workflows_wrapper(workflow_type: int = 1):
    return sys_test_workflows(modules, workflow_type)

def sys_list_test_workflows_wrapper():
    return sys_list_test_workflows(modules)

def test_file_workflow_wrapper(workflow_type: str):
    return test_file_workflow(modules, workflow_type)

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

# MEM 函數別名（匹配實際的函數名稱）
mem_fetch_test = mem_fetch_test_wrapper
mem_store_test = mem_store_test_wrapper
mem_clear_test = mem_clear_test_wrapper
mem_list_all_test = mem_list_all_test_wrapper
# 為了向後兼容，添加一些常用的別名
mem_test_save = mem_store_test_wrapper
mem_test_load = mem_fetch_test_wrapper
mem_test_search = mem_fetch_test_wrapper
mem_test_list = mem_list_all_test_wrapper
mem_test_clear = mem_clear_test_wrapper

# LLM 函數別名（匹配實際的函數名稱）
llm_test_chat = llm_test_chat_wrapper
llm_test_command = llm_test_command_wrapper
# 為了向後兼容，添加一些常用的別名
llm_test_generation = llm_test_chat_wrapper
llm_test_completion = llm_test_chat_wrapper
llm_test_qa = llm_test_chat_wrapper
llm_test_conversation = llm_test_chat_wrapper

# TTS 函數別名（匹配實際的函數名稱）
tts_test = tts_test_wrapper
# 為了向後兼容，添加一些常用的別名
tts_test_speak = tts_test_wrapper

# SYS 函數別名（匹配實際的函數名稱）
sys_list_functions = sys_list_functions_wrapper
test_command_workflow = test_command_workflow_wrapper
sys_test_functions = sys_test_functions_wrapper
sys_test_workflows = sys_test_workflows_wrapper
sys_list_test_workflows = sys_list_test_workflows_wrapper
test_file_workflow = test_file_workflow_wrapper
# 為了向後兼容，添加一些常用的別名
sys_test_resources = sys_list_functions_wrapper
sys_test_performance = sys_test_functions_wrapper
sys_test_cleanup = test_command_workflow_wrapper


# 整合測試 - 新版

def integration_test_SN():
    """STT + NLP 整合測試"""
    # 直接傳入模組字典
    test_stt_nlp(modules)

# 暫時停用其他整合測試，只保留 STT+NLP (因為其他模組尚未完成重構)
# 其他整合測試將在相應模組重構完成後添加

# 注意：目前只有 STT 和 NLP 模組完成重構，其他整合測試將在模組重構後添加
#
# 以下是可用的整合測試：
# - STT + NLP: integration_test_SN()
#
# 為保持程式碼整潔，其餘整合測試函數已移除

def integration_test_SN(production_mode=False):
    """STT + NLP 整合測試"""
    info_log(f"[Controller] 執行 STT+NLP 整合測試 (新版) ({'生產模式' if production_mode else '除錯模式'})")
    # 目前生產模式參數未被使用，因為新版整合測試不區分生產和除錯模式
    return test_stt_nlp(modules)

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

def test_speaker_context_workflow():
    """測試語者上下文工作流程"""
    print("🎤 語者上下文工作流程測試")
    print("   這個測試會累積多個語音樣本，並觀察工作上下文的行為")
    
    # 初始化工作上下文
    setup_working_context()
    
    # 執行多次 STT 測試以累積樣本
    for i in range(5):
        print(f"\n--- 第 {i+1} 次語音識別 ---")
        result = stt_test_single(mode="manual", enable_speaker_id=True)
        
        # 顯示工作上下文狀態
        get_working_context_status()
        
        if i < 4:  # 最後一次不需要暫停
            print("   按 Enter 繼續下一次測試...")
            input()
    
    print("\n✅ 語者上下文工作流程測試完成")

# 在模組載入時自動初始化工作上下文
setup_working_context()
