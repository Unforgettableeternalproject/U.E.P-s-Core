# debug/module_manager.py
"""
Module Manager for Debug Interface

管理模組的載入、狀態檢查和測試功能整合
整合 debug_api.py 中的模組註冊和測試功能
"""

import os
import sys
from typing import Dict, Any, Optional

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, info_log, error_log, KEY_LEVEL, OPERATION_LEVEL, SYSTEM_LEVEL, ELABORATIVE_LEVEL
from configs.config_loader import load_config

class ModuleManager:
    """
    模組管理器
    
    負責：
    1. 根據 config.yaml 載入啟用的模組
    2. 提供模組狀態查詢
    3. 整合 debug_api.py 中的測試函數
    4. 管理模組測試結果
    """
    
    def __init__(self):
        self.config = load_config()
        self.enabled_modules = self.config.get("modules_enabled", {})
        self.refactored_modules = self.config.get("modules_refactored", {})
        self.modules = {}
        self.module_status = {}
        self.test_functions = {}
        
        # 不再自動初始化所有模組，改為按需載入
        self._init_debug_api()
        self._init_test_functions()
    
    def _init_debug_api(self):
        """初始化 debug_api 連接"""
        try:
            import devtools.debug_api as debug_api
            self.debug_api = debug_api
            debug_log(SYSTEM_LEVEL, "[ModuleManager] debug_api 連接成功")
        except ImportError as e:
            error_log(f"[ModuleManager] 無法載入 debug_api: {e}")
            self.debug_api = None
    
    def _normalize_module_name(self, module_name: str) -> str:
        """正規化模組名稱"""
        # 將 "stt" -> "stt_module" 的映射
        if not module_name.endswith("_module"):
            return f"{module_name}_module"
        return module_name
    
    def _is_module_loaded(self, module_name: str) -> bool:
        """檢查模組是否已載入"""
        if not self.debug_api:
            return False
        
        # 檢查 debug_api.modules 字典中的狀態
        try:
            modules_dict = getattr(self.debug_api, 'modules', {})
            return modules_dict.get(module_name) is not None
        except Exception:
            return False
    
    def load_module(self, module_name: str) -> Dict[str, Any]:
        """載入模組"""
        try:
            normalized_name = self._normalize_module_name(module_name)
            
            # 檢查設定檔狀態
            if not self.enabled_modules.get(normalized_name, False):
                return {
                    'success': False,
                    'error': f'模組 {module_name} 在設定檔中被禁用，無法載入'
                }
            
            # 檢查是否已經載入
            if self._is_module_loaded(module_name):
                return {
                    'success': True,
                    'message': f'模組 {module_name} 已經載入'
                }
            
            # 嘗試載入模組
            if self.debug_api:
                debug_log(OPERATION_LEVEL, f"[ModuleManager] 載入模組: {module_name}")
                
                # 使用 debug_api 的 get_or_load_module 方法載入模組
                try:
                    module_instance = self.debug_api.get_or_load_module(module_name)
                    
                    if module_instance is not None:
                        info_log(f"[ModuleManager] 模組 {module_name} 載入成功")
                        return {
                            'success': True,
                            'message': f'模組 {module_name} 載入成功'
                        }
                    else:
                        error_log(f"[ModuleManager] 模組 {module_name} 載入失敗，返回 None")
                        return {
                            'success': False,
                            'error': f'模組 {module_name} 載入失敗，可能模組未啟用或註冊失敗'
                        }
                except Exception as load_error:
                    error_log(f"[ModuleManager] 載入模組 {module_name} 時發生異常: {load_error}")
                    return {
                        'success': False,
                        'error': f'載入異常: {str(load_error)}'
                    }
            else:
                return {
                    'success': False,
                    'error': 'debug_api 未可用'
                }
                
        except Exception as e:
            error_log(f"[ModuleManager] 載入模組 {module_name} 失敗: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def unload_module(self, module_name: str) -> Dict[str, Any]:
        """卸載模組"""
        try:
            if not self._is_module_loaded(module_name):
                return {
                    'success': True,
                    'message': f'模組 {module_name} 未載入，無需卸載'
                }
            
            # 嘗試卸載模組
            if self.debug_api:
                debug_log(OPERATION_LEVEL, f"[ModuleManager] 卸載模組: {module_name}")
                
                # 在 debug_api 中將模組設為 None 來卸載
                try:
                    # 取得模組字典並將指定模組設為 None
                    modules_dict = getattr(self.debug_api, 'modules', {})
                    if module_name in modules_dict:
                        modules_dict[module_name] = None
                        info_log(f"[ModuleManager] 模組 {module_name} 已從記憶體中卸載")
                        return {
                            'success': True,
                            'message': f'模組 {module_name} 卸載成功'
                        }
                    else:
                        return {
                            'success': True,
                            'message': f'模組 {module_name} 未在模組字典中找到，可能已經卸載'
                        }
                except Exception as unload_error:
                    error_log(f"[ModuleManager] 卸載模組 {module_name} 時發生異常: {unload_error}")
                    return {
                        'success': False,
                        'error': f'卸載異常: {str(unload_error)}'
                    }
            else:
                return {
                    'success': False,
                    'error': 'debug_api 未可用'
                }
                
        except Exception as e:
            error_log(f"[ModuleManager] 卸載模組 {module_name} 失敗: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def reload_module(self, module_name: str) -> Dict[str, Any]:
        """重載模組"""
        try:
            debug_log(OPERATION_LEVEL, f"[ModuleManager] 重載模組: {module_name}")
            
            # 先卸載再載入
            unload_result = self.unload_module(module_name)
            if not unload_result.get('success', False):
                return unload_result
            
            load_result = self.load_module(module_name)
            if load_result.get('success', False):
                load_result['message'] = f'模組 {module_name} 重載成功'
            
            return load_result
            
        except Exception as e:
            error_log(f"[ModuleManager] 重載模組 {module_name} 失敗: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _init_test_functions(self):
        """初始化測試函數映射"""
        try:
            import devtools.debug_api as debug_api
            
            # STT 測試函數 - 使用包裝函數，這些不需要 modules 參數
            self.test_functions["stt"] = {
                "single_test": debug_api.stt_test_single_wrapper,
                "continuous_test": debug_api.stt_test_continuous_listening_wrapper,
                "get_stats": debug_api.stt_get_stats_wrapper,
                "speaker_list": debug_api.stt_speaker_list_wrapper,
                "speaker_info": debug_api.stt_speaker_info_wrapper,
            }
            
            # NLP 測試函數 - 使用包裝函數
            self.test_functions["nlp"] = {
                "basic_test": debug_api.nlp_test_wrapper,
                "state_queue_test": debug_api.nlp_test_state_queue_integration_wrapper,
                "multi_intent_test": debug_api.nlp_test_multi_intent_wrapper,
                "identity_test": debug_api.nlp_test_identity_management_wrapper,
                "analyze_context": debug_api.nlp_analyze_context_queue_wrapper,
                "clear_contexts": debug_api.nlp_clear_contexts_wrapper,
            }
            
            # MEM 測試函數 - 使用包裝函數 (更新為新架構)
            self.test_functions["mem"] = {
                "store_memory": debug_api.mem_test_store_memory_wrapper,
                "memory_query": debug_api.mem_test_memory_query_wrapper,
                "conversation_snapshot": debug_api.mem_test_conversation_snapshot_wrapper,
                "identity_manager_stats": debug_api.mem_test_identity_manager_stats_wrapper,
                "write_then_query": debug_api.mem_test_write_then_query_wrapper,
            }
            
            # LLM 測試函數 - 使用包裝函數
            self.test_functions["llm"] = {
                "chat": debug_api.llm_test_chat_wrapper,
                "command": debug_api.llm_test_command_wrapper,
                "cache_functionality": debug_api.llm_test_cache_functionality_wrapper,
                "learning_engine": debug_api.llm_test_learning_engine_wrapper,
                "system_status_monitoring": debug_api.llm_test_system_status_monitoring_wrapper,
            }
            
            # TTS 測試函數 - 使用包裝函數
            self.test_functions["tts"] = {
                "basic_test": debug_api.tts_test_wrapper,
            }
            
            # Frontend 測試函數 - 使用包裝函數 (統合 UI、ANI、MOV)
            self.test_functions["frontend"] = {
                "show_desktop_pet": debug_api.show_desktop_pet_wrapper,
                "hide_desktop_pet": debug_api.hide_desktop_pet_wrapper,
                "control_desktop_pet": debug_api.control_desktop_pet_wrapper,
                "test_mov_ani_integration": debug_api.test_mov_ani_integration_wrapper,
                "test_behavior_modes": debug_api.test_behavior_modes_wrapper,
                "test_animation_state_machine": debug_api.test_animation_state_machine_wrapper,
                "frontend_test_full": debug_api.frontend_test_full_wrapper,
                "frontend_get_status": debug_api.frontend_get_status_wrapper,
                "frontend_test_animations": debug_api.frontend_test_animations_wrapper,
                "frontend_test_user_interaction": debug_api.frontend_test_user_interaction_wrapper,
            }
            
            # SYS 測試函數 - 使用包裝函數
            self.test_functions["sysmod"] = {
                "list_functions": debug_api.sys_list_functions_wrapper,
                "test_functions": debug_api.sys_test_functions_wrapper,
                "test_workflows": debug_api.sys_test_workflows_wrapper,
                "command_workflow": debug_api.test_command_workflow_wrapper,
            }
            
            # 整合測試 - 使用包裝函數
            self.test_functions["integration"] = {
                "stt_nlp_test": debug_api.test_stt_nlp_wrapper,
            }
            
            debug_log(SYSTEM_LEVEL, f"[ModuleManager] 測試函數初始化完成，共 {len(self.test_functions)} 個模組")
            
        except Exception as e:
            error_log(KEY_LEVEL, f"[ModuleManager] 初始化測試函數失敗: {e}")
    
    def _is_module_enabled(self, module_key: str) -> bool:
        """檢查模組是否在配置中啟用"""
        module_name_map = {
            "stt": "stt_module",
            "nlp": "nlp_module", 
            "mem": "mem_module",
            "llm": "llm_module",
            "tts": "tts_module",
            "sysmod": "sys_module",
            "ui": "ui_module",
            "ani": "ani_module",
            "mov": "mov_module",
            "frontend": "ui_module"  # frontend 使用 ui_module 的配置
        }
        
        config_name = module_name_map.get(module_key, f"{module_key}_module")
        return self.enabled_modules.get(config_name, False)
    
    def get_module_status(self, module_key: str) -> Dict[str, Any]:
        """獲取模組狀態"""
        # 獲取模組名稱的標準格式
        normalized_name = self._normalize_module_name(module_key)
        
        # 確保我們有最新的實際模組載入狀態
        try:
            import devtools.debug_api as debug_api
            # 直接從 debug_api 檢查模組是否真的已載入
            is_loaded = debug_api.modules.get(module_key) is not None
            debug_log(ELABORATIVE_LEVEL, f"[ModuleManager] 模組 {module_key} 載入狀態: {is_loaded}")
        except Exception as e:
            error_log(f"[ModuleManager] 檢查模組狀態時出錯: {e}")
            is_loaded = False
            
        # 檢查設定檔中的啟用狀態
        enabled = self.enabled_modules.get(normalized_name, False)
        refactored = self.refactored_modules.get(normalized_name, False)
        
        # 獲取模組實例和記憶體使用
        module_instance = None
        memory_usage = 'N/A'
        load_time = 'N/A'
        
        if is_loaded:
            try:
                import time
                from datetime import datetime
                import psutil
                import sys
                import gc
                
                module_instance = debug_api.modules.get(module_key)
                
                # 獲取模組載入時間
                try:
                    # 嘗試從 debug_api 獲取實際的載入時間
                    load_time = debug_api.get_module_load_time(module_key)
                    debug_log(ELABORATIVE_LEVEL, f"[ModuleManager] 模組 {module_key} 載入時間: {load_time}")
                except Exception as time_err:
                    # 如果獲取失敗，使用當前時間
                    error_log(f"[ModuleManager] 獲取模組載入時間失敗: {time_err}")
                    load_time = datetime.now().strftime('%H:%M:%S')
                
                # 估算記憶體使用量
                if module_instance:
                    try:
                        # 先嘗試直接獲取模組大小
                        module_size = sys.getsizeof(module_instance) / (1024 * 1024)  # MB
                        
                        # 如果模組大小太小（可能是包裝器），嘗試獲取其屬性的大小
                        if module_size < 0.1:  # 小於 0.1 MB，可能需要深度分析
                            total_size = module_size
                            # 遍歷模組的所有屬性，計算總大小
                            for attr_name in dir(module_instance):
                                try:
                                    attr = getattr(module_instance, attr_name)
                                    if not attr_name.startswith('__'):  # 跳過內建屬性
                                        total_size += sys.getsizeof(attr) / (1024 * 1024)
                                except:
                                    pass
                            module_size = total_size
                        
                        memory_usage = f"{module_size:.2f} MB"
                        debug_log(KEY_LEVEL, f"[ModuleManager] 模組 {module_key} 記憶體使用: {memory_usage}")
                    except Exception as mem_err:
                        memory_usage = '計算中...'
                        debug_log(KEY_LEVEL, f"[ModuleManager] 計算模組 {module_key} 記憶體失敗: {mem_err}")
            except Exception as e:
                error_log(f"[ModuleManager] 獲取模組 {module_key} 狀態資訊失敗: {e}")
                # 即使出錯，仍然至少顯示模組已載入
                if is_loaded:
                    load_time = "已載入"
                    memory_usage = "已使用"
        
        # 組合完整的狀態信息
        status = {
            'status': 'enabled' if enabled else 'disabled',
            'loaded': is_loaded,  # 使用實際的載入狀態
            'enabled': enabled,
            'enabled_in_config': enabled,
            'refactored': refactored,
            'instance': module_instance,
            'memory_usage': memory_usage if is_loaded else 'N/A',
            'load_time': load_time if is_loaded else 'N/A',
            'message': '模組已載入且啟用' if (enabled and is_loaded) else 
                      '模組已啟用但未載入' if (enabled and not is_loaded) else 
                      '模組在設定檔中被禁用'
        }
        
        # 針對已載入的模組，確保始終顯示有用的信息
        if is_loaded:
            from datetime import datetime
            if memory_usage == 'N/A':
                # 嘗試直接估算模組記憶體用量
                try:
                    if module_key in debug_api.modules and debug_api.modules[module_key] is not None:
                        import sys
                        module_size = 0
                        # 直接估算
                        module_size = sys.getsizeof(debug_api.modules[module_key]) / (1024 * 1024)  # MB
                        status['memory_usage'] = f"{module_size:.2f} MB"
                    else:
                        status['memory_usage'] = '已加載'
                except:
                    status['memory_usage'] = '已加載'
            
            if load_time == 'N/A':
                # 使用當前時間作為估計的載入時間
                status['load_time'] = datetime.now().strftime('%H:%M:%S')
        
        # 更新我們的內部狀態記錄
        self.module_status[module_key] = status.copy()
        
        return status
    
    def get_all_module_status(self) -> Dict[str, Dict[str, Any]]:
        """獲取所有模組狀態"""
        return {k: v.copy() for k, v in self.module_status.items()}
    
    def get_module_instance(self, module_key: str):
        """獲取模組實例"""
        status = self.get_module_status(module_key)
        return status.get("instance")
    
    def get_test_functions(self, module_key: str) -> Dict[str, callable]:
        """獲取模組的測試函數"""
        return self.test_functions.get(module_key, {})
    
    def run_test_function(self, module_key: str, function_name: str, params: Dict[str, Any] = None):
        """執行測試函數"""
        try:
            # 特殊處理 frontend 模組 - 檢查 UI 模組是否載入
            check_module = "ui" if module_key == "frontend" else module_key
            
            # 檢查模組是否已載入
            if not self._is_module_loaded(check_module):
                return {
                    "success": False,
                    "error": f"模組 {check_module} 未載入，無法執行 {module_key} 測試"
                }
            
            # 獲取測試函數
            test_funcs = self.get_test_functions(module_key)
            if function_name not in test_funcs:
                return {
                    "success": False,
                    "error": f"測試函數 {function_name} 在模組 {module_key} 中不存在"
                }
            
            func = test_funcs[function_name]
            
            # 執行包裝函數 - 包裝函數已經自動處理 modules 參數的傳遞
            if params:
                result = func(**params)
            else:
                result = func()
            
            debug_log(OPERATION_LEVEL, f"[ModuleManager] 執行測試 {module_key}.{function_name} 完成")
            
            return {
                "success": True,
                "message": f"測試 {function_name} 執行完成",
                "data": result
            }
            
        except Exception as e:
            error_log(KEY_LEVEL, f"[ModuleManager] 執行測試 {module_key}.{function_name} 失敗: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def refresh_module(self, module_key: str):
        """刷新模組狀態"""
        try:
            # 重新檢查模組狀態
            if module_key in self.modules:
                module_instance = self.modules[module_key]
                if module_instance is not None:
                    # 模組已載入，檢查是否仍然正常
                    if hasattr(module_instance, 'debug'):
                        try:
                            module_instance.debug()
                            self.module_status[module_key]["error"] = None
                        except Exception as e:
                            self.module_status[module_key]["error"] = f"模組狀態異常: {e}"
                    
                    self.module_status[module_key]["loaded"] = True
                else:
                    self.module_status[module_key]["loaded"] = False
                    self.module_status[module_key]["error"] = "模組未載入"
            
            debug_log(ELABORATIVE_LEVEL, f"[ModuleManager] 刷新模組 {module_key} 狀態完成")
            
        except Exception as e:
            error_log(KEY_LEVEL, f"[ModuleManager] 刷新模組 {module_key} 失敗: {e}")
            self.module_status[module_key]["error"] = str(e)

# 全局模組管理器實例
_module_manager = None

def get_module_manager() -> ModuleManager:
    """獲取全局模組管理器實例"""
    global _module_manager
    if _module_manager is None:
        _module_manager = ModuleManager()
    return _module_manager
