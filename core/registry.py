# core/registry.py
import importlib
import sys
import traceback
from typing import Dict, Any, Optional
from utils.debug_helper import debug_log, info_log, error_log

_loaded_modules = {}

def get_module(name: str):
    """根據模組名稱載入並回傳其實例（模組需提供 register()）"""
    if name in _loaded_modules:
        return _loaded_modules[name]

    try:
        # 假設模組資料夾為 modules/stt_module，匯入為 modules.stt_module
        import_path = f"modules.{name}"
        debug_log(2, f"[Registry] 正在載入模組：{import_path}")
        
        module = importlib.import_module(import_path)
        debug_log(2, f"[Registry] 模組 {import_path} 已導入，檢查 register() 函數")

        if hasattr(module, "register"):
            debug_log(2, f"[Registry] 調用 {name} 的 register() 函數")
            instance = module.register()
            
            if instance is None:
                error_log(f"[Registry] {name} register() 回傳為 None")
                return None
            
            _loaded_modules[name] = instance
            info_log(f"[Registry] 模組 {name} 註冊成功")
            return instance
        else:
            raise ImportError(f"Module {name} 中沒有 register() 函數。")
    except NotImplementedError as e:
        raise NotImplementedError
    except Exception as e:
        error_log(f"[Registry] 載入模組 {name} 失敗: {e}")
        error_log(f"[Registry] 詳細錯誤追蹤:\n{traceback.format_exc()}")
        return None

def is_loaded(name: str) -> bool:
    """檢查指定模組是否已載入（不會觸發載入）。"""
    return name in _loaded_modules

def get_loaded(name: str):
    """取得已載入的模組實例，若未載入則回傳 None（不會觸發載入）。"""
    return _loaded_modules.get(name)

def unload_module(name: str) -> bool:
    """
    卸載模組
    
    Args:
        name: 模組名稱
        
    Returns:
        bool: 是否成功卸載
    """
    if name not in _loaded_modules:
        debug_log(2, f"[Registry] 模組 {name} 未載入，無需卸載")
        return False
    
    try:
        module_instance = _loaded_modules[name]
        
        # 如果模組有 shutdown 方法，先調用
        if hasattr(module_instance, 'shutdown'):
            debug_log(2, f"[Registry] 調用 {name} 的 shutdown() 方法")
            module_instance.shutdown()
        
        # 從字典中移除
        del _loaded_modules[name]
        
        info_log(f"[Registry] 模組 {name} 已卸載")
        return True
        
    except Exception as e:
        error_log(f"[Registry] 卸載模組 {name} 失敗: {e}")
        error_log(f"[Registry] 詳細錯誤追蹤:\n{traceback.format_exc()}")
        return False

def reload_module(name: str):
    """
    重新載入模組
    
    先卸載再載入，確保使用最新的代碼
    
    Args:
        name: 模組名稱
        
    Returns:
        模組實例，失敗則返回 None
    """
    try:
        debug_log(2, f"[Registry] 重新載入模組: {name}")
        
        # 1. 卸載現有模組
        if name in _loaded_modules:
            unload_module(name)
        
        # 2. 清除 Python 的模組快取
        import_path = f"modules.{name}"
        if import_path in sys.modules:
            debug_log(3, f"[Registry] 清除模組快取: {import_path}")
            del sys.modules[import_path]
        
        # 3. 重新載入
        module = importlib.import_module(import_path)
        importlib.reload(module)
        
        if hasattr(module, "register"):
            debug_log(2, f"[Registry] 調用 {name} 的 register() 函數")
            instance = module.register()
            
            if instance is None:
                error_log(f"[Registry] {name} register() 回傳為 None")
                return None
            
            _loaded_modules[name] = instance
            info_log(f"[Registry] 模組 {name} 重新載入成功")
            return instance
        else:
            raise ImportError(f"Module {name} 中沒有 register() 函數。")
            
    except Exception as e:
        error_log(f"[Registry] 重新載入模組 {name} 失敗: {e}")
        error_log(f"[Registry] 詳細錯誤追蹤:\n{traceback.format_exc()}")
        return None

def unload_all_modules(exclude: Optional[list] = None) -> Dict[str, bool]:
    """
    卸載所有模組（可排除特定模組）
    
    Args:
        exclude: 排除的模組名稱列表（例如 ["ui"] 保留 UI 模組）
        
    Returns:
        Dict[str, bool]: 每個模組的卸載結果
    """
    exclude = exclude or []
    results = {}
    
    # 複製鍵列表，避免在迭代時修改字典
    module_names = list(_loaded_modules.keys())
    
    for name in module_names:
        if name in exclude:
            debug_log(2, f"[Registry] 跳過卸載: {name} (已排除)")
            results[name] = None  # None 表示跳過
            continue
        
        results[name] = unload_module(name)
    
    successful = sum(1 for v in results.values() if v is True)
    info_log(f"[Registry] 批量卸載完成: {successful}/{len(module_names)} 個模組")
    
    return results

def get_all_loaded_modules() -> Dict[str, Any]:
    """
    獲取所有已載入的模組
    
    Returns:
        Dict[str, Any]: 模組名稱 -> 模組實例的字典
    """
    return _loaded_modules.copy()
