from .sys_module import SYSModule
from configs.config_loader import load_module_config

def register():
    config = load_module_config("sys_module")
    instance = SYSModule(config=config)
    instance.initialize()
    return instance
