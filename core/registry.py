# Init and load the registry modules

# core/registry.py

import importlib
import os

_loaded_modules = {}

def get_module(name: str):
    """�ھڼҲզW�ٸ��J�æ^�Ǩ��ҡ]�Ҳջݴ��� register()�^"""
    if name in _loaded_modules:
        return _loaded_modules[name]

    try:
        # ���]�Ҳո�Ƨ��� modules/stt_module�A�פJ�� modules.stt_module
        import_path = f"modules.{name}"
        module = importlib.import_module(import_path)

        if hasattr(module, "register"):
            instance = module.register()
            _loaded_modules[name] = instance
            return instance
        else:
            raise ImportError(f"Module {name} ���S�� register() ��ơC")
    except Exception as e:
        print(f"[Registry] �L�k���J�Ҳ� '{name}': {e}")
        return None
