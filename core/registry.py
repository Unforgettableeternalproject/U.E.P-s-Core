# core/registry.py
import importlib
from utils.debug_helper import debug_log, info_log, error_log

_loaded_modules = {}

def get_module(name: str):
    """根據模組名稱載入並回傳其實例（模組需提供 register()）"""
    if name in _loaded_modules:
        return _loaded_modules[name]

    try:
        # 假設模組資料夾為 modules/stt_module，匯入為 modules.stt_module
        import_path = f"modules.{name}"
        module = importlib.import_module(import_path)

        if hasattr(module, "register"):
            instance = module.register()
            _loaded_modules[name] = instance
            return instance
        else:
            raise ImportError(f"Module {name} 中沒有 register() 函數。")
    except NotImplementedError as e:
        raise NotImplementedError
    except Exception as e:
        error_log(f"載入模組 {name} 失敗: {e}")
        return None
