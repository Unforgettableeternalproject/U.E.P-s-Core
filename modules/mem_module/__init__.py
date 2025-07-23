from .mem_module import MEMModule
from configs.config_loader import load_module_config

def register():
    config = load_module_config("mem_module")
    instance = MEMModule(config=config)
    instance.initialize()
    return instance
