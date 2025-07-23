# modules/nlp_module/__init__.py
from .nlp_module import NLPModule
from configs.config_loader import load_module_config

def register():
    config = load_module_config("nlp_module")
    instance = NLPModule(config=config)
    instance.initialize()
    return instance
