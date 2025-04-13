from .stt_module import STTModule
from configs.config_loader import load_module_config

def register():
    config = load_module_config("stt_module")
    instance = STTModule(config=config)  # 之後可以用 config_loader 傳入設定
    instance.initialize()
    return instance
