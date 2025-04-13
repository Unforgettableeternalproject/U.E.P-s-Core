# Init and load the registry modules

# core/registry.py

import importlib
import os

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
    except Exception as e:
        print(f"[Registry] 無法載入模組 '{name}': {e}")
        return None
