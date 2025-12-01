# core/registry.py
import importlib
import traceback
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
