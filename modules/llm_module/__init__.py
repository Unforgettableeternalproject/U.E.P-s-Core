from .llm_module import LLMModule
from configs.config_loader import load_module_config

def register():
    config = load_module_config("llm_module")
    instance = LLMModule(config=config)
    instance.initialize()
    return instance
