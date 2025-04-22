from .tts_module import TTSModule
from configs.config_loader import load_module_config

def register():
    config = load_module_config("tts_module")
    instance = TTSModule(config=config)
    instance.initialize()
    return instance
